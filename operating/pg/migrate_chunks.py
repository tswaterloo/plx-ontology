"""Apply a migration's unit and step layers to the Postgres ontology in chunks, writing
only the rows that differ from what Postgres serves, each chunk in its own transaction,
resumable.

Measured on the live host (the lp2 migration, 2026-11-08): ``ontology_migrate apply``
took 5509 s in one transaction (the units: deleting 523 s, inserting 2316 s; the steps
2473 s), every affected row deleted and copied in again although most read alike (of
12,000 sampled documents' mention links, 84 % are identical under the new code).

``python3 -m operating.pg.migrate_chunks apply [LAYER] [--steps STEPS] [--dsn DSN]``
ends with what ``ontology_migrate apply`` leaves: every logical row
(``scripts.schema_migration_proof.logical_rows``), the served schema and step code, the
migrations' records and the objects queued to be projected again are the one
transaction's. It goes in this order, each chunk under the delta's advisory lock and
recorded in ``ontology.load_state`` (``migration_chunk:<key>:<chunk>``) in its own
transaction, so a rerun of the same command skips the chunks done:

1. ``events``: the events the served schema made from the affected bindings' versions
   deleted, the layer's events of its objects copied in (``_apply_units``'s, whole);
2. ``links:<n>``, one per link unit the layer rewrites (``delete.links``): the served
   links of the unit compared with the layer's, by link id and every column as the
   ``links`` view reads them; only the differing ones deleted or written;
   ``links:rest``: the layer's links of no such unit, written (the plain apply copies
   every layer link);
3. ``units:<n>``, one per group of the affected bindings that select the same objects
   (type and id prefix): each object's rows (object, versions, removals, identifiers,
   values) compared with the layer's; the objects that differ, or that only one side
   holds, deleted and copied in (``ontology_delta._DELETE`` and ``_insert``, their links
   left to step 2), then the links of the objects the migration drops, the text index's
   and the search tables' copies of the group's permissions (``ontology_migrate._dropped``,
   ``_text_index``, ``_search``, over every object the group held); ``units:new``: the
   objects of the bindings the migration adds, copied in;
4. ``steps:<n>`` per re-run step's owned links (its prefix, from its from types), as in
   step 2; ``steps:rest``; ``steps:values``: the steps' values (``_apply_step_values``);
   ``steps:text_marks``: the text fields of the documents a marks-only text index change
   marks get those markings (``ontology_migrate._apply_text_marks``). An owned entry
   naming ``objects`` (a table of the step layer: the documents an in-place step run
   re-derived, ``ontology.pg_step_inplace``) scopes its chunk to the links from those
   objects; such a layer's ``from`` carries :data:`SCOPED` first, so the plain apply
   (which would read the entry as the whole prefix) refuses it as of other step code;
   ``steps:staged:<step>``: a step whose difference to the served links the run staged in
   Postgres (``operating.pg.step_staging``, a layer whose ``from`` carries :data:`STAGED`):
   its removed and replaced links deleted and its new ones inserted, set-based;
5. ``final``: the objects a delta replaced since either snapshot queued again, the served
   schema and step code switched, the migrations recorded, the chunk records removed.

While it runs ``store_meta.migration_in_progress`` names it (a reader sees a migration
half applied); the delta must stay paused (``/data/scratch/cfg/fast_build.sh`` pauses it):
the schema switches only in the last chunk. A chunk that refuses (``Refused``) rolls back
alone; the refusals the plain apply can meet are checked before the first chunk.
``--workers N`` applies the chunks of one wave (``waves``: the link units' chunks, the unit
groups, the steps' scopes, each set touching disjoint rows) in N processes at once, every
chunk still its own transaction and record; the coordinator holds the delta's lock as a
session lock throughout and runs every other chunk, ``final`` last, alone and in order.
``--stop-file`` stops a run between chunks. ``... measure`` compares without writing (one read-only transaction per chunk) and reports
what each chunk would write.

Why a module of its own: ``operating.pg.ontology_migrate`` and ``ontology_delta`` are
derivation code (the kept fields' key); nothing they cover imports this module.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from operating.pg import ontology_migrate as om

FLAG = "migration_in_progress"
SCOPED = "objects-scoped:"   # a step layer whose owned entries may name ``objects``
STAGED = "staged:"   # a step layer whose links are staged in Postgres (operating.pg.step_staging)
PREFIX = "migration_chunk:"
_BATCH = 20_000


class ChunkRefused(RuntimeError):
    """The chunked apply cannot run (another migration in progress, a layer object no
    binding selects, a refusal found before the first chunk): nothing written."""


def _hash(row: Iterable[Any]) -> int:
    text = "\x1f".join("\x00" if v is None else str(v) for v in row)
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=16).digest(), "big")


def _end(prefix: str) -> str | None:
    if not prefix:
        return None
    from ontology.step_migrate import prefix_end
    return prefix_end(prefix)


# ----------------------------------------------------------------- what the chunks are

def unit_groups(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The affected bindings in groups selecting disjoint objects: one type and the
    shortest of prefixes each starting with it, in (type, prefix) order."""
    out: list[dict[str, Any]] = []
    for bid, spec in sorted(record["delete"]["bindings"].items(),
                            key=lambda kv: (kv[1]["type"], kv[1]["prefix"], kv[0])):
        last = out[-1] if out else None
        if last is not None and last["type"] == spec["type"] and \
                spec["prefix"].startswith(last["prefix"]):
            last["bindings"].append(bid)
            continue
        out.append({"type": spec["type"], "prefix": spec["prefix"], "bindings": [bid]})
    return out


def chunks(record: Mapping[str, Any] | None, steps: Mapping[str, Any] | None) -> list[str]:
    out: list[str] = []
    if record is not None:
        out.append("events")
        out += [f"links:{n}" for n in range(len(record["delete"]["links"]))]
        out.append("links:rest")
        out += [f"units:{n}" for n in range(len(unit_groups(record)))]
        out.append("units:new")
    if steps is not None:
        out += [f"steps:{n}" for n in range(len(steps["owned"]))]
        out += [f"steps:staged:{name}" for name in sorted(steps.get("staged") or ())]
        out.append("steps:rest")
        if steps.get("values"):
            out.append("steps:values")
        if steps.get("text_marks"):
            out.append("steps:text_marks")
    out.append("final")
    return out


