"""Rehearse ``operating.pg.link_partitions`` at a share of the served table (default 10 %).

``python3 scripts/rehearse_link_partitions.py [--share 0.10] [--workers 8] [--out F]``

Everything is written in the schema ``PLX_LINK_PARTITIONS_SCHEMA`` (default
``di03_rehearsal``, never ``ontology``), dropped at the end. The served
``ontology.link_rows`` is only read: its per-type counts (the real layout) and
``share`` of its blocks in evenly spread ranges (real rows, a real spread of types, the
real bloat on disk). Phases, each timed:

* **A, the conversion's expensive path**: the partitioned table built from those block
  ranges exactly as ``convert`` builds it (``build_target``: routed fill in parallel
  block ranges, every index per partition side by side, keys, parent indexes,
  statistics), checked row for row against its source (count and an order-free hash of
  every column per range); sizes per partition.
* **B, rebuilds on that table**: the largest type (its own partition) and a small type
  (its bucket's partition) rebuilt from a changed copy of their rows, the small one while
  a writer changes a kept type of its bucket (caught up in the swap); a writer of the
  replaced type then fails a rebuild closed; the delta's upsert shape with and without the
  one-row-per-link-id delete and the undo trigger; lookups.
* **C, the whole conversion and its undo** on an unpartitioned copy of a smaller share
  (``--c-share``, default 1 %) with the served indexes, a writer upserting meanwhile:
  ``convert`` (trigger, fill, catch-up, rename), checked against the copy, then ``undo``,
  checked again.

Rates measured in A and B project the full conversion (rows from ``pg_class``; the heap
read whole) and the disk it needs. Prints and writes JSON. Statements yield to a delta
apply (the delta's advisory lock); the script stops when the rehearsal schema passes
``--max-gb``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PLX_LINK_PARTITIONS_SCHEMA", "di03_rehearsal")
if os.environ["PLX_LINK_PARTITIONS_SCHEMA"] == "ontology":
    raise SystemExit("a rehearsal never writes the ontology schema")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operating.pg import config  # noqa: E402
from operating.pg import link_partitions as lp  # noqa: E402

R = lp.SCHEMA
COLS = ", ".join(lp.COLUMNS)
SERVED = "ontology.link_rows"
HASH = f"count(*), coalesce(sum(hashtextextended(({COLS})::text, 0)::numeric), 0)"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def schema_bytes(cur) -> int:
    cur.execute("SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
                "WHERE c.relnamespace = %s::regnamespace AND c.relkind IN ('r', 'm')", (R,))
    return int(cur.fetchone()[0])


def spread(pages: int, share: float, parts: int) -> list[tuple[int, int]]:
    """``parts`` block ranges, ``share`` of ``pages`` in all, evenly spread."""
    width = max(1, int(pages * share / parts))
    stride = pages // parts
    return [(i * stride, i * stride + width) for i in range(parts)]


def template(cur, view: bool) -> None:
    """``R.link_rows``: the served columns, defaults and checks, row numbers from a
    sequence of the rehearsal (never the served one); ``R.links`` over it."""
    cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {R}.link_rows_row_no START 9000000000000")
    cur.execute(f"CREATE TABLE {R}.link_rows (LIKE {SERVED} INCLUDING DEFAULTS "
                "INCLUDING CONSTRAINTS)")
    cur.execute(f"ALTER TABLE {R}.link_rows ALTER COLUMN row_no SET DEFAULT "
                f"nextval('{R}.link_rows_row_no')")
    if view:
        cur.execute("SELECT pg_get_viewdef('ontology.links'::regclass, true)")
        text = cur.fetchone()[0].replace("ontology.link_rows", f"{R}.link_rows")
        cur.execute(f"CREATE VIEW {R}.links AS {text}")


def range_hashes(s: lp.Session, table: str, ranges) -> dict:
    sqls = [f"SELECT {HASH} FROM {table} WHERE {lp._ctid(lo, hi)}" for lo, hi in ranges]

    def one(sql):
        with s.connect() as c:
            return c.execute(sql).fetchone()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(s.workers) as pool:
        got = list(pool.map(one, sqls))
    return {"rows": sum(r[0] for r in got), "hash": str(sum(r[1] for r in got))}


def table_hash(cur, table: str, where: str = "TRUE") -> dict:
    cur.execute(f"SELECT {HASH} FROM {table} WHERE {where}")
    n, h = cur.fetchone()
    return {"rows": n, "hash": str(h)}


class Writer(threading.Thread):
    """A delta-like writer: every ``every`` s, ``n`` rows of ``link_type`` updated in
    place (``tier = tier``) through the parent, until stopped. Counts what it wrote."""

    def __init__(self, dsn: str, table: str, link_type: str, n: int = 50, every: float = 0.5,
                 insert_prefix: str | None = None):
        super().__init__(daemon=True)
        self.dsn, self.table, self.type, self.n, self.every = dsn, table, link_type, n, every
        self.insert_prefix = insert_prefix
        self.stop, self.rows, self.errors = threading.Event(), 0, []

    def run(self) -> None:
        import psycopg
        k = 0
        try:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("SET lock_timeout = 30000")
                while not self.stop.is_set():
                    cur = c.execute(f"UPDATE {self.table} SET tier = tier WHERE link_type = %s "
                                    f"AND link_id IN (SELECT link_id FROM {self.table} WHERE "
                                    "link_type = %s OFFSET %s LIMIT %s)",
                                    (self.type, self.type, (k * self.n) % 5000, self.n))
                    self.rows += cur.rowcount
                    if self.insert_prefix:
                        c.execute(f"INSERT INTO {self.table} ({COLS}) SELECT %s || link_id, "
                                  f"{', '.join(lp.COLUMNS[1:-1])}, NULL FROM {self.table} "
                                  "WHERE link_type = %s LIMIT 5",
                                  (f"{self.insert_prefix}{k}:", self.type))
                        self.rows += 5
                    k += 1
                    time.sleep(self.every)
        except Exception as exc:   # reported, never hidden
            self.errors.append(repr(exc))


def wait_delta(s: lp.Session) -> float:
    t = time.monotonic()
    with s.connect() as c:
        s.wait_for_delta(c)
    return time.monotonic() - t


# --- phase A ----------------------------------------------------------------------------

def phase_a(s: lp.Session, a, report: dict) -> dict:
    with s.connect() as c, c.cursor() as cur:
        t = time.monotonic()
        counts = lp.type_counts(cur, SERVED)
        report["count_types_s"] = round(time.monotonic() - t, 1)
        pages = lp._pages(cur, SERVED)
        cur.execute(f"SELECT reltuples::bigint FROM pg_class WHERE oid = '{SERVED}'::regclass")
        served_rows = cur.fetchone()[0]
        options = lp._reloptions(cur, SERVED)
        template(cur, view=False)
    ranges = spread(pages, a.share, s.workers * 32)
    layout = lp.groups(counts)
    report["served"] = {"rows": sum(counts.values()), "reltuples": served_rows,
                        "pages": pages, "types": len(counts),
                        "partitions": len(layout) + 1}
    log(f"A: {len(ranges)} ranges, {sum(h - l for l, h in ranges)} of {pages} blocks")
    s.timings.clear()
    t = time.monotonic()
    parts = lp.build_target(s, "link_rows_part", counts, source=SERVED, ranges=ranges,
                            options=options)
    wall = time.monotonic() - t
    with s.connect() as c, c.cursor() as cur:
        rows = table_hash(cur, f"{R}.link_rows_part")
        per = []
        for p in parts:
            cur.execute("SELECT pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass), "
                        "(SELECT count(*) FROM " f"{R}.{p.name})",
                        (f"{R}.{p.name}", f"{R}.{p.name}"))
            heap, idx, n = cur.fetchone()
            per.append({"name": p.name, "types": len(p.types or ()), "rows": n,
                        "heap": heap, "index": idx,
                        "top": (p.types or ("DEFAULT",))[0] if len(p.types or ()) <= 1 else None})
        total_bytes = schema_bytes(cur)
    t = time.monotonic()
    src = range_hashes(s, SERVED, ranges)
    check_s = round(time.monotonic() - t, 1)
    heap = sum(p["heap"] for p in per)
    idx = sum(p["index"] for p in per)
    n = rows["rows"]
    out = {"ranges": len(ranges), "blocks_read": sum(h - l for l, h in ranges),
           "bytes_read": sum(h - l for l, h in ranges) * 8192,
           "rows": n, "wall_s": round(wall, 1), "timings": dict(s.timings),
           "yielded_to_delta_s": round(s.yielded_s, 1),
           "heap_bytes": heap, "index_bytes": idx, "schema_bytes": total_bytes,
           "bytes_per_row": round(heap / max(n, 1), 1),
           "index_bytes_per_row": round(idx / max(n, 1), 1),
           "fill_rows_s": round(n / s.timings["fill"], 1),
           "fill_rows_s_per_worker": round(n / s.timings["fill"] / s.workers, 1),
           "index_rows_s": round(n / s.timings["index"], 1),
           "partitions": per,
           "equal_to_source": rows == src, "source": src, "target": rows,
           "source_check_s": check_s}
    if rows != src:
        # the delta may have written the source ranges meanwhile: by how much
        out["note"] = "source changed or rows differ; see counts"
    return out


# --- phase B ----------------------------------------------------------------------------

def make_served(s: lp.Session) -> None:
    """The rehearsal's partitioned table becomes its ``link_rows`` (the template goes)."""
    with s.connect() as c, c.cursor() as cur:
        cur.execute(f"DROP TABLE {R}.link_rows")
        cur.execute(f"ALTER TABLE {R}.link_rows_part RENAME TO link_rows")
        lp._rename_indexes(cur, "link_rows", "link_rows_part", "link_rows")


