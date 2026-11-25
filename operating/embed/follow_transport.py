"""Split Spark-compute / box-apply transport for ``operating.embed follow --mode layers``.

* **prepare** (on the box): checks the disk floor, window lock and people exclusion,
  reads ``search/follow.json``, ``search/embed-write-budget.json`` and the unindexed
  delta layers' ``delta_owned`` object lists, and writes a compact context snapshot in
  ``--prepare-dir`` (zero ``bge-m3`` load, zero heavy chunking).
* **compute** (on a Spark): runs read-only against the Spark's hot standby Postgres and
  encodes embeddings on the Spark's GPU/CPU, exporting precomputed chunks, entities and
  vectors to ``--export-dir``.
* **apply** (on the box): validates the exported payload against current state under
  ``follow.json.lock``, re-checks the people exclusion, writes chunks/entities/deletions
  to primary Postgres, stores precomputed vectors in ``--embed-batch`` / ``--embed-rows``
  batches under the rolling ``--embed-write-mb-per-hour`` WAL budget and 60 GB disk floor,
  and updates ``follow.json`` and ``embed-write-budget.json`` atomically — without ever
  importing torch, sentence-transformers, or loading ``bge-m3``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import json
import os
import shlex
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from integrations.people_exclusion import is_object_id
from ontology import parent_watch
from operating.chunking import KINDS, Chunk
from operating.chunking.entities import PERSON_TYPES, Entity, entities, participants
from operating.chunking.permissions import Exclusion, unit_row
from operating.chunking.pg_source import PG_PROJECTION, PgProjection
from operating.chunking.projection import Counts, open_ro, withheld_messages

from . import config
from . import store as st
from .shards import PERSON_KINDS, WRITE_BATCH, _exclusion, produce

TRANSPORT_VERSION = 1


def _write_json_atomic(path: Path, data: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_gzip_json_atomic(path: Path, data: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _exclusion_context(exclusion: Exclusion) -> dict[str, Any]:
    """Serializable non-secret capture context for reconstructing ``Exclusion`` on Spark.

    Never writes ``PLX_EXCLUDED_PEOPLE_OIDS`` (the guard's excluded OIDs arrive strictly via
    ``spark_run_spec.py`` on ssh stdin).
    """
    return {
        "directory": dict(exclusion.directory),
        "internal": sorted(exclusion.internal),
        "drive_owners": dict(exclusion.drive_owners),
        "chat_members": {k: list(v) for k, v in exclusion.chat_members.items()},
        "drive_sites": dict(exclusion.drive_sites),
    }


def _exclusion_from_context(state: Path, values: Mapping[str, str],
                            ctx_file: Path | None) -> Exclusion:
    if ctx_file is not None and ctx_file.is_file():
        from integrations.people_exclusion import PeopleExclusion
        env = dict(os.environ)
        for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
            if values.get(name) and name not in env:
                env[name] = values[name]
        guard = PeopleExclusion.from_env(env)
        raw = json.loads(ctx_file.read_text(encoding="utf-8"))
        return Exclusion(
            guard,
            directory=raw.get("directory") or {},
            internal_domains=raw.get("internal") or (),
            drive_owners=raw.get("drive_owners") or {},
            chat_members={k: tuple(v) for k, v in (raw.get("chat_members") or {}).items()},
            drive_sites=raw.get("drive_sites") or {},
        )
    return _exclusion(state, dict(values))


def _effective_cursor(state: Path, cursor_path: Path, now_served: Mapping[str, Any]
                      ) -> tuple[dict[str, Any], list[str], list[str], dict[str, Any], str | None]:
    from . import follow as fl
    cursor = fl.load_cursor(cursor_path)
    passed = fl.load_cursor(state / fl.GENERATION_CURSOR)
    pass_done = (passed.get("generation_indexed_at")
                 if passed.get("digest") == now_served["digest"] else None)
    reset = (cursor.get("digest") != now_served["digest"]
             or bool(pass_done and cursor.get("after_pass") != pass_done))
    layers = fl.delta_layers(state, now_served["digest"])
    indexed = [] if reset else [p for p in cursor.get("layers") or [] if p in layers]
    partial = ({} if reset else
               (dict(cursor["partial"]) if isinstance(cursor.get("partial"), dict) else {}))
    return cursor, layers, indexed, partial, pass_done


def _embed_gate_state(args: Any, state: Path, values: dict[str, str]
                      ) -> tuple[str | None, dict[str, Any] | None]:
    from . import backfill, follow as fl
    from .write_budget import RollingWriteBudget, WriteBudgetError
    write_mb = float(getattr(args, "embed_write_mb_per_hour", 0.0) or 0.0)
    budget_info: dict[str, Any] | None = None
    if write_mb > 0:
        try:
            wb = RollingWriteBudget(state / "search" / "embed-write-budget.json",
                                    write_mb * 1024 * 1024)
            wb.save()
        except WriteBudgetError as exc:
            return str(exc), None
        status = wb.status()
        budget_info = {"bytes_left": status["bytes_left"],
                       "bytes_spent": status["bytes_spent"],
                       "bytes_per_hour": status["bytes_per_hour"]}
        if wb.blocked():
            return "write_budget_exhausted", budget_info
    if fl.delta_running(state):
        return "delta_running", budget_info
    if backfill.running(state):
        return "backfill_running", budget_info
    paused = fl._wal_paused(values)
    if paused:
        return paused, budget_info
    if not getattr(args, "embed_seconds", 0.0):
        return "embed_disabled", budget_info
    return None, budget_info


def prepare(args: Any, state: Path) -> dict[str, Any]:
    """Lightweight box prep for Spark compute: writes ``manifest.json``,
    ``layer-owned.json.gz`` and ``exclusion-context.json`` to ``args.prepare_dir``, or
    skips without creating ``manifest.json`` when idle, locked, or below disk floor."""
    from . import follow as fl
    out_dir = Path(args.prepare_dir)
    report: dict[str, Any] = {"command": "follow", "mode": args.mode, "phase": "prepare",
                              "started_at": fl._now()}
    now_served = fl.served(state)
    if now_served is None:
        report["skipped"] = "no_served_generation"
        return report
    report["generation"] = now_served["generation"]
    values = config.settings()
    exclusion = _exclusion(state, values)
    if exclusion.unavailable and PERSON_KINDS & set(KINDS):
        report["refused"] = f"people_exclusion_unavailable:{exclusion.unavailable}"
        return report
    if args.min_free_gb and fl._free_gb(state) < args.min_free_gb:
        report["skipped"] = "disk_below_min_free"
        return report
    cursor_path = Path(args.cursor) if args.cursor else state / fl.CURSOR_NAME
    cursor, layers, indexed, partial, pass_done = _effective_cursor(state, cursor_path, now_served)
    pending_layers = [p for p in layers if p not in indexed and Path(p).is_file()]
    embed_skip, budget_info = _embed_gate_state(args, state, values)
    if not pending_layers and (embed_skip is not None or cursor.get("pending_after") == 0):
        report["layers"] = {}
        report["layers_behind"] = 0
        report["skipped"] = embed_skip or "idle"
        return report

    owned_map: dict[str, dict[str, Any]] = {}
    for layer in pending_layers:
        try:
            src = open_ro(layer)
            try:
                owned = sorted(r[0] for r in src.execute("SELECT object_id FROM delta_owned"))
            finally:
                src.close()
            owned_map[layer] = {"status": "ok", "owned": owned}
        except sqlite3.Error as error:
            if fl._schema_mismatch(error):
                owned_map[layer] = {"status": "not_a_delta_layer"}
            else:
                owned_map[layer] = {"status": "layer_unreadable",
                                    "error": type(error).__name__}
                break

    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    _write_gzip_json_atomic(out_dir / "layer-owned.json.gz", owned_map)
    _write_json_atomic(out_dir / "exclusion-context.json", _exclusion_context(exclusion))
    manifest = {
        "transport_version": TRANSPORT_VERSION,
        "generation": now_served["generation"],
        "digest": now_served["digest"],
        "after_pass": pass_done,
        "follow_cursor": cursor,
        "layers": layers,
        "indexed": indexed,
        "partial": partial,
        "pending_layers": pending_layers,
        "embed_skip": embed_skip,
        "write_budget": budget_info,
    }
    _write_json_atomic(out_dir / "manifest.json", manifest)
    report["pending_layers"] = len(pending_layers)
    report["embed_skip"] = embed_skip
    return report


def _chunk_to_row(chunk: Chunk) -> list[Any]:
    return [
        chunk.chunk_id, chunk.source_handle, chunk.object_id, chunk.kind, chunk.title,
        chunk.author_role, chunk.occurred_at, chunk.header, chunk.body, chunk.content_sha,
        list(chunk.linked_ids), chunk.audience_key, list(chunk.markings), chunk.authority,
    ]


def _row_to_chunk(row: Sequence[Any]) -> Chunk:
    return Chunk(
        chunk_id=str(row[0]),
        source_handle=str(row[1]),
        object_id=None if row[2] is None else str(row[2]),
        kind=str(row[3]),
        title=None if row[4] is None else str(row[4]),
        author_role=None if row[5] is None else str(row[5]),
        occurred_at=None if row[6] is None else str(row[6]),
        header=str(row[7]),
        body=str(row[8]),
        linked_ids=tuple(str(x) for x in row[10]),
        audience_key=str(row[11]),
        markings=tuple(str(x) for x in row[12]),
        authority=int(row[13]),
    )


def _entity_to_row(entity: Entity, *, new_only: bool,
                   identities: Sequence[str] = ()) -> dict[str, Any]:
    item: dict[str, Any] = {
        "new_only": new_only,
        "row": [entity.entity_id, entity.object_id, entity.type, entity.name,
                list(entity.alias), entity.normalized, entity.audience_key,
                list(entity.markings)],
    }
    if identities:
        item["identities"] = list(identities)
    return item


def _row_to_entity(item: Mapping[str, Any]) -> tuple[bool, Entity]:
    r = item["row"]
    return bool(item.get("new_only")), Entity(
        entity_id=str(r[0]),
        object_id=None if r[1] is None else str(r[1]),
        type=str(r[2]),
        name=str(r[3]),
        alias=tuple(str(x) for x in r[4]),
        normalized=str(r[5]),
        audience_key=str(r[6]),
        markings=tuple(str(x) for x in r[7]),
    )


def _looks_like_identity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped) and (
        is_object_id(stripped)
        or ("@" in stripped and not any(c.isspace() for c in stripped))
    )


def _person_identities(source: Any, records: Sequence[Entity],
                       people: Sequence[Entity]) -> dict[str, list[str]]:
    """Collect email / Teams user OID identities for ``person``/``contact`` entities so
    ``apply()`` can re-check them against ``exclusion`` even when ``Entity.alias`` omits them."""
    need_email = sorted({
        e.object_id for e in records
        if e.type in PERSON_TYPES and e.object_id
    })
    need_from = sorted({
        e.object_id for e in people
        if e.type in PERSON_TYPES and e.object_id
        and not any(_looks_like_identity(v) for v in (e.name, *e.alias))
    })
    by_oid: dict[str, list[str]] = {}
    for prop, oids in (("email", need_email), ("from", need_from)):
        for start in range(0, len(oids), 500):
            batch = oids[start:start + 500]
            for r in source.execute(
                    "SELECT object_id, value FROM object_values WHERE "
                    "object_id IN (SELECT value FROM json_each(?)) "
                    "AND superseded_at IS NULL AND property = ?",
                    (json.dumps(batch), prop)):
                oid, val = r["object_id"], r["value"]
                if not isinstance(val, str) or not val.strip():
                    continue
                if prop == "email":
                    by_oid.setdefault(oid, []).append(val.strip())
                else:
                    try:
                        who = json.loads(val)
                    except ValueError:
                        who = {}
                    if isinstance(who, dict) and isinstance(who.get("id"), str) and who["id"].strip():
                        by_oid.setdefault(oid, []).append(who["id"].strip().lower())
    out: dict[str, list[str]] = {}
    for e in (*records, *people):
        if e.type in PERSON_TYPES and e.object_id and e.object_id in by_oid:
            out[e.entity_id] = list(dict.fromkeys(by_oid[e.object_id]))
    return out


def _entity_clear(entity: Entity, exclusion: Exclusion,
                  identities: Sequence[str] = ()) -> bool:
    """Re-check ``person``/``contact`` entities against ``exclusion`` (fail-closed),
    even inside an otherwise clear audience unit."""
    if entity.type not in PERSON_TYPES:
        return True
    if exclusion is None or exclusion.unavailable:
        return False
    checks: list[str] = []
    for raw in identities:
        if isinstance(raw, str) and raw.strip():
            checks.append(raw.strip())
    for val in (entity.object_id, entity.name, *entity.alias):
        if _looks_like_identity(val):
            checks.append(val.strip())
    if not checks:
        return False
    return all(exclusion.identity_clear(v) for v in dict.fromkeys(checks))


def _readonly_layer_shard(task: tuple[dict[str, Any], int, int]) -> dict[str, Any]:
    """Read-only chunk worker for Spark compute: never writes Postgres."""
    job, index, total = task
    state = Path(job["state"])
    ctx_file = Path(job["exclusion_ctx"]) if job.get("exclusion_ctx") else None
    exclusion = _exclusion_from_context(state, dict(job["values"]), ctx_file)
    if exclusion.unavailable and PERSON_KINDS & set(KINDS):
        raise RuntimeError(f"people_exclusion_unavailable:{exclusion.unavailable}")
    counts = Counts()
    chunks_rows: list[list[Any]] = []
    seen: list[str] = []
    produced: set[tuple[str, str]] = set()
    projection = Path(job["projection"]) if job["projection"] != PG_PROJECTION else PG_PROJECTION
    for chunk in produce(state, projection, exclusion, counts, KINDS, None,
                         shard=(index, total) if total > 1 else None,
                         objects=frozenset(job["objects"]), dsn=job.get("dsn")):
        chunks_rows.append(_chunk_to_row(chunk))
        seen.append(chunk.chunk_id)
        if chunk.object_id:
            produced.add((chunk.object_id, chunk.kind))
    return {
        "chunks": chunks_rows,
        "seen": seen,
        "produced": sorted(produced),
        "withheld": sorted(counts.withheld),
        "skipped": dict(counts.as_dict().get("skipped") or {}),
    }


def _current_handles(source: Any, objects: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for start in range(0, len(objects), 500):
        batch = objects[start:start + 500]
        for oid, handle in source.execute(
                "SELECT object_id, l1_handle FROM object_versions WHERE superseded_at IS NULL "
                "AND object_id IN (SELECT value FROM json_each(?))",
                (json.dumps(batch),)):
            out[oid] = handle
    return out


def _compute_layer(conn: Any, source: Any, projection_arg: str | Path, state: Path,
                   exclusion: Exclusion, owned_all: list[str], *,
                   deadline: float | None, progress: dict[str, Any], workers: int,
                   values: dict[str, str], dsn: str, ctx_file: Path | None,
                   out_chunks: list[list[Any]], out_entities: list[dict[str, Any]],
                   out_purge: set[str], out_other_versions: dict[str, str],
                   out_replaced: list[dict[str, Any]], out_withheld_units: set[str]
                   ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp
    from . import follow as fl
    started = time.monotonic()
    done = int(progress.get("done") or 0)
    interrupted = progress.get("started")
    report: dict[str, Any] = {
        "owned": len(owned_all), "already_indexed": 0, "other_versions_deleted": 0,
        "chunks": 0, "inserted": 0, "updated": 0, "unchanged": 0, "replaced_deleted": 0,
        "withheld_units_deleted": 0, "sender_rows_purged": 0,
    }
    current = _current_handles(source, owned_all)
    withheld_msgs = withheld_messages(source, exclusion, objects=frozenset(owned_all))
    out_purge.update(withheld_msgs)
    report["sender_rows_purged_candidates"] = len(withheld_msgs)
    skipped: dict[str, int] = {}
    slices = [owned_all[i:i + fl.SLICE] for i in range(0, len(owned_all), fl.SLICE)]
    last = 0.0
    partial_out: dict[str, Any] | None = None
    for number in range(done, len(slices)):
        begun = fl._clock()
        if deadline is not None and begun + last >= deadline:
            report["partial"] = {"slices_done": number, "slices": len(slices)}
            partial_out = {"done": number}
            break
        part = slices[number]
        held = set() if interrupted == number else set(conn.execute(
            "SELECT DISTINCT object_id, split_part(source_handle, '#', 1) FROM search.chunks "
            "WHERE object_id = ANY(%s)", (part,)).fetchall())
        conn.commit()
        indexed = {oid for oid, handle in held if current.get(oid) == handle}
        objects = frozenset(part) - indexed
        report["already_indexed"] += len(indexed)
        for oid in indexed:
            if oid in current:
                out_other_versions[oid] = current[oid]
        n = max(1, workers) if len(objects) >= fl.PARALLEL_MIN else 1
        if not objects:
            parts: list[dict[str, Any]] = []
        elif n == 1:
            parts = [_readonly_layer_shard((
                {"state": str(state), "projection": str(projection_arg),
                 "values": dict(values), "dsn": dsn,
                 "exclusion_ctx": str(ctx_file) if ctx_file else "",
                 "objects": sorted(objects)}, 0, 1))]
        else:
            job = {"state": str(state), "projection": str(projection_arg),
                   "values": dict(values), "dsn": dsn,
                   "exclusion_ctx": str(ctx_file) if ctx_file else "",
                   "objects": sorted(objects)}
            with ProcessPoolExecutor(n, mp_context=mp.get_context("spawn"),
                                     **parent_watch.pool_init()) as pool:
                parts = list(pool.map(_readonly_layer_shard,
                                      [(job, i, n) for i in range(n)]))
        seen = [cid for p in parts for cid in p["seen"]]
        produced = sorted({tuple(pair) for p in parts for pair in p["produced"]})
        for p in parts:
            out_chunks.extend(p["chunks"])
            out_withheld_units.update(p["withheld"])
            for key, value in p["skipped"].items():
                skipped[key] = skipped.get(key, 0) + value
        if produced:
            out_replaced.append({"produced": [list(pair) for pair in produced],
                                 "keep": sorted(set(seen))})
        report["chunks"] += len(seen)
        last = fl._clock() - begun
        interrupted = None
        partial_out = {"done": number + 1}
    if "partial" not in report:
        partial_out = None
        ecounts = Counts()
        owned_set = frozenset(owned_all)
        records = list(entities(source, ecounts, exclusion=exclusion, objects=owned_set))
        people = list(participants(source, ecounts, exclusion=exclusion,
                                   objects=owned_set)) if owned_all else []
        ident_map = _person_identities(source, records, people)
        for e in records:
            out_entities.append(_entity_to_row(e, new_only=False,
                                               identities=ident_map.get(e.entity_id, ())))
        for e in people:
            out_entities.append(_entity_to_row(e, new_only=True,
                                               identities=ident_map.get(e.entity_id, ())))
        report["entities"] = {"records": len(records), "people_seen": len(people)}
    report["skipped"] = skipped
    report["seconds"] = round(time.monotonic() - started, 1)
    return report, partial_out


@contextlib.contextmanager
def _spark_embedder(model: str, column_dim: int, values: Mapping[str, str], *,
                    conn: Any = None):
    """Obtain an embedding encoder on the Spark without ever running on the live box.

    Prefers the Spark's GPU/embedding virtualenv (``~/plx-embed/venv/bin/python`` +
    ``remote_worker.py``) or ``PLX_SEARCH_FOLLOW_EMBED_CMD``, verifying hello + stored
    sample parity via ``remote``. Falls back to ``models.load`` only when no separate
    embed venv exists (e.g. a unit-test environment).
    """
    from . import models as md
    from . import remote
    from . import spark_embed

    cmd_override = os.environ.get("PLX_SEARCH_FOLLOW_EMBED_CMD", "").strip()
    embed_py = Path.home() / "plx-embed" / "venv" / "bin" / "python"
    embed_hf = Path.home() / "plx-embed" / "hf"
    if cmd_override or (embed_py.is_file() and embed_hf.is_dir() and model in md.LOCAL_MODELS):
        if cmd_override:
            argv = shlex.split(cmd_override)
            host_env = None
        else:
            precision = values.get("PLX_EMBED_PRECISION") or "fp16"
            if precision not in ("fp32", "fp16", "bf16"):
                precision = "fp16"
            batch = int(values.get("PLX_EMBED_BATCH") or 64)
            argv = [str(embed_py), str(remote.WORKER_FILE), "--model", model,
                    "--precision", precision, "--batch", str(batch), "--memory-fraction", "0.2"]
            host_env = {**os.environ, "HF_HOME": str(embed_hf), "HF_HUB_OFFLINE": "1"}
        host = _LocalWorkerHost("spark-local", argv, env=host_env)
        try:
            hello = spark_embed._started(host, spark_embed.START_SECONDS)
            differs = remote.check_hello(hello, model, column_dim)
            if differs is None and conn is not None:
                sample = remote.stored_sample(conn, model, spark_embed.VERIFY_ROWS)
                if sample:
                    least = remote.verify([host], sample, column_dim)[host.name]
                    if least < spark_embed.MIN_COSINE:
                        differs = "parity_below_min"
            if differs is not None:
                raise remote.HostFailed(f"spark_local:{differs}")
            yield md.Embedder(name=model, dim=int(hello["dim"]), _encode=host.encode,
                              remote=True)
        finally:
            host.close()
        return

    yield md.load(values, column_dim=column_dim)


class _LocalWorkerHost:
    """``remote.Host`` subclass allowing explicit environment variables (e.g. ``HF_HOME``)."""

    def __init__(self, name: str, argv: Sequence[str], *, env: Mapping[str, str] | None = None):
        import subprocess
        from . import remote
        self.name = name
        self._log = subprocess.DEVNULL
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
        self.hello: dict[str, Any] = {}
        self.texts = 0
        self.seconds_busy = 0.0
        self._delegate = remote.Host

    def start(self) -> dict[str, Any]:
        return self._delegate.start(self)

    def _header(self) -> dict[str, Any]:
        return self._delegate._header(self)

    def send(self, texts: Sequence[str]) -> None:
        return self._delegate.send(self, texts)

    def receive(self, n: int) -> np.ndarray:
        return self._delegate.receive(self, n)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return self._delegate.encode(self, texts)

    def close(self) -> None:
        return self._delegate.close(self)


def _already_embedded(conn: Any, model: str, shas: Sequence[str]) -> set[str]:
    from operating.pg import chunk_vectors
    if not shas:
        return set()
    if chunk_vectors.narrow(conn):
        rows = conn.execute(
            "SELECT DISTINCT content_sha FROM search.chunk_vectors "
            "WHERE embed_model = %s AND content_sha = ANY(%s)",
            (model, list(shas))).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT content_sha FROM search.chunks "
            "WHERE embed_model = %s AND embedding IS NOT NULL AND content_sha = ANY(%s)",
            (model, list(shas))).fetchall()
    conn.commit()
    return {r[0] for r in rows}


def _collect_pending_texts(conn: Any, model: str, chunks_rows: Sequence[Sequence[Any]],
                           max_hashes: int | None) -> list[tuple[str, str]]:
    """Collect distinct ``(content_sha, text)`` needing embeddings: newly chunked texts
    plus already-stored pending rows in ``recent`` order."""
    new_texts: dict[str, tuple[str, str, int]] = {}
    for r in chunks_rows:
        kind = str(r[3])
        if kind in st.WORDS_ONLY_KINDS:
            continue
        sha = str(r[9])
        occurred_at = "" if r[6] is None else str(r[6])
        authority = int(r[13])
        prev = new_texts.get(sha)
        if prev is None or (occurred_at, authority) > (prev[1], prev[2]):
            new_texts[sha] = (str(r[7]) + "\n\n" + str(r[8]), occurred_at, authority)
    already = _already_embedded(conn, model, sorted(new_texts))
    new_items = [
        (sha, data[0], data[1], data[2]) for sha, data in new_texts.items()
        if sha not in already
    ]
    # Sort newest occurred_at DESC (empty last), authority DESC, sha ASC
    new_items.sort(key=lambda item: item[0])
    new_items.sort(key=lambda item: item[3], reverse=True)
    new_items.sort(key=lambda item: item[2], reverse=True)
    new_items.sort(key=lambda item: item[2] == "")
    ordered: list[tuple[str, str]] = [(sha, text) for sha, text, _, _ in new_items]
    seen_shas = {sha for sha, _ in ordered}
    remaining = None if max_hashes is None else max(0, max_hashes - len(ordered))
    if remaining is None or remaining > 0:
        db_shas = [s for s in st.pending(conn, model, None, remaining, "recent")
                   if s not in seen_shas]
        if db_shas:
            by_sha = dict(st.texts_for(conn, db_shas))
            for sha in db_shas:
                if sha in by_sha:
                    ordered.append((sha, by_sha[sha]))
        conn.commit()
    return ordered[:max_hashes] if max_hashes is not None else ordered


def compute(args: Any, state: Path) -> dict[str, Any]:
    """Read-only Spark compute: chunks unindexed delta layers against the standby Postgres
    (or local SQLite in tests) and embeds pending texts on the Spark GPU/CPU, exporting
    the payload to ``args.export_dir``."""
    from operating.pg.ontology_reader import pg_only
    from . import follow as fl
    ctx_dir = Path(args.context_dir)
    out_dir = Path(args.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    manifest = json.loads((ctx_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("transport_version") != TRANSPORT_VERSION:
        raise ValueError("search_follow_manifest_version_unsupported")
    with gzip.open(ctx_dir / "layer-owned.json.gz", "rt", encoding="utf-8") as fh:
        owned_map = json.load(fh)
    ctx_file = ctx_dir / "exclusion-context.json"
    values = config.settings()
    exclusion = _exclusion_from_context(state, values, ctx_file if ctx_file.is_file() else None)
    report: dict[str, Any] = {"command": "follow", "mode": args.mode, "phase": "compute",
                              "generation": manifest["generation"], "started_at": fl._now()}
    if exclusion.unavailable and PERSON_KINDS & set(KINDS):
        report["refused"] = f"people_exclusion_unavailable:{exclusion.unavailable}"
        return report

    dsn = values.get("PLX_PG_DSN") or config.DEFAULT_DSN
    conn = st.connect(dsn)
    conn.read_only = True
    st.require_schema(conn)
    pg_proj: PgProjection | None = None
    try:
        pending_layers: list[str] = list(manifest["pending_layers"])
        use_pg = pg_only(os.environ) or any(
            owned_map.get(l, {}).get("status") == "ok" and not Path(l).is_file()
            for l in pending_layers)
        applied: set[str] | None = None
        if use_pg:
            if conn.execute("SELECT to_regclass('ontology.load_state')").fetchone()[0] is not None:
                applied = {r[0] for r in conn.execute(
                    "SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'delta:%%'")}
                conn.commit()
            pg_proj = PgProjection(dsn)

        indexed = list(manifest["indexed"])
        partial = dict(manifest.get("partial") or {})
        newly_indexed: list[str] = []
        new_partial: dict[str, Any] | None = partial if partial else None
        layer_reports: dict[str, Any] = {}
        layer_failed: str | None = None
        chunks_out: list[list[Any]] = []
        entities_out: list[dict[str, Any]] = []
        purge_ids: set[str] = set()
        other_versions: dict[str, str] = {}
        replaced_ops: list[dict[str, Any]] = []
        withheld_units: set[str] = set()

        started = fl._clock()
        deadline = started + args.layer_seconds if args.layer_seconds else None
        for layer in pending_layers:
            if deadline is not None and fl._clock() >= deadline:
                break
            info = owned_map.get(layer) or {"status": "layer_unreadable", "error": "missing"}
            name = Path(layer).name
            if info["status"] == "not_a_delta_layer":
                layer_reports[name] = {"skipped": "not_a_delta_layer"}
                newly_indexed.append(layer)
                indexed.append(layer)
                new_partial = None
                continue
            if info["status"] != "ok":
                layer_reports[name] = {"failed": info["status"], "error": info.get("error")}
                layer_failed = name
                break
            if applied is not None and name not in applied:
                layer_reports[name] = {"waiting": "waiting_for_postgres"}
                break
            if use_pg:
                assert pg_proj is not None
                source: Any = pg_proj
                projection_arg: str | Path = PG_PROJECTION
            else:
                source = open_ro(layer)
                projection_arg = Path(layer)
            try:
                progress = ({k: v for k, v in partial.items() if k != "layer"}
                            if partial.get("layer") == layer else {})
                res, slice_partial = _compute_layer(
                    conn, source, projection_arg, state, exclusion, list(info["owned"]),
                    deadline=deadline, progress=progress, workers=args.chunk_workers,
                    values=values, dsn=dsn, ctx_file=ctx_file if ctx_file.is_file() else None,
                    out_chunks=chunks_out, out_entities=entities_out,
                    out_purge=purge_ids, out_other_versions=other_versions,
                    out_replaced=replaced_ops, out_withheld_units=withheld_units)
            finally:
                if not use_pg:
                    source.close()
            layer_reports[name] = res
            if res.get("partial"):
                new_partial = dict(slice_partial or {}, layer=layer)
                break
            newly_indexed.append(layer)
            indexed.append(layer)
            new_partial = None
            partial = {}

        embed_report: dict[str, Any]
        embed_shas: list[str] = []
        vectors_arr: np.ndarray | None = None
        embed_model: str | None = None
        embed_dim: int | None = None
        pending_after_est: int | None = None

        wrote = len(chunks_out) > 0
        if manifest.get("embed_skip") is not None:
            embed_report = {"skipped": manifest["embed_skip"]}
        elif args.embed_seconds and (wrote or manifest["follow_cursor"].get("pending_after") != 0):
            dim = st.column_dim(conn)
            model = values.get("PLX_EMBED_MODEL") or config.DEFAULT_MODEL
            if dim is None:
                embed_report = {"refused": "embedding_column_absent"}
            else:
                batch_size = max(1, int(getattr(args, "embed_batch", 0) or 16))
                wb = manifest.get("write_budget")
                max_hashes: int | None = None
                if isinstance(wb, dict) and "bytes_left" in wb:
                    max_hashes = max(batch_size * 4, min(2048, int(wb["bytes_left"] // 2048)))
                todo = _collect_pending_texts(conn, model, chunks_out, max_hashes)
                if not todo:
                    embed_report = {"on": "spark", "model": model, "dim": dim,
                                    "texts_encoded": 0}
                    pending_after_est = 0
                else:
                    t0 = fl._clock()
                    encoded_vecs: list[np.ndarray] = []
                    with _spark_embedder(model, dim, values, conn=conn) as embedder:
                        embed_model = embedder.name
                        embed_dim = embedder.dim
                        encode_batch = max(batch_size, 64)
                        for start in range(0, len(todo), encode_batch):
                            if fl._clock() - t0 > float(args.embed_seconds):
                                break
                            part = todo[start:start + encode_batch]
                            vecs = embedder.encode([t for _, t in part])
                            encoded_vecs.append(np.asarray(vecs, dtype=np.float32))
                            embed_shas.extend(s for s, _ in part)
                    if encoded_vecs:
                        vectors_arr = np.vstack(encoded_vecs)
                    embed_report = {
                        "on": "spark", "model": embed_model, "dim": embed_dim,
                        "texts_encoded": len(embed_shas),
                        "seconds": round(fl._clock() - t0, 1),
                    }
        else:
            embed_report = {"skipped": "nothing_pending"}

        chunks_file: str | None = None
        if chunks_out:
            chunks_file = "chunks.jsonl.gz"
            tmp = out_dir / (chunks_file + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                for row in chunks_out:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, out_dir / chunks_file)

        entities_file: str | None = None
        if entities_out:
            entities_file = "entities.jsonl.gz"
            tmp = out_dir / (entities_file + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                for row in entities_out:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, out_dir / entities_file)

        vectors_file: str | None = None
        if vectors_arr is not None and embed_shas:
            vectors_file = "vectors.npy"
            tmp = out_dir / (vectors_file + ".tmp")
            with open(tmp, "wb") as fh:
                np.save(fh, vectors_arr.astype(np.float32), allow_pickle=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, out_dir / vectors_file)

        layers_list = list(manifest["layers"])
        export = {
            "search_follow_transport": TRANSPORT_VERSION,
            "generation": manifest["generation"],
            "digest": manifest["digest"],
            "after_pass": manifest["after_pass"],
            "base_layers": list(manifest["indexed"]),
            "base_partial": manifest.get("partial") or {},
            "newly_indexed": newly_indexed,
            "new_partial": new_partial,
            "layers": layers_list,
            "layer_reports": layer_reports,
            "layer_failed": layer_failed,
            "layers_behind": len([x for x in layers_list if x not in indexed]),
            "search_lag": fl.layer_lag(layers_list, set(indexed)),
            "purge_object_ids": sorted(purge_ids),
            "delete_other_versions": other_versions,
            "delete_replaced": replaced_ops,
            "delete_withheld_units": sorted(withheld_units),
            "chunks_file": chunks_file,
            "entities_file": entities_file,
            "embed": {
                "model": embed_model,
                "dim": embed_dim,
                "shas": embed_shas,
                "vectors_file": vectors_file,
                "pending_after_est": pending_after_est,
                "report": embed_report,
            },
        }
        _write_json_atomic(out_dir / "export.json", export)
        report["layers"] = layer_reports
        report["layers_behind"] = export["layers_behind"]
        report["search_lag"] = export["search_lag"]
        report["embed"] = embed_report
        if layer_failed:
            report["layer_failed"] = layer_failed
        return report
    finally:
        if pg_proj is not None:
            pg_proj.close()
        conn.close()


def _validate_chunks_file(path: Path) -> str | None:
    """Stream-parse and validate ``chunks.jsonl.gz`` before any DB mutation."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, list) or len(row) < 14:
                return "invalid_chunks_row"
            if not all(isinstance(row[i], str) for i in (0, 1, 3, 7, 8, 9, 11)):
                return "invalid_chunks_row"
            if any(row[i] is not None and not isinstance(row[i], str) for i in (2, 4, 5, 6)):
                return "invalid_chunks_row"
            if not isinstance(row[10], list) or not isinstance(row[12], list):
                return "invalid_chunks_row"
            if isinstance(row[13], bool) or not isinstance(row[13], int):
                return "invalid_chunks_row"
            _row_to_chunk(row)
    return None