def migration_key(record: Mapping[str, Any] | None, steps: Mapping[str, Any] | None) -> str:
    return f"{record['id'] if record else '-'}+{steps['id'] if steps else '-'}"


# ----------------------------------------------------------------- links, compared

def _served_links(cur: Any, where: str, params: Sequence[Any]) -> Iterator[tuple]:
    """The served links ``where`` holds (on ``ontology.link_rows l``), as the ``links``
    view reads them (``step_migrate._read_links``'s formatting), as ``LINK_COLUMNS``
    tuples."""
    from ontology import step_migrate as sm
    from ontology import store_ddl
    ix = {c: i for i, c in enumerate(sm._RAW_LINK)}
    cols = ", ".join(f"l.{c}" for c in sm._RAW_LINK)
    plain = [ix[c] for c in store_ddl.LINK_COLUMNS]
    r_id, e_id, slots, ek = ix["reason_id"], ix["evidence_id"], ix["slots"], ix["ek"]
    reason, evidence = ix["reason"], ix["evidence_handles"]
    at_reason = store_ddl.LINK_COLUMNS.index("reason")
    at_evidence = store_ddl.LINK_COLUMNS.index("evidence_handles")
    split: dict[str, Any] = {}
    conn = cur.connection
    with conn.cursor(name=f"scope_links_{id(cur)}") as named:
        named.itersize = _BATCH
        named.execute(f"SELECT {cols} FROM ontology.link_rows l WHERE {where}", params)
        while True:
            rows = named.fetchmany(_BATCH)
            if not rows:
                return
            skel_ids = {r[c] for r in rows for c in (r_id, e_id) if r[c] is not None}
            text_ids = {t for r in rows if r[r_id] is not None or r[e_id] is not None
                        for t in r[slots]}
            skeletons = dict(cur.execute("SELECT id, text FROM ontology.link_skeletons WHERE "
                                         "id = ANY(%s::bigint[])", (sorted(skel_ids),)
                                         ).fetchall()) if skel_ids else {}
            texts = dict(cur.execute("SELECT id, text FROM ontology.link_texts WHERE "
                                     "id = ANY(%s::bigint[])", (sorted(text_ids),)
                                     ).fetchall()) if text_ids else {}
            for r in rows:
                row = [r[i] for i in plain]
                n = r[ek]
                if r[reason] is None:
                    row[at_reason] = sm._link_text(skeletons.get(r[r_id]), r[slots][n:64],
                                                   texts, split)
                if r[evidence] is None:
                    row[at_evidence] = sm._link_text(skeletons.get(r[e_id]), r[slots][:n],
                                                     texts, split)
                yield tuple(row)


def _diff_links(cur: Any, served: Iterator[tuple], layer: Iterable[tuple], write: bool,
                keys: dict[str, list[str]]) -> dict[str, int]:
    """Make the served links of one scope (``served``) the layer's (``layer``): delete
    those the layer does not hold alike, write those Postgres does not hold alike."""
    from ontology import store_ddl
    from operating.pg.ontology_delta import _copy
    held = {row[0]: _hash(row) for row in served}
    out = {"served": len(held), "layer": 0, "same": 0, "written": 0, "deleted": 0}
    pending: list[tuple] = []

    def flush() -> None:
        if pending and write:
            _copy(cur, "link_rows", store_ddl.LINK_COLUMNS, pending, keys=keys)
        out["written"] += len(pending)
        pending.clear()

    for row in layer:
        out["layer"] += 1
        h = held.pop(row[0], None)
        if h is not None and h == _hash(row):
            out["same"] += 1
            continue
        pending.append(tuple(row))
        if len(pending) >= _BATCH:
            flush()
    flush()
    gone = sorted(held)   # served, not in the layer (one the layer rewrote is replaced)
    out["deleted"] = len(gone)
    if write:
        for k in range(0, len(gone), _BATCH):
            cur.execute("DELETE FROM ontology.link_rows WHERE link_id = ANY(%s::text[])",
                        (gone[k:k + _BATCH],))
    return out


def _layer_links(conn: sqlite3.Connection, where: str, params: Sequence[Any]
                 ) -> Iterator[tuple]:
    from ontology import store_ddl
    for row in conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links "
                            f"WHERE {where}", params):
        yield tuple(row)


def _unit_link_scope(link: Mapping[str, Any]) -> tuple[str, list[Any], str, list[Any]]:
    """(Postgres where, params, SQLite where, params) of one link unit's links."""
    pg = ("l.link_type = %s AND l.binding_kind = %s AND starts_with(l.from_object, %s) "
          "AND starts_with(l.to_object, %s)")
    pgp = [link["link_type"], link["kind"], link["from"], link["to"]]
    lite, litep = ["link_type = ?", "binding_kind = ?"], [link["link_type"], link["kind"]]
    end = _end(link["from"])
    if end is not None:
        lite += ["from_object >= ?", "from_object < ?"]
        litep += [link["from"], end]
    lite.append("substr(to_object, 1, ?) = ?")
    litep += [len(link["to"]), link["to"]]
    return pg, pgp, " AND ".join(lite), litep


def steps_from(step_record: Mapping[str, Any]) -> str:
    """The served step code a step layer migrates from (:data:`SCOPED` taken off)."""
    held = step_record["from"]
    for marker in (SCOPED, STAGED):
        if held.startswith(marker):
            return held[len(marker):]
    return held


def _scope_objects(sconn: sqlite3.Connection, table: str) -> list[str]:
    """The object ids of the step layer's object table ``table``."""
    if not re.fullmatch(r"scope_objects_[0-9]+", table):
        raise ValueError(f"scope_objects_table:{table}")
    return [r[0] for r in sconn.execute(f"SELECT object_id FROM {table} ORDER BY object_id")]


