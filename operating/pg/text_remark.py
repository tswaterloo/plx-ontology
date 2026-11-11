"""Add ``finance`` and ``pii`` to the served text fields of payroll and HR documents, in place.

Main's text index (``ontology.text_index._sensitive``, 7aba9f1245) marks the text fields of
a pay stub, an offer or termination letter, or a payroll- or HR-labelled file ``finance`` and
``pii``. The served Postgres text fields were indexed by older code without those marks.
This command adds them on their own: the same judging as the step migration's re-mark
(``ontology.step_migrate.text_marks``) and the same write as its apply
(``operating.pg.ontology_migrate._apply_text_marks``). It writes only
``ontology.text_fields.markings`` and only ever adds marks. No text, chunk, ``store_meta``
key or steps digest is touched.

    python3 -m operating.pg.text_remark judge --state-dir /data/plx-state --work DIR [--tmp T]
        [--readings SNAP] [--stride I/N]
    python3 -m operating.pg.text_remark judge-others --state-dir /data/plx-state --work DIR
        [--tmp T] [--readings SNAP]
    python3 -m operating.pg.text_remark apply --work DIR --dsn DSN [--dry-run]
    python3 -m operating.pg.text_remark undo --work DIR --dsn DSN

* ``judge`` reads every capture source's documents (not through the build's kept readings,
  a shared cache it must not prune or lock) in ``--shards`` shards on ``--workers`` processes. A shard is the groups whose
  key hashes to it. Each finished shard is written atomically to ``DIR/shards/``, and a rerun
  judges only the shards not done. A group that cannot be read fails its shard (its
  documents cannot be known unmarked). Every shard records how many groups it listed and a
  digest of their keys; ``apply`` refuses shards that disagree (a listing that changed under
  the judge). Run it with ``PYTHONHASHSEED=0``: a mail group's key is the repr of a
  frozenset. ``--readings SNAP``: a directory holding a copy of the kept readings
  (``readings.sqlite``, ``member-readings.sqlite``, e.g. made with ``sqlite3 .backup``),
  served read-only so a file read before is not extracted again; the live files of the
  state directory are refused (a reader there held them while a build wrote).
  ``--stride I/N``: judge only the shards ``s`` with ``s % N == I`` (one of N judges
  side by side, each its own process and memory limit). A group of no whole-table
  listing (a document of a source without groups has no key) is in the shard of its
  ordinal among those groups only (``RULE``); shard files judged before that used its
  ordinal among all the groups the shard listed, which differs per shard (such a group
  was judged in no shard or in several).
* ``judge-others`` (for shard files judged before ``RULE``): judges every group of no
  listing once into ``DIR/others.json``, with what each shard judged of them under the
  old rule; ``apply`` then takes each shard's own listed groups (checked to be exactly
  those) and these. Marks are only ever added, so an old shard's mark stays.
* ``apply --dry-run`` reads the fields of the flagged documents (one read-only statement
  per batch, ``statement_timeout``) and prints the counts. Nothing is written.
* ``apply`` refuses unless this tree's text index code is a pinned marks-only target
  (``step_migrate.MARKS_ONLY``). It then adds the marks in batches. Each batch is one
  transaction under the delta's lock (the same advisory lock the apply and delta take), its
  rows locked and compared, and a field changed since it was read keeps its value. A field
  is never changed before its previous markings are fsynced to ``DIR/undo.jsonl``. A rerun
  resumes: a field already marked is not written again.
* ``undo`` restores the previous markings of every field in ``DIR/undo.jsonl`` that still
  holds what this command wrote.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator, Mapping, Sequence

MARKS = ("finance", "pii")
LOCK = "SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))"   # ontology_migrate.LOCK
BATCH = 2_000
TIMEOUT_MS = 600_000


class Refused(RuntimeError):
    pass


def _key_hash(key: Any, index: int) -> int:
    text = repr(key) if key is not None else f"#{index}"
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


# The capture sources whose latest rows ``document_groups`` lists with one ``ORDER BY``
# over the whole table (``ontology.project``: mail ``messages``, SharePoint ``items``). Sorted
# whole, that is a temporary B-tree of every row with its record (the mail store's is about
# 7 GB); in every shard at once it filled the disk. Here each shard's query keeps only its
# own handles (in the subquery, from the covering handle index), so its sort holds about
# 1/shards of the rows, and no row of another shard is read. The order, the rows and the
# loads are the source's own. A source whose query is not this shape is refused.
SHARDED_LOADS = {"_message_documents", "_file_documents"}
_LATEST_TAIL = re.compile(r" FROM (messages|items) GROUP BY l1_handle\)$")


def _in_shard(handle: Any, shard: int, shards: int) -> bool:
    return _key_hash(handle, 0) % shards == shard


def _shard_sources(projector: Any, shard: int, shards: int
                   ) -> tuple[dict[int, tuple[int, int]], str]:
    """Restrict the whole-table listing of each source that has one to ``shard`` (an
    instance ``_LATEST``); return {id(source): (its latest handles in all shards, in
    this one)} and a digest of those handles (every shard must see the same)."""
    listing, digest = _listing(projector, shard, shards)
    return {k: (total, per[shard]) for k, (total, per) in listing.items()}, digest


def _listing(projector: Any, shard: int | None, shards: int
             ) -> tuple[dict[int, tuple[int, list[int]]], str]:
    """:func:`_shard_sources` with each source's latest handles counted per shard
    ({id(source): (all, [in shard 0, 1, ...])}); ``shard`` None: no group of these
    sources is listed at all (the other sources' groups judged on their own)."""
    sharded: dict[int, tuple[int, list[int]]] = {}
    digest = hashlib.sha256()
    for name in sorted(getattr(projector, "sources", {}) or {}):
        source = projector.sources[name]
        source = getattr(source, "_inner", source)
        latest = getattr(type(source), "_LATEST", None)
        conn = getattr(source, "_conn", None)
        if not callable(getattr(source, "document_groups", None)) or latest is None \
                or conn is None:
            continue
        found = _LATEST_TAIL.search(latest)
        if found is None or getattr(source, "sample", None) is not None:
            raise Refused(f"listing_not_shardable:{name}")
        table = found.group(1)
        conn.create_function("plx_remark_shard", 1,
                             lambda handle: int(shard is not None
                                                and _in_shard(handle, shard, shards)),
                             deterministic=True)
        source._LATEST = latest[:-1] + " HAVING plx_remark_shard(l1_handle))"
        handles = 0
        per = [0] * shards
        digest.update(f"{name}\x00".encode("utf-8"))
        for (handle,) in conn.execute(f"SELECT l1_handle FROM {table} GROUP BY l1_handle "
                                      "ORDER BY l1_handle"):
            handles += 1
            per[_key_hash(handle, 0) % shards] += 1
            digest.update(repr(handle).encode("utf-8") + b"\x00")
        sharded[id(source)] = (handles, per)
    return sharded, digest.hexdigest()


KEPT_FILES = ("readings.sqlite", "member-readings.sqlite")


def readings_snapshot(state: str | Path, readings: str | Path) -> Path:
    """``readings`` as a directory of kept-readings copies the judge may read: it holds a
    ``readings.sqlite`` and none of its files is a live kept store of ``state`` (its
    projection or delta directory, the same file under another name included)."""
    snap = Path(readings).resolve()
    if not (snap / KEPT_FILES[0]).is_file():
        raise Refused(f"readings_snapshot_missing:{KEPT_FILES[0]}")
    projection = Path(state) / "projection"
    live = [d / f for d in (projection, projection / "delta") for f in KEPT_FILES]
    for name in KEPT_FILES:
        mine = snap / name
        if not mine.exists():
            continue
        for other in live:
            if other.exists() and os.path.samefile(mine, other):
                raise Refused(f"readings_snapshot_is_live:{name}")
    return snap


def _claim(path: Path) -> bool:
    """Claim the shard written to ``path`` for this process: ``<path>.lock`` created
    atomically (a link of a file naming this process); a lock whose process no longer
    runs (``host.process_token`` differs) is taken over. False: a running judge holds it."""
    from operating.host import process_token
    lock = path.with_name(path.name + ".lock")
    mine = json.dumps({"pid": os.getpid(), "token": process_token(os.getpid())})
    for _attempt in range(3):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.claim")
        tmp.write_text(mine, encoding="utf-8")
        try:
            os.link(tmp, lock)
            return True
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
            alive = held.get("token") is not None and \
                process_token(int(held["pid"])) == held["token"]
        except (OSError, ValueError, TypeError, KeyError):
            alive = False   # a lock being written or unreadable: decided on the next look
            time.sleep(0.2)
            if lock.exists() and lock.read_text(encoding="utf-8").strip():
                continue
        if alive:
            return False
        stale = lock.with_name(f"{lock.name}.{os.getpid()}.stale")
        try:
            os.rename(lock, stale)   # one taker wins; the others retry the link
            stale.unlink()
        except FileNotFoundError:
            pass
    return False


def judge_shard(state: str, shard: int, shards: int, out: str,
                readings: str | None = None) -> dict[str, Any]:
    """Judge the documents of the groups in ``shard`` (of ``shards``); write
    ``out/shard-<n>.json`` atomically (a shard already written is not judged again, so
    judges may share a work directory: a shard in progress is locked, :func:`_claim`;
    one held by another running judge is left to it). The capture sources are opened read-only, as
    the step build opens them. The build's live kept readings are never opened (closing
    them prunes and writes that shared cache, and a reader there held a build's writes). A group of a whole-table listing
    (``_shard_sources``) is in the shard of its handle; any other group in the shard of its
    key. ``readings``: a snapshot of the kept readings (:func:`readings_snapshot`),
    served read-only (``kept_members.serve_kept``: a hit is the reading a read would
    make, nothing is written). Progress (counts only) goes to stderr."""
    path = Path(out) / f"shard-{shard}.json"
    if path.exists():
        held = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in held.items() if k != "marked"}
    if not _claim(path):
        return {"shard": shard, "shards": shards, "held_by_other_judge": True}
    try:
        if path.exists():   # finished by the judge whose lock this replaced
            held = json.loads(path.read_text(encoding="utf-8"))
            return {k: v for k, v in held.items() if k != "marked"}
        return _judge_shard(state, shard, shards, path, readings)
    finally:
        path.with_name(path.name + ".lock").unlink(missing_ok=True)


