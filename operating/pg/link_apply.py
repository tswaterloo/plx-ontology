"""Apply a schema change that touches only link bindings in place: the changed link kinds'
links re-derived from the captures and written set-based, per link type, with undo.

Why a tool of its own (live 2026-11-11): the unit planner (``ontology.schema_migrate.diff``)
puts every link type touching a binding into that binding's unit slice
(``build_keys.SchemaParts.touching``), so a fix of 5 link bindings re-derived 10 binding
units and, through their types, required every build step (mentions included): many hours
for a change of a few hundred thousand links. Here:

* ``plan``: the diff from the served schema must change link types only (any type,
  property, binding or event change refuses: that is a unit migration). The changed kinds'
  link units (every binding of each kind), the bindings they read (``deps``: from, to, via)
  and the object-id prefixes of their from-bindings (``scope``) are the plan.
* Step readers, fail closed. A step re-runs when it reads a changed kind: its code or data
  files name the kind (``static``), or the kind is one a generic reader follows
  (``generic``): verified links from a mentioned record to a party carrying a website
  (``mentions`` ``party_domain``) or reached by a sender's contact
  (``sender_corroboration``), links from a named record to a Customer or Supplier
  (``sender_parties``), and same-record twins. A static reader refuses. A generic one is
  checked on the data at ``stage``: the served step links from the kind's from-objects must
  cite none of the handles of the kind's verified targets (served or re-derived);
  otherwise it refuses, naming the step to re-run.
* ``build``: ``schema_migrate.build_layer`` over the captures, read-only, emitting only the
  plan's link units (no object, value or event rows).
* ``stage``: per changed kind, the layer's links compared with the served current links of
  that kind (link id and every column, as ``migrate_chunks`` compares them); only the
  difference is kept, in the run-owned schema :data:`SCHEMA` (never a served table).
* ``apply``: the delta's advisory lock is held as a session lock from the first check to the
  switch (a delta cycle cannot commit between two kinds; ``delta_lock_busy`` after
  ``DELTA_LOCK_WAIT`` s), the layer is re-checked for staleness under it before every kind
  and the switch, and one apply covers every kind of the plan: a combined plan (this
  branch merged with another link-only branch) is one layer with
  both branches' kinds, one switch, one undo (a second layer is not staged over an applied
  one: its undo state would be lost). Per kind, one transaction under the delta's lock: the served rows it replaces
  kept (``undo_deleted``) and deleted, its links inserted (``undo_inserted``), counts
  checked; then one last transaction switches the served schema (``store_meta``,
  ``load_state.schema``) and records the migration (``schema_migrations``, scope: the
  from-bindings' prefixes, so a delta layer of the older schema leaves those objects to be
  projected again). ``--rehearse F`` runs each kind's delete and insert on the first share
  F of its rows and rolls back: timed, nothing written.
* ``undo``: the kinds taken back in reverse order, the schema switch reverted.

Refused, nothing written: the served schema is not the plan's ``from``; a delta replaced an
object a changed link can name since the layer's snapshot; a re-derived link id is held by a current row of another kind (one only a
superseded row of the same kind holds replaces that row, kept for undo); a staged
``gone`` link is no longer served at the apply.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

SCHEMA = os.environ.get("PLX_LINK_APPLY_SCHEMA", "links_apply")
FORMAT = "plx-link-apply/v1"
_BATCH = 20_000
# readers that follow links of any type (module docstring)
_GENERIC_STEPS = ("mentions", "sender_corroboration", "sender_parties", "twins")
# link kinds a unit projects (``ontology.project._PROJECTED_LINK_KINDS``): every other
# binding kind is a step's
_PROJECTED = ("fk", "json_path", "same_record", "text_match")


class Refused(RuntimeError):
    """The link-only apply cannot run as described: nothing written."""


def _schema_name() -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", SCHEMA):
        raise Refused(f"stage_schema_name:{SCHEMA}")
    return SCHEMA


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- plan

def plan(served: Any, new: Any) -> dict[str, Any]:
    """The link-only plan from the ``served`` schema to ``new`` (module docstring)."""
    from ontology.schema_migrate import diff
    from ontology.project_incremental import plan as unit_plan
    whole = diff(served, new, present=())   # the steps: :func:`step_readers` instead
    if whole["refused"]:
        raise Refused("diff_refused:" + ",".join(whole["refused"]))
    other = sorted(k for k, v in whole["changes"].items() if v and k != "link_types")
    if other:
        raise Refused("not_link_only:" + ",".join(other))
    kinds = sorted(whole["changes"].get("link_types") or {})
    if not kinds:
        raise Refused("no_link_change")
    links = new.link_types
    units_plan = unit_plan(new)
    units = sorted(u for u, rows in units_plan.rows.items()
                   if rows[0] == "links" and rows[1] in kinds)
    deps: set[str] = set().union(*(units_plan.units[u] for u in units)) if units else set()
    froms: set[str] = {lb["from_binding"] for kind in kinds
                       for lb in (links.get(kind) or {}).get("bindings", ())
                       if lb.get("from_binding")}
    types = {b["id"]: tid for tid, t in new.object_types.items() for b in t.get("bindings", ())}
    scope = sorted(f"{types[b]}:{b}:" for b in froms if b in types)
    # every object a changed link can name: a delta replacing one after the layer's
    # snapshot makes the layer stale (both ends, and the via records)
    dep_scope = sorted(f"{types[b]}:{b}:" for b in deps if b in types)
    readers = step_readers(new, kinds)
    if readers["static"]:
        raise Refused("step_reads_kind:" + ",".join(f"{s}:{k}" for k, s in readers["static"]))
    body = {"from": served.digest, "to": new.digest, "kinds": kinds, "units": units}
    return {"format": FORMAT, "id": _sha(body)[:16], **body,
            "dropped": [k for k in kinds if k not in links],
            "deps": sorted(deps), "scope": scope, "dep_scope": dep_scope,
            "readers": readers}


def step_readers(schema: Any, kinds: Iterable[str]) -> dict[str, list]:
    """``static``: (kind, step) a step's code or data names; ``generic``: (kind, step) a
    generic reader may follow (checked on the data at :func:`stage`)."""
    from ontology import mentions, sender_parties
    from ontology.step_migrate import STEPS
    root = Path(__file__).resolve().parents[2]
    texts = {}
    for step, spec in STEPS.items():
        parts = []
        for path in spec.code:
            p = root / path
            if p.is_file():
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
        texts[step] = "\n".join(parts)
    targets = {k[1] for k in mentions.mention_link_types(schema.link_types)}
    static, generic = [], []
    for kind in kinds:
        quoted = (f'"{kind}"', f"'{kind}'")
        for step, text in texts.items():
            if any(q in text for q in quoted):
                static.append((kind, step))
        link = schema.link_types.get(kind)
        if link is None:
            continue
        same_record = kind.endswith(".same_record_as") or any(
            lb.get("kind") == "same_record" for lb in link.get("bindings", ()))
        if same_record:
            static.append((kind, "twins"))
        if link["from"] in targets:
            generic += [(kind, "mentions"), (kind, "sender_corroboration")]
        if link["from"] in sender_parties.NAMED_TYPES and link["to"] in \
                sender_parties.PARTY_TYPES:
            static.append((kind, "sender_parties"))
    return {"static": sorted(set(static)), "generic": sorted(set(generic))}


# ---------------------------------------------------------------- build

def migration_record(p: Mapping[str, Any]) -> dict[str, Any]:
    """The plan as ``schema_migrate.build_layer`` takes a migration: only link units."""
    return {"format": "plx-schema-migration/v1", "id": p["id"], "from": p["from"],
            "to": p["to"], "refused": [], "changes": {"link_types": p["kinds"]},
            "units": {"affected": list(p["units"]), "dropped": [], "total": len(p["units"])},
            "deps": list(p["deps"]), "scope": list(p["scope"]),
            "dep_scope": list(p["dep_scope"]),
            "delete": {"bindings": {}, "links": list(p["units"])},
            "link_apply": {k: p[k] for k in ("kinds", "readers")}}


def build(p: Mapping[str, Any], new: Any, state: Path, out: Path,
          dsn: str | None = None) -> dict[str, Any]:
    """The layer of the plan's link units over a host state directory, read-only."""
    from ontology.schema_migrate import build_from_state
    return build_from_state(migration_record(p), new, state, out, dsn=dsn)