def _step_scope(o: Mapping[str, Any], objects: Sequence[str] | None = None,
                c: str = ' COLLATE "C"') -> tuple[str, list[Any], str, list[Any]]:
    """(Postgres where, params, SQLite where, params) of one step's owned links
    (``objects``: the ids of the entry's object table, when it names one; ``c``: the
    collation of a byte-order key range, ``ontology.pg_byte_order.key_collate``)."""
    if o.get("objects"):   # the links from the layer's objects
        table = o["objects"]
        return (f"l.from_object = ANY(%s::text[]) AND l.link_id{c} >= %s AND "
                f"l.link_id{c} < %s", [list(objects or ()), o["prefix"], o["end"]],
                f"link_id >= ? AND link_id < ? AND from_object IN (SELECT object_id FROM "
                f"{table})", [o["prefix"], o["end"]])
    pg = (f"l.link_id{c} >= %s AND l.link_id{c} < %s AND "
          "(%s::text[] IS NULL OR EXISTS (SELECT 1 FROM unnest(%s::text[]) f WHERE "
          "starts_with(l.from_object, f)))")
    pgp = [o["prefix"], o["end"], o["from"], o["from"]]
    lite, litep = "link_id >= ? AND link_id < ?", [o["prefix"], o["end"]]
    if o["from"] is not None:
        lite += " AND (" + " OR ".join("substr(from_object, 1, ?) = ?"
                                       for _ in o["from"]) + ")"
        litep += [x for f in o["from"] for x in (len(f), f)]
    return pg, pgp, lite, litep


def _in_unit_scope(row: Sequence[Any], table: Mapping[tuple[str, str], list[tuple[str, str]]]
                   ) -> bool:
    return any(row[2].startswith(f) and row[3].startswith(t)
               for f, t in table.get((row[1], row[5]), ()))


def _in_step_scope(row: Sequence[Any], owned: Sequence[Mapping[str, Any]],
                   objects: Mapping[str, set[str]] | None = None) -> bool:
    return any(o["prefix"] <= row[0] < o["end"] and
               (row[2] in (objects or {}).get(o["objects"], ()) if o.get("objects") else
                o["from"] is None or any(row[2].startswith(f) for f in o["from"]))
               for o in owned)


def _rest(cur: Any, conn: sqlite3.Connection, inside: Any, write: bool,
          keys: dict[str, list[str]]) -> dict[str, int]:
    """The layer's links of no scope, written (as the plain apply's every-link copy)."""
    from ontology import store_ddl
    from operating.pg.ontology_delta import _copy
    out = {"written": 0}
    pending: list[tuple] = []
    for row in conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links"):
        if inside(row):
            continue
        pending.append(tuple(row))
        if len(pending) >= _BATCH:
            if write:
                _copy(cur, "link_rows", store_ddl.LINK_COLUMNS, pending, keys=keys)
            out["written"] += len(pending)
            pending.clear()
    if pending and write:
        _copy(cur, "link_rows", store_ddl.LINK_COLUMNS, pending, keys=keys)
    out["written"] += len(pending)
    return out


# ----------------------------------------------------------------- objects, compared

def _object_tables() -> dict[str, tuple[str, ...]]:
    from ontology import store_ddl
    from operating.pg.ontology_load import TABLES
    return {"objects": tuple(TABLES["objects"].columns[1:]),
            "object_removals": tuple(TABLES["object_removals"].columns),
            "object_versions": tuple(TABLES["object_versions"].columns[1:]),
            "identifiers": tuple(TABLES["identifiers"].columns),
            "object_values": tuple(store_ddl.VALUE_COLUMNS)}


def _add(digests: dict[str, list[int]], table: str, oid: str, row: Sequence[Any]) -> None:
    held = digests.get(oid)
    if held is None:
        held = digests[oid] = [0, 0]
    held[0] += 1
    held[1] = (held[1] + _hash((table, *row))) % (1 << 128)


def _served_objects(cur: Any, group: Mapping[str, Any] | None,
                    ids: Sequence[str] = ()) -> dict[str, list[int]]:
    """Each served object of ``group`` (None: of ``ids``): (rows, digest) of its rows."""
    out: dict[str, list[int]] = {}
    if group is not None:
        sel = ("object_id IN (SELECT object_id FROM ontology.objects WHERE object_type = %s "
               "AND starts_with(object_id, %s))")
        params: tuple[Any, ...] = (group["type"], group["prefix"])
    else:
        sel, params = "object_id = ANY(%s::text[])", (list(ids),)
    conn = cur.connection
    for table, cols in _object_tables().items():
        at = cols.index("object_id")
        with conn.cursor(name=f"objs_{table}_{id(cur)}") as named:
            named.itersize = _BATCH
            named.execute(f"SELECT {', '.join(cols)} FROM ontology.{table} WHERE {sel}",
                          params)
            for row in named:
                _add(out, table, row[at], row)
    return out


def _layer_objects(conn: sqlite3.Connection, group: Mapping[str, Any] | None,
                   ids: Sequence[str] = ()) -> dict[str, list[int]]:
    """Each layer object of ``group`` (None: of ``ids``): (rows, digest) of its rows."""
    from ontology import store as S
    out: dict[str, list[int]] = {}
    if group is not None:
        end = _end(group["prefix"])
        low = group["prefix"] or None
        rng = "object_id >= ? AND object_id < ?" if end is not None else "1"
        rp: list[Any] = [group["prefix"], end] if end is not None else []
        mine = {r[0] for r in conn.execute(f"SELECT object_id FROM objects WHERE "
                                           f"object_type = ? AND {rng}", [group["type"], *rp])}
    else:
        mine = set(ids)
        if not mine:
            return out
        low, top = min(mine), max(mine)
        end = top + "\x00"
        rng, rp = "object_id >= ? AND object_id < ?", [low, end]
    for table, cols in _object_tables().items():
        at = cols.index("object_id")
        if table == "object_values":
            for batch in S._value_rows(conn, low, end):
                for row in batch:
                    if row[at] in mine:
                        _add(out, table, row[at], row)
            continue
        select = {"objects": "SELECT {c} FROM objects", "object_removals":
                  "SELECT {c} FROM object_removals", "object_versions":
                  "SELECT {c} FROM object_versions", "identifiers":
                  "SELECT {c} FROM identifiers"}[table].format(c=", ".join(cols))
        for row in conn.execute(f"{select} WHERE {rng}", rp):
            if row[at] in mine:
                _add(out, table, row[at], row)
    for oid in mine:   # an object with no rows but its own is still the layer's
        out.setdefault(oid, [0, 0])
    return out