def phase_b(s: lp.Session, a, report: dict) -> dict:
    out: dict = {}
    make_served(s)
    with s.connect() as c, c.cursor() as cur:
        # room for a rebuild under the disk cap: the largest bucket partitions dropped
        # (a rebuild reads and writes only its own partitions; the others are probed by
        # their keys alone), recorded
        dropped = []
        while schema_bytes(cur) > (a.max_gb - 7) * (1 << 30):
            victim = max((p for p in lp.layout(cur) if p.bucket is not None),
                         key=lambda p: p.pages)
            cur.execute(f"ALTER TABLE {R}.link_rows DETACH PARTITION {R}.{victim.name}")
            cur.execute(f"DROP TABLE {R}.{victim.name}")
            dropped.append(victim.name)
        out["dropped_for_room"] = dropped
        cur.execute(f"SELECT count(*) FROM {R}.link_rows")
        out["rows"] = cur.fetchone()[0]
        out["schema_bytes_before"] = schema_bytes(cur)
        parts = lp.layout(cur)
        own = max((p for p in parts if p.types and len(p.types) == 1), key=lambda p: p.rows)
        bucket = max((p for p in parts if p.bucket is not None), key=lambda p: p.rows)
        cur.execute(f"SELECT link_type, count(*) FROM {R}.{bucket.name} GROUP BY 1 ORDER BY 2")
        by = cur.fetchall()
        small = by[len(by) // 2][0]       # a median type of the bucket
        kept = by[-1][0] if by[-1][0] != small else by[-2][0]
    jobs = [("largest_own_type", own.types[0], None),
            ("small_bucketed_type", small, kept)]
    for key, link_type, writer_type in jobs:
        wait_delta(s)
        with s.connect() as c, c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS public.di03_step_out")
            cur.execute(f"CREATE UNLOGGED TABLE {R}.step_out AS SELECT * FROM {R}.link_rows "
                        "WHERE link_type = %s", (link_type,))
            # a step's changed output: a tenth of the rows re-tiered, one in 50 gone
            cur.execute(f"UPDATE {R}.step_out SET tier = 'contradicted' WHERE "
                        "hashtext(link_id) % 10 = 0")
            cur.execute(f"DELETE FROM {R}.step_out WHERE hashtext(link_id) % 50 = 1")
            want = table_hash(cur, f"{R}.step_out")
            others = table_hash(cur, f"{R}.link_rows", f"link_type <> {lp.lit(link_type)}")
            before = dict(cur.execute(
                "SELECT c.relname, c.relfilenode FROM pg_inherits i JOIN pg_class c ON "
                "c.oid = i.inhrelid WHERE i.inhparent = %s::regclass",
                (f"{R}.link_rows",)).fetchall())
            planned = lp.plan(cur, lp.Rebuild((link_type,), f"{R}.step_out"),
                              workers=s.workers)
            need = schema_bytes(cur) + sum(
                p.pages * 8192 * 1.4 for p in parts if p.name in planned["partitions"])
        if need > a.max_gb * (1 << 30):
            with s.connect() as c:
                c.execute(f"DROP TABLE {R}.step_out")
            raise SystemExit(f"B {key}: would pass {a.max_gb} GB; nothing skipped quietly")
        w = None
        if writer_type:
            w = Writer(s.dsn, f"{R}.link_rows", writer_type)
            w.start()
        s.timings.clear()
        t = time.monotonic()
        try:
            res = lp.rebuild(s, lp.Rebuild((link_type,), f"{R}.step_out"))
        finally:
            if w:
                w.stop.set()
                w.join()
        wall = time.monotonic() - t
        with s.connect() as c, c.cursor() as cur:
            got = table_hash(cur, f"{R}.link_rows", f"link_type = {lp.lit(link_type)}")
            after = dict(cur.execute(
                "SELECT c.relname, c.relfilenode FROM pg_inherits i JOIN pg_class c ON "
                "c.oid = i.inhrelid WHERE i.inhparent = %s::regclass",
                (f"{R}.link_rows",)).fetchall())
            others_after = (table_hash(cur, f"{R}.link_rows", f"link_type <> {lp.lit(link_type)}")
                            if not writer_type else None)
            cur.execute(f"DROP TABLE {R}.step_out")
        out[key] = {"type_rows": want["rows"], "rows_written": planned["rows"],
                    "planned": {k: planned[k] for k in ("total_s", "bytes", "partitions")},
                    "wall_s": round(wall, 1), "timings": res["timings"],
                    "swap_locked_s": res.get("swap_locked_s"),
                    "swap_attempts": res.get("swap_attempts"),
                    "caught_up": res.get("caught_up"),
                    "writer_rows": w.rows if w else 0, "writer_errors": w.errors if w else [],
                    "type_equal_step_output": got == want,
                    "others_unchanged": (others_after == others) if others_after else
                    "written by the writer meanwhile",
                    "untouched_partitions": sum(1 for n, f in before.items()
                                                if after.get(n) == f),
                    "partitions": len(after)}
        log(f"B {key}: {out[key]['wall_s']} s")
    # a writer of the replaced type fails the rebuild closed
    wait_delta(s)
    with s.connect() as c, c.cursor() as cur:
        cur.execute(f"CREATE UNLOGGED TABLE {R}.step_out AS SELECT * FROM {R}.link_rows "
                    "WHERE link_type = %s", (small,))
        before = table_hash(cur, f"{R}.link_rows", f"link_type <> {lp.lit(small)}")
    w = Writer(s.dsn, f"{R}.link_rows", small, every=0.2)
    w.start()
    try:
        lp.rebuild(s, lp.Rebuild((small,), f"{R}.step_out"))
        out["replaced_type_written"] = "NOT REFUSED"
    except lp.Changed as exc:
        out["replaced_type_written"] = f"refused: {exc}"
    finally:
        w.stop.set()
        w.join()
    with s.connect() as c, c.cursor() as cur:
        out["refused_left_others"] = table_hash(
            cur, f"{R}.link_rows", f"link_type <> {lp.lit(small)}") == before
        cur.execute(f"DROP TABLE {R}.step_out")
    out["delta_upsert"] = upsert_shapes(s)
    out["lookups"] = lookups(s)
    with s.connect() as c, c.cursor() as cur:
        t = time.monotonic()
        out["audit"] = lp.audit(cur)
        out["audit_s"] = round(time.monotonic() - t, 1)
        out["schema_bytes"] = schema_bytes(cur)
    return out


def upsert_shapes(s: lp.Session, n: int = 20_000) -> dict:
    """The delta's link upsert (``ontology_delta._copy``) of ``n`` served rows, timed:
    the one-row-per-id delete plus the upsert; the same under the undo trigger."""
    out = {}
    with s.connect(autocommit=False) as c, c.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE stage AS SELECT {COLS} FROM {R}.link_rows "
                    f"TABLESAMPLE SYSTEM (1) LIMIT {n}")
        sets = ", ".join(f"{x} = excluded.{x}" for x in lp.COLUMNS if x not in lp.PKEY)
        for key, trig in (("plain", False), ("under_undo_trigger", True)):
            if trig:
                lp._log_table(cur)
                cur.execute(f"INSERT INTO {R}.{lp.ACTIVE} VALUES ('undo') ON CONFLICT DO NOTHING")
                lp._watch(cur, "link_rows", "undo")
            t = time.monotonic()
            cur.execute(f"DELETE FROM {R}.link_rows t USING (SELECT DISTINCT ON (link_id) "
                        "link_id, link_type FROM stage ORDER BY link_id) s WHERE t.link_id = "
                        "s.link_id AND t.link_type IS DISTINCT FROM s.link_type")
            d = time.monotonic() - t
            cur.execute(f"INSERT INTO {R}.link_rows ({COLS}) SELECT DISTINCT ON (link_id) {COLS} "
                        f"FROM stage ORDER BY link_id ON CONFLICT (link_id, link_type) DO UPDATE "
                        f"SET {sets}")
            out[key] = {"rows": n, "identity_delete_s": round(d, 3),
                        "upsert_s": round(time.monotonic() - t - d, 3)}
        c.rollback()
    return out


