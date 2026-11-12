"""Prove write-stamp sightings and link stretches (``ontology.stamps``, ``Projector._write_links``)
on a real capture sample: same current answers, less history per capture day.

``python3 -m scripts.stamp_sighting_proof --old-tree DIR --work DIR [--state-dir S]
[--share 10] [--bulk-share 1] [--day-end T] [--reuse]``

1. A sample copied read-only out of the live stores (``ontology.delta.mirror_subset``,
   ``store_subset``): ``--share`` percent of the records (by key hash) of every Portal
   relation whose declared stamps churn it (``ontology/schema/stamps.json``; the five
   bulk relations the ontology barely projects at ``--bulk-share`` percent), every product
   (the end most links of those relations reach), and ``--share`` percent of the BC
   capture's records with every company record (lineage, currency).
2. Four cold builds of the sample (the one projector, no build steps): the code in
   ``--old-tree`` (before) and this tree's (after), each as known at the last capture and
   one day earlier (a simulated capture day).
3. Current equality, old vs new as known at the last capture: objects whole; current
   versions, values, identifiers, links and events without their start (a folded version
   starts at the first capture of its content) and without link ids (a link's id no longer
   names its end versions); capture-observed events whole, ids included.
4. The Postgres apply path, in a throwaway cluster under ``--work`` (never the live one):
   the old build loaded, the new build applied over it as a delta layer owning every
   object (``operating.pg.ontology_delta.apply_layer``): every logical row equals the new
   cold build's.
5. Growth of one capture day, before and after: rows per table and Postgres bytes
   (each build loaded alone; total relation size of ``ontology.*``).

Prints one JSON report. Writes only under ``--work`` (dropped by the caller after).
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any

BULK = ("public.app_manufacturing_order_line", "public.app_production_task",
        "public.app_assembly_order_status", "public.app_batch_schedule",
        "public.app_assembly_order_damage")
ALWAYS = ("public.app_product",)
BC_ALWAYS = ("companies", "companyInformation")
PORT = 55433
_BUILD = """
import json, sys
from ontology import project
from ontology.schema import load_schema
mirror, bc, out, cut = sys.argv[1:5]
with project.MirrorCaptures(mirror) as portal, project.capture_source("bc", bc) as bcs:
    report = project.build_store(out, load_schema(), {"portal_api": portal, "bc": bcs},
                                 as_known_at=cut)
print(json.dumps({k: report.get(k) for k in ("counts", "content_digest")}, default=str))
"""


def _picked(key: str, percent: float) -> bool:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 10_000 < percent * 100


def sample(state: Path, work: Path, share: float, bulk_share: float,
           products_share: float = 100.0) -> dict[str, Any]:
    from ontology import stamps
    from ontology.delta import mirror_subset, store_subset
    declared = [rel for (source, rel) in stamps.load() if source == "portal_api"]
    mirror = state / "case-runtime" / "source-mirror.sqlite"
    src = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    keys: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for rel in sorted(set(declared) | set(ALWAYS)):
        pct = products_share if rel in ALWAYS else bulk_share if rel in BULK else share
        found = [(rel, k) for (k,) in src.execute(
            "SELECT DISTINCT key_json FROM row_current WHERE relation = ?", (rel,))
            if _picked(k, pct)]
        counts[rel] = len(found)
        keys += found
    src.close()
    mirror_subset(mirror, work / "mirror.sqlite", keys)
    bc = state / "bc-capture" / "capture.sqlite"
    src = sqlite3.connect(f"file:{bc}?mode=ro", uri=True)
    bc_keys = [(kind, path) for kind, path in src.execute(
        "SELECT DISTINCT kind, path FROM versions")
        if path.split("/")[2] in BC_ALWAYS or _picked(path, share)]
    src.close()
    store_subset(bc, work / "bc" / "capture.sqlite", bc_keys)
    return {"portal_records": counts, "bc_records": len(bc_keys)}


def last_capture(work: Path) -> str:
    conn = sqlite3.connect(f"file:{work / 'mirror.sqlite'}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT MAX(finished_at) FROM capture").fetchone()[0]
    finally:
        conn.close()


def build(tree: Path, work: Path, name: str, cut: str) -> dict[str, Any]:
    out = work / f"{name}.sqlite"
    out.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tree)
    t = time.monotonic()
    done = subprocess.run([sys.executable, "-c", _BUILD, str(work / "mirror.sqlite"),
                           str(work / "bc" / "capture.sqlite"), str(out), cut],
                          cwd=tree, env=env, capture_output=True, text=True, check=False)
    if done.returncode:
        raise RuntimeError(f"build {name} failed: {done.stderr[-3000:]}")
    return {**json.loads(done.stdout.strip().splitlines()[-1]),
            "seconds": round(time.monotonic() - t, 1), "bytes": out.stat().st_size}


# ------------------------------------------------------------------ current equality

def _current(path: Path) -> dict[str, Any]:
    """Each table's current rows as a multiset of tuples (start, link id left out)."""
    from ontology import store as S, store_ddl as D
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, Any] = {}
        out["objects"] = Counter(conn.execute("SELECT * FROM objects"))
        out["object_versions"] = Counter(conn.execute(
            "SELECT object_id, l1_handle, digest, lineage, markings FROM object_versions "
            "WHERE superseded_at IS NULL"))
        vcols = list(D.VALUE_COLUMNS)
        keep = [i for i, c in enumerate(vcols) if c != "recorded_at"]
        sup = vcols.index("superseded_at")
        out["values"] = Counter(tuple(row[i] for i in keep) for batch in S._value_rows(conn)
                               for row in batch if row[sup] is None)
        icols = [r[1] for r in conn.execute("PRAGMA table_info(identifiers)")
                 if r[1] not in ("recorded_at", "rowid")]
        out["identifiers"] = Counter(conn.execute(
            f"SELECT {', '.join(icols)} FROM identifiers WHERE superseded_at IS NULL"))
        lcols = [c for c in D.LINK_COLUMNS if c not in ("link_id", "recorded_at")]
        out["links"] = Counter(conn.execute(
            f"SELECT {', '.join(lcols)} FROM links WHERE superseded_at IS NULL"))
        ecols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        keep = [c for c in ecols if c not in ("recorded_at", "event_id")]
        out["date_events"] = Counter(conn.execute(
            f"SELECT {', '.join(keep)} FROM events WHERE superseded_at IS NULL AND "
            "time_basis <> 'capture_observed'"))
        out["capture_events"] = Counter(conn.execute(
            "SELECT * FROM events WHERE time_basis = 'capture_observed'"))
        return out
    finally:
        conn.close()