def _validate_entities_file(path: Path) -> str | None:
    """Stream-parse and validate ``entities.jsonl.gz`` before any DB mutation."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict) or not isinstance(item.get("new_only"), bool):
                return "invalid_entities_row"
            r = item.get("row")
            if not isinstance(r, list) or len(r) < 8:
                return "invalid_entities_row"
            if not all(isinstance(r[i], str) for i in (0, 2, 3, 5, 6)):
                return "invalid_entities_row"
            if r[1] is not None and not isinstance(r[1], str):
                return "invalid_entities_row"
            if not isinstance(r[4], list) or not isinstance(r[7], list):
                return "invalid_entities_row"
            if "identities" in item and (
                not isinstance(item["identities"], list)
                or not all(isinstance(x, str) for x in item["identities"])
            ):
                return "invalid_entities_row"
            _row_to_entity(item)
    return None


def _validate_export(export_dir: Path) -> tuple[str, dict[str, Any] | None, str | None]:
    """Read and validate ``export.json`` and referenced payload files before any DB write."""
    export_path = export_dir / "export.json"
    try:
        export = json.loads(export_path.read_text(encoding="utf-8"))
        if not isinstance(export, dict) or export.get("search_follow_transport") != TRANSPORT_VERSION:
            return "bad", None, "invalid_transport_version"
        for key in ("generation", "digest"):
            if not isinstance(export.get(key), str) or not export[key]:
                return "bad", None, f"missing_{key}"
        for key in ("base_layers", "newly_indexed", "layers", "purge_object_ids",
                    "delete_replaced", "delete_withheld_units"):
            if not isinstance(export.get(key), list):
                return "bad", None, f"invalid_{key}"
        for item in export["delete_replaced"]:
            if (not isinstance(item, dict) or not isinstance(item.get("produced"), list)
                    or not isinstance(item.get("keep"), list)):
                return "bad", None, "invalid_delete_replaced"
        if not isinstance(export.get("delete_other_versions"), dict):
            return "bad", None, "invalid_delete_other_versions"
        if export.get("chunks_file") is not None:
            if not isinstance(export["chunks_file"], str) or not export["chunks_file"]:
                return "bad", None, "invalid_chunks_file"
            p = export_dir / export["chunks_file"]
            if not p.is_file():
                return "bad", None, "missing_chunks_file"
            err = _validate_chunks_file(p)
            if err is not None:
                return "bad", None, err
        if export.get("entities_file") is not None:
            if not isinstance(export["entities_file"], str) or not export["entities_file"]:
                return "bad", None, "invalid_entities_file"
            p = export_dir / export["entities_file"]
            if not p.is_file():
                return "bad", None, "missing_entities_file"
            err = _validate_entities_file(p)
            if err is not None:
                return "bad", None, err
        embed_meta = export.get("embed")
        if not isinstance(embed_meta, dict) or not isinstance(embed_meta.get("shas"), list):
            return "bad", None, "invalid_embed_meta"
        vfile = embed_meta.get("vectors_file")
        if vfile is not None:
            vp = export_dir / str(vfile)
            if not vp.is_file():
                return "bad", None, "missing_vectors_file"
            arr = np.load(vp, allow_pickle=False)
            shas = embed_meta["shas"]
            dim = embed_meta.get("dim")
            if (not isinstance(dim, int) or dim <= 0
                    or arr.ndim != 2 or arr.shape != (len(shas), dim)
                    or not np.isfinite(arr).all()):
                return "bad", None, "invalid_vectors_shape_or_values"
            if not isinstance(embed_meta.get("model"), str) or not embed_meta["model"]:
                return "bad", None, "missing_embed_model"
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError, IndexError) as exc:
        return "bad", None, f"export_unreadable:{type(exc).__name__}"
    return "ok", export, None


def _unlink_applied(export_dir: Path, export: Mapping[str, Any]) -> None:
    for name in ("export.json", export.get("chunks_file"), export.get("entities_file"),
                 (export.get("embed") or {}).get("vectors_file")):
        if isinstance(name, str) and name:
            with contextlib.suppress(OSError):
                (export_dir / name).unlink()


def apply(args: Any, state: Path, *,
          clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Lightweight box apply: writes precomputed chunks, entities and deletions to primary
    Postgres, stores precomputed vectors under ``--embed-batch`` / ``--embed-rows`` /
    ``--embed-write-mb-per-hour`` and ``--min-free-gb``, and updates ``follow.json`` and
    ``embed-write-budget.json`` atomically without loading any embedding model."""
    from . import backfill, follow as fl
    from .write_budget import RollingWriteBudget, WalMeter, WriteBudgetError

    apply_dir = Path(args.apply_dir)
    report: dict[str, Any] = {"command": "follow", "mode": args.mode, "phase": "apply",
                              "started_at": fl._now()}
    values = config.settings()
    exclusion = _exclusion(state, values)
    if exclusion.unavailable and PERSON_KINDS & set(KINDS):
        report["refused"] = f"people_exclusion_unavailable:{exclusion.unavailable}"
        return report
    if args.min_free_gb and fl._free_gb(state) < args.min_free_gb:
        report["skipped"] = "disk_below_min_free"
        return report

    status, export, why = _validate_export(apply_dir)
    if status != "ok" or export is None:
        report["bad"] = 1
        report["error"] = why
        return report

    cursor_path = Path(args.cursor) if args.cursor else state / fl.CURSOR_NAME
    with backfill.waited(cursor_path.with_name(cursor_path.name + ".lock"),
                         args.wait_seconds) as mine:
        if not mine:
            report["skipped"] = "another_follow_running"
            return report
        now_served = fl.served(state)
        if now_served is None or now_served["digest"] != export["digest"]:
            report["stale"] = 1
            report["reason"] = "digest_changed"
            return report
        cursor, _layers, indexed, partial, pass_done = _effective_cursor(
            state, cursor_path, now_served)
        if pass_done != export["after_pass"]:
            report["stale"] = 1
            report["reason"] = "after_pass_changed"
            return report
        if indexed != export["base_layers"] or partial != (export.get("base_partial") or {}):
            report["stale"] = 1
            report["reason"] = "cursor_moved"
            return report

        conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
        try:
            st.require_schema(conn)
            embed_meta = export["embed"]
            model = embed_meta.get("model")
            dim = embed_meta.get("dim")
            col_dim = st.column_dim(conn)
            if embed_meta.get("vectors_file") is not None:
                if col_dim is None or not isinstance(dim, int) or dim > col_dim:
                    report["bad"] = 1
                    report["error"] = "embedding_column_incompatible"
                    return report
                if model not in ("BAAI/bge-m3", "intfloat/e5-large-v2") and not str(
                        model).startswith("openai:"):
                    report["bad"] = 1
                    report["error"] = f"embed_model_unknown:{model}"
                    return report

            # Apply chunk/entity writes and deletions
            writer = st.ChunkWriter(conn, unit_of=lambda key: unit_row(key, exclusion))
            extra_withheld: set[str] = set(export["delete_withheld_units"])
            st.purge_objects(conn, export["purge_object_ids"])
            st.delete_other_versions(conn, export["delete_other_versions"])

            if export.get("chunks_file"):
                batch: list[Chunk] = []
                with gzip.open(apply_dir / export["chunks_file"], "rt", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        chunk = _row_to_chunk(json.loads(line))
                        if not exclusion.unit_clear(chunk.audience_key):
                            extra_withheld.add(chunk.audience_key)
                            continue
                        batch.append(chunk)
                        if len(batch) >= WRITE_BATCH:
                            writer.write(batch)
                            batch = []
                    if batch:
                        writer.write(batch)

            for item in export["delete_replaced"]:
                st.delete_replaced(conn, [tuple(p) for p in item["produced"]], item["keep"])

            if export.get("entities_file"):
                rec_batch: list[Entity] = []
                ppl_batch: list[Entity] = []
                with gzip.open(apply_dir / export["entities_file"], "rt", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        new_only, entity = _row_to_entity(item)
                        if not exclusion.unit_clear(entity.audience_key):
                            extra_withheld.add(entity.audience_key)
                            continue
                        if not _entity_clear(entity, exclusion, item.get("identities") or ()):
                            continue
                        target = ppl_batch if new_only else rec_batch
                        target.append(entity)
                        if len(target) >= WRITE_BATCH:
                            writer.write_entities(target, new_only=new_only)
                            target.clear()
                    if rec_batch:
                        writer.write_entities(rec_batch, new_only=False)
                    if ppl_batch:
                        writer.write_entities(ppl_batch, new_only=True)

            writer.delete_units(extra_withheld)

            # Update follow cursor for layers processed
            new_indexed = list(indexed) + list(export["newly_indexed"])
            next_cursor = dict(cursor, generation=now_served["generation"],
                               digest=now_served["digest"], after_pass=pass_done,
                               layers=new_indexed)
            if export.get("new_partial"):
                next_cursor["partial"] = export["new_partial"]
            else:
                next_cursor.pop("partial", None)
            if writer.inserted or writer.updated:
                next_cursor.pop("pending_after", None)
            fl.save_cursor(cursor_path, next_cursor)

            # Apply precomputed embeddings under WAL budget + disk floor
            embed_report = _apply_embeddings(
                args, state, conn, values, apply_dir, embed_meta,
                next_cursor, cursor_path, clock=clock)
            report["generation"] = now_served["generation"]
            report["layers"] = export["layer_reports"]
            report["layers_behind"] = export["layers_behind"]
            report["search_lag"] = export["search_lag"]
            report["write"] = {"inserted": writer.inserted, "updated": writer.updated,
                               "unchanged": writer.unchanged}
            report["embed"] = embed_report
            if export.get("layer_failed"):
                report["layer_failed"] = export["layer_failed"]
            if writer.inserted or writer.updated or "embed" in report:
                report["newest"] = {kind: str(at) for kind, at in conn.execute(
                    "SELECT kind, max(occurred_at) FROM search.chunks WHERE occurred_at <= now() "
                    "+ interval '1 hour' GROUP BY kind ORDER BY kind").fetchall()}
                conn.commit()
            _unlink_applied(apply_dir, export)
        finally:
            conn.close()
    report["embedded_gate"] = fl._embedded_gate(
        values, state, force=getattr(args, "embedded_gate_now", False))
    return report


def _apply_embeddings(args: Any, state: Path, conn: Any, values: dict[str, str],
                      apply_dir: Path, embed_meta: Mapping[str, Any],
                      cursor: dict[str, Any], cursor_path: Path, *,
                      clock: Callable[[], float]) -> dict[str, Any]:
    from . import backfill, follow as fl
    from .write_budget import RollingWriteBudget, WalMeter, WriteBudgetError

    low = bool(args.min_free_gb and fl._free_gb(state) < args.min_free_gb)
    paused = None if low else fl._wal_paused(values)
    if low:
        return {"skipped": "disk_below_min_free"}
    if paused:
        return {"skipped": paused}
    if fl.delta_running(state):
        return {"skipped": "delta_running"}
    if backfill.running(state):
        return {"skipped": "backfill_running"}
    if not args.embed_seconds:
        return {"skipped": "embed_disabled"}

    vfile = embed_meta.get("vectors_file")
    shas: list[str] = list(embed_meta.get("shas") or [])
    if vfile is None or not shas:
        est = embed_meta.get("pending_after_est")
        if est == 0:
            cursor["pending_after"] = 0
            fl.save_cursor(cursor_path, cursor)
        return dict(embed_meta.get("report") or {"texts_embedded": 0, "on": "spark"})

    model = str(embed_meta["model"])
    dim = int(embed_meta["dim"])
    col_dim = st.column_dim(conn)
    if col_dim is None:
        return {"refused": "embedding_column_absent"}
    row_cap_raw = getattr(args, "embed_rows", 0) or 0
    row_cap = int(row_cap_raw) if row_cap_raw > 0 else None
    batch_size = max(1, int(getattr(args, "embed_batch", 0) or 256))

    write_budget = None
    write_mb = float(getattr(args, "embed_write_mb_per_hour", 0.0) or 0.0)
    meter = None
    if write_mb > 0:
        try:
            write_budget = RollingWriteBudget(
                state / "search" / "embed-write-budget.json", write_mb * 1024 * 1024,
                clock=clock)
            write_budget.save()
        except WriteBudgetError as exc:
            return {"skipped": str(exc), "on": "not_started"}
        if write_budget.blocked():
            return {"skipped": "write_budget_exhausted", "on": "not_started",
                    "write_budget": write_budget.status()}
        meter = WalMeter(conn)
        if meter.position() is None:
            return {"refused": "wal_position_unreadable", "model": model,
                    "write_budget": write_budget.status()}

    vectors = np.load(apply_dir / str(vfile), allow_pickle=False)
    started = fl._clock()
    budget_seconds = float(args.embed_seconds)
    texts_done = rows = 0
    wal_written = 0
    for start in range(0, len(shas), batch_size):
        if fl._clock() - started > budget_seconds:
            break
        if args.min_free_gb and fl._free_gb(state) < args.min_free_gb:
            break
        if write_budget is not None and write_budget.blocked():
            break
        batch_shas = shas[start:start + batch_size]
        batch_vecs = vectors[start:start + batch_size]
        before = meter.position() if meter is not None else None
        if meter is not None and before is None:
            raise RuntimeError("wal_position_lost_before_embed_batch")
        rows += st.store_embeddings(conn, model, col_dim, batch_shas, batch_vecs,
                                    row_cap=row_cap)
        if meter is not None and write_budget is not None:
            written = meter.since(before)
            if written is None:
                raise RuntimeError("wal_position_lost_during_embed")
            wal_written += written
            write_budget.record(written)
        texts_done += len(batch_shas)

    remaining = len(st.pending(conn, model, None, None))
    conn.commit()
    cursor["pending_after"] = remaining
    fl.save_cursor(cursor_path, cursor)
    out: dict[str, Any] = {
        "on": "spark", "model": model, "dim": dim, "column_dim": col_dim,
        "texts_embedded": texts_done, "rows_updated": rows,
        "pending_after": remaining, "batch_size": batch_size,
        "seconds": round(fl._clock() - started, 1),
    }
    if row_cap is not None:
        out["row_cap"] = row_cap
    if write_budget is not None:
        out["write_budget"] = write_budget.status()
        out["wal_bytes"] = wal_written
        out["wal_bytes_per_row"] = int(wal_written / rows) if rows else None
    return out