@contextlib.contextmanager
def _projector(state: str, readings: str | None) -> Iterator[Any]:
    """The step build's projector over the capture sources of ``state``, opened
    read-only; ``readings``: a kept-readings snapshot served read-only."""
    from ontology import step_migrate as sm
    from ontology.delta import _internal_domains
    from ontology.schema import load_schema
    from operating.host import state_captures

    snap = readings_snapshot(state, readings) if readings is not None else None
    with contextlib.ExitStack() as stack:
        sources = state_captures(Path(state), stack,
                                 internal_domains=_internal_domains(os.environ))
        if snap is not None:
            from integrations.kept_members import serve_kept
            serve_kept([snap], [s.blobs for s in sources.values()
                                if isinstance(getattr(s, "blobs", None), (str, Path))])
        if hasattr(sm, "open_snapshots"):   # one read snapshot per capture store
            sm.open_snapshots(sources)
        yield sm._projector(load_schema(), sources, None)


def _write(path: Path, result: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.part")
    tmp.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp, path)


def _judge_shard(state: str, shard: int, shards: int, path: Path,
                 readings: str | None) -> dict[str, Any]:
    from ontology.text_index import _sensitive
    started = time.monotonic()
    with _projector(state, readings) as projector:
        result = judge_groups(projector, shard, shards, _sensitive)
    result["seconds"] = round(time.monotonic() - started, 1)
    _write(path, result)
    return {k: v for k, v in result.items() if k != "marked"}