def _layer(path: Path) -> dict[str, Any]:
    from ontology.schema_migrate import read_layer
    record = read_layer(path)
    if "link_apply" not in record:
        raise Refused("not_a_link_apply_layer")
    return record


def _layer_rows(path: Path, kind: str) -> Iterator[tuple]:
    from ontology import store_ddl
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links WHERE "
                           "link_type = ? ORDER BY link_id", (kind,))
        while True:
            got = cur.fetchmany(_BATCH)
            if not got:
                return
            yield from (tuple(r) for r in got)
    finally:
        conn.close()


# ---------------------------------------------------------------- stage

def _create(cur: Any, s: str) -> None:
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.meta (key text PRIMARY KEY, value text "
                "NOT NULL)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.links (LIKE ontology.link_rows INCLUDING "
                "DEFAULTS, ord bigint NOT NULL)")
    cur.execute(f"ALTER TABLE {s}.links ALTER COLUMN row_no DROP DEFAULT")
    cur.execute(f"CREATE INDEX IF NOT EXISTS links_by_type ON {s}.links (link_type, ord)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.gone (link_id text PRIMARY KEY, "
                "link_type text NOT NULL, ord bigint NOT NULL)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.staged (link_type text PRIMARY KEY, "
                "report text NOT NULL)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.undo_deleted (LIKE ontology.link_rows)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.undo_inserted (link_id text NOT NULL, "
                "link_type text NOT NULL)")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {s}.applied (link_type text PRIMARY KEY, "
                "report text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")