def _uncovered(conn: sqlite3.Connection, groups: Sequence[Mapping[str, Any]]
               ) -> list[tuple[str, str]]:
    """(object id, binding) of the layer's objects no group selects: the objects of the
    bindings the migration adds (the plain apply copies them in, deleting nothing)."""
    by_type: dict[str, list[str]] = {}
    for g in groups:
        by_type.setdefault(g["type"], []).append(g["prefix"])
    return sorted((oid, binding) for oid, otype, binding in conn.execute(
        "SELECT object_id, object_type, binding_id FROM objects")
        if not any(oid.startswith(p) for p in by_type.get(otype, ())))


def _new_objects(cur: Any, conn: sqlite3.Connection, layer: Path, record: Mapping[str, Any],
                 groups: Sequence[Mapping[str, Any]], write: bool) -> dict[str, Any]:
    """The objects of the bindings the migration adds, copied in as the plain apply does
    (one Postgres already holds alike is left; one it holds otherwise is replaced), and the
    objects of a document binding the migration does not rewrite queued to be projected
    again (``ontology_migrate._text_index``: none of their versions was held before)."""
    from operating.pg.ontology_delta import _DELETE, _insert
    ids = [oid for oid, _ in _uncovered(conn, groups)]
    served = _served_objects(cur, None, ids)
    mine = _layer_objects(conn, None, ids)
    changed = sorted(o for o in mine if served.get(o) != mine[o])
    owned = [o for o in changed if o in served]
    out: dict[str, Any] = {"objects.layer": len(mine), "objects.same": len(mine) - len(changed),
                           "objects.added": len(changed) - len(owned),
                           "objects.changed": len(owned),
                           "rows.written": sum(mine[o][0] for o in changed)}
    extra = sorted(set(record.get("document_bindings") or ())
                   - set(record["delete"]["bindings"]))
    if not write:
        return out
    cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
    with cur.copy("COPY owned (object_id) FROM STDIN") as copy:
        for oid in owned:
            copy.write_row((oid,))
    for table, where in _DELETE:
        if table in om._TABLES:
            cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
    conn.execute("CREATE TEMP VIEW IF NOT EXISTS links AS SELECT * FROM main.links WHERE 0")
    try:
        out.update(_insert(conn, str(layer), cur, json.dumps(changed)))
    finally:
        conn.execute("DROP VIEW IF EXISTS temp.links")
    if extra:
        cur.execute("SELECT DISTINCT o.object_id FROM ontology.objects o JOIN "
                    "ontology.object_versions v ON v.object_id = o.object_id WHERE "
                    "o.binding_id = ANY(%s)", (extra,))
        out["text_index.reprojected"] = om.reproject(cur, sorted(r[0] for r in cur.fetchall()),
                                                     record["id"])
    return out


# ----------------------------------------------------------------- the chunks

def _events(cur: Any, conn: sqlite3.Connection, layer: Path, record: Mapping[str, Any],
            write: bool, keys: dict[str, list[str]]) -> dict[str, Any]:
    """``_apply_units``'s events: deleted by the served versions of every affected
    binding, the layer's copied in for every layer object."""
    from ontology.project import event_id_of
    from operating.pg.ontology_delta import _PLAIN, _copy
    from operating.pg.ontology_load import TABLES
    out: dict[str, Any] = {}
    ids: set[str] = set()
    for spec in record["delete"]["bindings"].values():
        if not spec["events"]:
            continue
        cur.execute("SELECT v.object_id, v.l1_handle FROM ontology.object_versions v "
                    "JOIN ontology.objects o ON o.object_id = v.object_id WHERE "
                    "o.object_type = %s AND starts_with(o.object_id, %s) AND "
                    "starts_with(v.object_id, %s)", (spec["type"], spec["prefix"],
                                                     spec["prefix"]))
        ids |= {event_id_of(eid, handle, oid, part)
                for oid, handle in cur.fetchall() for eid, part in spec["events"]}
    out["gone_events"] = len(ids)
    if not write:
        return out
    cur.execute("CREATE TEMP TABLE gone_events (event_id text PRIMARY KEY) ON COMMIT DROP")
    with cur.copy("COPY gone_events (event_id) FROM STDIN") as copy:
        for eid in ids:
            copy.write_row((eid,))
    for table in ("event_objects", "events"):
        cur.execute(f"DELETE FROM ontology.{table} WHERE event_id IN "
                    "(SELECT event_id FROM gone_events)")
        out[f"deleted.{table}"] = cur.rowcount
    every = json.dumps([r[0] for r in conn.execute("SELECT object_id FROM objects")])
    for table in ("events", "event_objects"):
        spec = TABLES[table]
        rows = conn.execute(f"SELECT * FROM ({spec.select}) WHERE {_PLAIN[table]}",
                            {"ids": every})
        out[f"copied.{table}"] = _copy(cur, table, spec.columns, rows, keys=keys)
    return out


