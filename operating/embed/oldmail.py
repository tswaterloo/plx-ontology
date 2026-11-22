"""Mail missing from search, chunked into a search store off the box, unit by unit, resumable.

``python3 -m operating.embed.oldmail plan --dsn D [--unit-size 1000]`` freezes the work once:
every mail of ``biz.emails`` with no mail chunk in ``search.chunks`` (any year), newest
first, cut into units of ``--unit-size`` mails (``search.oldmail_plan``), and each unit's
ontology objects (the mail and its attachments' text fields, ``search.oldmail_plan_objects``).

``python3 -m operating.embed.oldmail chunk --dsn D --state S --workers N`` chunks the units
not yet done with the box's own chunk code (``ProjectionChunker`` over the Postgres ontology,
the row-delta layers' path: only the documents holding the unit's objects are read; the same
chunk ids, audience keys and markings) in ``N`` processes, and records each unit in
``search.oldmail_units`` as soon as its chunks are committed. A recorded unit is skipped, so
a stopped run (a kill, an OOM, a reboot under its systemd unit) loses only the units in
flight; a unit is chunked again whole (chunk writes are upserts by chunk id: nothing
doubles). The identity of the chunk code (``chunker_code``: the chunker package and the shard and
store writers, by content) is recorded with each unit; a run of other chunk code refuses
to add units to a store built by different code (``--code`` overrides it). A unit is not begun while the store's disk has less than
``--min-free-gb`` free (the run ends, exit 3, nothing recorded for it).

Why units of mail and not months: a month (the history window's ``since``/``until``) is
decided after every mail field of the store is read and dated, so each month cost a full
read; a unit reads only its own objects.

The people exclusion is read as the box reads it (``PLX_EXCLUDED_PEOPLE_OIDS`` and the
captures' directory tables under ``--state``); a process that cannot load it writes
nothing and fails the run. Prints JSON lines of counts only (never a text or an address).
Exit 0 done, 2 refused, 3 stopped at the disk floor, 1 failed.
"""
from __future__ import annotations

import argparse
import json
import os
import hashlib
import resource
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

KINDS = ("attachment", "mail")
DOCUMENT_KINDS = ("email", "attachment")
PLAN_DDL = (
    "CREATE TABLE search.oldmail_plan (handle text PRIMARY KEY, unit int NOT NULL, "
    "sent timestamptz)",
    "CREATE TABLE search.oldmail_plan_objects (unit int NOT NULL, object_id text NOT NULL, "
    "PRIMARY KEY (unit, object_id))",
)
UNITS_DDL = ("CREATE TABLE IF NOT EXISTS search.oldmail_units (unit int PRIMARY KEY, mails int "
             "NOT NULL, objects int NOT NULL, documents bigint NOT NULL, chunks bigint NOT NULL, "
             "inserted bigint NOT NULL, seconds real NOT NULL, code text NOT NULL, "
             "skipped jsonb NOT NULL DEFAULT '{}', done_at timestamptz NOT NULL DEFAULT now())")
# The store's own index (not the box's): the layer path reads text fields by version handle.
STORE_INDEX = ("CREATE INDEX IF NOT EXISTS oldmail_text_fields_by_version "
               "ON ontology.text_fields (version_handle)")


CHUNKER_FILES = ("operating/chunking/**/*.py", "operating/chunking/**/*.json",
                 "operating/embed/shards.py", "operating/embed/store.py")


def chunker_code(root: Path) -> str:
    """The identity of the code that makes and writes the chunks (the chunker package and
    the shard/store writers, by content): a change to this runner alone keeps the units
    already done; a change to the chunks' code refuses to mix them."""
    digest = hashlib.sha256()
    for path in sorted({p for pattern in CHUNKER_FILES for p in root.glob(pattern)
                        if "tests" not in p.relative_to(root).parts}):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return "chunker:" + digest.hexdigest()[:16]


def _exists(conn: Any, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]


def plan(dsn: str, unit_size: int) -> dict[str, Any]:
    """The frozen work list (refused when one exists: a resumed run must see the same
    units)."""
    import psycopg
    if unit_size < 1:
        raise ValueError("unit_size_below_1")
    started = time.monotonic()
    with psycopg.connect(dsn) as conn:
        if _exists(conn, "search.oldmail_plan"):
            return {"refused": "plan_exists"}
        for ddl in PLAN_DDL:
            conn.execute(ddl)
        conn.execute(
            "INSERT INTO search.oldmail_plan (handle, unit, sent) "
            "SELECT handle, ((row_number() OVER (ORDER BY sent DESC NULLS LAST, handle)) - 1) "
            "/ %s, sent FROM (SELECT DISTINCT ON (e.handle) e.handle, "
            "coalesce(e.sent_at, e.received_at) AS sent FROM biz.emails e "
            "WHERE e.handle IS NOT NULL AND NOT EXISTS (SELECT 1 FROM search.chunks c "
            "WHERE c.source_handle = e.handle AND c.kind = 'mail') "
            "ORDER BY e.handle, coalesce(e.sent_at, e.received_at) DESC NULLS LAST) m",
            (unit_size,))
        conn.execute(
            "INSERT INTO search.oldmail_plan_objects (unit, object_id) "
            "SELECT DISTINCT p.unit, f.object_id FROM ontology.text_fields f "
            "JOIN search.oldmail_plan p ON p.handle = split_part(f.version_handle, '#', 1) "
            "WHERE f.document_kind = ANY(%s)", (list(DOCUMENT_KINDS),))
        conn.execute(STORE_INDEX)
        mails, units = conn.execute(
            "SELECT count(*), count(DISTINCT unit) FROM search.oldmail_plan").fetchone()
        objects, = conn.execute("SELECT count(*) FROM search.oldmail_plan_objects").fetchone()
        empty, = conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT unit FROM search.oldmail_plan) u WHERE "
            "NOT EXISTS (SELECT 1 FROM search.oldmail_plan_objects o WHERE o.unit = u.unit)"
        ).fetchone()
    return {"planned": {"mails": mails, "units": units, "objects": objects,
                        "units_without_objects": empty,
                        "seconds": round(time.monotonic() - started, 1)}}