def lookups(s: lp.Session, k: int = 200) -> dict:
    """Warm per-lookup milliseconds on the partitioned table (second run)."""
    out = {}
    with s.connect() as c, c.cursor() as cur:
        cur.execute(f"SELECT from_object, to_object, link_id, link_type FROM {R}.link_rows "
                    f"TABLESAMPLE SYSTEM (0.1) LIMIT {k}")
        sample = cur.fetchall()
        shapes = {
            "from_object": (f"SELECT * FROM {R}.link_rows WHERE from_object = %s", 0),
            "to_object": (f"SELECT * FROM {R}.link_rows WHERE to_object = %s", 1),
            "link_id": (f"SELECT * FROM {R}.link_rows WHERE link_id = %s", 2),
            "from_typed": (f"SELECT * FROM {R}.link_rows WHERE from_object = %s AND "
                           "link_type = %s", (0, 3)),
        }
        for name, (sql, col) in shapes.items():
            for rnd in range(2):
                t = time.monotonic()
                for row in sample:
                    args = (row[col],) if isinstance(col, int) else tuple(row[i] for i in col)
                    cur.execute(sql, args)
                    cur.fetchall()
                ms = (time.monotonic() - t) * 1000 / len(sample)
            out[name + "_ms"] = round(ms, 3)
    return out


