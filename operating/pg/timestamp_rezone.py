"""A zone declared on a timestamp source (C-09), applied to the Postgres ontology in place,
set-based, instead of re-projecting every unit holding it.

C-09 declares how each timestamp source writes time (``naive_zone``: Prisma's
``timestamp without time zone`` is UTC; ``wall_clock_zone``: the Portal's FileMaker ETL
writes Eastern wall time labelled UTC; ``ontology.project._timestamp``). Only the text of
those timestamps changes: no object, identifier, link or other value moves. The plan
(``ontology.step_migrate.rezone_carriers``) names them in ``units.timestamp_rezone``
(``values``: a property's source; ``events``: an event type's time) and keeps their units
out of the unit layer; the layer's final chunk and the plain apply refuse until this phase
has applied (``ontology_migrate.rezone_pending``).

The new text of a served cell is the projection's own parse of its stored text under the
declared zone (``project._timestamp(stored, zone)``):

- ``naive_zone``: a stored zoneless text (the old projection kept a zoneless source value
  as written) becomes that instant in UTC; a stored instant (the source wrote a zone) is
  kept, as the new projection keeps it.
- ``wall_clock_zone``: the stored text's wall digits read in the zone. That equals the
  projection of the captured value only when the source wrote UTC (``Z``/``+00:00``) or no
  zone, so the old projection kept the wall digits: ``check`` proves it over every
  captured row of each wall-clock relation in the Portal mirror (current and superseded),
  and refuses otherwise. A wall time the zone skips or repeats (the DST change hour) is the
  projection's gap ``wall_time_ambiguous``: the cell (or event, with its event_objects) is
  deleted, never guessed.

``python3 -m operating.pg.timestamp_rezone check --plan PLAN --state-dir STATE [--dsn DSN]``:
read-only. Per carrier: the cells (events) that change, stay, are ambiguous or unparsed;
the wall-clock proof over the mirror; a sample of served cells re-read from their capture
by handle, whose projection must equal the new text. Exit 1 on any failure.

``... stage --plan PLAN --out STAGE [--dsn DSN]``: read-only; every changing
cell and event with its old and new text (a SQLite file).

``... apply --stage STAGE [--dsn DSN] [--undo-log NAME] [--chunk N]``: the staged rows
written in chunks, each its own transaction under the delta's lock, only while the served
schema is the plan's ``from`` and the row still holds its old text (a rerun skips what is
done: resumable). ``--undo-log`` logs every write into the run's ``migrate_chunks`` undo
log, so the train's undo takes them back. The last transaction records
``ontology_migrate.rezone_id`` per carrier.

``... undo --stage STAGE [--dsn DSN]``: without an undo log, the staged old text put back
where the new text stands, deleted rows re-inserted (one transaction).

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

FORMAT = "plx-timestamp-rezone/v1"
CHUNK = 20_000
SAMPLE = 200          # served cells per carrier re-read from their capture by handle
# what a wall-clock source may have written for the stored wall digits to be the wall time:
# UTC, or no zone at all
_UTC_OFFSET = re.compile(r"(?:Z|[+-]00(?::?00)?)?")
# the zone a timestamp's text ends with, after its time of day (a date alone has none)
_ZONE_TAIL = re.compile(r"[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)$")


def _units(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    return plan["units"] if "units" in plan and "format" in plan else plan


def carriers(plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    part = _units(plan).get("timestamp_rezone") or {}
    out = {"values": list(part.get("values") or ()), "events": list(part.get("events") or ())}
    for c in out["values"] + out["events"]:
        zone = c.get("zone") or {}
        if len(zone) != 1 or not set(zone) <= {"naive_zone", "wall_clock_zone"}:
            raise ValueError(f"rezone_zone_invalid:{c.get('binding')}:{c.get('path')}")
    return out


def rezoned(stored: str | None, zone: Mapping[str, str]) -> tuple[str, str | None]:
    """(``same``|``changed``|``ambiguous``|``unparsed``, new text) of one stored timestamp
    text under ``zone`` (module docstring): the projection's own parse."""
    from ontology import project
    if stored is None:
        return "same", None
    text, problem = project._timestamp(stored, zone)
    if problem == "wall_time_ambiguous":
        return "ambiguous", None
    if problem is not None:
        return "unparsed", stored   # a date-only event time, never a timestamp: kept
    return ("same" if text == stored else "changed"), text