def current_equality(old: Path, new: Path) -> dict[str, Any]:
    a, b = _current(old), _current(new)
    out = {}
    for table in a:
        sa, sb = a[table], b[table]
        out[table] = {"rows": [sum(sa.values()), sum(sb.values())], "equal": sa == sb}
        if sa != sb:
            only_a = sorted(sa - sb, key=str)[:3]
            only_b = sorted(sb - sa, key=str)[:3]
            out[table].update({"differing": [sum((sa - sb).values()),
                                             sum((sb - sa).values())]})
            out[table].update({"only_old": [str(r)[:400] for r in only_a],
                               "only_new": [str(r)[:400] for r in only_b]})
    return out


def counts(path: Path) -> dict[str, int]:
    from ontology import store as S
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("objects", "object_versions", "links", "identifiers", "events")}
        out["values"] = sum(len(batch) for batch in S._value_rows(conn))
        return out
    finally:
        conn.close()


# ------------------------------------------------------------------ throwaway Postgres

class Cluster:
    def __init__(self, work: Path) -> None:
        self.dir, self.sock = work / "pg", work / "pgsock"
        self.bin = Path("/usr/lib/postgresql/17/bin")

    def __enter__(self) -> "Cluster":
        shutil.rmtree(self.dir, ignore_errors=True)
        self.sock.mkdir(parents=True, exist_ok=True)
        subprocess.run([self.bin / "initdb", "-D", self.dir, "-A", "trust", "-E", "UTF8",
                        "--no-sync"], check=True, capture_output=True)
        opts = (f"-c listen_addresses='' -c unix_socket_directories={self.sock} -p {PORT} "
                "-c shared_buffers=512MB -c fsync=off -c synchronous_commit=off "
                "-c full_page_writes=off -c max_wal_size=4GB -c work_mem=64MB")
        subprocess.run([self.bin / "pg_ctl", "-D", self.dir, "-o", opts, "-l",
                        self.dir / "log", "-w", "start"], check=True, capture_output=True)
        return self

    def __exit__(self, *_: Any) -> None:
        subprocess.run([self.bin / "pg_ctl", "-D", self.dir, "-m", "fast", "stop"],
                       check=False, capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def dsn(self, db: str) -> str:
        return f"host={self.sock} port={PORT} dbname={db}"

    def create(self, db: str) -> str:
        import psycopg
        from operating.pg.migrate import migrate
        with psycopg.connect(self.dsn("postgres"), autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {db}")
            pg.execute(f"CREATE DATABASE {db}")
        with psycopg.connect(self.dsn(db), autocommit=True) as pg:
            migrate(pg)
        return self.dsn(db)


def pg_bytes(dsn: str) -> dict[str, int]:
    import psycopg
    with psycopg.connect(dsn) as pg:
        rows = pg.execute(
            "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c JOIN "
            "pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ontology' AND "
            "c.relkind = 'r'").fetchall()
    return {name: size for name, size in rows if size}


def as_layer(store: Path, base: Path, out: Path) -> Path:
    """``store`` as a delta layer over ``base`` owning every object."""
    shutil.copyfile(store, out)
    conn = sqlite3.connect(out)
    try:
        digest = sqlite3.connect(f"file:{base}?mode=ro", uri=True).execute(
            "SELECT value FROM store_meta WHERE key = 'content_digest'").fetchone()[0]
        conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'", (digest,))
        conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO delta_owned SELECT object_id FROM objects")
        conn.commit()
    finally:
        conn.close()
    return out


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-tree", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--share", type=float, default=10.0)
    parser.add_argument("--bulk-share", type=float, default=1.0)
    parser.add_argument("--products-share", type=float, default=100.0,
                        help="percent of products (default every one; a product's Sage "
                             "restamps make an all-products build of the old code tens of GB)")
    parser.add_argument("--min-free-gb", type=float, default=75.0,
                        help="refuse each build while the work disk has less free")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--day-end", default=None,
                        help="end of the simulated capture day (default: the last capture)")
    args = parser.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parents[1]
    report: dict[str, Any] = {"old_tree": args.old_tree, "share": args.share,
                              "bulk_share": args.bulk_share,
                              "products_share": args.products_share}
    t = time.monotonic()
    if not (args.reuse and (work / "mirror.sqlite").is_file()):
        report["sample"] = sample(Path(args.state_dir), work, args.share, args.bulk_share,
                                  args.products_share)
    report["sample_seconds"] = round(time.monotonic() - t, 1)
    from ontology.store import instant
    import datetime as dt
    day = instant(args.day_end or last_capture(work))
    before = (dt.datetime.fromisoformat(day.replace("Z", "+00:00"))
              - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    report["cuts"] = [before, day]
    builds = {}
    for code, tree in (("old", Path(args.old_tree)), ("new", here)):
        for label, cut in (("d0", before), ("d1", day)):
            name = f"{code}-{label}"
            if not (args.reuse and (work / f"{name}.sqlite").is_file()):
                free_gb = shutil.disk_usage(work).free / 1e9
                if free_gb < args.min_free_gb:   # the work disk is shared with live stores
                    report["refused"] = f"disk_guard:{name}:{free_gb:.0f}GB"
                    print(json.dumps(report, indent=1, default=str))
                    return 3
                report[f"build_{name}"] = build(tree, work, name, cut)
            builds[name] = work / f"{name}.sqlite"
    report["current"] = current_equality(builds["old-d1"], builds["new-d1"])
    report["current_equal"] = all(v["equal"] for v in report["current"].values())
    report["rows"] = {name: counts(path) for name, path in builds.items()}
    for code in ("old", "new"):
        a, b = report["rows"][f"{code}-d0"], report["rows"][f"{code}-d1"]
        report[f"rows_per_day_{code}"] = {t: b[t] - a[t] for t in a}
    sys.path.insert(0, str(here))
    from operating.pg import ontology_load
    from operating.pg.ontology_delta import apply_layer
    from scripts.schema_migration_proof import compare, logical_rows
    with Cluster(work) as cluster:
        sizes = {}
        for name, path in builds.items():
            dsn = cluster.create(name.replace("-", "_"))
            ontology_load.load(path, dsn, workers=2, replace=True, log=io.StringIO())
            sizes[name] = pg_bytes(dsn)
        report["pg_bytes"] = {n: sum(s.values()) for n, s in sizes.items()}
        for code in ("old", "new"):
            a, b = sizes[f"{code}-d0"], sizes[f"{code}-d1"]
            report[f"pg_bytes_per_day_{code}"] = {
                "total": sum(b.values()) - sum(a.values()),
                **{t: b.get(t, 0) - a.get(t, 0) for t in sorted(set(a) | set(b))
                   if b.get(t, 0) != a.get(t, 0)}}
        # the apply path: old rows served, the new code's layer owning every object
        dsn = cluster.create("apply")
        ontology_load.load(builds["old-d1"], dsn, workers=2, replace=True, log=io.StringIO())
        t = time.monotonic()
        layer = as_layer(builds["new-d1"], builds["old-d1"], work / "layer-new.sqlite")
        applied = apply_layer(layer, dsn)
        report["apply"] = {k: v for k, v in applied.items() if k != "meta"}
        report["apply_seconds"] = round(time.monotonic() - t, 1)
        report["apply_equivalence"] = compare(logical_rows(dsn=dsn),
                                              logical_rows(str(builds["new-d1"])))
        report["apply_equivalent"] = all(v["equal"] for v in
                                         report["apply_equivalence"].values())
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    return 0 if report["current_equal"] and report["apply_equivalent"] else 1


if __name__ == "__main__":
    sys.exit(main())