def _meta(cur: Any, s: str) -> dict[str, Any]:
    cur.execute("SELECT to_regclass(%s)", (f"{s}.meta",))
    if cur.fetchone()[0] is None:
        return {}
    cur.execute(f"SELECT key, value FROM {s}.meta")
    return {k: json.loads(v) for k, v in cur.fetchall()}


def stage(dsn: str, layer: Path) -> dict[str, Any]:
    """The layer's difference to the served links of its kinds, staged (one transaction)."""
    import psycopg
    from ontology import store_ddl
    from operating.pg.migrate_chunks import _hash, _served_links
    from operating.pg.ontology_migrate import served_schema
    record = _layer(layer)
    s = _schema_name()
    kinds = record["link_apply"]["kinds"]
    cols = list(store_ddl.LINK_COLUMNS)
    out: dict[str, Any] = {"layer": record["id"], "kinds": {}}
    with psycopg.connect(dsn, application_name="plx-link-apply") as pg, pg.cursor() as cur:
        if served_schema(cur) != record["from"]:
            raise Refused(f"served_schema_differs:{served_schema(cur)}")
        _create(cur, s)
        held = _meta(cur, s)
        if held.get("layer") not in (None, record["id"]) and held.get("state") != "undone":
            raise Refused(f"stage_of_another_layer:{held.get('layer')}")
        if held.get("state") == "applied":
            raise Refused("already_applied")
        for table in ("links", "gone", "staged"):
            cur.execute(f"DELETE FROM {s}.{table}")
        cur.execute(f"DELETE FROM {s}.meta")
        for kind in kinds:
            served = {row[0]: _hash(row) for row in _served_links(
                cur, "l.link_type = %s AND l.superseded_at IS NULL", [kind])}
            rep = {"layer": 0, "same": 0, "written": 0, "gone": 0}
            pending: list[tuple] = []
            new_ids: list[str] = []
            for n, row in enumerate(_layer_rows(layer, kind)):
                rep["layer"] += 1
                h = served.pop(row[0], None)
                if h == _hash(row):
                    rep["same"] += 1
                    continue
                if h is not None:
                    cur.execute(f"INSERT INTO {s}.gone VALUES (%s, %s, %s)", (row[0], kind, n))
                    rep["gone"] += 1
                else:
                    new_ids.append(row[0])
                pending.append((*row, n))
                rep["written"] += 1
                if len(pending) >= _BATCH:
                    _copy(cur, s, cols, pending)
            _copy(cur, s, cols, pending)
            # an id only a superseded row holds (a link of an earlier version of its object
            # derived again): that history row is replaced like a served one, kept for undo
            # (``undo_deleted``), as a delta layer's row replaces it on the key; an id a
            # current row of another kind holds is refused
            rep["superseded_replaced"] = 0
            for k in range(0, len(new_ids), _BATCH):
                cur.execute("SELECT link_id, link_type, superseded_at IS NULL FROM "
                            "ontology.link_rows WHERE link_id = ANY(%s::text[])",
                            (new_ids[k:k + _BATCH],))
                for lid, held_type, current in cur.fetchall():
                    if current or held_type != kind:
                        raise Refused(f"link_id_held_by_another_row:{kind}")
                    cur.execute(f"INSERT INTO {s}.gone VALUES (%s, %s, %s)",
                                (lid, kind, -10_000_000 - rep["superseded_replaced"]))
                    rep["superseded_replaced"] += 1
                    rep["gone"] += 1
            rest = sorted(served)
            with cur.copy(f"COPY {s}.gone (link_id, link_type, ord) FROM STDIN") as cp:
                for i, lid in enumerate(rest):
                    cp.write_row((lid, kind, -1 - i))
            rep["gone"] += len(rest)
            cur.execute(f"INSERT INTO {s}.staged VALUES (%s, %s)", (kind, json.dumps(rep)))
            out["kinds"][kind] = rep
        out["readers"] = generic_reader_check(cur, record, s)
        if out["readers"]["reads"]:
            raise Refused("step_reads_changed_links:" + ",".join(
                f"{r['step']}:{r['kind']}" for r in out["readers"]["reads"]))
        for key, value in (("layer", record["id"]), ("record", record), ("state", "staged"),
                           ("stage", out)):
            cur.execute(f"INSERT INTO {s}.meta VALUES (%s, %s)", (key, json.dumps(value)))
        pg.commit()
    return out