# --- phase C ----------------------------------------------------------------------------

SERVED_INDEXES = ("CREATE UNIQUE INDEX link_rows_pkey ON {t} (link_id)",
                  "CREATE INDEX link_rows_by_row_no ON {t} (row_no)",
                  "CREATE INDEX link_rows_by_type ON {t} (link_type, tier)",
                  "CREATE INDEX link_rows_from ON {t} (from_object, link_type)",
                  "CREATE INDEX link_rows_to ON {t} (to_object, link_type)",
                  'CREATE INDEX link_rows_lead_from ON {t} (from_object, link_id COLLATE "C") '
                  "WHERE tier = 'lead' AND superseded_at IS NULL",
                  "CREATE INDEX link_rows_contra_from ON {t} (from_object) INCLUDE "
                  "(to_object, link_type) WHERE tier = 'contradicted'",
                  "CREATE INDEX link_rows_odd_slots ON {t} USING gin ((slots[1:ek])) "
                  "WHERE evidence_id IS NOT NULL")


def phase_c(s: lp.Session, a, report: dict) -> dict:
    with s.connect() as c, c.cursor() as cur:
        for t in ("link_rows", "link_rows_part", "link_rows_unpartitioned"):
            cur.execute(f"DROP TABLE IF EXISTS {R}.{t} CASCADE")
        cur.execute(f"DROP VIEW IF EXISTS {R}.links")
        template(cur, view=True)
        pages = lp._pages(cur, SERVED)
    ranges = spread(pages, a.c_share, s.workers * 4)
    s.run(lp.fill_sqls(f"{R}.link_rows", SERVED, None, 0, ranges=ranges))
    s.run([x.format(t=f"{R}.link_rows") for x in SERVED_INDEXES])
    with s.connect() as c, c.cursor() as cur:
        cur.execute(f"ANALYZE {R}.link_rows")
        before = table_hash(cur, f"{R}.link_rows")
        cur.execute(f"SELECT link_type FROM {R}.link_rows GROUP BY 1 ORDER BY count(*) DESC "
                    "LIMIT 1")
        busy = cur.fetchone()[0]
    # the delta writes meanwhile: updates in place and new rows of the busiest type
    w = Writer(s.dsn, f"{R}.link_rows", busy, every=0.2, insert_prefix="W")
    w.start()
    s.timings.clear()
    t = time.monotonic()
    try:
        res = lp.convert(s)
    finally:
        w.stop.set()
        w.join()
    wall = time.monotonic() - t
    with s.connect() as c, c.cursor() as cur:
        kind = cur.execute(f"SELECT relkind FROM pg_class WHERE oid = '{R}.link_rows'::regclass"
                           ).fetchone()[0]
        # equal at the rename: every row but those of ids written after it (the undo log)
        later = (f"link_id NOT IN (SELECT link_id FROM {R}.{lp.LOG} WHERE token = "
                 f"{lp.lit(lp.UNDO)})")
        new = table_hash(cur, f"{R}.link_rows", later)
        old = table_hash(cur, f"{R}.{lp.OLD}", later)
        cur.execute(f"SELECT count(DISTINCT link_id) FROM {R}.{lp.LOG} WHERE token = "
                    f"{lp.lit(lp.UNDO)}")
        logged_after = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {R}.links")
        view_rows = cur.fetchone()[0]
        # a write after the rename, kept by undo
        cur.execute(f"UPDATE {R}.link_rows SET tier = 'contradicted' WHERE link_id IN "
                    f"(SELECT link_id FROM {R}.link_rows WHERE link_type = %s LIMIT 7)", (busy,))
        after_write = table_hash(cur, f"{R}.link_rows")
    out = {"rows_before": before["rows"], "wall_s": round(wall, 1), "convert": res,
           "writer_rows": w.rows, "writer_errors": w.errors, "relkind": kind,
           "new_equals_old_at_rename": new == old, "ids_written_after_rename": logged_after,
           "view_rows": view_rows,
           "rows_after": new["rows"]}
    t = time.monotonic()
    u = lp.undo(s)
    with s.connect() as c, c.cursor() as cur:
        kind = cur.execute(f"SELECT relkind FROM pg_class WHERE oid = '{R}.link_rows'::regclass"
                           ).fetchone()[0]
        out["undo"] = {**u, "s": round(time.monotonic() - t, 2), "relkind": kind,
                       "keeps_writes_after_rename": table_hash(cur, f"{R}.link_rows") ==
                       after_write}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--share", type=float, default=0.10)
    ap.add_argument("--c-share", type=float, default=0.01)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-gb", type=float, default=20.0)
    ap.add_argument("--phases", default="ABC")
    ap.add_argument("--keep", action="store_true", help="keep the schema (debugging)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    s = lp.Session(config.dsn(), workers=a.workers, log=log)
    report: dict = {"schema": R, "share": a.share, "workers": a.workers,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with s.connect() as c, c.cursor() as cur:
        report["free_bytes_before"] = lp.free_bytes(cur)
        # what the gate says of converting the served table now (read-only)
        saved, lp.SCHEMA = lp.SCHEMA, "ontology"
        try:
            report["convert_plan"] = lp.convert_plan(cur, workers=s.workers)
        except (lp.OverBudget, lp.Refused) as exc:
            report["convert_plan"] = {"refused": type(exc).__name__, "why": str(exc)}
        finally:
            lp.SCHEMA = saved
        cur.execute(f"DROP SCHEMA IF EXISTS {R} CASCADE")
        cur.execute(f"CREATE SCHEMA {R}")
    try:
        if "A" in a.phases:
            report["A"] = phase_a(s, a, report)
            log(f"A: {json.dumps({k: v for k, v in report['A'].items() if k != 'partitions'})}")
            if report["A"]["schema_bytes"] > a.max_gb * (1 << 30):
                raise SystemExit("rehearsal schema over its disk cap")
        if "B" in a.phases:
            report["B"] = phase_b(s, a, report)
        if "C" in a.phases:
            report["C"] = phase_c(s, a, report)
        report["projection"] = projection(report)
    finally:
        if not a.keep:
            with s.connect() as c:
                c.execute(f"DROP SCHEMA IF EXISTS {R} CASCADE")
        report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        text = json.dumps(report, indent=1, default=str)
        if a.out:
            Path(a.out).write_text(text)
        print(text)
    return 0


def projection(report: dict) -> dict:
    """The full conversion projected linearly from phase A's rates (the served rows from
    its type counts, its whole heap read)."""
    A = report.get("A")
    if not A:
        return {}
    served = report["served"]
    scale = served["rows"] / max(A["rows"], 1)
    out = {"scale": round(scale, 2)}
    for workers in (8, 12, 16):
        fill = A["timings"]["fill"] * scale * report["workers"] / workers
        index = A["timings"]["index"] * scale * report["workers"] / max(workers, 6)
        # keys and parent indexes are catalog changes (not linear in the rows; counted
        # twice over); the vacuum reads each partition's heap once, the largest partition
        # bounds it whatever the workers: linear in the rows
        rest = 2 * sum(A["timings"].get(k, 0) for k in ("pkey", "parent_index", "analyze"))
        vacuum = A["timings"].get("vacuum", 0) * scale
        total = fill + index + rest + vacuum
        out[f"workers_{workers}"] = {"fill_s": round(fill), "index_s": round(index),
                                     "vacuum_s": round(vacuum),
                                     "rest_s": round(rest), "total_s": round(total),
                                     "linear_in_workers": "assumed; 8 is measured"}
    # heap, TOAST and indexes: every byte the rehearsal schema took
    out["disk_bytes"] = int(A["schema_bytes"] * scale)
    out["projected_by_rates"] = report.get("convert_plan")
    out["read_bytes"] = served["pages"] * 8192
    return out


if __name__ == "__main__":
    raise SystemExit(main())