# A group of no whole-table listing (no key: a document of a source without groups) is in
# the shard of its ordinal among those groups only. Shard files of RULE carry it; a shard
# file without was judged with the ordinal among all groups the shard listed, which
# counts its own mail and file groups and so differs per shard (such a group was judged
# in no shard or in several): ``judge-others`` judges those groups again (others.json).
RULE = "other-ordinal"


def _judge_group(load: Any, sensitive: Any, counts: dict[str, int], marked: set[str],
                 label: str) -> int:
    """Judge one group into ``counts``/``marked``; its documents (-1: unread)."""
    counts["groups"] += 1
    try:
        documents, _remembered = load()
    except (MemoryError, KeyboardInterrupt):
        raise
    except Exception:  # noqa: BLE001 - counted, the judge then fails
        counts["failed_groups"] += 1
        return -1
    for document in documents:
        counts["documents"] += 1
        if sensitive(document):
            marked.add(document.citation)
    if counts["groups"] % 5_000 == 0:
        print(f"{label}: groups {counts['groups']} documents {counts['documents']}",
              file=sys.stderr, flush=True)
    return len(documents)


def judge_groups(projector: Any, shard: int, shards: int, sensitive: Any
                 ) -> dict[str, Any]:
    """The judged shard ``shard`` of ``projector``'s groups: a group of a whole-table
    listing in the shard of its handle, any other in the shard of its key (no key: of
    its ordinal among the groups of no listing, the same in every shard)."""
    from ontology.mentions import source_document_groups
    sharded, listed = _shard_sources(projector, shard, shards)
    keys = hashlib.sha256(listed.encode("utf-8"))
    counts = {"groups_listed": sum(total for total, _mine in sharded.values()),
              "groups": 0, "documents": 0, "failed_groups": 0, "sharded_groups": 0}
    marked: set[str] = set()
    seen: dict[int, int] = {k: 0 for k in sharded}
    other = -1
    for key, load in source_document_groups(projector):
        method = getattr(load, "method", None)
        if method in SHARDED_LOADS:
            owner = id(getattr(load.source, "_inner", load.source))
            if owner not in sharded or not _in_shard(load.args[0]["l1_handle"], shard,
                                                     shards):
                raise Refused(f"group_outside_its_shard:{method}")
            seen[owner] += 1
            counts["sharded_groups"] += 1
        else:
            other += 1
            counts["groups_listed"] += 1
            keys.update(repr(key).encode("utf-8") if key is not None else b"\x00")
            if _key_hash(key, other) % shards != shard:
                continue
        _judge_group(load, sensitive, counts, marked, f"shard {shard}")
    if any(seen[k] != mine for k, (_total, mine) in sharded.items()):
        raise Refused(f"shard_listing_incomplete:{shard}")
    if counts["failed_groups"]:
        raise Refused(f"groups_unread:{shard}:{counts['failed_groups']}")
    return {"shard": shard, "shards": shards, "rule": RULE, "keys": keys.hexdigest(),
            "counts": counts, "marked": sorted(marked)}