def _unit_group(cur: Any, conn: sqlite3.Connection, layer: Path, record: Mapping[str, Any],
                group: Mapping[str, Any], write: bool) -> dict[str, Any]:
    from operating.pg.ontology_delta import _DELETE, _insert
    served = _served_objects(cur, group)
    mine = _layer_objects(conn, group)
    changed = sorted(o for o in set(served) | set(mine) if served.get(o) != mine.get(o))
    owned = [o for o in changed if o in served]
    ids = [o for o in changed if o in mine]
    out: dict[str, Any] = {
        "objects.served": len(served), "objects.layer": len(mine),
        "objects.same": len(set(served) & set(mine)) - len(set(owned) & set(ids)),
        "objects.changed": len(set(owned) & set(ids)), "objects.dropped": len(
            set(owned) - set(ids)), "objects.added": len(set(ids) - set(owned)),
        "rows.served": sum(v[0] for v in served.values()),
        "rows.layer": sum(v[0] for v in mine.values()),
        "rows.written": sum(mine[o][0] for o in ids),
        "rows.deleted": sum(served[o][0] for o in owned)}
    if not write:
        return out
    sel = ("SELECT o.object_id, o.binding_id, o.audience_key FROM ontology.objects o WHERE "
           "o.object_type = %s AND starts_with(o.object_id, %s)")
    params = (group["type"], group["prefix"])
    cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
    with cur.copy("COPY owned (object_id) FROM STDIN") as copy:
        for oid in owned:
            copy.write_row((oid,))
    cur.execute("ANALYZE owned")
    # every object the group held (the text index, the search tables, the dropped ones)
    cur.execute(f"CREATE TEMP TABLE prior ON COMMIT DROP AS {sel}", params)
    cur.execute("CREATE TEMP TABLE prior_versions ON COMMIT DROP AS SELECT DISTINCT "
                "v.object_id, v.l1_handle FROM ontology.object_versions v JOIN prior "
                "USING (object_id)")
    cur.execute("CREATE INDEX ON prior (object_id)")
    cur.execute("CREATE INDEX ON prior_versions (object_id, l1_handle)")
    cur.execute("ANALYZE prior")
    cur.execute("ANALYZE prior_versions")
    for table, where in _DELETE:
        if table in om._TABLES:
            cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
            out[f"deleted.{table}"] = cur.rowcount
    # the layer's links are the link chunks' (a view of none hides them from ``_insert``)
    conn.execute("CREATE TEMP VIEW IF NOT EXISTS links AS SELECT * FROM main.links WHERE 0")
    try:
        out.update(_insert(conn, str(layer), cur, json.dumps(ids)))
    finally:
        conn.execute("DROP VIEW IF EXISTS temp.links")
    mine_bindings = set(group["bindings"])
    scoped = {**record, "document_bindings": [
        b for b in record.get("document_bindings") or () if b in mine_bindings]}
    om._dropped(cur, out)
    om._text_index(cur, scoped, out)
    om._search(cur, scoped, out)
    return out


def _preflight(cur: Any, conn: sqlite3.Connection | None, record: Mapping[str, Any] | None
               ) -> None:
    """The refusals of the plain apply found before any chunk is written."""
    if record is None or conn is None:
        return
    groups = unit_groups(record)
    bindings = record["delete"]["bindings"]
    astray = sum(1 for _, binding in _uncovered(conn, groups) if binding in bindings)
    if astray:   # an object of a rewritten binding outside the objects it deletes
        raise ChunkRefused(f"layer_objects_outside_their_binding:{astray}")
    texted = set(record.get("document_bindings") or ())
    for spec in bindings.values():
        cur.execute("SELECT DISTINCT o.binding_id FROM ontology.objects o JOIN "
                    "ontology.text_fields f ON f.object_id = o.object_id WHERE "
                    "o.object_type = %s AND starts_with(o.object_id, %s)",
                    (spec["type"], spec["prefix"]))
        texted |= {r[0] for r in cur.fetchall()}
    for bid in sorted(texted & set(bindings)):
        if bindings[bid].get("text_paths_changed"):
            raise ChunkRefused(f"text_index_input:{bid}:text_paths")
        if bindings[bid].get("marks") is None:
            raise ChunkRefused(f"text_index_markings:{bid}")
    for bid in sorted(texted - set(bindings)):   # a texted object of no affected binding
        cur.execute("SELECT 1 FROM ontology.objects o JOIN ontology.text_fields f ON "
                    "f.object_id = o.object_id WHERE o.binding_id = %s AND EXISTS (SELECT 1 "
                    "FROM jsonb_each(%s::jsonb) b WHERE o.object_type = b.value->>'type' AND "
                    "starts_with(o.object_id, b.value->>'prefix')) LIMIT 1",
                    (bid, json.dumps(bindings)))
        if cur.fetchone() is not None:
            raise ChunkRefused(f"text_index_markings:{bid}")


# ----------------------------------------------------------------- the apply

# The delta's lock as a session lock: a parallel run's coordinator holds it from its first
# transaction to its end, so its workers (other sessions) write while the delta stays out.
SESSION_LOCK = "SELECT pg_advisory_lock(hashtext('ontology_delta'))"
_HELD = ("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND "
         "pid = %s AND objsubid = 1 AND classid = ((hashtext('ontology_delta')::bigint >> 32) "
         "& 4294967295)::oid AND objid = (hashtext('ontology_delta')::bigint & 4294967295)::oid)")
_ATTEMPTS = 5   # a parallel chunk that met another's lock or new dictionary row, run again


def waves(names: Sequence[str]) -> list[list[str]]:
    """The chunks in waves run in order; a wave's chunks may run at once. Together only:
    the link units' chunks with ``links:rest`` (disjoint scopes; ``links:rest`` is the
    layer's links of no unit), the unit groups ``units:<n>`` (disjoint objects; the links of
    the objects they drop were settled by the link wave before), the steps' owned scopes
    with ``steps:rest``. Every other chunk (``events``, ``units:new``, ``steps:values``,
    ``steps:text_marks``, ``final``, a name not known here) is a wave of its own, so what
    one reads that another writes keeps the serial order."""
    def kind(name: str) -> str | None:
        k, _, which = name.partition(":")
        if k == "links" and (which.isdigit() or which == "rest"):
            return "links"
        if k == "units" and which.isdigit():
            return "units"
        if k == "steps" and (which.isdigit() or which == "rest"):
            return "steps"
        return None
    out: list[tuple[str | None, list[str]]] = []
    for name in names:
        k = kind(name)
        if k is not None and out and out[-1][0] == k:
            out[-1][1].append(name)
        else:
            out.append((k, [name]))
    return [w for _, w in out]


