"""Rehearse the DI-03 conversion with GA-F11 packing on real rows, away from the served host.

``dump`` (on the served host, read only): ``--share`` of the served ``ontology.link_rows``
blocks in evenly spread ranges, each a short ``COPY ... TO STDOUT`` (between ranges it
waits while a delta applies), zstd-compressed into ``--dir``, with the link texts and
skeletons the already packed rows among them name.

``load`` and ``run`` (on a throwaway Postgres, e.g. a Spark's test cluster): a database
of its own (its name must start with ``plx_di03_rehearsal``), the migrations, then the
served layout of the table (unpartitioned, keyed by the link id, the served indexes),
the dumped rows and texts. ``run``:

1. an order-free hash of every row as ``ontology.links`` reads it, the link slices
   (``ontology_reader.link_slice_sql``, the generic read and the typed one) and the odd
   texts the slice reader counts; table sizes;
   (after ``link_partitions.convert`` as stored and its ``finish``: DI-03 as served);
2. ``link_partitions.repack_all`` (GA-F11, ``--workers``), a writer changing, deleting
   and adding rows meanwhile, each new partition compared whole before its swap
   (``verify_percent`` 100), old partitions kept;
3. the same hash, slices and odd count, then ``rebuild_undo`` of every partition and the
   same again: the packed table must read exactly as the plain one with the writes;
4. the pass again, ``rebuild_finish``, the same again; sizes (the packed bytes a row).

Prints and writes JSON (``--out``). Timing here does not transfer to the served host.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operating.pg import link_partitions as lp  # noqa: E402

COLS = ", ".join(lp.COLUMNS)
SERVED_INDEXES = (
    "ALTER TABLE ontology.link_rows ADD CONSTRAINT link_rows_pkey PRIMARY KEY (link_id)",
    "CREATE INDEX link_rows_by_row_no ON ontology.link_rows (row_no)",
    "CREATE INDEX link_rows_by_type ON ontology.link_rows (link_type, tier)",
    "CREATE INDEX link_rows_from ON ontology.link_rows (from_object, link_type)",
    "CREATE INDEX link_rows_to ON ontology.link_rows (to_object, link_type)",
    'CREATE INDEX link_rows_lead_from ON ontology.link_rows (from_object, link_id COLLATE "C") '
    "WHERE tier = 'lead' AND superseded_at IS NULL",
    "CREATE INDEX link_rows_contra_from ON ontology.link_rows (from_object) INCLUDE "
    "(to_object, link_type) WHERE tier = 'contradicted'",
    "CREATE INDEX link_rows_odd_slots ON ontology.link_rows USING gin ((slots[1:ek])) "
    "WHERE evidence_id IS NOT NULL")
SCHEMES = ["sharepoint", "mail", "graph", "portal", "bc", "filemaker", "teams", "mc", "fm",
           "vmc", "file", "https", "http"]


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def spread(pages: int, share: float, parts: int) -> list[tuple[int, int]]:
    width = max(1, int(pages * share / parts))
    stride = pages // parts
    return [(i * stride, i * stride + width) for i in range(parts)]


# --- dump (served host, read only) ------------------------------------------------------

def dump(a) -> dict:
    import psycopg
    from operating.pg import config
    out = Path(a.dir)
    # the texts and skeletons (small) are written here, shipped after (``--ssh``)
    local = Path(a.local_dir) if a.ssh else out
    local.mkdir(parents=True, exist_ok=True)
    if a.ssh:
        subprocess.run(["ssh", "-F", a.ssh_config, a.ssh, f"mkdir -p {a.dir}"], check=True)
    report: dict = {"share": a.share, "parts": a.parts}
    with psycopg.connect(a.dsn or config.dsn(), autocommit=True) as conn:
        conn.execute("SET statement_timeout = '120s'")
        # the served table, or its partitions once DI-03 converted it
        tables = conn.execute(
            "SELECT 'ontology.' || c.relname, pg_relation_size(c.oid) / 8192 FROM pg_class c "
            "WHERE c.oid = 'ontology.link_rows'::regclass AND c.relkind = 'r' UNION ALL "
            "SELECT 'ontology.' || c.relname, pg_relation_size(c.oid) / 8192 FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = "
            "'ontology.link_rows'::regclass").fetchall()
        pages = sum(n for _, n in tables)
        report["served_pages"], report["tables"] = pages, len(tables)
        ranges = [(t, lo, hi) for t, n in tables if n
                  for lo, hi in spread(n, a.share, max(1, round(a.parts * n / pages)))]
        sql = {
            "rows": "COPY (SELECT {c} FROM {t} WHERE {w}) TO STDOUT",
            "texts": "COPY (SELECT t.id, t.text FROM ontology.link_texts t WHERE t.id IN "
                     "(SELECT unnest(slots) FROM {t} WHERE {w} AND "
                     "(reason_id IS NOT NULL OR evidence_id IS NOT NULL))) TO STDOUT",
            "skeletons": "COPY (SELECT k.id, k.text FROM ontology.link_skeletons k WHERE k.id IN "
                         "(SELECT unnest(ARRAY[reason_id, evidence_id]) FROM {t} "
                         "WHERE {w} AND (reason_id IS NOT NULL OR evidence_id IS NOT NULL))) "
                         "TO STDOUT"}
    dsn = a.dsn or config.dsn()
    sizes = {k: 0 for k in sql}
    lock = threading.Lock()

    def one_file(n: int) -> None:
        """File ``n`` of each kind: ranges n, n + files, ...; one connection, its own
        zstd streams (each to a local file, or through ``--ssh`` to the remote ``--dir``),
        side by side with the other files (one box-to-Spark stream is slow)."""
        mine = ranges[n::a.files]
        pipes = {}
        for k in sql:
            name = f"{k}.{n:03d}.copy.zst"
            if a.ssh and k == "rows":
                sink = subprocess.Popen(["ssh", "-F", a.ssh_config, a.ssh,
                                         f"cat > {a.dir}/{name}"], stdin=subprocess.PIPE)
                z = subprocess.Popen(["zstd", "-q", "-3", "-c"], stdin=subprocess.PIPE,
                                     stdout=sink.stdin)
                pipes[k] = (z, sink, None)
            else:
                f = open(local / name, "wb")
                pipes[k] = (subprocess.Popen(["zstd", "-q", "-3", "-c"], stdin=subprocess.PIPE,
                                             stdout=f), None, f)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SET statement_timeout = '600s'")
            for table, lo, hi in mine:
                while lp.delta_applying(c.cursor()):
                    time.sleep(2)
                w = lp._ctid(lo, hi)
                for k in sql:
                    # a range is read whole into memory, then written: a statement the box
                    # cancels (the stage guard ends reads past its limit) is read again
                    # from the start, never written twice
                    for attempt in range(1, 11):
                        buf = []
                        try:
                            with c.cursor().copy(sql[k].format(c=COLS, w=w, t=table)) as cp:
                                for data in cp:
                                    buf.append(bytes(data))
                            break
                        except psycopg.errors.QueryCanceled:
                            log(f"range {table} {lo} {k} cancelled (attempt {attempt})")
                            if attempt == 10:
                                raise
                            time.sleep(5 * attempt)
                    for data in buf:
                        pipes[k][0].stdin.write(data)
                    with lock:
                        sizes[k] += sum(map(len, buf))
        for z, sink, f in pipes.values():
            z.stdin.close()
            if z.wait():
                raise RuntimeError("zstd failed")
            if sink is not None:
                sink.stdin.close()
                if sink.wait():
                    raise RuntimeError("ssh sink failed")
            if f is not None:
                f.close()
        log(f"file {n} done: {sizes['rows'] >> 20} MB rows so far")

    from concurrent.futures import ThreadPoolExecutor
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=a.files) as pool:
        list(pool.map(one_file, range(a.files)))
    if a.ssh:
        tar = subprocess.Popen(["tar", "-C", str(local), "-c", "."], stdout=subprocess.PIPE)
        subprocess.run(["ssh", "-F", a.ssh_config, a.ssh, f"tar -x -C {a.dir}"],
                       stdin=tar.stdout, check=True)
        tar.wait()
    report.update({"raw_bytes": sizes, "dump_s": round(time.monotonic() - t0, 1),
                   "blocks": sum(hi - lo for _, lo, hi in ranges),
                   "local_bytes": sum(x.stat().st_size for x in local.glob("*.copy.zst"))})
    (local / "dump.json").write_text(json.dumps(report, indent=1))
    return report


# --- load and run (throwaway database) --------------------------------------------------

def _guard(dsn: str) -> None:
    from psycopg.conninfo import conninfo_to_dict
    d = conninfo_to_dict(dsn)
    if not str(d.get("dbname", "")).startswith("plx_di03_rehearsal"):
        raise SystemExit("load/run write only a database named plx_di03_rehearsal*")


def load(a) -> dict:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from operating.pg import migrate
    _guard(a.dsn)
    name = conninfo_to_dict(a.dsn)["dbname"]
    admin = make_conninfo(a.dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    src = Path(a.dir)
    report: dict = {}
    t0 = time.monotonic()
    with psycopg.connect(a.dsn, autocommit=True) as c:
        migrate.migrate(c)
        view = c.execute("SELECT pg_get_viewdef('ontology.links'::regclass, true)").fetchone()[0]
        c.execute("CREATE TABLE public.served_link_rows (LIKE ontology.link_rows "
                  "INCLUDING CONSTRAINTS)")
        c.execute("DROP VIEW ontology.links")
        c.execute("DROP TABLE ontology.link_rows CASCADE")
        c.execute("CREATE TABLE ontology.link_rows (LIKE public.served_link_rows INCLUDING "
                  "CONSTRAINTS) WITH (autovacuum_vacuum_scale_factor = 0.01)")
        c.execute("ALTER TABLE ontology.link_rows ALTER COLUMN row_no SET DEFAULT "
                  "nextval('ontology.link_rows_row_no'), ALTER COLUMN ek SET DEFAULT 0, "
                  "ALTER COLUMN slots SET DEFAULT '{}'")
        c.execute("DROP TABLE public.served_link_rows")
        for k, table, cols in (("rows", "ontology.link_rows", COLS),
                               ("texts", "ontology.link_texts", "id, text"),
                               ("skeletons", "ontology.link_skeletons", "id, text")):
            target = table if k == "rows" else f"stage_{k}"
            if k != "rows":
                c.execute(f"CREATE TEMP TABLE {target} (id bigint, text text)")
            for part in sorted(src.glob(f"{k}.*.copy.zst")):
                z = subprocess.Popen(["zstd", "-dc", str(part)], stdout=subprocess.PIPE)
                with c.cursor().copy(f"COPY {target} ({cols}) FROM STDIN") as cp:
                    while True:
                        data = z.stdout.read(1 << 20)
                        if not data:
                            break
                        cp.write(data)
                if z.wait():
                    raise SystemExit(f"zstd failed on {part}")
            if k != "rows":
                c.execute(f"INSERT INTO {table} SELECT DISTINCT ON (id) id, text FROM {target} "
                          "ORDER BY id ON CONFLICT DO NOTHING")
            log(f"loaded {k}")
        # a row a writer moved between two ranges' reads was read twice: keep one
        report["duplicates_dropped"] = c.execute(
            "DELETE FROM ontology.link_rows a USING ontology.link_rows b WHERE a.link_id = "
            "b.link_id AND a.ctid < b.ctid").rowcount
        if a.drop_packed:
            # a dump stopped before its texts were written: the rows already stored packed
            # (copied as they are by a repack) cannot be read back here, so they go
            report["packed_dropped"] = c.execute(
                "DELETE FROM ontology.link_rows WHERE reason_id IS NOT NULL OR evidence_id "
                "IS NOT NULL").rowcount
        c.execute("SELECT setval('ontology.link_rows_row_no', (SELECT max(row_no) FROM "
                  "ontology.link_rows))")
        for sql in SERVED_INDEXES:
            c.execute(sql)
        c.execute(f"CREATE VIEW ontology.links AS {view}")
        c.execute("VACUUM (ANALYZE) ontology.link_rows")
        report["rows"] = c.execute("SELECT count(*) FROM ontology.link_rows").fetchone()[0]
        report["packed_before"] = c.execute("SELECT count(*) FROM ontology.link_rows WHERE "
                                            "reason_id IS NOT NULL").fetchone()[0]
        report["dangling_texts"] = c.execute(
            "SELECT count(*) FROM ontology.link_rows l, unnest(l.slots) u(id) WHERE NOT EXISTS "
            "(SELECT 1 FROM ontology.link_texts t WHERE t.id = u.id)").fetchone()[0]
    report["load_s"] = round(time.monotonic() - t0, 1)
    return report


def row_hash(cur, table: str) -> dict:
    """Count and order-free hash of every row as the view reads it (texts formatted)."""
    r, e = lp._texts_sql("l")
    plain = [c for c in lp.COLUMNS if c not in ("reason_id", "evidence_id", "ek", "slots",
                                                "reason", "evidence_handles")]
    cur.execute(f"SELECT count(*), coalesce(sum(hashtextextended(ROW({', '.join('l.' + c for c in plain)}, "
                f"{r}, {e})::text, 7)::numeric), 0) FROM {table} l")
    n, h = cur.fetchone()
    return {"rows": n, "hash": str(h)}


def _fold(rows) -> str:
    from operating.ontology_tools_traverse import _folded_slices
    return str(hash(json.dumps(_folded_slices(rows), sort_keys=True, default=str)))


def slices(cur) -> dict:
    """The link slices as the readers fold them (a packed row's gated handles come one a
    row, a plain row's as a list), and the odd texts the slice reader counts."""
    from operating.pg import ontology_reader as reader
    cur.execute(reader.LINK_ODD_SQL, {"cap": reader.LINK_ODD_CAP + 1})
    odd_texts = cur.fetchone()[0] or []
    out: dict = {"odd_texts": len(odd_texts)}
    odd: list = []
    if len(odd_texts) <= reader.LINK_ODD_CAP:
        cur.execute(reader.LINK_ODD_ROWS_SQL, {"texts": odd_texts})
        odd = cur.fetchone()[0]
    out["odd_rows"] = len(odd)
    p = {"schemes": SCHEMES, "odd": odd}
    t = time.monotonic()
    cur.execute(reader.LINK_SLICE_SQL, p)
    rows = cur.fetchall()
    out["slice_s"] = round(time.monotonic() - t, 1)
    out["slice_hash"] = _fold(rows)
    cur.execute("SELECT link_type FROM ontology.link_rows GROUP BY 1 ORDER BY count(*) DESC "
                "LIMIT 1")
    p["name"] = cur.fetchone()[0]
    cur.execute(reader.link_slice_sql(typed=True), p)
    out["typed_hash"] = _fold(cur.fetchall())
    return out


def sizes(cur) -> dict:
    cur.execute(
        "SELECT coalesce(sum(pg_table_size(c.oid)), 0), coalesce(sum(pg_indexes_size(c.oid)), 0) "
        "FROM pg_class c WHERE c.relnamespace = 'ontology'::regnamespace AND c.relkind = 'r' "
        "AND (c.relname = 'link_rows' OR c.relispartition AND c.relname LIKE 'link_rows_%%')")
    table, index = cur.fetchone()
    cur.execute("SELECT pg_total_relation_size('ontology.link_texts'), "
                "pg_total_relation_size('ontology.link_skeletons'), "
                "(SELECT count(*) FROM ontology.link_texts), "
                "(SELECT count(*) FROM ontology.link_skeletons)")
    t, k, nt, nk = cur.fetchone()
    return {"table": table, "indexes": index, "texts": t, "skeletons": k, "n_texts": nt,
            "n_skeletons": nk}


def writer(dsn: str, stop: threading.Event, stats: dict) -> None:
    """Delta-like writes during the fill: tier changes, deletes, new rows."""
    import psycopg
    rnd = random.Random(11)
    with psycopg.connect(dsn, autocommit=True) as c:
        ids = [r[0] for r in c.execute("SELECT link_id FROM ontology.link_rows TABLESAMPLE "
                                       "SYSTEM (0.5) LIMIT 4000").fetchall()]
        n = 0
        while not stop.is_set() and ids:
            i = ids.pop()
            k = rnd.random()
            if k < 0.6:
                c.execute("UPDATE ontology.link_rows SET tier = 'reviewed' WHERE link_id = %s",
                          (i,))
            elif k < 0.8:
                c.execute("DELETE FROM ontology.link_rows WHERE link_id = %s", (i,))
            else:
                cols = ", ".join(lp.COLUMNS[1:-1])
                c.execute(f"INSERT INTO ontology.link_rows (link_id, {cols}) SELECT "
                          f"'rehearsal:' || link_id, {cols} FROM ontology.link_rows "
                          "WHERE link_id = %s ON CONFLICT DO NOTHING", (i,))
            n += 1
            time.sleep(0.01)
        stats["writes"] = n


def run(a) -> dict:
    """The served path tonight: ``convert`` as stored (DI-03, the code on main), ``finish``,
    then the GA-F11 repack pass, its undo and the pass again, made final."""
    import psycopg
    _guard(a.dsn)
    os.environ.setdefault("PLX_PG_DATA_DIR", a.data_dir)
    report: dict = {"workers": a.workers}
    s = lp.Session(a.dsn, workers=a.workers, log=log, maintenance_mem="2GB")
    t = time.monotonic()
    report["convert"] = lp.convert(s)
    report["convert_finish"] = lp.finish(s)
    report["convert_s"] = round(time.monotonic() - t, 1)
    with psycopg.connect(a.dsn, autocommit=True) as c, c.cursor() as cur:
        report["before"] = {**row_hash(cur, "ontology.link_rows"), **slices(cur),
                            "sizes": sizes(cur)}
    log(f"before: {report['before']['rows']} rows")
    stop, wstats = threading.Event(), {}
    w = threading.Thread(target=writer, args=(a.dsn, stop, wstats))
    s = lp.Session(a.dsn, workers=a.workers, log=log, maintenance_mem="2GB")
    t = time.monotonic()
    w.start()
    try:
        report["repack"] = lp.repack_all(s, every=True, verify_percent=100.0)
    finally:
        stop.set()
        w.join()
    report["repack_s"] = round(time.monotonic() - t, 1)
    report["writer"] = wstats
    with psycopg.connect(a.dsn, autocommit=True) as c, c.cursor() as cur:
        # the writer's rows written: the reference is the old partitions with the writes
        # caught up, i.e. this table after an undo; read it as is, then undo and compare
        after = {**row_hash(cur, "ontology.link_rows"), **slices(cur), "sizes": sizes(cur),
                 "audit": lp.audit(cur)}
        report["after"] = after
        gens = lp.kept_rebuilds(cur)
    report["undo"] = [lp.rebuild_undo(s, g) for g in sorted(gens, reverse=True)]
    with psycopg.connect(a.dsn, autocommit=True) as c, c.cursor() as cur:
        undone = {**row_hash(cur, "ontology.link_rows"), **slices(cur)}
        report["undone"] = undone
        keys = ("rows", "hash", "slice_hash", "typed_hash", "odd_rows")
        report["packed_equal_to_plain"] = all(after[k] == undone[k] for k in keys)
        report["undo_left_plain"] = cur.execute(
            "SELECT count(*) FROM ontology.link_rows WHERE reason_id IS NOT NULL").fetchone()[0]
        cur.execute(f"DELETE FROM ontology.{lp.REPACKED}")
    s2 = lp.Session(a.dsn, workers=a.workers, log=log, maintenance_mem="2GB")
    report["repack_again"] = lp.repack_all(s2, every=True, verify_percent=1.0)
    with psycopg.connect(a.dsn, autocommit=True) as c, c.cursor() as cur:
        report["finish"] = [lp.rebuild_finish(s2, g) for g in lp.kept_rebuilds(cur)]
        final = {**row_hash(cur, "ontology.link_rows"), **slices(cur), "sizes": sizes(cur),
                 "audit": lp.audit(cur)}
        report["final"] = final
        report["final_equal"] = all(final[k] == undone[k] for k in keys)
        report["final_packed"] = cur.execute(
            "SELECT count(*) FILTER (WHERE reason_id IS NOT NULL), count(*) FILTER (WHERE "
            "reason IS NOT NULL) FROM ontology.link_rows").fetchone()
    b, f = report["before"]["sizes"], final["sizes"]
    rows = final["rows"]
    report["per_row"] = {
        "before_table": round(b["table"] / rows), "before_indexes": round(b["indexes"] / rows),
        "after_table": round(f["table"] / rows), "after_indexes": round(f["indexes"] / rows),
        "texts_added": round((f["texts"] + f["skeletons"] - b["texts"] - b["skeletons"]) / rows)}
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("dump", "load", "run"))
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--share", type=float, default=0.10)
    ap.add_argument("--parts", type=int, default=256)
    ap.add_argument("--files", type=int, default=32)
    ap.add_argument("--ssh", default=None, help="dump: ship each file to this ssh host's --dir")
    ap.add_argument("--ssh-config", default="/data/plx-state/ts-petra/ssh_config")
    ap.add_argument("--local-dir", default=None, help="dump --ssh: the small files' folder")
    ap.add_argument("--drop-packed", action="store_true",
                    help="load: drop rows already packed (a dump without its texts)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--data-dir", default=str(Path.home()))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = {"dump": dump, "load": load, "run": run}[a.command](a)
    text = json.dumps(out, indent=1, default=str)
    print(text)
    if a.out:
        Path(a.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
