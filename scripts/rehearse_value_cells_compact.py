"""Rehearse ``operating.pg.value_cells_compact`` on a share of the served value cells
(default 10 %) in a scratch database of the served cluster (never ``plx``).

``python3 scripts/rehearse_value_cells_compact.py setup [--share 0.10]``
``python3 scripts/rehearse_value_cells_compact.py run [--workers 4] [--out F]``
``python3 scripts/rehearse_value_cells_compact.py drop``

* **setup**: the scratch database ``plx_rehearsal_f13`` built from the repository's
  migrations (every view and function the served schema has), the served value fields,
  kinds and packed sources whole, and ``share`` of the served ``ontology.value_cells``
  blocks (``TABLESAMPLE SYSTEM``: real rows, real values, in the served physical order),
  then every index the served table has (the production-only ``value_cells_by_field``,
  the word index with the served statistics target, the document-name index), and a
  share of its bloat: the same share of the served dead space, made by deleting rows
  evenly spread (the served heap is 39 GB for ~28 GB of live rows).
* **run**: the whole conversion, timed per phase, with a writer changing cells meanwhile
  (inserts, deletes, updates of random sources, as a delta's apply does): ``prepare``
  (change log, the new table), the fill in parallel block ranges, an interrupted fill
  resumed from its marks, catch-up, indexes side by side, vacuum, the snapshot check,
  the swap, then ``undo`` and the swap again and ``finish``. Every reader view is read
  before and after (an order-free hash of every row); the sizes before and after, and
  the projection of the served table from the measured rates.

Reads of the served database are read-only. Stops when ``/data`` would fall under
``--min-free-gb`` (default 60).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = "plx_rehearsal_f13"
SERVED_DSN = os.environ.get("PLX_PG_DSN") or "postgresql:///plx"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def scratch_dsn() -> str:
    import psycopg
    return psycopg.conninfo.make_conninfo(SERVED_DSN, dbname=DB)


def free_gb() -> float:
    return shutil.disk_usage("/data").free / (1 << 30)


def guard(min_free: float) -> None:
    if free_gb() < min_free:
        raise SystemExit(f"stopped: /data has {free_gb():.1f} GB free < {min_free} GB")


def setup(share: float, min_free: float) -> dict:
    import psycopg
    from operating.pg.migrate import migrate
    guard(min_free + 8)
    with psycopg.connect(SERVED_DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DB}")
    out: dict = {"share": share}
    with psycopg.connect(scratch_dsn(), autocommit=True) as pg:
        migrate(pg)
        pg.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        with psycopg.connect(SERVED_DSN) as served:
            served.execute("SET TRANSACTION READ ONLY")
            for table, cols in (("value_fields", "field, property, field_path"),
                                ("value_kinds", "kind, value_type, unit, quantity_role, "
                                 "tax_basis, parser, authority_rank, markings"),
                                ("packed_sources", "src")):
                t = time.monotonic()
                with served.cursor().copy(f"COPY (SELECT {cols} FROM ontology.{table}) TO "
                                          "STDOUT (FORMAT binary)") as src, \
                        pg.cursor().copy(f"COPY ontology.{table} ({cols}) FROM STDIN "
                                         "(FORMAT binary)") as dst:
                    for chunk in src:
                        dst.write(chunk)
                log(f"copied {table} in {time.monotonic() - t:.1f} s")
            t = time.monotonic()
            cols = "src, field, kind, value, value_num, currency, currency_citation"
            # ``share`` of the served heap as many short block-range reads, evenly spread
            # (autocommit: each its own snapshot, held for a second or so; never one long
            # read holding the table against a migration's lock)
            served.commit()
            served.autocommit = True
            served.execute("SET default_transaction_read_only = on")
            (pages,) = served.execute(
                "SELECT pg_relation_size('ontology.value_cells') / "
                "current_setting('block_size')::int").fetchone()
            parts = 400
            width = max(1, int(pages * share / parts))
            stride = max(width, pages // parts)
            for k, lo in enumerate(range(0, pages, stride)):
                guard(min_free)
                with served.cursor().copy(
                        f"COPY (SELECT {cols} FROM ontology.value_cells WHERE ctid >= "
                        f"'({lo},0)'::tid AND ctid < '({lo + width},0)'::tid) TO STDOUT "
                        "(FORMAT binary)") as src, \
                        pg.cursor().copy(f"COPY ontology.value_cells ({cols}) FROM STDIN "
                                         "(FORMAT binary)") as dst:
                    for chunk in src:
                        dst.write(chunk)
                if k % 50 == 0:
                    log(f"copied block range {k}/{parts}")
            out["copy_s"] = round(time.monotonic() - t, 1)
            log(f"copied the value cells sample in {out['copy_s']} s")
            heap, tuples, dead = served.execute(
                "SELECT pg_relation_size('ontology.value_cells'), (SELECT reltuples FROM "
                "pg_class WHERE oid = 'ontology.value_cells'::regclass)::bigint, "
                "(SELECT n_dead_tup FROM pg_stat_user_tables WHERE relid = "
                "'ontology.value_cells'::regclass)").fetchone()
            out["served"] = {"heap_bytes": int(heap), "rows": int(tuples), "dead": int(dead)}
            fields = [r[0] for r in served.execute(
                "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i WHERE "
                "i.indexrelid = 'ontology.value_cells_doc_names'::regclass")]
        (rows,) = pg.execute("SELECT count(*) FROM ontology.value_cells").fetchone()
        out["rows"] = int(rows)
        # the served heap's dead share: its heap beyond what its live rows take
        (live_bytes,) = pg.execute("SELECT pg_relation_size('ontology.value_cells')"
                                   ).fetchone()
        served_live = out["served"]["rows"] / max(rows, 1) * live_bytes
        dead_share = max(0.0, 1 - served_live / max(out["served"]["heap_bytes"], 1))
        out["served_dead_share"] = round(dead_share, 3)
        # bloat like the served heap's: delete an even spread of rows, keep their space
        if dead_share > 0.02:
            every = max(2, int(round(1 / dead_share)))
            pg.execute("CREATE TEMP TABLE gone AS SELECT src, field FROM ontology.value_cells "
                       "TABLESAMPLE BERNOULLI (%s) REPEATABLE (5)" % (100.0 / every))
            (n,) = pg.execute("SELECT count(*) FROM gone").fetchone()
            # put them back at the end (the fresh rows of a delta), the old copies dead
            pg.execute("CREATE TEMP TABLE back AS SELECT c.* FROM ontology.value_cells c "
                       "JOIN gone g USING (src, field)")
            pg.execute("DELETE FROM ontology.value_cells c USING gone g WHERE "
                       "c.src = g.src AND c.field = g.field")
            pg.execute("INSERT INTO ontology.value_cells SELECT * FROM back")
            out["bloat_rows_moved"] = int(n)
        pg.execute("VACUUM ontology.value_cells")
        t = time.monotonic()
        pg.execute("SET maintenance_work_mem = '1GB'")
        pg.execute("CREATE INDEX value_cells_by_field ON ontology.value_cells (field, src)")
        pg.execute("CREATE INDEX value_cells_words ON ontology.value_cells USING gin "
                   "(ontology.sq_words(value)) WHERE value_num IS NULL AND value IS NOT NULL")
        pg.execute("ALTER INDEX ontology.value_cells_words ALTER COLUMN 1 SET STATISTICS 1000")
        if fields:
            pg.execute("CREATE INDEX value_cells_doc_names ON ontology.value_cells USING gin "
                       f"(lower(value) gin_trgm_ops) WHERE {fields[0]}")
        pg.execute("ANALYZE ontology.value_cells")
        out["source_index_s"] = round(time.monotonic() - t, 1)
        log(f"source indexes in {out['source_index_s']} s")
        out["source_bytes"] = int(pg.execute(
            "SELECT pg_total_relation_size('ontology.value_cells')").fetchone()[0])
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=("setup", "run", "drop"))
    p.add_argument("--share", type=float, default=0.10)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--min-free-gb", type=float, default=60.0)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    if a.phase == "setup":
        out = setup(a.share, a.min_free_gb)
    elif a.phase == "run":
        out = run(a.workers, a.min_free_gb)
    else:
        import psycopg
        with psycopg.connect(SERVED_DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        out = {"dropped": DB}
    text = json.dumps(out, indent=2, sort_keys=True)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


WORDS = ("order", "invoice", "ship*", "po", "steel", "2026*")


def probes(cur) -> dict:
    """What the readers' index reads give on the scratch table: each word query through
    the word index, the document-name index, a key range; (count, hash) each."""
    from operating.pg.words import tsquery
    out = {}
    cur.execute("SET enable_seqscan = off")
    for word in WORDS:
        cur.execute("SELECT count(*), coalesce(sum(hashtextextended(row(c.*)::text, 0)"
                    "::numeric), 0) FROM ontology.value_cells c WHERE "
                    "ontology.sq_words(c.value) @@ %s::tsquery AND c.value_num IS NULL "
                    "AND c.value IS NOT NULL", (tsquery(f'"{word.rstrip("*")}"'
                                                       + ("*" if word.endswith("*") else "")),))
        out["words:" + word] = [str(x) for x in cur.fetchone()]
    cur.execute("SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i WHERE "
                "i.indexrelid = to_regclass('ontology.value_cells_doc_names')")
    row = cur.fetchone()
    if row:
        cur.execute("SELECT count(*), coalesce(sum(hashtextextended(row(c.*)::text, 0)"
                    f"::numeric), 0) FROM ontology.value_cells c WHERE {row[0]} AND "
                    "lower(c.value) LIKE '%%pdf%%'")
        out["doc_names"] = [str(x) for x in cur.fetchone()]
    cur.execute("SELECT min(src), max(src) FROM ontology.value_cells")
    lo, hi = cur.fetchone()
    mid = (lo + hi) // 2
    cur.execute("SELECT count(*), coalesce(sum(hashtextextended(row(c.*)::text, 0)::numeric),"
                " 0) FROM ontology.value_cells c WHERE c.src BETWEEN %s AND %s",
                (mid, mid + 20000))
    out["key_range"] = [str(x) for x in cur.fetchone()]
    cur.execute("RESET enable_seqscan")
    return out


class Writer(threading.Thread):
    """A delta's apply in small transactions: a source's cells deleted and written again
    with a value changed, a cell updated in place, a cell deleted."""

    def __init__(self, dsn: str, pause: float = 0.2):
        super().__init__(daemon=True)
        self.dsn, self.pause, self.stop, self.n, self.err = dsn, pause, threading.Event(), 0, None

    def run(self) -> None:
        import psycopg
        rnd = random.Random(3)
        try:
            with psycopg.connect(self.dsn) as pg:
                lo, hi = pg.execute("SELECT min(src), max(src) FROM ontology.value_cells"
                                    ).fetchone()
                pg.commit()
                while not self.stop.is_set():
                    src = pg.execute("SELECT src FROM ontology.value_cells WHERE src >= %s "
                                     "ORDER BY src LIMIT 1", (rnd.randint(lo, hi),)).fetchone()
                    if src:
                        src = src[0]
                        pg.execute("CREATE TEMP TABLE keep ON COMMIT DROP AS SELECT * FROM "
                                   "ontology.value_cells WHERE src = %s", (src,))
                        pg.execute("DELETE FROM ontology.value_cells WHERE src = %s", (src,))
                        pg.execute("UPDATE keep SET value = value || ' w' WHERE value_num IS "
                                   "NULL AND value IS NOT NULL AND field = (SELECT min(field) "
                                   "FROM keep)")
                        pg.execute("INSERT INTO ontology.value_cells SELECT * FROM keep")
                        pg.execute("UPDATE ontology.value_cells SET value = value || 'u' "
                                   "WHERE src = %s + 1 AND value_num IS NULL", (src,))
                        pg.execute("DELETE FROM ontology.value_cells WHERE ctid = (SELECT "
                                   "ctid FROM ontology.value_cells WHERE src = %s + 2 "
                                   "LIMIT 1)", (src,))
                    pg.commit()
                    self.n += 1
                    time.sleep(self.pause)
        except Exception as exc:  # reported, never silent
            self.err = repr(exc)


def run(workers: int, min_free: float) -> dict:
    import psycopg
    os.environ["PLX_VALUE_CELLS_SCHEMA"] = "ontology"
    from operating.pg import value_cells_compact as vc
    dsn = scratch_dsn()
    if DB not in dsn:
        raise SystemExit("not the scratch database")
    guard(min_free + 6)
    out: dict = {"workers": workers}
    with psycopg.connect(dsn, autocommit=True) as pg:
        cur = pg.cursor()
        before = vc.sizes(cur, "value_cells")
        cur.execute("SELECT count(*) FROM ontology.value_cells")
        rows = int(cur.fetchone()[0])
        out["before"] = {**before, "rows": rows}
        idx = vc.indexes(cur, "value_cells")
        btrees = sum(1 for i in idx if not vc.DURING_FILL.search(i["def"]))
        out["indexes"] = [i["name"] for i in idx]

    class Stop(Exception):
        pass

    stop_at = 7

    def stopper(i: int) -> None:
        guard(min_free)
        if i == stop_at:
            raise Stop()

    def session(after=None):
        return vc.Session(dsn, workers=workers, log=log, after_range=after,
                          maintenance_mem="1GB")

    writer = Writer(dsn)
    writer.start()
    runs = []
    for attempt in ("first", "again"):
        s = session(stopper)
        t0 = time.monotonic()
        try:
            got = vc.convert(s, budget_s=7200, reserve=int(min_free * 2**30))
        except Stop:
            log("fill stopped on purpose; resuming")
            s2 = session(lambda i: guard(min_free))
            got = vc.convert(s2, budget_s=7200, reserve=int(min_free * 2**30))
            for k, v in s2.timings.items():
                s.timings[k] = round(s.timings.get(k, 0) + v, 3)
        with psycopg.connect(dsn, autocommit=True) as pg:
            want = probes(pg.cursor())
            new = vc.sizes(pg.cursor(), vc.NEW)
        t1 = time.monotonic()
        sw = vc.swap(session())
        swap_s = time.monotonic() - t1
        with psycopg.connect(dsn, autocommit=True) as pg:
            seen = probes(pg.cursor())
        run_out = {"timings": s.timings, "convert_wall_s": round(t1 - t0, 1),
                   "swap_wall_s": round(swap_s, 1), "swap": sw, "check": got.get("check"),
                   "fill": got.get("fill"), "new": new,
                   "readers_same_after_swap": seen == want}
        if seen != want:
            run_out["probes_before"], run_out["probes_after"] = want, seen
        if attempt == "first":
            t2 = time.monotonic()
            run_out["undo"] = vc.undo(session())
            run_out["undo_wall_s"] = round(time.monotonic() - t2, 1)
            with psycopg.connect(dsn, autocommit=True) as pg:
                undone = probes(pg.cursor())
            run_out["readers_same_after_undo"] = undone == seen
        else:
            writer.stop.set()
            writer.join()
            t2 = time.monotonic()
            run_out["finish"] = vc.finish(session())
            run_out["finish_wall_s"] = round(time.monotonic() - t2, 1)
        runs.append(run_out)
        log(json.dumps(run_out, default=str)[:600])
    writer.stop.set()
    out["runs"] = runs
    out["writer"] = {"transactions": writer.n, "error": writer.err}
    with psycopg.connect(dsn, autocommit=True) as pg:
        after = vc.sizes(pg.cursor(), "value_cells")
    out["after"] = after
    # rates from the slower run (rounded down in the module's RATES)
    def rate(key, f):
        return min(f(r) for r in runs)
    n = rows
    out["rates"] = {
        "fill_rows_s_per_worker": rate("fill", lambda r: n / r["timings"]["fill"] / workers),
        "index_rows_s": rate("idx", lambda r: n * btrees / r["timings"]["indexes"]),
        "vacuum_rows_s": rate("vac", lambda r: n / r["timings"]["vacuum"]),
        "check_rows_s": rate("chk", lambda r: 2 * n / r["timings"]["check"]),
        "heap_bytes_per_row": max(r["new"]["heap_bytes"] / n for r in runs),
        "index_bytes_per_row": max(r["new"]["index_bytes"] / n for r in runs),
        "swap_s": max(r["swap_wall_s"] for r in runs),
    }
    # the served table: projected from these rates (read-only)
    with psycopg.connect(SERVED_DSN, autocommit=True) as served:
        served.execute("SET default_transaction_read_only = on")
        planned = vc.plan(served.cursor(), workers=workers, rates=out["rates"],
                          reserve=int(min_free * 2**30), check=False)
    try:
        vc.gate(planned["projection"], budget_s=vc.BUDGET_S, free=planned["free_bytes"],
                reserve=int(min_free * 2**30))
        planned["gate"] = "pass"
    except vc.OverBudget as exc:
        planned["gate"] = f"refused: {exc}"
    out["served_projection"] = planned
    out["saving_share"] = round(1 - (after["heap_bytes"] + after["index_bytes"])
                                / (before["heap_bytes"] + before["index_bytes"]), 3)
    return out


if __name__ == "__main__":
    sys.exit(main())