class _Run:
    """What one chunk needs (the coordinator's, or a worker process's own)."""

    def __init__(self, layer: str | Path | None, steps: str | Path | None) -> None:
        self.record = self.step_record = None
        if layer is not None:
            from ontology.schema_migrate import read_layer
            self.record = read_layer(layer)
        if steps is not None:
            from ontology.step_migrate import read_layer as read_steps
            self.step_record = read_steps(steps)
        self.layer = Path(layer) if layer is not None else None
        self.steps = Path(steps) if steps is not None else None
        self.key = migration_key(self.record, self.step_record)
        self.groups = unit_groups(self.record) if self.record is not None else []
        self.conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True) \
            if layer is not None else None
        self.sconn = sqlite3.connect(f"file:{steps}?mode=ro", uri=True) \
            if steps is not None else None

    def close(self) -> None:
        for c in (self.conn, self.sconn):
            if c is not None:
                c.close()


def _one(pg: Any, run: _Run, name: str, write: bool, holder: int | None
         ) -> tuple[str, Any]:
    """Apply (or measure) chunk ``name`` in its own transaction on ``pg``: ("done", its
    report), ("skipped", None) when recorded done meanwhile, ("refused", reason).
    ``holder``: the backend holding the delta's lock for this (parallel) run, else the
    chunk takes it itself."""
    t = time.monotonic()
    with pg.cursor() as cur:
        if write:
            if holder is None:
                cur.execute(om.LOCK)
            elif not cur.execute(_HELD, (holder,)).fetchone()[0]:
                pg.rollback()   # the coordinator is gone: the delta could run, write nothing
                return "refused", "coordinator_lock_lost"
            cur.execute("SET LOCAL jit = off")
            refused = _checks(cur, run.record, run.step_record, run.key, write)
            if refused:
                pg.rollback()
                return "refused", refused
            if name in _done(cur, run.key):   # another run applied it meanwhile
                pg.rollback()
                return "skipped", None
        else:
            cur.execute("SET TRANSACTION READ ONLY")
        try:
            out = _chunk(cur, name, run.conn, run.sconn, run.layer, run.steps, run.record,
                         run.step_record, run.groups, run.key, write)
        except om.Refused as why:
            pg.rollback()
            return "refused", str(why)
        out["seconds"] = round(time.monotonic() - t, 3)
        if write and name != "final":
            cur.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                        (f"{PREFIX}{run.key}:{name}", json.dumps(out, sort_keys=True)))
    if write:
        pg.commit()
    else:
        pg.rollback()
    return "done", out


_WORKER: dict[str, Any] = {}


def _worker_start(layer: Any, steps: Any, dsn: str, holder: int) -> None:
    import psycopg
    _WORKER.update(run=_Run(layer, steps), pg=psycopg.connect(dsn), holder=holder)


def _worker_chunk(name: str, write: bool) -> tuple[str, str, Any]:
    """One chunk in a worker process. A deadlock, a serialization failure or a unique
    violation (two chunks adding the same new value field or kind at once) rolls the
    chunk back whole and runs it again (it compares anew, so it writes what is still
    missing); the last attempt's error is raised."""
    import psycopg
    pg = _WORKER["pg"]
    for attempt in range(_ATTEMPTS):
        try:
            status, out = _one(pg, _WORKER["run"], name, write,
                               _WORKER["holder"] if write else None)
            return name, status, out
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure,
                psycopg.errors.UniqueViolation):
            pg.rollback()
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")


def apply_chunked(layer: str | Path | None, dsn: str, *, steps: str | Path | None = None,
                  write: bool = True, log: Any = None, stop_after: int | None = None,
                  workers: int = 1, stop_file: str | Path | None = None) -> dict[str, Any]:
    """Apply (``write``) or measure the unit layer ``layer`` and the step layer ``steps``
    chunk by chunk (module docstring). ``stop_after``: return after that many chunks were
    applied by this call (a test's stop). ``workers`` > 1: the chunks of one wave
    (:func:`waves`) applied by that many processes at once, each chunk its own transaction
    and record as in the serial run, the delta's lock held by this (the coordinator's)
    session throughout; ``final`` and every chunk alone in its wave run here, in order.
    ``stop_file``: once it exists no chunk starts (those running finish), the run returns
    ``stopped`` and resumes when run again. Returns the report."""
    import psycopg

    run = _Run(layer, steps)
    record, step_record, key = run.record, run.step_record, run.key
    unit_id = record["id"] if record is not None else None
    report: dict[str, Any] = {"key": key, "chunks": {}, "write": write}
    if step_record is not None and step_record.get("unit") != unit_id:
        run.close()
        return {**report, "applied": False, "reason": "steps_of_another_unit_layer"}
    required = sorted((record or {}).get("steps_required") or ())
    if required and (step_record is None or set(required) - set(step_record["steps"])):
        run.close()
        return {**report, "applied": False, "reason": "step_migration_required"}
    names = chunks(record, step_record)
    started = time.monotonic()
    applied_now = 0
    pool = None

    def say(text: str) -> None:
        if log is not None:
            print(text, file=log, flush=True)

    def stopping(running: int = 0) -> bool:
        return (stop_after is not None and applied_now + running >= stop_after) or \
            (stop_file is not None and Path(stop_file).exists())

    def record_out(name: str, status: str, out: Any) -> dict[str, Any] | None:
        nonlocal applied_now
        if status == "refused":
            return {**report, "applied": False, "reason": out, "chunk": name}
        if status == "skipped":
            report["chunks"][name] = {"skipped": "done"}
            return None
        if write:
            applied_now += 1
        report["chunks"][name] = out
        say(f"chunk {names.index(name) + 1}/{len(names)} {name}: "
            f"{json.dumps(out, sort_keys=True)}")
        return None

    try:
        with psycopg.connect(dsn) as pg:
            with pg.cursor() as cur:
                if write:
                    cur.execute(om.LOCK)
                    if workers > 1:
                        cur.execute(SESSION_LOCK)
                else:
                    cur.execute("SET TRANSACTION READ ONLY")
                refused = _checks(cur, record, step_record, key, write)
                if refused:
                    pg.rollback()
                    return {**report, "applied": False, "reason": refused}
                done = _done(cur, key)
                if write and not done:
                    _preflight(cur, run.conn, record)
                    cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s)"
                                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                                (FLAG, json.dumps({"key": key, "chunks": len(names)})))
                holder = pg.info.backend_pid
            pg.commit()
            for wave in waves(names):
                todo = []
                for name in wave:
                    if name in done:
                        report["chunks"][name] = {"skipped": "done"}
                    elif write or name != "final":
                        todo.append(name)
                if workers <= 1 or len(todo) <= 1:
                    for name in todo:
                        if stopping():
                            return {**report, "applied": False, "reason": "stopped"}
                        status, out = _one(pg, run, name, write, None)
                        stop = record_out(name, status, out)
                        if stop is not None:
                            return stop
                    continue
                if pool is None:
                    import concurrent.futures as cf
                    import multiprocessing
                    pool = cf.ProcessPoolExecutor(
                        max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                        initializer=_worker_start,
                        initargs=(str(layer) if layer is not None else None,
                                  str(steps) if steps is not None else None, dsn, holder))
                stop = _wave(pool, workers, todo, write, stopping, record_out)
                if stop == "stopped":
                    return {**report, "applied": False, "reason": "stopped"}
                if stop is not None:
                    return stop
    except ChunkRefused as why:
        return {**report, "applied": False, "reason": str(why)}
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        run.close()
    report["seconds"] = round(time.monotonic() - started, 3)
    report["totals"] = _totals(report["chunks"])
    if write and applied_now:
        report["vacuum"] = vacuum_after_apply(dsn)
    return {**report, "applied": write}