def judge_other_groups(projector: Any, shards: int, sensitive: Any) -> dict[str, Any]:
    """Judge once every group of no whole-table listing (none of a listing is read), for
    shard files judged before :data:`RULE`. Also told: each shard's listed groups
    (``sharded``) and, per shard, the groups of no listing it judged under the old rule
    (its ordinal among all groups it listed: the groups of the listed sources sorted
    before plus its ordinal among these) and their documents (``old_rule``), so their
    contribution in each shard file is replaced by this one."""
    from types import SimpleNamespace
    from ontology.mentions import source_document_groups
    listing, listed = _listing(projector, None, shards)
    keys = hashlib.sha256(listed.encode("utf-8"))
    counts = {"groups_listed": sum(total for total, _per in listing.values()),
              "groups": 0, "documents": 0, "failed_groups": 0}
    sharded = [sum(per[s] for _total, per in listing.values()) for s in range(shards)]
    old_groups, old_documents = [0] * shards, [0] * shards
    before = [0] * shards
    marked: set[str] = set()
    other = -1
    for name in sorted(getattr(projector, "sources", {}) or {}):
        source = projector.sources[name]
        owner = id(getattr(source, "_inner", source))
        alone = SimpleNamespace(sources={name: source})
        for key, load in source_document_groups(alone):
            if getattr(load, "method", None) in SHARDED_LOADS:
                raise Refused(f"listed_group_in_others:{name}")
            other += 1
            counts["groups_listed"] += 1
            keys.update(repr(key).encode("utf-8") if key is not None else b"\x00")
            if key is not None:
                old = {_key_hash(key, 0) % shards}
            elif owner in listing:   # its old ordinal depends on the listing's interleaving
                raise Refused(f"old_rule_unknown:{name}")
            else:
                old = {s for s in range(shards)
                       if _key_hash(None, before[s] + other) % shards == s}
            documents = _judge_group(load, sensitive, counts, marked, "others")
            for s in old:
                old_groups[s] += 1
                old_documents[s] += max(documents, 0)
        if owner in listing:
            before = [b + n for b, n in zip(before, listing[owner][1])]
    if counts["failed_groups"]:
        raise Refused(f"groups_unread:others:{counts['failed_groups']}")
    return {"rule": RULE, "shards": shards, "keys": keys.hexdigest(), "counts": counts,
            "sharded": sharded, "old_rule": {"groups": old_groups,
                                             "documents": old_documents},
            "marked": sorted(marked)}