def chunk_unit(task: tuple[dict[str, Any], int]) -> dict[str, Any]:
    """One unit chunked and written over this process's own connection; counts only."""
    from operating.chunking.permissions import unit_row
    from operating.chunking.pg_source import PG_PROJECTION
    from operating.chunking.projection import Counts
    from . import store as st
    from .shards import PERSON_KINDS, _exclusion, _Tally, produce
    job, unit = task
    started = time.monotonic()
    if shutil.disk_usage(job["floor_path"]).free / 1e9 < job["min_free_gb"]:
        return {"unit": unit, "stopped": "disk_below_min_free"}
    exclusion = _exclusion(Path(job["state"]), dict(job["values"]))
    if exclusion.unavailable and PERSON_KINDS & set(KINDS):
        raise RuntimeError(f"people_exclusion_unavailable:{exclusion.unavailable}")
    conn = st.connect(job["dsn"])
    try:
        mails, = conn.execute("SELECT count(*) FROM search.oldmail_plan WHERE unit = %s",
                              (unit,)).fetchone()
        objects = frozenset(r[0] for r in conn.execute(
            "SELECT object_id FROM search.oldmail_plan_objects WHERE unit = %s", (unit,)))
        conn.commit()
        tally = _Tally()
        tally.counts = Counts()
        if objects:
            writer = st.ChunkWriter(conn, unit_of=lambda key: unit_row(key, exclusion))
            tally.take(produce(Path(job["state"]), PG_PROJECTION, exclusion, tally.counts,
                               list(KINDS), None, objects=objects, dsn=job["dsn"]), writer)
    finally:
        conn.close()
    return {"unit": unit, "mails": mails, "objects": len(objects),
            "documents": sum(tally.counts.documents.values()), "chunks": len(tally.seen),
            "inserted": tally.written[0], "skipped": dict(tally.counts.skipped),
            "seconds": round(time.monotonic() - started, 1),
            "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)}


def run(dsn: str, state: str, workers: int, code: str, min_free_gb: float,
        max_units: int | None = None, floor_path: str | None = None) -> int:
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    import multiprocessing as mp
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        if not _exists(conn, "search.oldmail_plan"):
            print(json.dumps({"refused": "no_plan"}), flush=True)
            return 2
        conn.execute(UNITS_DDL)
        other = conn.execute("SELECT DISTINCT code FROM search.oldmail_units WHERE code <> %s",
                             (code,)).fetchall()
        if other:
            print(json.dumps({"refused": "store_built_by_other_code",
                              "codes": [r[0] for r in other]}), flush=True)
            return 2
        pending = [r[0] for r in conn.execute(
            "SELECT DISTINCT unit FROM search.oldmail_plan p WHERE NOT EXISTS (SELECT 1 FROM "
            "search.oldmail_units u WHERE u.unit = p.unit) ORDER BY unit")]
    if max_units is not None:
        pending = pending[:max_units]
    print(json.dumps({"pending_units": len(pending), "workers": workers}), flush=True)
    job = {"dsn": dsn, "state": state, "min_free_gb": min_free_gb,
           "floor_path": floor_path or state,
           "values": {"PLX_EXCLUDED_PEOPLE_OIDS": os.environ.get("PLX_EXCLUDED_PEOPLE_OIDS", ""),
                      "PLX_PG_DSN": dsn}}
    todo = list(reversed(pending))
    stopped = None
    from ontology import parent_watch   # a worker ends with this process (a killed run)
    with ProcessPoolExecutor(max(1, workers), mp_context=mp.get_context("spawn"),
                             **parent_watch.pool_init()) as pool, \
            psycopg.connect(dsn, autocommit=True) as conn:
        running = set()
        while todo or running:
            while todo and not stopped and len(running) < max(1, workers):
                running.add(pool.submit(chunk_unit, (job, todo.pop())))
            if not running:
                break
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                got = fut.result()   # a failed unit fails the run (systemd restarts it)
                print(json.dumps(got), flush=True)
                if got.get("stopped"):
                    stopped = got["stopped"]
                    continue
                conn.execute(
                    "INSERT INTO search.oldmail_units (unit, mails, objects, documents, chunks, "
                    "inserted, seconds, code, skipped) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s) ON CONFLICT (unit) DO NOTHING",
                    (got["unit"], got["mails"], got["objects"], got["documents"], got["chunks"],
                     got["inserted"], got["seconds"], code, json.dumps(got["skipped"])))
    if stopped:
        print(json.dumps({"stopped": stopped}), flush=True)
        return 3
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="oldmail", description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("plan", "chunk"))
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--state")
    ap.add_argument("--unit-size", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--max-units", type=int, help="stop after this many units (pilots)")
    ap.add_argument("--min-free-gb", type=float, default=300.0)
    ap.add_argument("--floor-path", help="the disk to watch (default: --state)")
    ap.add_argument("--code")
    args = ap.parse_args(argv)
    if args.command == "plan":
        got = plan(args.dsn, args.unit_size)
        print(json.dumps(got), flush=True)
        return 2 if "refused" in got else 0
    if not args.state:
        ap.error("chunk needs --state")
    code = args.code or chunker_code(Path(__file__).resolve().parents[2])
    return run(args.dsn, args.state, args.workers, code, args.min_free_gb, args.max_units,
               args.floor_path)


if __name__ == "__main__":
    sys.exit(main())