def _copy(cur: Any, s: str, cols: Sequence[str], pending: list[tuple]) -> None:
    if not pending:
        return
    with cur.copy(f"COPY {s}.links ({', '.join(cols)}, ord) FROM STDIN") as cp:
        for row in pending:
            cp.write_row(row)
    pending.clear()


def generic_reader_check(cur: Any, record: Mapping[str, Any], s: str) -> dict[str, Any]:
    """For each (kind, generic step): the verified targets of the kind, served now and
    staged, and the served step links from the kind's from-objects citing any of them
    (their versions' handles in the step link's evidence or reason). Any such link means
    the step read the kind: ``reads`` names it."""
    out: dict[str, Any] = {"checked": [], "reads": []}
    for kind, step in record["link_apply"]["readers"]["generic"]:
        cur.execute("SELECT from_object, to_object FROM ontology.link_rows WHERE link_type = "
                    "%s AND tier = 'verified' AND superseded_at IS NULL UNION SELECT "
                    f"from_object, to_object FROM {s}.links WHERE link_type = %s AND tier = "
                    "'verified'", (kind, kind))
        pairs = cur.fetchall()
        froms = sorted({p[0] for p in pairs})
        handles = _handles(cur, sorted({p[1] for p in pairs}))
        cited = 0
        if froms and handles:
            from operating.pg.migrate_chunks import _served_links
            from ontology import store_ddl
            r = store_ddl.LINK_COLUMNS.index("reason")
            e = store_ddl.LINK_COLUMNS.index("evidence_handles")
            for k in range(0, len(froms), _BATCH):
                for row in _served_links(cur, "l.to_object = ANY(%s::text[]) AND "
                                         "l.superseded_at IS NULL AND l.link_type <> ALL("
                                         "%s::text[]) AND l.binding_kind <> ALL(%s::text[])",
                                         [froms[k:k + _BATCH], record["link_apply"]["kinds"],
                                          list(_PROJECTED)]):
                    text = (row[r] or "") + (row[e] or "")
                    if any(h in text for h in handles):
                        cited += 1
        item = {"kind": kind, "step": step, "verified_pairs": len(pairs),
                "step_links_citing_targets": cited}
        out["checked"].append(item)
        if cited:
            out["reads"].append(item)
    return out