def judge_others(state: str, work: Path, readings: Path | None = None,
                 tmp: Path | None = None) -> dict[str, Any]:
    """``judge-others``: :func:`judge_other_groups` into ``work/others.json`` (once)."""
    from ontology.text_index import _sensitive
    meta = json.loads((work / "judge.json").read_text(encoding="utf-8"))
    if meta["code"] != _code() or meta["state"] != state:
        raise Refused("work_of_other_judge")
    snap = str(readings_snapshot(state, readings)) if readings is not None else None
    tmp = Path(tmp) if tmp is not None else work / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = os.environ["TMPDIR"] = str(tmp.resolve())
    path = work / "others.json"
    if path.exists():
        held = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in held.items() if k not in ("marked", "old_rule", "sharded")}
    started = time.monotonic()
    with _projector(state, snap) as projector:
        result = judge_other_groups(projector, meta["shards"], _sensitive)
    result.update(code=meta["code"], seconds=round(time.monotonic() - started, 1))
    _write(path, result)
    return {k: v for k, v in result.items() if k not in ("marked", "old_rule", "sharded")}


def _code() -> str:
    from ontology import step_migrate as sm
    return sm.code_digests()["text_index"]


def judge(state: str, work: Path, shards: int, workers: int,
          tmp: Path | None = None, reverse: bool = False, first: int = 0,
          readings: Path | None = None, stride: tuple[int, int] = (0, 1)) -> dict[str, Any]:
    """Judge the shards not done on ``workers`` processes. Their SQLite and Python
    temporary files go to ``tmp`` (default ``work/tmp``), never the system temp
    directory: the workers inherit ``SQLITE_TMPDIR`` and ``TMPDIR``."""
    import multiprocessing as mp
    index, every = stride
    if every < 1 or not 0 <= index < every:
        raise Refused(f"stride_invalid:{index}/{every}")
    snap = str(readings_snapshot(state, readings)) if readings is not None else None
    tmp = Path(tmp) if tmp is not None else work / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = os.environ["TMPDIR"] = str(tmp.resolve())
    out = work / "shards"
    out.mkdir(parents=True, exist_ok=True)
    meta = work / "judge.json"
    code = _code()
    if meta.exists():
        held = json.loads(meta.read_text(encoding="utf-8"))
        if held != {"code": code, "shards": shards, "state": state}:
            raise Refused(f"work_of_other_judge:{held}")
    else:
        meta.write_text(json.dumps({"code": code, "shards": shards, "state": state}),
                        encoding="utf-8")
    todo = [s for s in range(shards) if not (out / f"shard-{s}.json").exists()]
    todo = [s for s in todo if s >= first and s % every == index]
    if reverse:
        todo.reverse()
    started = time.monotonic()
    done, held = [], 0
    with mp.get_context("spawn").Pool(min(workers, max(1, len(todo)))) as pool:
        for got in pool.starmap(judge_shard,
                                [(state, s, shards, str(out), snap) for s in todo],
                                chunksize=1):
            if got.get("held_by_other_judge"):
                held += 1
            else:
                done.append(got)
    return {"judged_now": len(done), "skipped_done": shards - len(todo),
            "held_by_other_judges": held,
            "seconds": round(time.monotonic() - started, 1)}