def _like(text: str) -> str:
    """``text`` matched literally in a LIKE pattern."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fields(cur: Any, c: Mapping[str, Any]) -> list[int]:
    cur.execute("SELECT field FROM ontology.value_fields WHERE property = %s AND "
                "(field_path = %s OR field_path LIKE %s)",
                (c["property"], f"row.{c['path']}", f"rows.%.{_like(c['path'])}"))
    return sorted(r[0] for r in cur.fetchall())


def _event_paths(c: Mapping[str, Any]) -> tuple[str, str]:
    return f"row.{c['path']}", f"rows.%.{_like(c['path'])}"


def _value_rows(pg: Any, c: Mapping[str, Any], name: str, batch: int) -> Iterable[list[tuple]]:
    with pg.cursor() as cur:
        fields = _fields(cur, c)
    if not fields:
        return
    with pg.cursor(name=name) as cur:
        cur.itersize = batch
        cur.execute("SELECT c.src, c.field, c.value, c.kind, c.value_num, c.currency, "
                    "c.currency_citation FROM ontology.value_cells c JOIN "
                    "ontology.value_sources s ON s.src = c.src WHERE c.field = ANY(%s) AND "
                    "s.binding_id = %s AND c.value IS NOT NULL", (fields, c["binding"]))
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield rows


def _event_rows(pg: Any, c: Mapping[str, Any], name: str, batch: int) -> Iterable[list[tuple]]:
    exact, like = _event_paths(c)
    with pg.cursor(name=name) as cur:
        cur.itersize = batch
        cur.execute("SELECT to_jsonb(e.*), coalesce((SELECT jsonb_agg(to_jsonb(o.*)) FROM "
                    "ontology.event_objects o WHERE o.event_id = e.event_id), '[]') FROM "
                    "ontology.events e WHERE e.event_type = %s AND (e.field_path = %s OR "
                    "e.field_path LIKE %s) AND e.time IS NOT NULL",
                    (c["event_type"], exact, like))
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield rows


def _served_from(cur: Any, plan: Mapping[str, Any]) -> None:
    from operating.pg.ontology_migrate import served_schema
    served = served_schema(cur)
    if served != _units(plan)["from"]:
        raise ValueError(f"served_schema_differs:{served}")


# ------------------------------------------------------------------ the captured values

def _mirror(state_dir: str | Path) -> Path:
    return Path(state_dir) / "case-runtime" / "source-mirror.sqlite"


def _raw_of(row_json: str, path: str) -> tuple[bool, Any]:
    from ontology import project
    try:
        row = json.loads(row_json)
    except ValueError:
        return False, None
    return project._cell(row, path) if isinstance(row, Mapping) else (False, None)


def wall_proof(mirror: sqlite3.Connection, relation: str, path: str) -> dict[str, Any]:
    """Every captured value of ``path`` in ``relation`` (current and superseded rows):
    how many carry a zone other than UTC (the stored wall digits would not be the wall
    time: refused)."""
    out = {"values": 0, "utc_or_none": 0, "other_offset": 0, "sample_other": []}
    for table in ("row_current", "row_version"):
        for (row_json,) in mirror.execute(f"SELECT row_json FROM {table} WHERE relation = ?",
                                          (relation,)):
            present, raw = _raw_of(row_json, path)
            if not present or not isinstance(raw, str) or not raw.strip():
                continue
            out["values"] += 1
            tail = _ZONE_TAIL.search(raw.strip())
            if tail is None or _UTC_OFFSET.fullmatch(tail[1]):
                out["utc_or_none"] += 1
            else:
                out["other_offset"] += 1
                if len(out["sample_other"]) < 3:
                    out["sample_other"].append(tail[1])
    return out


def _captured(mirror: sqlite3.Connection, handle: str, field_path: str
              ) -> tuple[bool, Any]:
    """The captured value a served cell cites (its version's handle and field path), read
    from the Portal mirror; (False, None) when the mirror no longer holds that capture."""
    from integrations import source_mirror
    try:
        relation, key_json, _, capture_id = source_mirror.parse_citation(handle)
    except ValueError:
        return False, None
    head, _, path = field_path.partition(".")
    run = 0
    if head == "rows":
        index, _, path = path.partition(".")
        run = int(index)
    for table in ("row_current", "row_version"):
        got = mirror.execute(f"SELECT row_json FROM {table} WHERE relation = ? AND key_json = ?"
                             " AND capture_id = ? AND run_index = ?",
                             (relation, key_json, capture_id, run)).fetchone()
        if got is not None:
            return _raw_of(got[0], path)
    return False, None


# ------------------------------------------------------------------ check

def check(plan: Mapping[str, Any], dsn: str, state_dir: str | Path, *,
          sample: int = SAMPLE, batch: int = 50_000, seed: int = 13) -> dict[str, Any]:
    """Read-only (module docstring)."""
    import psycopg

    parts = carriers(plan)
    out: dict[str, Any] = {"values": [], "events": [], "ok": True}
    rng = random.Random(seed)
    mirror = sqlite3.connect(f"file:{_mirror(state_dir)}?mode=ro", uri=True)
    with closing(mirror), psycopg.connect(dsn) as pg:
        pg.read_only = True
        with pg.cursor() as cur:
            _served_from(cur, plan)
        for n, c in enumerate(parts["values"]):
            started = time.monotonic()
            counts = {"same": 0, "changed": 0, "ambiguous": 0, "unparsed": 0}
            picks: list[tuple[int, int, str]] = []
            seen = 0
            for rows in _value_rows(pg, c, f"rezone_check_v{n}", batch):
                for src, field, value, *_ in rows:
                    status, _new = rezoned(value, c["zone"])
                    counts[status] += 1
                    seen += 1
                    # reservoir sample of the served cells, for the capture re-read
                    if len(picks) < sample:
                        picks.append((src, field, value))
                    elif rng.random() < sample / seen:
                        picks[rng.randrange(sample)] = (src, field, value)
            report = {**c, **counts, "cells": seen}
            if "wall_clock_zone" in c["zone"]:
                report["wall_proof"] = wall_proof(mirror, c["relation"], c["path"])
            report["sample"] = _sample_check(pg, mirror, c, picks)
            report["seconds"] = round(time.monotonic() - started, 1)
            report["ok"] = (counts["unparsed"] == 0
                            and not report.get("wall_proof", {}).get("other_offset")
                            and report["sample"]["unequal"] == 0)
            out["values"].append(report)
            out["ok"] = out["ok"] and report["ok"]
        for n, c in enumerate(parts["events"]):
            started = time.monotonic()
            counts = {"same": 0, "changed": 0, "ambiguous": 0, "unparsed": 0}
            for rows in _event_rows(pg, c, f"rezone_check_e{n}", batch):
                for event, _objects in rows:
                    counts[rezoned(event["time"], c["zone"])[0]] += 1
            report = {**c, **counts}
            if "wall_clock_zone" in c["zone"]:
                report["wall_proof"] = wall_proof(mirror, c["relation"], c["path"])
            report["seconds"] = round(time.monotonic() - started, 1)
            # an event time the parser reads as a date (no time of day) stays a date
            report["ok"] = not report.get("wall_proof", {}).get("other_offset")
            out["events"].append(report)
            out["ok"] = out["ok"] and report["ok"]
    return out


def _sample_check(pg: Any, mirror: sqlite3.Connection, c: Mapping[str, Any],
                  picks: Sequence[tuple[int, int, str]]) -> dict[str, Any]:
    """Each sampled served cell's captured value projected under the new zone
    (``project._timestamp``) equals its rezoned stored text."""
    from ontology import project
    out = {"sampled": len(picks), "equal": 0, "unequal": 0, "capture_gone": 0,
           "unequal_examples": []}
    if not picks:
        return out
    with pg.cursor() as cur:
        cur.execute("SELECT x.src, x.field, v.l1_handle, f.field_path FROM unnest(%s::bigint[],"
                    " %s::int[]) AS x(src, field) JOIN ontology.value_sources s ON s.src = "
                    "x.src JOIN ontology.object_versions v ON v.ver = s.ver JOIN "
                    "ontology.value_fields f ON f.field = x.field",
                    ([p[0] for p in picks], [p[1] for p in picks]))
        where = {(src, field): (handle, path) for src, field, handle, path in cur.fetchall()}
    for src, field, stored in picks:
        handle, path = where.get((src, field), (None, None))
        present, raw = _captured(mirror, handle, path) if handle else (False, None)
        if not present:
            out["capture_gone"] += 1
            continue
        text, problem = project._timestamp(raw, c["zone"]) if raw is not None else (None, None)
        status, new = rezoned(stored, c["zone"])
        want = None if problem == "wall_time_ambiguous" else text
        if (new if status != "ambiguous" else None) == want:
            out["equal"] += 1
        else:
            out["unequal"] += 1
            if len(out["unequal_examples"]) < 3:
                out["unequal_examples"].append({"stored": stored, "captured_shape":
                                                re.sub(r"\d", "9", str(raw))})
    return out


# ------------------------------------------------------------------ stage

_STAGE_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # one changing cell: new NULL = delete (an ambiguous wall time); row = the cell's
    # columns (put back by undo)
    "CREATE TABLE cells (n INTEGER PRIMARY KEY, carrier INTEGER NOT NULL, src INTEGER NOT "
    "NULL, field INTEGER NOT NULL, old TEXT NOT NULL, new TEXT, row TEXT NOT NULL)",
    "CREATE TABLE events (n INTEGER PRIMARY KEY, carrier INTEGER NOT NULL, event_id TEXT NOT "
    "NULL, old TEXT NOT NULL, new TEXT, row TEXT NOT NULL, objects TEXT NOT NULL)",
    "CREATE TABLE chunks (part TEXT NOT NULL, chunk INTEGER NOT NULL, written INTEGER NOT "
    "NULL, skipped INTEGER NOT NULL, at TEXT NOT NULL, PRIMARY KEY (part, chunk))")


def stage(plan: Mapping[str, Any], out: str | Path, dsn: str, *,
          batch: int = 50_000) -> dict[str, Any]:
    """Read-only on Postgres (module docstring)."""
    import psycopg

    started = time.monotonic()
    out = Path(out)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    parts = carriers(plan)
    report: dict[str, Any] = {"values": [], "events": []}
    with closing(sqlite3.connect(tmp)) as db, psycopg.connect(dsn) as pg:
        pg.read_only = True
        for ddl in _STAGE_DDL:
            db.execute(ddl)
        with pg.cursor() as cur:
            _served_from(cur, plan)
        n = 0
        for index, c in enumerate(parts["values"]):
            counts = {"same": 0, "changed": 0, "ambiguous": 0, "unparsed": 0}
            for rows in _value_rows(pg, c, f"rezone_stage_v{index}", batch):
                staged = []
                for src, field, value, kind, num, currency, citation in rows:
                    status, new = rezoned(value, c["zone"])
                    counts[status] += 1
                    if status in ("changed", "ambiguous"):
                        n += 1
                        staged.append((n, index, src, field, value, new, json.dumps(
                            [src, field, kind, value, None if num is None else str(num),
                             currency, citation])))
                db.executemany("INSERT INTO cells VALUES (?, ?, ?, ?, ?, ?, ?)", staged)
            if counts["unparsed"]:
                raise ValueError(f"rezone_unparsed:{c['binding']}:{c['path']}:"
                                 f"{counts['unparsed']}")
            report["values"].append({**c, **counts})
        m = 0
        for index, c in enumerate(parts["events"]):
            counts = {"same": 0, "changed": 0, "ambiguous": 0, "unparsed": 0}
            for rows in _event_rows(pg, c, f"rezone_stage_e{index}", batch):
                staged = []
                for event, objects in rows:
                    status, new = rezoned(event["time"], c["zone"])
                    counts[status] += 1
                    if status in ("changed", "ambiguous"):
                        m += 1
                        staged.append((m, index, event["event_id"], event["time"], new,
                                       json.dumps(event, sort_keys=True),
                                       json.dumps(objects, sort_keys=True)))
                db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)", staged)
            report["events"].append({**c, **counts})
        units = _units(plan)
        report.update(format=FORMAT, plan=units.get("id"), to=units["to"],
                      **{"from": units["from"]}, cells=n, event_rows=m,
                      seconds=round(time.monotonic() - started, 1))
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [(k, json.dumps(v, sort_keys=True)) for k, v in report.items()])
        db.commit()
    tmp.replace(out)
    return report


def _meta(db: sqlite3.Connection) -> dict[str, Any]:
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
    if meta.get("format") != FORMAT:
        raise ValueError("stage_format")
    return meta


# ------------------------------------------------------------------ apply / undo

def apply(stage_path: str | Path, dsn: str, *, undo_log: str | None = None,
          chunk: int = CHUNK) -> dict[str, Any]:
    """The staged rows written (module docstring)."""
    import psycopg
    from operating.pg import migrate_chunks as mc
    from operating.pg import ontology_migrate as om

    started = time.monotonic()
    report = {"chunks": 0, "cells_updated": 0, "cells_deleted": 0, "events_updated": 0,
              "events_deleted": 0, "event_objects_deleted": 0, "skipped": 0, "resumed": 0}
    with closing(sqlite3.connect(stage_path)) as db:
        meta = _meta(db)
        done = {(p, k) for p, k in db.execute("SELECT part, chunk FROM chunks")}
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

            def chunks(table: str) -> Iterable[tuple[int, list[tuple]]]:
                last = db.execute(f"SELECT coalesce(max(n), 0) FROM {table}").fetchone()[0]
                for low in range(0, last, chunk):
                    index = low // chunk
                    if (table, index) in done:
                        report["resumed"] += 1
                        continue
                    yield index, db.execute(f"SELECT * FROM {table} WHERE n > ? AND n <= ? "
                                            "ORDER BY n", (low, low + chunk)).fetchall()

            def finish(table: str, index: int, written: int, skipped: int) -> None:
                db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                           (table, index, written, skipped,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                db.commit()
                report["chunks"] += 1
                report["skipped"] += skipped

            for index, rows in chunks("cells"):
                upd = [r for r in rows if r[5] is not None]
                dele = [r for r in rows if r[5] is None]
                with pg.cursor() as cur:
                    begin(cur)
                    # only a cell still holding its old text: a delta under the served
                    # schema rewrote nothing of it since the stage (else: skipped)
                    cur.execute("UPDATE ontology.value_cells c SET value = x.new FROM unnest("
                                "%s::bigint[], %s::int[], %s::text[], %s::text[]) AS x(src, "
                                "field, old, new) WHERE c.src = x.src AND c.field = x.field "
                                "AND c.value = x.old",
                                ([r[2] for r in upd], [r[3] for r in upd],
                                 [r[4] for r in upd], [r[5] for r in upd]))
                    a = cur.rowcount
                    cur.execute("DELETE FROM ontology.value_cells c USING unnest(%s::bigint[],"
                                " %s::int[], %s::text[]) AS x(src, field, old) WHERE c.src = "
                                "x.src AND c.field = x.field AND c.value = x.old",
                                ([r[2] for r in dele], [r[3] for r in dele],
                                 [r[4] for r in dele]))
                    b = cur.rowcount
                    pg.commit()
                report["cells_updated"] += a
                report["cells_deleted"] += b
                finish("cells", index, a + b, len(rows) - a - b)
            for index, rows in chunks("events"):
                upd = [r for r in rows if r[4] is not None]
                dele = [r for r in rows if r[4] is None]
                with pg.cursor() as cur:
                    begin(cur)
                    cur.execute("UPDATE ontology.events e SET time = x.new FROM unnest("
                                "%s::text[], %s::text[], %s::text[]) AS x(id, old, new) WHERE "
                                "e.event_id = x.id AND e.time = x.old",
                                ([r[2] for r in upd], [r[3] for r in upd], [r[4] for r in upd]))
                    a = cur.rowcount
                    cur.execute("DELETE FROM ontology.events e USING unnest(%s::text[], "
                                "%s::text[]) AS x(id, old) WHERE e.event_id = x.id AND "
                                "e.time = x.old RETURNING e.event_id",
                                ([r[2] for r in dele], [r[3] for r in dele]))
                    gone = [r[0] for r in cur.fetchall()]
                    cur.execute("DELETE FROM ontology.event_objects WHERE event_id = ANY(%s)",
                                (gone,))
                    report["event_objects_deleted"] += cur.rowcount
                    pg.commit()
                report["events_updated"] += a
                report["events_deleted"] += len(gone)
                finish("events", index, a + len(gone), len(rows) - a - len(gone))
            with pg.cursor() as cur:
                begin(cur)
                for c in meta["values"] + meta["events"]:
                    cur.execute(
                        "INSERT INTO ontology.schema_migrations (id, from_digest, to_digest, "
                        "scope, report) VALUES (%s, 'rezone', %s, %s, %s) ON CONFLICT (id) "
                        "DO NOTHING", (om.rezone_id(meta["to"], c), f"rezone:{c['path']}",
                                       json.dumps([f"{c['type']}:{c['binding']}:"]),
                                       json.dumps({**report, "carrier": c["binding"]})))
                pg.commit()
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def undo(stage_path: str | Path, dsn: str) -> dict[str, Any]:
    """Without an undo log: every staged row back to its old text, deleted rows inserted
    again, the phase's records removed; one transaction (module docstring)."""
    import psycopg
    from operating.pg import ontology_migrate as om

    started = time.monotonic()
    out = {"cells_restored": 0, "cells_reinserted": 0, "events_restored": 0,
           "events_reinserted": 0}
    with closing(sqlite3.connect(stage_path)) as db, psycopg.connect(dsn) as pg, \
            pg.cursor() as cur:
        meta = _meta(db)
        cur.execute(om.LOCK)
        # once the unit layer has switched the served schema the new zones are the served
        # ones: putting old-zone text back would mix them (the train's undo log goes first)
        if om.served_schema(cur) != meta["from"]:
            raise ValueError(f"served_schema_differs:{om.served_schema(cur)}")
        cells = db.execute("SELECT src, field, old, new, row FROM cells").fetchall()
        upd = [r for r in cells if r[3] is not None]
        cur.execute("UPDATE ontology.value_cells c SET value = x.old FROM unnest(%s::bigint[], "
                    "%s::int[], %s::text[], %s::text[]) AS x(src, field, old, new) WHERE "
                    "c.src = x.src AND c.field = x.field AND c.value = x.new",
                    ([r[0] for r in upd], [r[1] for r in upd], [r[2] for r in upd],
                     [r[3] for r in upd]))
        out["cells_restored"] = cur.rowcount
        gone = [json.loads(r[4]) for r in cells if r[3] is None]
        if gone:
            cur.execute("INSERT INTO ontology.value_cells (src, field, kind, value, value_num,"
                        " currency, currency_citation) SELECT * FROM unnest(%s::bigint[], "
                        "%s::int[], %s::int[], %s::text[], %s::numeric[], %s::text[], "
                        "%s::text[]) ON CONFLICT DO NOTHING",
                        tuple([g[i] for g in gone] for i in range(7)))
            out["cells_reinserted"] = cur.rowcount
        events = db.execute("SELECT event_id, old, new, row, objects FROM events").fetchall()
        upd = [r for r in events if r[2] is not None]
        cur.execute("UPDATE ontology.events e SET time = x.old FROM unnest(%s::text[], "
                    "%s::text[], %s::text[]) AS x(id, old, new) WHERE e.event_id = x.id AND "
                    "e.time = x.new", ([r[0] for r in upd], [r[1] for r in upd],
                                       [r[2] for r in upd]))
        out["events_restored"] = cur.rowcount
        for event_id, _old, new, row, objects in events:
            if new is not None:
                continue
            cur.execute("INSERT INTO ontology.events SELECT * FROM jsonb_populate_record("
                        "NULL::ontology.events, %s::jsonb) ON CONFLICT DO NOTHING", (row,))
            out["events_reinserted"] += cur.rowcount
            for o in json.loads(objects):
                cur.execute("INSERT INTO ontology.event_objects SELECT * FROM "
                            "jsonb_populate_record(NULL::ontology.event_objects, %s::jsonb) "
                            "ON CONFLICT DO NOTHING", (json.dumps(o),))
        cur.execute("DELETE FROM ontology.schema_migrations WHERE id = ANY(%s)",
                    ([om.rezone_id(meta["to"], c) for c in meta["values"] + meta["events"]],))
        pg.commit()
    db2 = sqlite3.connect(stage_path)
    with closing(db2):
        db2.execute("DELETE FROM chunks")
        db2.commit()
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.timestamp_rezone")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--plan", required=True)
    c.add_argument("--state-dir", required=True)
    c.add_argument("--dsn", default=None)
    c.add_argument("--sample", type=int, default=SAMPLE)
    s = sub.add_parser("stage")
    s.add_argument("--plan", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--dsn", default=None)
    a = sub.add_parser("apply")
    a.add_argument("--stage", required=True)
    a.add_argument("--dsn", default=None)
    a.add_argument("--undo-log", default=None)
    a.add_argument("--chunk", type=int, default=CHUNK)
    u = sub.add_parser("undo")
    u.add_argument("--stage", required=True)
    u.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.command == "apply":
        print(json.dumps(apply(args.stage, dsn, undo_log=args.undo_log, chunk=args.chunk),
                         sort_keys=True))
        return 0
    if args.command == "undo":
        print(json.dumps(undo(args.stage, dsn), sort_keys=True))
        return 0
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.command == "check":
        out = check(plan, dsn, args.state_dir, sample=args.sample)
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1
    print(json.dumps(stage(plan, args.out, dsn), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
