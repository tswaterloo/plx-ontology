"""Prove a schema migration on real captures in a scratch Postgres database.

``python3 -m scripts.schema_migration_proof --from-ref A --to-ref B --work DIR
[--state-dir /data/plx-state] [--control 3] [--real]``

1. :func:`ontology.schema_migrate.diff` of the schema at ``A`` and at ``B``.
2. A capture sample: every Portal mirror row of the relations the affected units read,
   plus ``--control`` relations no affected unit reads (500 rows each), copied read-only
   out of the live mirror (``ontology.delta.mirror_subset``).
3. Cold builds of the sample under ``A`` and under ``B`` (the one projector; no build
   steps on either side).
4. A scratch database (created here, dropped at the end) loaded with the ``A`` build; the
   migration layer built from the sample under ``B`` and applied in place.
5. Equivalence: every logical row of the database (objects, versions, removals, values,
   identifiers, events and their objects, links) equals the ``B`` cold build's, and no
   row outside the affected units was rewritten.
6. ``--real``: the migration layer built over the whole live mirror and BC capture store
   (read-only) for the affected units, and applied to a second scratch database holding
   the ``A`` sample: the real migration's time and rows.

Prints one JSON report. Nothing outside ``--work`` and the scratch databases is written.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping

MIGRATIONS = ("0006_ontology.sql", "0007_ontology_compact.sql", "0008_ontology_read.sql",
              "0009_schema_migrations.sql")


def _scratch_db() -> tuple[str, str]:
    import psycopg
    from operating.pg import scratch_db as pg_testdb
    name, dsn = pg_testdb.create_db()
    folder = Path(__file__).resolve().parents[1] / "operating" / "pg" / "migrations"
    with psycopg.connect(dsn, autocommit=True) as pg:
        for migration in MIGRATIONS:
            pg.execute((folder / migration).read_text(encoding="utf-8"))
    return name, dsn


def _hash(row: Any) -> int:
    text = "\x1f".join("\x00" if v is None else str(v) for v in row)
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=16).digest(), "big")


# per logical table: its columns (numbering left out) and the column rows are grouped by
def _tables() -> dict[str, tuple[tuple[str, ...], int]]:
    from ontology import store_ddl
    from operating.pg.ontology_load import TABLES
    return {
        "objects": (tuple(TABLES["objects"].columns[1:]), 0),
        "object_removals": (tuple(TABLES["object_removals"].columns), 0),
        "object_versions": (tuple(TABLES["object_versions"].columns[1:]), 0),
        "identifiers": (tuple(TABLES["identifiers"].columns),
                        list(TABLES["identifiers"].columns).index("object_id")),
        "events": (tuple(TABLES["events"].columns), 0),
        "event_objects": (tuple(TABLES["event_objects"].columns), 1),
        "links": (tuple(store_ddl.LINK_COLUMNS), 0),
        "object_values": (tuple(store_ddl.VALUE_COLUMNS), 0),
    }


def logical_rows(sqlite_path: str | None = None, dsn: str | None = None
                 ) -> dict[str, dict[str, tuple[int, int]]]:
    """Every logical row of one store, as a multiset digest per object (or link, or
    event): key -> (rows, sum of row hashes mod 2**128). Numbering columns are left out,
    so two stores holding the same rows give equal maps, in bounded memory."""
    from ontology import store as S
    out: dict[str, dict[str, tuple[int, int]]] = {}

    def add(table: str, rows: Any, key: int) -> None:
        held = out.setdefault(table, {})
        for row in rows:
            n, h = held.get(row[key], (0, 0))
            held[row[key]] = (n + 1, (h + _hash(row)) % (1 << 128))
    if sqlite_path is not None:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        for table, (cols, key) in _tables().items():
            if table == "object_values":
                for batch in S._value_rows(conn):
                    add(table, batch, key)
            else:
                add(table, conn.execute(f"SELECT {', '.join(cols)} FROM {table}"), key)
        conn.close()
        return out
    import psycopg
    with psycopg.connect(dsn) as pg:
        for table, (cols, key) in _tables().items():
            with pg.cursor(name=f"rows_{table}") as cur:
                cur.itersize = 20_000
                cur.execute(f"SELECT {', '.join(cols)} FROM ontology.{table}")
                add(table, cur, key)
    return out


def compare(a: Mapping[str, Mapping[str, Any]], b: Mapping[str, Mapping[str, Any]]
            ) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in sorted(set(a) | set(b)):
        sa, sb = a.get(table, {}), b.get(table, {})
        rows = [sum(n for n, _ in sa.values()), sum(n for n, _ in sb.values())]
        differing = sorted(k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k))
        out[table] = {"rows": rows, "keys": len(sb), "equal": not differing}
        if differing:
            out[table]["differing"] = len(differing)
            out[table]["first"] = differing[:5]
    return out


def _sample(mirror: Path, out: Path, migration: dict[str, Any], schema: Any,
            control: int) -> dict[str, Any]:
    from ontology.delta import mirror_subset
    bindings = {b["id"]: b for t in schema.object_types.values() for b in t["bindings"]}
    wanted = sorted({bindings[b]["relation"] for b in migration["deps"]
                     if b in bindings and bindings[b]["source"] == "portal_api"})
    others = sorted({b["relation"] for b in bindings.values() if b["source"] == "portal_api"}
                    - set(wanted))
    src = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    keys: list[tuple[str, str]] = []
    for relation in wanted:
        keys += src.execute("SELECT DISTINCT relation, key_json FROM row_current WHERE "
                            "relation = ?", (relation,)).fetchall()
    chosen = []
    for relation in others:
        if len(chosen) >= control:
            break
        rows = src.execute("SELECT DISTINCT relation, key_json FROM row_current WHERE "
                           "relation = ? LIMIT 500", (relation,)).fetchall()
        if len(rows) >= 50:
            chosen.append(relation)
            keys += rows
    src.close()
    mirror_subset(mirror, out, keys)
    return {"relations": wanted, "control_relations": chosen, "records": len(keys)}


def main(argv: Any = None) -> int:
    from ontology import project
    from ontology.schema_migrate import build_layer, diff, schema_at
    from operating.pg import ontology_load
    from operating.pg.ontology_migrate import apply_migration, delta_mark
    from operating.pg import scratch_db as pg_testdb

    parser = argparse.ArgumentParser()
    parser.add_argument("--from-ref", required=True)
    parser.add_argument("--to-ref", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--control", type=int, default=3)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--reuse", action="store_true",
                        help="keep the sample and cold builds an earlier run left in --work")
    args = parser.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    state = Path(args.state_dir)
    mirror = state / "case-runtime" / "source-mirror.sqlite"
    report: dict[str, Any] = {"from_ref": args.from_ref, "to_ref": args.to_ref}
    t = time.monotonic()
    old, new = schema_at(args.from_ref), schema_at(args.to_ref)
    migration = diff(old, new)
    report["diff"] = {"changes": migration["changes"], "refused": migration["refused"],
                      "units": len(migration["units"]["affected"]),
                      "of": migration["units"]["total"],
                      "dropped": migration["units"]["dropped"],
                      "seconds": round(time.monotonic() - t, 2)}
    if migration["refused"]:
        print(json.dumps(report, indent=1))
        return 1
    t = time.monotonic()
    sample = work / "mirror.sqlite"
    if not (args.reuse and sample.is_file()):
        report["sample"] = _sample(mirror, sample, migration, new, args.control)
    else:
        report["sample"] = {"reused": str(sample)}
    report["sample"]["seconds"] = round(time.monotonic() - t, 2)
    stores = {}
    for name, schema in (("from", old), ("to", new)):
        t = time.monotonic()
        stores[name] = work / f"cold-{name}.sqlite"
        if args.reuse and stores[name].is_file():
            continue
        stores[name].unlink(missing_ok=True)
        with project.MirrorCaptures(sample) as source:
            project.build_store(stores[name], schema, {"portal_api": source})
        report[f"cold_build_{name}_seconds"] = round(time.monotonic() - t, 2)
    dbs = []
    try:
        name, dsn = _scratch_db()
        dbs.append(name)
        ontology_load.load(stores["from"], dsn, workers=2, replace=True, log=io.StringIO())
        import psycopg
        with psycopg.connect(dsn) as pg:
            top = pg.execute("SELECT max(ver) FROM ontology.object_versions").fetchone()[0]
        t = time.monotonic()
        layer = work / "migration.sqlite"
        with project.MirrorCaptures(sample) as source:
            built = build_layer(migration, new, {"portal_api": source}, layer,
                                delta_mark=delta_mark(dsn))
        applied = apply_migration(layer, dsn)
        report["migration_sample"] = {"build": built, "apply": applied,
                                      "seconds": round(time.monotonic() - t, 2)}
        before = logical_rows(str(stores["from"]))
        after_pg = logical_rows(dsn=dsn)
        cold_to = logical_rows(str(stores["to"]))
        report["equivalence"] = compare(after_pg, cold_to)
        report["equivalent"] = all(v["equal"] for v in report["equivalence"].values())
        report["schema_changed_keys"] = {
            t: v.get("differing", 0) for t, v in compare(before, cold_to).items()}
        scope = migration["scope"]
        with psycopg.connect(dsn) as pg:
            report["untouched"] = pg.execute(
                "SELECT count(*) FILTER (WHERE v.ver <= %s), count(*) FROM "
                "ontology.object_versions v WHERE NOT EXISTS (SELECT 1 FROM "
                "unnest(%s::text[]) p WHERE starts_with(v.object_id, p))",
                (top, scope)).fetchone()
            report["served_schema"] = pg.execute(
                "SELECT value FROM ontology.store_meta WHERE key = 'schema_digest'"
                ).fetchone()[0] == migration["to"]
        if args.real:
            from operating.host import state_capture_paths
            name2, dsn2 = _scratch_db()
            dbs.append(name2)
            ontology_load.load(stores["from"], dsn2, workers=2, replace=True,
                               log=io.StringIO())
            t = time.monotonic()
            real = work / "migration-real.sqlite"
            with project.MirrorCaptures(mirror) as portal, \
                    project.capture_source("bc", state_capture_paths(state)["bc"]) as bc:
                built = build_layer(migration, new, {"portal_api": portal, "bc": bc}, real,
                                    delta_mark=delta_mark(dsn2))
            build_seconds = round(time.monotonic() - t, 2)
            applied = apply_migration(real, dsn2)
            report["migration_real"] = {"build": built, "build_seconds": build_seconds,
                                        "apply": applied,
                                        "layer_bytes": real.stat().st_size}
    finally:
        for name in dbs:
            pg_testdb.drop_db(name)
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    return 0 if report.get("equivalent") else 1


if __name__ == "__main__":
    sys.exit(main())