def _handles(cur: Any, objects: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for k in range(0, len(objects), _BATCH):
        cur.execute("SELECT DISTINCT l1_handle FROM ontology.object_versions WHERE object_id = "
                    "ANY(%s::text[])", (list(objects[k:k + _BATCH]),))
        out |= {h.split("@", 1)[0] for (h,) in cur.fetchall()}
    return out


# ---------------------------------------------------------------- apply / undo

DELTA_LOCK_WAIT = 300   # seconds apply() waits for a running delta cycle to release its lock
_HOLD = "SELECT pg_try_advisory_lock(hashtext('ontology_delta'))"
_RELEASE = "SELECT pg_advisory_unlock(hashtext('ontology_delta'))"


def _hold(cur: Any, wait: float) -> None:
    """The delta apply's own advisory lock (``ontology_load._LOCK``), held for the whole
    apply as a session lock: it survives each kind's commit, so no delta cycle can commit
    between two kinds or before the schema switch. Refused when a cycle holds it past
    ``wait`` seconds."""
    end = time.monotonic() + wait
    while True:
        cur.execute(_HOLD)
        if cur.fetchone()[0]:
            return
        if time.monotonic() >= end:
            raise Refused("delta_lock_busy")
        time.sleep(min(2.0, max(0.05, wait / 20)))


def _stale(cur: Any, record: Mapping[str, Any]) -> list[str]:
    """The objects a delta replaced in the layer's scope since its snapshot: a layer that
    has any is stale (build it again); deltas elsewhere do not matter."""
    from operating.pg.ontology_migrate import _newer
    return _newer(cur, _dep_scope(record), int(record.get("delta_mark") or 0))


def apply(dsn: str, rehearse: float | None = None,
          lock_wait: float | None = None) -> dict[str, Any]:
    """The staged kinds written (module docstring); ``rehearse``: a share of each kind's
    rows deleted and inserted, timed and rolled back. A real apply holds the delta lock
    from its first check to the schema switch (:func:`_hold`) and re-checks the layer for
    staleness under it before every kind and the switch, so a delta never lands between
    the kinds; one apply covers every kind of the plan (a combined plan of two branches
    is one layer)."""
    import psycopg
    s = _schema_name()
    wait = DELTA_LOCK_WAIT if lock_wait is None else lock_wait
    with psycopg.connect(dsn, application_name="plx-link-apply") as pg, pg.cursor() as cur:
        held = False
        try:
            if rehearse is None:
                _hold(cur, wait)
                held = True
            return _apply(pg, cur, s, rehearse)
        finally:
            if held:
                pg.rollback()
                cur.execute(_RELEASE)


def _apply(pg: Any, cur: Any, s: str, rehearse: float | None) -> dict[str, Any]:
    from ontology import store_ddl
    from operating.pg.ontology_migrate import LOCK, served_schema
    cols = ", ".join(store_ddl.LINK_COLUMNS)
    meta = _meta(cur, s)
    if meta.get("state") not in ("staged", "applying"):
        raise Refused(f"nothing_staged:{meta.get('state')}")
    record = meta["record"]
    if served_schema(cur) != record["from"]:
        raise Refused("served_schema_differs")
    newer = _stale(cur, record)
    pg.commit()
    if newer:
        raise Refused(f"delta_replaced_objects_in_scope_since_the_snapshot:{len(newer)}")
    out: dict[str, Any] = {"layer": record["id"], "rehearse": rehearse, "kinds": {}}
    cur.execute(f"SELECT link_type, report FROM {s}.staged ORDER BY link_type")
    staged = {k: json.loads(v) for k, v in cur.fetchall()}
    cur.execute(f"SELECT link_type FROM {s}.applied")
    done = {r[0] for r in cur.fetchall()}
    pg.commit()
    if rehearse is None:
        cur.execute(f"UPDATE {s}.meta SET value = %s WHERE key = 'state'",
                    (json.dumps("applying"),))
        pg.commit()
    for kind, rep in staged.items():
        if kind in done and rehearse is None:
            out["kinds"][kind] = {"skipped": "applied"}
            continue
        t = time.monotonic()
        cur.execute(LOCK)
        late = _stale(cur, record)   # under the lock, in this kind's transaction
        if late:
            pg.rollback()
            raise Refused(f"delta_replaced_objects_in_scope_during_the_apply:{kind}:"
                          f"{len(late)}")
        cur.execute("SET LOCAL jit = off")
        limit = ""
        if rehearse is not None:
            share = max(1, int(round(max(rep["gone"], rep["written"]) * rehearse)))
            limit = f" LIMIT {share}"
        cur.execute(f"SELECT link_id FROM {s}.gone WHERE link_type = %s ORDER BY ord"
                    + limit, (kind,))
        gone = [r[0] for r in cur.fetchall()]
        if limit:
            # a rehearsal's share: also the gone rows whose ids the inserted share re-uses
            # (a changed or superseded row the full apply deletes before that insert)
            cur.execute(f"SELECT g.link_id FROM {s}.gone g JOIN (SELECT link_id FROM "
                        f"{s}.links WHERE link_type = %s ORDER BY ord{limit}) x "
                        "ON x.link_id = g.link_id WHERE g.link_type = %s", (kind, kind))
            gone = sorted(set(gone) | {r[0] for r in cur.fetchall()})
        if rehearse is None:
            cur.execute(f"INSERT INTO {s}.undo_deleted SELECT * FROM ontology.link_rows "
                        "WHERE link_id = ANY(%s::text[])", (gone,))
        cur.execute("DELETE FROM ontology.link_rows WHERE link_id = ANY(%s::text[])",
                    (gone,))
        deleted = cur.rowcount
        if deleted != len(gone):
            pg.rollback()
            raise Refused(f"gone_not_served:{kind}:{deleted}:{len(gone)}")
        cur.execute(f"INSERT INTO ontology.link_rows ({cols}) SELECT {cols} FROM "
                    f"(SELECT * FROM {s}.links WHERE link_type = %s ORDER BY ord{limit}) x",
                    (kind,))
        inserted = cur.rowcount
        expect = min(rep["written"], int(limit.split()[-1])) if limit else rep["written"]
        if inserted != expect or (rehearse is None and deleted != rep["gone"]):
            pg.rollback()
            raise Refused(f"counts_differ:{kind}")
        bit = {"deleted": deleted, "inserted": inserted,
               "seconds": round(time.monotonic() - t, 3)}
        if rehearse is not None:
            pg.rollback()
            bit["projected_seconds"] = round(bit["seconds"] / max(rehearse, 1e-9), 1)
        else:
            cur.execute(f"INSERT INTO {s}.undo_inserted SELECT link_id, link_type FROM "
                        f"{s}.links WHERE link_type = %s", (kind,))
            cur.execute(f"INSERT INTO {s}.applied VALUES (%s, %s)",
                        (kind, json.dumps(bit)))
            pg.commit()
        out["kinds"][kind] = bit
    if rehearse is not None:
        return out
    out["switch"] = _switch(cur, record, s)
    pg.commit()
    return out


def _dep_scope(record: Mapping[str, Any]) -> list[str]:
    """The record's ``dep_scope``; for a layer recorded without it, its ``deps`` under this
    tree's schema, which must be the record's ``to``."""
    if record.get("dep_scope") is not None:
        return list(record["dep_scope"])
    from ontology.schema import load_schema
    schema = load_schema()
    if schema.digest != record["to"]:
        raise Refused("dep_scope_unknown:schema_differs")
    types = {b["id"]: tid for tid, t in schema.object_types.items()
             for b in t.get("bindings", ())}
    return sorted(f"{types[b]}:{b}:" for b in record["deps"] if b in types)


def _switch(cur: Any, record: Mapping[str, Any], s: str) -> dict[str, Any]:
    from operating.pg.ontology_migrate import LOCK
    cur.execute(LOCK)
    late = _stale(cur, record)
    if late:
        cur.connection.rollback()
        raise Refused(f"delta_replaced_objects_in_scope_before_the_switch:{len(late)}")
    cur.execute("SELECT value FROM ontology.load_state WHERE name = 'schema'")
    row = cur.fetchone()
    before = row[0] if row else None
    version = int((before or {}).get("version") or 0) + 1
    cur.execute(f"INSERT INTO {s}.meta VALUES ('schema_before', %s) ON CONFLICT (key) DO "
                "UPDATE SET value = excluded.value", (json.dumps(before),))
    cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES ('schema_digest', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (record["to"],))
    cur.execute("INSERT INTO ontology.load_state (name, value) VALUES ('schema', %s) ON "
                "CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = now()",
                (json.dumps({"digest": record["to"], "version": version,
                             "migration": "links:" + record["id"]}),))
    cur.execute("INSERT INTO ontology.schema_migrations (id, from_digest, to_digest, scope, "
                "report) VALUES (%s, %s, %s, %s, %s)",
                ("links:" + record["id"], record["from"], record["to"],
                 json.dumps(record["scope"]), json.dumps({"link_apply": record["link_apply"]
                                                          ["kinds"]})))
    cur.execute(f"UPDATE {s}.meta SET value = %s WHERE key = 'state'", (json.dumps("applied"),))
    return {"schema_digest": record["to"], "version": version}


def undo(dsn: str) -> dict[str, Any]:
    """The applied kinds taken back (reverse order) and the schema switch reverted."""
    import psycopg
    from operating.pg.ontology_migrate import LOCK
    s = _schema_name()
    out: dict[str, Any] = {"kinds": {}}
    with psycopg.connect(dsn, application_name="plx-link-apply") as pg, pg.cursor() as cur:
        meta = _meta(cur, s)
        record = meta.get("record")
        if not record:
            raise Refused("nothing_to_undo")
        cur.execute("SELECT string_agg(quote_ident(column_name), ', ' ORDER BY "
                    "ordinal_position) FROM information_schema.columns WHERE table_schema = "
                    "'ontology' AND table_name = 'link_rows'")
        raw = cur.fetchone()[0]
        cur.execute(LOCK)
        if meta.get("state") == "applied":
            cur.execute("SELECT value FROM ontology.store_meta WHERE key = 'schema_digest'")
            if cur.fetchone()[0] != record["to"]:
                raise Refused("served_schema_moved_since_the_apply")
            cur.execute("UPDATE ontology.store_meta SET value = %s WHERE key = "
                        "'schema_digest'", (record["from"],))
            before = meta.get("schema_before")
            if before is None:
                cur.execute("DELETE FROM ontology.load_state WHERE name = 'schema'")
            else:
                cur.execute("UPDATE ontology.load_state SET value = %s, updated_at = now() "
                            "WHERE name = 'schema'", (json.dumps(before),))
            cur.execute("DELETE FROM ontology.schema_migrations WHERE id = %s",
                        ("links:" + record["id"],))
        cur.execute(f"SELECT link_type FROM {s}.applied ORDER BY link_type DESC")
        for (kind,) in cur.fetchall():
            cur.execute(f"DELETE FROM ontology.link_rows l USING {s}.undo_inserted u WHERE "
                        "u.link_type = %s AND l.link_id = u.link_id", (kind,))
            removed = cur.rowcount
            # a delta may have re-inserted a link id the apply deleted: the pre-apply row
            # replaces it, so the restore never aborts on the key
            cur.execute(f"DELETE FROM ontology.link_rows l USING {s}.undo_deleted u WHERE "
                        "u.link_type = %s AND l.link_id = u.link_id", (kind,))
            cur.execute(f"INSERT INTO ontology.link_rows ({raw}) SELECT {raw} FROM "
                        f"{s}.undo_deleted WHERE link_type = %s", (kind,))
            out["kinds"][kind] = {"removed": removed, "restored": cur.rowcount}
            cur.execute(f"DELETE FROM {s}.undo_inserted WHERE link_type = %s", (kind,))
            cur.execute(f"DELETE FROM {s}.undo_deleted WHERE link_type = %s", (kind,))
            cur.execute(f"DELETE FROM {s}.applied WHERE link_type = %s", (kind,))
        cur.execute(f"UPDATE {s}.meta SET value = %s WHERE key = 'state'", (json.dumps("undone"),))
        pg.commit()
    return out


# ---------------------------------------------------------------- command line

def main(argv: Sequence[str] | None = None) -> int:
    from ontology.schema import load_schema
    from ontology.schema_migrate import schema_at
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.link_apply")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--from-ref", required=True)
    b = sub.add_parser("build")
    b.add_argument("--from-ref", required=True)
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    st = sub.add_parser("stage")
    st.add_argument("layer")
    a = sub.add_parser("apply")
    a.add_argument("--rehearse", type=float, default=None)
    sub.add_parser("undo")
    for x in (b, st, a, sub.choices["undo"]):
        x.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            out: Any = plan(schema_at(args.from_ref), load_schema())
        elif args.command == "build":
            new = load_schema()
            out = build(plan(schema_at(args.from_ref), new), new, Path(args.state_dir),
                        Path(args.out), dsn=args.dsn or config.dsn())
        elif args.command == "stage":
            out = stage(args.dsn or config.dsn(), Path(args.layer))
        elif args.command == "apply":
            out = apply(args.dsn or config.dsn(), rehearse=args.rehearse)
        else:
            out = undo(args.dsn or config.dsn())
    except Refused as refused:
        print(json.dumps({"refused": str(refused)}))
        return 1
    print(json.dumps(out, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