# The tables a bulk apply rewrites most, whose index-only reads need a current visibility
# map (a hub's link count fell to heap reads at a 29% map, 11-11).
VACUUMED = ("link_rows", "value_cells", "object_versions", "objects", "identifiers")


def vacuum_after_apply(dsn: str, tables: Sequence[str] = VACUUMED) -> dict[str, Any]:
    """``VACUUM (ANALYZE)`` of ``tables`` once a bulk apply has written: their visibility
    map and statistics current before the next reader, not when autovacuum gets to them.
    Outside the apply's transactions; a table that fails (cancelled, a lock wait) is
    reported and left to autovacuum, the apply stands."""
    import psycopg
    out: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as pg:
        for table in tables:
            t = time.monotonic()
            try:
                pg.execute(f"VACUUM (ANALYZE) ontology.{table}")
                out[table] = {"seconds": round(time.monotonic() - t, 1)}
            except psycopg.Error as why:
                out[table] = {"error": type(why).__name__}
    return out


def _wave(pool: Any, width: int, todo: list[str], write: bool, stopping: Any,
          record_out: Any) -> dict[str, Any] | str | None:
    """The chunks ``todo`` on the pool, at most its workers at once. A refusal or an error
    starts no further chunk; those running finish (each its own transaction) before the
    refusal is returned or the error raised."""
    import concurrent.futures as cf
    queue = list(todo)
    running: set[Any] = set()
    first: dict[str, Any] | None = None
    error: BaseException | None = None
    while queue or running:
        while queue and first is None and error is None and len(running) < width and \
                not stopping(len(running)):
            running.add(pool.submit(_worker_chunk, queue.pop(0), write))
        if not running:
            break
        finished, running = cf.wait(running, return_when=cf.FIRST_COMPLETED)
        for f in finished:
            try:
                name, status, out = f.result()
            except BaseException as e:   # (a worker's error: raised once the others end)
                error = error or e
                continue
            stop = record_out(name, status, out)
            first = first or stop
    if error is not None:
        raise error
    if first is not None:
        return first
    return "stopped" if queue else None



def _checks(cur: Any, record: Any, step_record: Any, key: str, write: bool) -> str | None:
    """Why the migration cannot (go on) apply here, else None."""
    if om._whole(cur) is None:
        return "generation_loading"
    if not om._has_tables(cur):
        return "migration_0009_missing"
    for rec, prefix in ((record, ""), (step_record, "steps:")):
        if rec is None:
            continue
        cur.execute("SELECT 1 FROM ontology.schema_migrations WHERE id = %s",
                    (prefix + rec["id"],))
        if cur.fetchone() is not None:
            return "already_applied"
    if record is not None and om.served_schema(cur) != record["from"]:
        return "other_schema"
    if step_record is not None:
        held = om.served_steps(cur)
        if held is None:
            return "steps_baseline_unknown"
        if held != steps_from(step_record):
            return "other_steps"
    cur.execute("SELECT value FROM ontology.store_meta WHERE key = %s", (FLAG,))
    row = cur.fetchone()
    if row is not None and json.loads(row[0]).get("key") != key:
        return "another_migration_in_progress"
    return None


def _done(cur: Any, key: str) -> set[str]:
    cur.execute("SELECT substr(name, %s) FROM ontology.load_state WHERE starts_with(name, %s)",
                (len(f"{PREFIX}{key}:") + 1, f"{PREFIX}{key}:"))
    return {r[0] for r in cur.fetchall()}


