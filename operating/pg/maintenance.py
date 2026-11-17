"""Postgres maintenance as chunked jobs (``operating.pg.chunked``): one slice per table or
block range, each checkpointed, resumable, stopped cleanly by the disk floor or a window
lock instead of being killed mid-way.

Replaces the ad-hoc scripts of 11-13/14 (DI-30 ``ops-1113d/repack.sh``, the post-F3
``VACUUM`` chain, ``f7_drop.py``, the GA-F13 rehearsal copy that ran as one 25-minute COPY):

    python3 -m operating.pg.chunked run operating.pg.maintenance:vacuum \\
        --opt tables=ontology.link_rows,ontology.value_cells [--opt run=1114]
    python3 -m operating.pg.chunked run operating.pg.maintenance:repack \\
        --disk-floor-gb 30 --disk-path /data [--opt tables=...] [--opt jobs=2]
    python3 -m operating.pg.chunked run operating.pg.maintenance:drop_indexes \\
        --opt indexes=ontology.link_rows_by_id
    python3 -m operating.pg.chunked run operating.pg.maintenance:copy_sample \\
        --dsn <scratch db> --opt source_dsn=<served> --opt table=ontology.value_cells \\
        --opt fraction=0.1 [--opt pages=2000]

``run`` names the pass (default: today's date, ET): a new name is a new pass over every
table; the same name resumes. A repack's slice declares the table's whole size as its
``disk_gb`` (pg_repack writes a full copy before it drops the old one), so with
``--disk-floor-gb 30`` a table starts only when free space stays above its size + 30 GB;
the lightest go first, so space they free lets the bigger ones in.
"""
from __future__ import annotations

from datetime import datetime
import os
import subprocess
from typing import Any
from zoneinfo import ZoneInfo

from operating.pg.chunked import Job, Slice, block_ranges, ctid_where

REPACK_TABLES = ("ontology.event_objects", "ontology.value_sources", "ontology.events",
                 "ontology.text_fields", "ontology.objects", "ontology.identifiers",
                 "ontology.object_versions")
VACUUM_TABLES = ("ontology.link_rows", "ontology.value_cells", "ontology.value_sources",
                 "ontology.object_versions", "ontology.identifiers", "ontology.events",
                 "ontology.event_objects")


def _run_name(run: str | None) -> str:
    return run or datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _names(text: str | None, default: tuple[str, ...]) -> list[str]:
    names = [t.strip() for t in (text or "").split(",") if t.strip()] or list(default)
    for name in names:   # identifiers only: they are written into statements
        if not all(part.replace("_", "").isalnum() for part in name.split(".")):
            raise ValueError(f"maintenance_bad_name: {name!r}")
    return names


def _sizes(conn: Any, tables: list[str]) -> dict[str, int]:
    out = {}
    for table in tables:
        oid = conn.execute("SELECT to_regclass(%s)::oid", (table,)).fetchone()[0]
        if oid is None:   # gone (or never made here): nothing to do for it
            continue
        out[table] = int(conn.execute("SELECT pg_total_relation_size(%s::oid)",
                                      (oid,)).fetchone()[0])
    return out


def vacuum(conn: Any, tables: str | None = None, run: str | None = None,
           analyze: str = "1") -> Job:
    """``VACUUM (ANALYZE)`` per table, one non-transactional slice each (a VACUUM is one
    statement that cannot be cut: no statement budget, it blocks no reader or writer)."""
    sizes = _sizes(conn, _names(tables, VACUUM_TABLES))
    verb = "VACUUM (ANALYZE)" if analyze not in ("0", "", "false") else "VACUUM"
    return Job(name=f"vacuum-{_run_name(run)}", slices=[
        Slice(key=table, part=table, sql=((f"{verb} {table}", ()),), weight=size,
              transactional=False, statement_seconds=0)
        for table, size in sizes.items()])


