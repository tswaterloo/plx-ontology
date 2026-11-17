"""An identifier kind that only accepts more text, completed in the Postgres ontology in
place, set-based, instead of re-projecting every unit holding it.

CT-D-09 loosened the Message-ID pattern (``internet_message_id``): 291,925 of the
3,278,018 served Email versions hold no Message-ID value, most of them ids the old pattern
rejected. Forcing ``b:graph_mail.messages`` into the migration re-projects all 3.27M
messages (hours). The plan instead names the kind's carriers
(``ontology.step_migrate.reparse_carriers``: a carrier whose parse decides only its own
value cell and identifier row; no link compares by the kind, no other property, event or
authority reads it, its binding has no marking rules) in ``units.identifier_reparse``, and
the unit layer's ``final`` refuses until this phase has applied
(``ontology_migrate.reparse_pending``).

``python3 -m operating.pg.identifier_reparse check --plan PLAN [--dsn DSN]``: read-only.
Every served identifier row of each kind re-read under this tree's grammar must give its
stored value (the kind only widened: nothing served changes), and every one's lineage and
interval must be its version's (what the phase writes its new rows from). Exit 1
otherwise.

``... stage --plan PLAN --state-dir STATE --out STAGE [--dsn DSN]``: read-only on
Postgres and the captures. Each carrier version Postgres holds no value cell of the
property for is read again from its capture (the projection's own record and parse:
``MailCaptures``' latest interval of the handle, ``project._cell``, ``project._parse``);
the ones that now parse are staged (a SQLite file): the value cell and the identifier row
a projection writes for them.

``... apply --stage STAGE [--dsn DSN] [--undo-log NAME] [--chunk N]``: the staged rows
inserted in chunks, each its own transaction under the delta's lock, written only while
the served schema is the plan's ``from``, the version's value source is still there and
holds no such cell (a rerun skips what is in: resumable). ``--undo-log`` logs every write
into a ``migrate_chunks`` undo log of the run (its units-first apply installs it), so the
train's undo takes them back. The last transaction records
``ontology_migrate.reparse_id`` per carrier.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

from ontology import parent_watch

FORMAT = "plx-identifier-reparse/v1"
CHUNK = 20_000


def _units(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    """The unit part of a plan file (``step_migrate.plan``'s or its ``units`` alone)."""
    return plan["units"] if "units" in plan and "format" in plan else plan


def carriers(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = list(_units(plan).get("identifier_reparse") or ())
    for c in out:
        if c.get("source") != "graph_mail" or c.get("relation") != "messages":
            raise ValueError(f"reparse_source_unreadable:{c.get('source')}.{c.get('relation')}")
    return out


def _parse(kind: str, raw: Any) -> str | None:
    """The projection's parse of one captured value (``project._parse``, its default
    normalizer): the normalized identifier, or None (blank, a placeholder, unparsed)."""
    from ontology import project
    text, problem = project._parse("identifier", raw, kind=kind,
                                   normalizer=project._default_normalizer())
    return text if problem is None else None


def _check_part(args: tuple[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    kind, rows = args
    return [(raw, normalized) for raw, normalized in rows if _parse(kind, raw) != normalized]


def check(plan: Mapping[str, Any], dsn: str, *, workers: int | None = None,
          batch: int = 50_000) -> dict[str, Any]:
    """Read-only (module docstring)."""
    import multiprocessing
    import psycopg

    out: dict[str, Any] = {"kinds": {}, "ok": True}
    width = workers or max(1, (os.cpu_count() or 2))
    with psycopg.connect(dsn) as pg:
        pg.read_only = True
        for kind in sorted({c["kind"] for c in carriers(plan)}):
            started = time.monotonic()
            seen, unequal, sample = 0, 0, []
            with pg.cursor(name=f"reparse_check_{kind}") as cur, \
                    multiprocessing.get_context("fork").Pool(
                        width, **parent_watch.pool_init()) as pool:
                cur.itersize = batch
                cur.execute("SELECT raw, normalized FROM ontology.identifiers "
                            "WHERE identifier_kind = %s", (kind,))
                parts: list[Any] = []

                def drain(keep: int) -> None:
                    nonlocal unequal, sample
                    while len(parts) > keep:
                        bad = parts.pop(0).get()
                        unequal += len(bad)
                        sample += [len(raw) for raw, _ in bad[:max(0, 3 - len(sample))]]
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        break
                    seen += len(rows)
                    parts.append(pool.apply_async(_check_part, ((kind, rows),)))
                    drain(2 * width)   # memory: a bounded window of batches in flight
                drain(0)
            with pg.cursor() as cur:
                # the new rows take their version's lineage and interval (the version
                # row's: a packed value source holds no lineage): so must every served
                # row of the kind
                cur.execute(
                    "SELECT count(*) FROM ontology.identifiers i JOIN ontology.object_versions"
                    " v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle JOIN "
                    "ontology.value_sources s ON s.ver = v.ver AND s.binding_id = ANY(%s) "
                    "WHERE i.identifier_kind = %s AND (v.lineage IS DISTINCT FROM i.lineage "
                    "OR v.recorded_at <> i.recorded_at OR v.superseded_at IS DISTINCT FROM "
                    "i.superseded_at)",
                    ([c["binding"] for c in carriers(plan) if c["kind"] == kind], kind))
                drift = cur.fetchone()[0]
            out["kinds"][kind] = {"served_rows": seen, "reparsed_unequal": unequal,
                                  "unequal_raw_lengths": sample, "interval_drift": drift,
                                  "seconds": round(time.monotonic() - started, 1)}
            out["ok"] = out["ok"] and unequal == 0 and drift == 0
    return out


_STAGE_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE rows (n INTEGER PRIMARY KEY, carrier INTEGER NOT NULL, src INTEGER NOT NULL,"
    " field INTEGER NOT NULL, kind INTEGER NOT NULL, value TEXT NOT NULL, raw TEXT NOT NULL,"
    " object_id TEXT NOT NULL, l1_handle TEXT NOT NULL, field_path TEXT NOT NULL,"
    " lineage TEXT NOT NULL, recorded_at TEXT NOT NULL, superseded_at TEXT)",
    "CREATE TABLE chunks (chunk INTEGER PRIMARY KEY, cells INTEGER NOT NULL,"
    " identifiers INTEGER NOT NULL, skipped INTEGER NOT NULL, at TEXT NOT NULL)")

_LATEST_OF = ("SELECT record FROM messages WHERE version_id = (SELECT MAX(version_id) FROM "
              "messages WHERE l1_handle = ?)")
_FIELD_PREFIX = "row"   # ``MailCaptures``' messages: every value cites row.<field>


def _served_from(cur: Any, plan: Mapping[str, Any]) -> None:
    from operating.pg.ontology_migrate import served_schema
    served = served_schema(cur)
    if served != _units(plan)["from"]:
        raise ValueError(f"served_schema_differs:{served}")


def stage(plan: Mapping[str, Any], state_dir: str | Path, out: str | Path, dsn: str,
          *, batch: int = 20_000) -> dict[str, Any]:
    """Read-only on Postgres and the captures (module docstring)."""
    import psycopg
    from integrations import capture_mail
    from ontology import project

    started = time.monotonic()
    out = Path(out)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    mail = Path(state_dir) / capture_mail.STORE_DIR / capture_mail.DB_FILE
    report: dict[str, Any] = {"carriers": []}
    with closing(sqlite3.connect(tmp)) as db, \
            closing(sqlite3.connect(f"file:{mail}?mode=ro", uri=True)) as cap, \
            psycopg.connect(dsn) as pg:
        pg.read_only = True
        for ddl in _STAGE_DDL:
            db.execute(ddl)
        n = 0
        with pg.cursor() as cur:
            _served_from(cur, plan)
        for index, c in enumerate(carriers(plan)):
            field_path = f"{_FIELD_PREFIX}.{c['path']}"
            marks = project._markings_text(frozenset(c["markings"]))
            with pg.cursor() as cur:
                cur.execute("SELECT field FROM ontology.value_fields WHERE property = %s AND "
                            "field_path = %s", (c["property"], field_path))
                row = cur.fetchone()
                cur.execute("SELECT kind FROM ontology.value_kinds WHERE value_type = "
                            "'identifier' AND unit IS NULL AND quantity_role IS NULL AND "
                            "tax_basis IS NULL AND parser = 'identifier' AND authority_rank "
                            "IS NULL AND markings = %s", (marks,))
                krow = cur.fetchone()
            if row is None or krow is None:   # served rows of it exist: never invented
                raise ValueError(f"value_dictionary_missing:{c['property']}")
            field, kind_id = row[0], krow[0]
            counts = {"missing": 0, "parsed": 0, "blank_or_unparsed": 0, "path_absent": 0,
                      "record_missing": 0}
            with pg.cursor(name=f"reparse_stage_{index}") as cur:
                cur.itersize = batch
                cur.execute(
                    "SELECT s.src, v.object_id, v.l1_handle, v.lineage, v.recorded_at, "
                    "v.superseded_at FROM ontology.value_sources s JOIN "
                    "ontology.object_versions v ON v.ver = s.ver WHERE s.binding_id = %s AND "
                    "NOT EXISTS (SELECT 1 FROM ontology.value_cells c WHERE c.src = s.src "
                    "AND c.field = %s)", (c["binding"], field))
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        break
                    staged = []
                    for src, oid, handle, lineage, recorded, superseded in rows:
                        counts["missing"] += 1
                        got = cap.execute(_LATEST_OF, (handle,)).fetchone()
                        if got is None:
                            counts["record_missing"] += 1
                            continue
                        record = project.MailCaptures._message_row(got[0])
                        present, raw = project._cell(record, c["path"])
                        if not present:
                            counts["path_absent"] += 1
                            continue
                        value = _parse(c["kind"], raw)
                        if value is None:
                            counts["blank_or_unparsed"] += 1
                            continue
                        n += 1
                        counts["parsed"] += 1
                        staged.append((n, index, src, field, kind_id, value,
                                       project._text(raw), oid, handle, field_path,
                                       lineage, recorded, superseded))
                    db.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                                   "?, ?)", staged)
            report["carriers"].append({**c, "field": field, "value_kind": kind_id, **counts})
        report.update(format=FORMAT, plan=_units(plan).get("id"), to=_units(plan)["to"],
                      **{"from": _units(plan)["from"]}, rows=n,
                      seconds=round(time.monotonic() - started, 1))
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [(k, json.dumps(v, sort_keys=True)) for k, v in report.items()])
        db.commit()
    tmp.replace(out)
    return report


def _meta(db: sqlite3.Connection) -> dict[str, Any]:
    return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}


def apply(stage_path: str | Path, dsn: str, *, undo_log: str | None = None,
          chunk: int = CHUNK) -> dict[str, Any]:
    """The staged rows inserted (module docstring)."""
    import psycopg
    from operating.pg import migrate_chunks as mc
    from operating.pg import ontology_migrate as om

    started = time.monotonic()
    report: dict[str, Any] = {"chunks": 0, "cells": 0, "identifiers": 0, "skipped": 0,
                              "resumed": 0}
    with closing(sqlite3.connect(stage_path)) as db:
        meta = _meta(db)
        if meta.get("format") != FORMAT:
            raise ValueError("stage_format")
        done = {r[0] for r in db.execute("SELECT chunk FROM chunks")}
        last = db.execute("SELECT coalesce(max(n), 0) FROM rows").fetchone()[0]
        bounds = list(range(0, last, chunk))
        with psycopg.connect(dsn) as pg:
            def begin(cur: Any) -> None:
                cur.execute(om.LOCK)
                if om.served_schema(cur) != meta["from"]:
                    raise ValueError(f"served_schema_differs:{om.served_schema(cur)}")
                if undo_log is not None:
                    flag = mc._flag(cur)
                    if not flag or flag.get("undo") != undo_log:
                        raise ValueError("undo_log_not_of_the_running_migration")
                    mc.log_into(cur, undo_log)

            for low in bounds:
                index = low // chunk
                if index in done:
                    report["resumed"] += 1
                    continue
                rows = db.execute(
                    "SELECT r.src, r.field, r.kind, r.value, r.raw, r.object_id, r.l1_handle,"
                    " r.field_path, r.lineage, r.recorded_at, r.superseded_at, r.carrier "
                    "FROM rows r WHERE r.n > ? AND r.n <= ? ORDER BY r.n",
                    (low, low + chunk)).fetchall()
                carriers_ = meta["carriers"]
                with pg.cursor() as cur:
                    begin(cur)
                    by_binding: dict[str, list[tuple]] = {}
                    for row in rows:
                        by_binding.setdefault(carriers_[row[11]]["binding"], []).append(row)
                    cells = idents = 0
                    for binding, part in sorted(by_binding.items()):
                        cur.execute(
                            "INSERT INTO ontology.value_cells (src, field, kind, value) SELECT "
                            "x.src, x.field, x.kind, x.value FROM unnest(%s::bigint[], "
                            "%s::int[], %s::int[], %s::text[]) AS x(src, field, kind, value) "
                            "WHERE EXISTS (SELECT 1 FROM ontology.value_sources s WHERE "
                            "s.src = x.src AND s.binding_id = %s) AND NOT EXISTS (SELECT 1 "
                            "FROM ontology.value_cells c WHERE c.src = x.src AND "
                            "c.field = x.field) RETURNING src",
                            ([r[0] for r in part], [r[1] for r in part], [r[2] for r in part],
                             [r[3] for r in part], binding))
                        wrote = {r[0] for r in cur.fetchall()}
                        cells += len(wrote)
                        mine = [r for r in part if r[0] in wrote]
                        c = carriers_[part[0][11]]
                        cur.execute(
                            "INSERT INTO ontology.identifiers (identifier_kind, normalized, "
                            "raw, role, object_id, property, l1_handle, field_path, lineage, "
                            "markings, recorded_at, superseded_at) SELECT %s, x.v, x.raw, %s,"
                            " x.oid, %s, x.h, x.fp, x.lin, %s, x.rec, x.sup FROM unnest("
                            "%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], "
                            "%s::text[], %s::text[], %s::text[]) AS x(v, raw, oid, h, fp, lin,"
                            " rec, sup) ON CONFLICT DO NOTHING",
                            (c["kind"], c["role"], c["property"],
                             _marks(c["markings"]),
                             [r[3] for r in mine], [r[4] for r in mine], [r[5] for r in mine],
                             [r[6] for r in mine], [r[7] for r in mine], [r[8] for r in mine],
                             [r[9] for r in mine], [r[10] for r in mine]))
                        idents += cur.rowcount
                    pg.commit()
                skipped = len(rows) - cells
                db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                           (index, cells, idents, skipped,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                db.commit()
                report["chunks"] += 1
                report["cells"] += cells
                report["identifiers"] += idents
                report["skipped"] += skipped
            with pg.cursor() as cur:
                begin(cur)
                for c in meta["carriers"]:
                    cur.execute(
                        "INSERT INTO ontology.schema_migrations (id, from_digest, to_digest, "
                        "scope, report) VALUES (%s, 'reparse', %s, %s, %s) ON CONFLICT (id) "
                        "DO NOTHING", (om.reparse_id(meta["to"], c), f"reparse:{c['kind']}",
                                       json.dumps([f"{c['type']}:{c['binding']}:"]),
                                       json.dumps({**report, "carrier": c["binding"]})))
                pg.commit()
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def _marks(markings: Iterable[str]) -> str:
    from ontology import project
    return project._markings_text(frozenset(markings))


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.identifier_reparse")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "stage"):
        p = sub.add_parser(name)
        p.add_argument("--plan", required=True)
        p.add_argument("--dsn", default=None)
        if name == "stage":
            p.add_argument("--state-dir", required=True)
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--workers", type=int, default=None)
    a = sub.add_parser("apply")
    a.add_argument("--stage", required=True)
    a.add_argument("--dsn", default=None)
    a.add_argument("--undo-log", default=None)
    a.add_argument("--chunk", type=int, default=CHUNK)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.command == "apply":
        print(json.dumps(apply(args.stage, dsn, undo_log=args.undo_log, chunk=args.chunk),
                         sort_keys=True))
        return 0
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.command == "check":
        out = check(plan, dsn, workers=args.workers)
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    print(json.dumps(stage(plan, args.state_dir, args.out, dsn), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