def _chunk(cur: Any, name: str, conn: sqlite3.Connection | None,
           sconn: sqlite3.Connection | None, layer: Path | None, steps: Path | None,
           record: Any, step_record: Any, groups: Sequence[Mapping[str, Any]], key: str,
           write: bool) -> dict[str, Any]:
    from ontology.step_migrate import _drop_table
    keys: dict[str, list[str]] = {}
    kind, _, which = name.partition(":")
    if name == "events":
        return _events(cur, conn, layer, record, write, keys)
    if kind == "links" and which == "rest":
        table = _drop_table([(x["link_type"], x["kind"], x["from"], x["to"])
                             for x in record["delete"]["links"]])
        return _rest(cur, conn, lambda row: _in_unit_scope(row, table), write, keys)
    if kind == "links":
        pg, pgp, lite, litep = _unit_link_scope(record["delete"]["links"][int(which)])
        return _diff_links(cur, _served_links(cur, pg, pgp), _layer_links(conn, lite, litep),
                           write, keys)
    if name == "units:new":
        return _new_objects(cur, conn, layer, record, groups, write)
    if kind == "units":
        return _unit_group(cur, conn, layer, record, groups[int(which)], write)
    if kind == "steps" and which.startswith("staged:"):
        # set-based from the run's stage schema: its removed and replaced links deleted,
        # its new and replacing ones inserted (one statement each)
        from operating.pg import step_staging
        from ontology import store_ddl
        step = which[len("staged:"):]
        return step_staging.apply_step(cur, step, step_record["staged"][step], write,
                                       store_ddl.LINK_COLUMNS)
    if kind == "steps" and which == "rest":
        objects = {o["objects"]: set(_scope_objects(sconn, o["objects"]))
                   for o in step_record["owned"] if o.get("objects")}
        return _rest(cur, sconn, lambda row: _in_step_scope(row, step_record["owned"],
                                                            objects), write, keys)
    if kind == "steps" and which == "text_marks":
        # the text index's re-marking (``step_migrate.MARKS_ONLY``): markings only ever
        # added, so a rerun after a stop adds nothing twice
        out = {}
        if write:
            om._apply_text_marks(cur, steps, step_record["text_marks"], out)
        else:   # (measured: the documents named; the plain statements write temp tables)
            out["text_marks.documents"] = sconn.execute(
                f"SELECT count(*) FROM {_text_marks_table()}").fetchone()[0]
        return out
    if kind == "steps" and which == "values":
        out: dict[str, Any] = {}
        if write:   # (measured: nothing to compare, the plain statements)
            om._apply_step_values(cur, steps, step_record["values"], out)
        return out
    if kind == "steps":
        o = step_record["owned"][int(which)]
        from ontology.pg_byte_order import key_collate
        pg, pgp, lite, litep = _step_scope(
            o, _scope_objects(sconn, o["objects"]) if o.get("objects") else None,
            key_collate(cur))
        return _diff_links(cur, _served_links(cur, pg, pgp), _layer_links(sconn, lite, litep),
                           write, keys)
    if name == "final":
        return _final(cur, record, step_record, key)
    raise ValueError(f"unknown_chunk:{name}")


def _text_marks_table() -> str:
    from ontology.step_migrate import TEXT_MARKS
    return TEXT_MARKS


def _final(cur: Any, record: Any, step_record: Any, key: str) -> dict[str, Any]:
    """The schema and step code switched, the migrations recorded (with the chunks'
    totals), the objects a delta replaced since either snapshot queued again."""
    cur.execute("SELECT substr(name, %s), value FROM ontology.load_state WHERE "
                "starts_with(name, %s)", (len(f"{PREFIX}{key}:") + 1, f"{PREFIX}{key}:"))
    parts = {name: value for name, value in cur.fetchall()}
    out: dict[str, Any] = {}
    if record is not None:
        units = _totals({k: v for k, v in parts.items() if not k.startswith("steps")})
        units["reproject"] = om.reproject(cur, om._newer(cur, record["scope"],
                                                         record.get("delta_mark") or 0),
                                          record["id"])
        cur.execute("SELECT value FROM ontology.load_state WHERE name = 'schema'")
        row = cur.fetchone()
        version = int(((row[0] if row else None) or {}).get("version") or 0) + 1
        cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES "
                    "('schema_digest', %s) ON CONFLICT (key) DO UPDATE SET value = "
                    "excluded.value", (record["to"],))
        cur.execute("INSERT INTO ontology.load_state (name, value) VALUES ('schema', %s) "
                    "ON CONFLICT (name) DO UPDATE SET value = excluded.value, "
                    "updated_at = now()",
                    (json.dumps({"digest": record["to"], "version": version,
                                 "migration": record["id"]}),))
        units.update(version=version, chunked=True, migration=record["id"])
        cur.execute("INSERT INTO ontology.schema_migrations (id, from_digest, to_digest, "
                    "scope, report) VALUES (%s, %s, %s, %s, %s)",
                    (record["id"], record["from"], record["to"],
                     json.dumps(record["scope"]), json.dumps(units, sort_keys=True)))
        out["version"] = version
        out["reproject.units"] = units["reproject"]
    if step_record is not None:
        stepped = _totals({k: v for k, v in parts.items() if k.startswith("steps")})
        stepped["reproject"] = om.reproject(cur, om._newer(cur, step_record["scope"],
                                                           step_record["delta_mark"]),
                                            "steps:" + step_record["id"])
        cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES ('steps_digest', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (step_record["to"],))
        stepped.update(migration=step_record["id"], steps=step_record["steps"], chunked=True)
        cur.execute("INSERT INTO ontology.schema_migrations (id, from_digest, to_digest, "
                    "scope, report) VALUES (%s, %s, %s, %s, %s)",
                    ("steps:" + step_record["id"], steps_from(step_record), step_record["to"],
                     json.dumps(step_record["scope"]), json.dumps(stepped, sort_keys=True)))
        out["reproject.steps"] = stepped["reproject"]
    cur.execute("DELETE FROM ontology.load_state WHERE starts_with(name, %s)",
                (f"{PREFIX}{key}:",))
    cur.execute("DELETE FROM ontology.store_meta WHERE key = %s", (FLAG,))
    return out


def _totals(parts: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for value in parts.values():
        for k, v in (value or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = round(out.get(k, 0) + v, 3)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.migrate_chunks")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "measure"):
        p = sub.add_parser(name)
        p.add_argument("layer", nargs="?", default=None)
        p.add_argument("--steps", default=None)
        p.add_argument("--dsn", default=None)
        p.add_argument("--report", default=None, help="write the JSON report here too")
        p.add_argument("--workers", type=int, default=1,
                       help="processes applying one wave's chunks at once (default 1)")
        p.add_argument("--stop-file", default=None,
                       help="once this file exists no chunk starts; rerun to resume")
    args = parser.parse_args(argv)
    if args.layer is None and args.steps is None:
        parser.error("a unit layer, --steps or both")
    from operating.pg import config
    report = apply_chunked(args.layer, args.dsn or config.dsn(), steps=args.steps,
                           write=args.command == "apply", log=sys.stderr,
                           workers=max(1, args.workers), stop_file=args.stop_file)
    text = json.dumps(report, sort_keys=True, default=str)
    if args.report:
        Path(args.report).write_text(text)
    print(json.dumps({k: v for k, v in report.items() if k != "chunks"}, sort_keys=True,
                     default=str))
    return 0 if report.get("applied") or (args.command == "measure"
                                          and "reason" not in report) else 1


if __name__ == "__main__":
    sys.exit(main())