def handles(work: Path) -> tuple[list[str], dict[str, Any]]:
    """The flagged documents' handles of a complete, consistent judge: shard files all of
    :data:`RULE`, or all from before it with ``others.json`` (:func:`judge_others`)
    replacing what each judged of the groups of no listing."""
    meta = json.loads((work / "judge.json").read_text(encoding="utf-8"))
    if meta["code"] != _code():
        raise Refused("text_index_code_changed_since_judge")
    shards = [json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((work / "shards").glob("shard-*.json"))]
    if len(shards) != meta["shards"] or {s["shard"] for s in shards} != \
            set(range(meta["shards"])):
        raise Refused("judge_incomplete")
    if len({(s["keys"], s["counts"]["groups_listed"]) for s in shards}) != 1:
        raise Refused("group_listing_differs_between_shards")
    shards.sort(key=lambda s: s["shard"])
    rules = {s.get("rule") for s in shards}
    others_path = work / "others.json"
    groups = [s["counts"]["groups"] for s in shards]
    documents = [s["counts"]["documents"] for s in shards]
    marked_sets = [s["marked"] for s in shards]
    seconds = sum(s["seconds"] for s in shards)
    extra: dict[str, Any] = {}
    if rules == {RULE}:
        if others_path.exists():
            raise Refused("others_beside_shards_of_rule")
    elif rules == {None}:
        if not others_path.exists():
            raise Refused("shards_of_old_rule_without_others")
        others = json.loads(others_path.read_text(encoding="utf-8"))
        n = meta["shards"]
        old = others.get("old_rule") or {}
        if others.get("rule") != RULE or others.get("code") != meta["code"] \
                or others.get("shards") != n \
                or others.get("keys") != shards[0]["keys"] \
                or others["counts"]["groups_listed"] != shards[0]["counts"]["groups_listed"]:
            raise Refused("others_of_other_judge")
        if others["counts"]["failed_groups"] or any(
                not isinstance(x, list) or len(x) != n
                for x in (others.get("sharded"), old.get("groups"), old.get("documents"))):
            raise Refused("others_incomplete")
        for s in range(n):
            groups[s] -= old["groups"][s]
            documents[s] -= old["documents"][s]
            # what is left of a shard is exactly its own listed groups
            if groups[s] != others["sharded"][s] or documents[s] < 0:
                raise Refused(f"others_do_not_account_for_shard:{s}")
        groups.append(others["counts"]["groups"])
        documents.append(others["counts"]["documents"])
        marked_sets.append(others["marked"])
        seconds += others["seconds"]
        extra = {"groups_rejudged": others["counts"]["groups"],
                 "documents_rejudged": others["counts"]["documents"]}
    else:
        raise Refused("shard_rules_differ")
    marked = sorted({h for m in marked_sets for h in m})
    counts = {"groups": sum(groups),
              "groups_listed": shards[0]["counts"]["groups_listed"],
              "documents": sum(documents),
              "documents_marked": len(marked),
              "judge_cpu_seconds": round(seconds, 1), **extra}
    if counts["groups"] != counts["groups_listed"]:
        raise Refused("groups_not_covered")
    return marked, counts


def _connect(dsn: str) -> Any:
    import psycopg
    pg = psycopg.connect(dsn, autocommit=True)
    pg.execute(f"SET statement_timeout = {TIMEOUT_MS}")
    pg.execute("SET lock_timeout = '30s'")
    pg.execute("SET jit = off")
    return pg


def _fields(pg: Any, chunk: Sequence[str]) -> list[tuple[int, str]]:
    return pg.execute("SELECT field_id, markings FROM ontology.text_fields WHERE "
                      "version_handle = ANY(%s) ORDER BY field_id", (list(chunk),)).fetchall()


def _new(markings: str) -> str:
    from ontology.store import markings_text
    return markings_text(set(json.loads(markings)) | set(MARKS))


def plan_fields(pg: Any, marked: Sequence[str], batch: int = 20_000
                ) -> tuple[list[tuple[int, str, str]], int]:
    """(field id, held markings, new markings) of the flagged documents' fields that
    lack a mark, and how many fields they hold."""
    todo: list[tuple[int, str, str]] = []
    total = 0
    with pg.transaction():
        pg.execute("SET TRANSACTION READ ONLY")
        for i in range(0, len(marked), batch):
            for field_id, markings in _fields(pg, marked[i:i + batch]):
                total += 1
                new = _new(markings)
                if new != markings:
                    todo.append((field_id, markings, new))
    return todo, total