def _repack_one(conn: Any, table: str, jobs: int, wait_timeout: int) -> dict:
    before = _sizes(conn, [table]).get(table)
    if before is None:
        return {"skipped": "table_gone"}
    info = conn.info
    env = {k: v for k, v in os.environ.items() if k != "PLX_WORKBENCH_API_KEY"}
    cmd = ["pg_repack", "-d", info.dbname, "-t", table, "-j", str(jobs),
           f"--wait-timeout={wait_timeout}", "--no-kill-backend"]
    if info.host:
        cmd += ["-h", info.host]
    if info.port:
        cmd += ["-p", str(info.port)]
    if info.user:
        cmd += ["-U", info.user]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        # the error class only in the checkpointless failure (its text may name objects)
        raise RuntimeError(f"pg_repack_failed: rc={done.returncode} "
                           f"{(done.stderr or '').strip().splitlines()[-1:]!s:.200}")
    after = _sizes(conn, [table]).get(table, before)
    return {"bytes_before": before, "bytes_after": after, "freed_mb": (before - after) >> 20}


def repack(conn: Any, tables: str | None = None, run: str | None = None, jobs: str = "2",
           wait_timeout: str = "60") -> Job:
    """pg_repack per table, lightest first, each slice declaring the table's size as the
    disk it takes (pair with ``--disk-floor-gb``)."""
    sizes = _sizes(conn, _names(tables, REPACK_TABLES))
    return Job(name=f"repack-{_run_name(run)}", order="lightest", slices=[
        Slice(key=table, part=table, fn=_repack_one, args=(table, int(jobs),
                                                           int(wait_timeout)),
              weight=size, disk_gb=size / 1e9, transactional=False, statement_seconds=0)
        for table, size in sizes.items()])


def _drop_one(conn: Any, index: str) -> dict:
    existed = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (index,)).fetchone()[0]
    conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index}")
    return {"dropped": bool(existed)}


def drop_indexes(conn: Any, indexes: str = "", run: str | None = None) -> Job:
    """``DROP INDEX CONCURRENTLY`` per index (non-transactional; the runner's lock timeout
    and retries stand in for a hand-made retry loop: a long reader makes it wait, never
    block others for long)."""
    names = _names(indexes, ())
    if not names:
        raise ValueError("maintenance_no_indexes: --opt indexes=schema.index,...")
    return Job(name=f"drop-indexes-{_run_name(run)}", slices=[
        Slice(key=name, part="index", fn=_drop_one, args=(name,), transactional=False)
        for name in names])


def _copy_range(conn: Any, source_dsn: str, table: str, target: str, first: int,
                last: int) -> dict:
    """Blocks [first, last) of ``table`` on the source copied into ``target`` here, in
    this slice's transaction (the checkpoint commits with the rows). The source read is
    one short autocommit statement: no long snapshot on the served database."""
    import psycopg
    rows = 0
    with psycopg.connect(source_dsn, autocommit=True) as src:
        src.execute("SET statement_timeout = '300s'")
        with src.cursor().copy(f"COPY (SELECT * FROM {table} WHERE "
                               f"{ctid_where(first, last)}) TO STDOUT") as out, \
                conn.cursor().copy(f"COPY {target} FROM STDIN") as into:
            for chunk in out:
                data = bytes(chunk)
                into.write(data)
                rows += data.count(b"\n")
    return {"rows": rows}


def copy_sample(conn: Any, source_dsn: str = "", table: str = "", fraction: str = "0.1",
                pages: str = "2000", target: str | None = None,
                run: str | None = None) -> Job:
    """A spread sample (``fraction`` of the heap, every n-th range of ``pages`` blocks) of
    ``table`` on ``source_dsn`` copied into the same-named table of the database the job
    runs on (it must exist with the same columns). Run on the scratch database; the served
    one is only read, in short range reads."""
    import psycopg
    if not source_dsn or not table:
        raise ValueError("maintenance_copy_needs: --opt source_dsn=... --opt table=...")
    (name,) = _names(table, ())
    (into,) = _names(target or name, ())
    share = float(fraction)
    if not 0 < share <= 1:
        raise ValueError("maintenance_fraction: in (0, 1]")
    with psycopg.connect(source_dsn, autocommit=True) as src:
        ranges = block_ranges(src, name, int(pages))
    every = max(1, round(1 / share))
    step = max(1, int(pages))
    # closed ranges of ``pages`` blocks (a sample needs no rows added since): a rerun after
    # the source grew plans the same ranges for the same blocks, so none is copied twice
    picked = [(a, a + step) for a, _b in ranges[::every]]
    return Job(name=f"copy-{name}-{_run_name(run)}", slices=[
        Slice(key=f"{a}", part=name, fn=_copy_range, args=(source_dsn, name, into, a, b),
              weight=b - a)
        for a, b in picked])