def apply(work: Path, dsn: str, *, dry_run: bool) -> dict[str, Any]:
    from ontology import step_migrate as sm
    started = time.monotonic()
    code = _code()
    if not any(code == to for pairs in sm.MARKS_ONLY.values() for _f, to in pairs):
        raise Refused(f"text_index_code_not_a_marks_only_target:{code[:12]}")
    if tuple(sm.SENSITIVE_MARKS) != MARKS:
        raise Refused("marks_differ_from_step_migration")
    marked, counts = handles(work)
    pg = _connect(dsn)
    try:
        t = time.monotonic()
        todo, total = plan_fields(pg, marked)
        report = {**counts, "fields_of_marked": total, "fields_to_mark": len(todo),
                  "read_seconds":
                  round(time.monotonic() - t, 1), "dry_run": dry_run}
        if dry_run:
            report["seconds"] = round(time.monotonic() - started, 1)
            return report
        undo = work / "undo.jsonl"
        written = 0
        with undo.open("a", encoding="utf-8") as log:
            for i in range(0, len(todo), BATCH):
                part = todo[i:i + BATCH]
                with pg.transaction():
                    pg.execute(LOCK)
                    held = dict(pg.execute(
                        "SELECT field_id, markings FROM ontology.text_fields WHERE "
                        "field_id = ANY(%s) FOR UPDATE", ([f for f, _o, _n in part],)
                    ).fetchall())
                    rows = []
                    for field_id, _old, _n in part:
                        now = held.get(field_id)
                        if now is None:
                            continue
                        new = _new(now)
                        if new != now:
                            rows.append((field_id, now, new))
                    for field_id, old, new in rows:
                        log.write(json.dumps({"field_id": field_id, "old": old,
                                              "new": new}) + "\n")
                    log.flush()
                    os.fsync(log.fileno())
                    if rows:
                        with pg.cursor() as cur:
                            cur.executemany(
                                "UPDATE ontology.text_fields SET markings = %s "
                                "WHERE field_id = %s AND markings = %s",
                                [(new, field_id, old) for field_id, old, new in rows])
                    written += len(rows)
        report.update(fields_marked=written, undo=str(undo),
                      seconds=round(time.monotonic() - started, 1))
        return report
    finally:
        pg.close()


def undo(work: Path, dsn: str) -> dict[str, Any]:
    rows = [json.loads(line) for line in
            (work / "undo.jsonl").read_text(encoding="utf-8").splitlines() if line]
    pg = _connect(dsn)
    restored = 0
    try:
        for i in range(0, len(rows), BATCH):
            part = rows[i:i + BATCH]
            with pg.transaction():
                pg.execute(LOCK)
                with pg.cursor() as cur:
                    for r in part:
                        cur.execute("UPDATE ontology.text_fields SET markings = %s WHERE "
                                    "field_id = %s AND markings = %s",
                                    (r["old"], r["field_id"], r["new"]))
                        restored += cur.rowcount
    finally:
        pg.close()
    return {"undo_rows": len(rows), "restored": restored}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.text_remark")
    sub = parser.add_subparsers(dest="command", required=True)
    j = sub.add_parser("judge")
    j.add_argument("--state-dir", required=True)
    j.add_argument("--work", required=True)
    j.add_argument("--shards", type=int, default=64)
    j.add_argument("--workers", type=int, default=16)
    j.add_argument("--tmp", help="temporary files (default WORK/tmp)")
    j.add_argument("--reverse", action="store_true",
                   help="judge the last shards first (a second judge beside a running one)")
    j.add_argument("--first", type=int, default=0, help="judge only shards from this one on")
    j.add_argument("--readings", help="a directory of kept-readings copies, read-only "
                   "(never the state directory's live files)")
    j.add_argument("--stride", default="0/1",
                   help="I/N: judge only the shards s with s %% N == I")
    o = sub.add_parser("judge-others", help="judge once the groups of no whole-table "
                       "listing, for shard files judged before the ordinal rule")
    o.add_argument("--state-dir", required=True)
    o.add_argument("--work", required=True)
    o.add_argument("--tmp", help="temporary files (default WORK/tmp)")
    o.add_argument("--readings", help="a directory of kept-readings copies, read-only")
    a = sub.add_parser("apply")
    a.add_argument("--work", required=True)
    a.add_argument("--dsn", required=True)
    a.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("undo")
    u.add_argument("--work", required=True)
    u.add_argument("--dsn", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("PYTHONHASHSEED") != "0":
        print("refused: run with PYTHONHASHSEED=0 (group keys hold frozenset reprs)",
              file=sys.stderr)
        return 2
    try:
        if args.command == "judge":
            try:
                index, every = (int(x) for x in args.stride.split("/"))
            except ValueError:
                raise Refused(f"stride_invalid:{args.stride}") from None
            report = judge(args.state_dir, Path(args.work), args.shards, args.workers,
                           Path(args.tmp) if args.tmp else None, args.reverse,
                           args.first, Path(args.readings) if args.readings else None,
                           (index, every))
        elif args.command == "judge-others":
            report = judge_others(args.state_dir, Path(args.work),
                                  Path(args.readings) if args.readings else None,
                                  Path(args.tmp) if args.tmp else None)
        elif args.command == "apply":
            report = apply(Path(args.work), args.dsn, dry_run=args.dry_run)
        else:
            report = undo(Path(args.work), args.dsn)
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
