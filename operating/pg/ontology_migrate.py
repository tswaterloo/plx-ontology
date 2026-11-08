"""Apply a schema migration layer (``ontology/schema_migrate.py``) to the Postgres ontology
in place, and the deploy check that goes with it (``docs/no-builds.md``).

``python3 -m operating.pg.ontology_migrate apply LAYER [--dsn DSN]``: in one transaction,
under the delta's advisory lock (no delta layer applies meanwhile), and only while
Postgres holds its generation whole and serves the layer's ``from`` schema
(``store_meta.schema_digest``): the affected bindings' rows (objects, versions, removals,
values, identifiers and the events their versions yield under the served schema) and the
affected link units' links are deleted, the layer's rows copied in (the delta's
numbering and field and kind matching), the objects a delta layer replaced since the
layer's capture snapshot began that fall in its scope are queued for the delta to project
again (``ontology.reproject``), and the served schema becomes ``to``
(``store_meta.schema_digest``, ``load_state.schema``). Rows outside the affected units
are not touched, except the copies of the rewritten objects' permissions (the text index's
markings, the search tables' markings and audience: :func:`_text_index`, :func:`_search`)
and every link naming an object the migration drops (:func:`_dropped`); what a build step
derives from the rewritten rows is the step layer's (a unit layer naming
``steps_required`` is refused without it).

``... apply [LAYER] --steps STEPS``: the step migration layer (``ontology.step_migrate``)
in the same transaction: each re-run step's served links (its link id prefixes, from its
from types) deleted and the layer's copied in, ``store_meta.steps_digest`` switched, and
the objects of its scope a delta layer replaced since its snapshot queued again; a delta
layer of older step code applies without them (:func:`steps_scope`).

``python3 -m operating.pg.ontology_migrate check [--digest D]``: exit 0 when the code's
schema (or ``D``) is the one Postgres serves, else 1 with what is pending: the deploy
check (code with a new schema deploys once its migration has applied).

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

LOCK = "SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))"   # ontology_delta's


def _has_tables(cur: Any) -> bool:
    cur.execute("SELECT to_regclass('ontology.schema_migrations') IS NOT NULL")
    return bool(cur.fetchone()[0])


def served_schema(cur: Any) -> str | None:
    cur.execute("SELECT value FROM ontology.store_meta WHERE key = 'schema_digest'")
    row = cur.fetchone()
    return row[0] if row else None


def layer_scope(cur: Any, digest: str | None) -> dict[str, Any] | None:
    """How a delta layer of schema ``digest`` applies: ``{}`` (falsy) when it is the
    served schema; ``{"id", "scope"}`` (the id prefixes no such layer may write, and the
    last migration's id) when migrations lead from it to the served one; None (refused)
    otherwise."""
    served = served_schema(cur)
    if digest == served:
        return {}
    if digest is None or not _has_tables(cur):
        return None
    scope: set[str] = set()
    at, last, seen = digest, None, set()
    while at != served:
        cur.execute("SELECT id, to_digest, scope FROM ontology.schema_migrations "
                    "WHERE from_digest = %s ORDER BY applied_at DESC LIMIT 1", (at,))
        row = cur.fetchone()
        if row is None or row[0] in seen:
            return None
        seen.add(row[0])
        last, at = row[0], row[1]
        scope.update(row[2])
    return {"id": last, "scope": sorted(scope)}


def served_steps(cur: Any) -> str | None:
    cur.execute("SELECT value FROM ontology.store_meta WHERE key = 'steps_digest'")
    row = cur.fetchone()
    return row[0] if row else None


def steps_scope(cur: Any, digest: str | None) -> dict[str, Any] | None:
    """How a delta layer whose build steps' code is ``digest`` (its ``steps_digest``;
    None: code older than step migrations) applies: ``{}`` when no step migration ever
    applied or it is the served step code; ``{"id", "scope"}`` (the id prefixes no such
    layer may write) when step migrations lead from it to the served one; None (refused)
    otherwise. A layer without a digest is of the step code before the first step
    migration."""
    served = served_steps(cur)
    if served is None or digest == served or not _has_tables(cur):
        return {}
    if digest is None:
        cur.execute("SELECT from_digest FROM ontology.schema_migrations WHERE "
                    "starts_with(id, 'steps:') ORDER BY applied_at LIMIT 1")
        row = cur.fetchone()
        if row is None:
            return {}
        digest = row[0]
    scope: set[str] = set()
    at, last, seen = digest, None, set()
    while at != served:
        cur.execute("SELECT id, to_digest, scope FROM ontology.schema_migrations "
                    "WHERE from_digest = %s AND starts_with(id, 'steps:') "
                    "ORDER BY applied_at DESC LIMIT 1", (at,))
        row = cur.fetchone()
        if row is None or row[0] in seen:
            return None
        seen.add(row[0])
        last, at = row[0], row[1]
        scope.update(row[2])
    return {"id": last, "scope": sorted(scope)}


def log_owned(cur: Any, layer: str, owned: Sequence[str]) -> None:
    """Record the objects a delta layer replaced (``ontology.applied_owned``)."""
    if not owned or not _has_tables(cur):
        return
    with cur.copy("COPY ontology.applied_owned (layer, object_id) FROM STDIN") as copy:
        for oid in owned:
            copy.write_row((layer, oid))


def reproject(cur: Any, object_ids: Iterable[str], migration: str) -> int:
    rows = [(oid, migration) for oid in object_ids]
    if rows:
        cur.executemany("INSERT INTO ontology.reproject (object_id, migration) VALUES "
                        "(%s, %s) ON CONFLICT (object_id) DO NOTHING", rows)
    return len(rows)


def done(cur: Any) -> int:
    """Drop from ``ontology.reproject`` the objects of the temp table ``owned`` (a layer
    of the served schema just replaced them)."""
    if not _has_tables(cur):
        return 0
    cur.execute("DELETE FROM ontology.reproject WHERE object_id IN "
                "(SELECT object_id FROM owned)")
    return cur.rowcount


def delta_mark(dsn: str) -> int:
    """The newest ``applied_owned`` row: a layer build records it before its snapshot."""
    import psycopg
    with psycopg.connect(dsn) as pg:
        return pg.execute("SELECT coalesce(max(seq), 0) FROM ontology.applied_owned"
                          ).fetchone()[0]


def _whole(cur: Any) -> str | None:
    """The content digest of the generation Postgres holds whole, else None."""
    from operating.pg.ontology_load import ORDER
    cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'")
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'table:%%'")
    done = {r[0] for r in cur.fetchall()}
    needed = set(ORDER) - set((row[0] or {}).get("deferred") or ())
    return (row[0] or {}).get("content_digest") if needed <= done else None


# the affected bindings' rows a migration deletes (a value source's packed record mark
# with it: the layer's sources record their own, migration 0010's word index reads them)
_TABLES = ("value_cells", "packed_sources", "value_sources", "identifiers",
           "object_versions", "object_removals", "objects")


def apply_migration(layer: str | Path | None, dsn: str, *,
                    steps: str | Path | None = None) -> dict[str, Any]:
    """Apply the migration layer (module docstring) and the step migration layer
    ``steps`` (``ontology.step_migrate.build_layer``), in one transaction; either may be
    None. Returns the report."""
    import psycopg

    record = None
    if layer is not None:
        from ontology.schema_migrate import read_layer
        record = read_layer(layer)
    step_record = None
    if steps is not None:
        from ontology.step_migrate import read_layer as read_steps
        step_record = read_steps(steps)
    report: dict[str, Any] = {}
    if record is not None:
        report.update(migration=record["id"], **{"from": record["from"], "to": record["to"]})
    if step_record is not None:
        report["steps"] = {"migration": step_record["id"], "from": step_record["from"],
                           "to": step_record["to"], "steps": step_record["steps"]}
    started = time.monotonic()
    # the step layer is bound to the unit layer it was built over; a unit layer whose
    # changes reach a step's inputs comes with that step's layer
    unit_id = record["id"] if record is not None else None
    if step_record is not None and step_record.get("unit") != unit_id:
        return {**report, "applied": False, "reason": "steps_of_another_unit_layer"}
    required = sorted((record or {}).get("steps_required") or ())
    if required and (step_record is None or set(required) - set(step_record["steps"])):
        return {**report, "applied": False, "reason": "step_migration_required",
                "steps": required}
    try:
        with psycopg.connect(dsn) as pg, pg.cursor() as cur:
            cur.execute(LOCK)
            if _whole(cur) is None:
                return {**report, "applied": False, "reason": "generation_loading"}
            if not _has_tables(cur):
                return {**report, "applied": False, "reason": "migration_0009_missing"}
            for rec, prefix in ((record, ""), (step_record, "steps:")):
                if rec is None:
                    continue
                cur.execute("SELECT 1 FROM ontology.schema_migrations WHERE id = %s",
                            (prefix + rec["id"],))
                if cur.fetchone() is not None:
                    return {**report, "applied": False, "reason": "already_applied"}
            if record is not None:
                served = served_schema(cur)
                if served != record["from"]:
                    return {**report, "applied": False, "reason": "other_schema",
                            "served": served}
            if step_record is not None:
                held = served_steps(cur)
                if held is None:   # the generation's step code is not known: no baseline
                    return {**report, "applied": False, "reason": "steps_baseline_unknown"}
                if held != step_record["from"]:
                    return {**report, "applied": False, "reason": "other_steps",
                            "served": held}
            cur.execute("SET LOCAL jit = off")
            if record is not None:
                _apply_units(cur, Path(layer), record, report)
            if step_record is not None:
                _apply_steps(cur, Path(steps), step_record, report)
            report["seconds"] = round(time.monotonic() - started, 3)
            if record is not None:
                cur.execute("INSERT INTO ontology.schema_migrations (id, from_digest, "
                            "to_digest, scope, report) VALUES (%s, %s, %s, %s, %s)",
                            (record["id"], record["from"], record["to"],
                             json.dumps(record["scope"]), json.dumps(report, sort_keys=True)))
            if step_record is not None:
                cur.execute("INSERT INTO ontology.schema_migrations (id, from_digest, "
                            "to_digest, scope, report) VALUES (%s, %s, %s, %s, %s)",
                            ("steps:" + step_record["id"], step_record["from"],
                             step_record["to"], json.dumps(step_record["scope"]),
                             json.dumps(report.get("steps"), sort_keys=True)))
    except Refused as refused:   # rolled back: nothing of it applied
        return {**report, "applied": False, "reason": str(refused)}
    return {**report, "applied": True}


class Refused(Exception):
    """A change the migration cannot carry over (its transaction rolls back)."""


def _newer(cur: Any, scope: Sequence[str], mark: int) -> list[str]:
    """The objects in ``scope`` a delta layer replaced since the snapshot ``mark``."""
    if not scope:
        return []
    cur.execute("SELECT DISTINCT object_id FROM ontology.applied_owned WHERE "
                "seq > %s AND EXISTS (SELECT 1 FROM unnest(%s::text[]) p "
                "WHERE starts_with(object_id, p))", (int(mark or 0), list(scope)))
    return [r[0] for r in cur.fetchall()]


def _apply_steps(cur: Any, layer: Path, record: Mapping[str, Any],
                 report: dict[str, Any]) -> None:
    """The step migration part: each step's served links (its prefixes, from its from
    types) deleted, the layer's copied in, the served step code switched."""
    from operating.pg.ontology_delta import _insert
    out = report["steps"]
    t = time.monotonic()
    deleted = 0
    for o in record["owned"]:
        # a range of the link id's binary order: the ``link_rows_by_id`` index (0009)
        cur.execute("DELETE FROM ontology.link_rows WHERE link_id COLLATE \"C\" >= %s AND "
                    "link_id COLLATE \"C\" < %s AND (%s::text[] IS NULL OR EXISTS (SELECT 1 "
                    "FROM unnest(%s::text[]) f WHERE starts_with(from_object, f)))",
                    (o["prefix"], o["end"], o["from"], o["from"]))
        deleted += cur.rowcount
    out["deleted.link_rows"] = deleted
    out["seconds.delete"] = round(time.monotonic() - t, 3)
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        copied = _insert(conn, str(layer), cur, "[]", every_link=True)
    finally:
        conn.close()
    out["copied.link_rows"] = copied["copied.link_rows"]
    if record.get("values"):
        _apply_step_values(cur, layer, record["values"], out)
    if record.get("text_marks"):
        _apply_text_marks(cur, layer, record["text_marks"], out)
    out["seconds"] = round(time.monotonic() - t, 3)
    out["reproject"] = reproject(cur, _newer(cur, record["scope"], record["delta_mark"]),
                                 "steps:" + record["id"])
    cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES ('steps_digest', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (record["to"],))


def _apply_text_marks(cur: Any, layer: Path, spec: Mapping[str, Any],
                      out: dict[str, Any]) -> None:
    """The text index's step migration across a change that only marks
    (``step_migrate.MARKS_ONLY``): every served text field of a document the layer names
    (``text_marks``: its version handle) gets ``spec["marks"]`` added; nothing else is
    written (no text read again, no chunk touched; a chunk is read under its field's
    markings). Markings are only ever added. An unknown marking refuses."""
    from ontology.step_migrate import TEXT_MARKS
    from ontology.store import StoreError, markings_text
    marks = set(spec.get("marks") or ())
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        handles = [r[0] for r in conn.execute(
            f"SELECT version_handle FROM {TEXT_MARKS} ORDER BY version_handle")]
    finally:
        conn.close()
    if len(handles) != int(spec.get("documents", -1)) or not marks:
        raise Refused("text_marks_incomplete")
    cur.execute("CREATE TEMP TABLE text_marked_documents (version_handle text PRIMARY KEY) "
                "ON COMMIT DROP")
    with cur.copy("COPY text_marked_documents (version_handle) FROM STDIN") as copy:
        for handle in handles:
            copy.write_row((handle,))
    cur.execute("ANALYZE text_marked_documents")
    cur.execute("SELECT f.field_id, f.markings FROM ontology.text_fields f "
                "JOIN text_marked_documents d ON d.version_handle = f.version_handle")
    held = cur.fetchall()
    changed = []
    for field_id, markings in held:
        try:
            new = markings_text(set(json.loads(markings)) | marks)
        except (StoreError, TypeError, ValueError) as exc:
            raise Refused("text_marks_markings") from exc
        if new != markings:
            changed.append((field_id, new))
    if changed:
        cur.execute("CREATE TEMP TABLE text_marked (field_id bigint PRIMARY KEY, "
                    "markings text NOT NULL) ON COMMIT DROP")
        with cur.copy("COPY text_marked (field_id, markings) FROM STDIN") as copy:
            for row in changed:
                copy.write_row(row)
        cur.execute("UPDATE ontology.text_fields f SET markings = r.markings FROM "
                    "text_marked r WHERE r.field_id = f.field_id")
    out["text_marks.documents"] = len(handles)
    out["text_marks.fields"] = len(held)
    out["remarked.text_fields"] = len(changed)


def _apply_step_values(cur: Any, layer: Path, owned: Sequence[Mapping[str, Any]],
                       out: dict[str, Any]) -> None:
    """The values the re-run steps own (``step_migrate.Spec.values``): the served ones
    (parser ``post_project`` of the binding on the type's objects, found from the type's
    versions by an id range) deleted, the layer's copied in onto the served versions they
    name. A layer value whose version Postgres does not hold refuses the migration (rolled
    back): a value is never attached to another version."""
    from ontology import store_ddl
    from ontology.step_migrate import VALUE_PARSER
    from operating.pg.ontology_delta import _copy, _match
    from operating.pg.ontology_load import NUMERIC, _number

    deleted = 0
    cur.execute("CREATE TEMP TABLE step_srcs (src bigint PRIMARY KEY) ON COMMIT DROP")
    for o in owned:
        cur.execute(
            "INSERT INTO step_srcs SELECT DISTINCT s.src FROM ontology.object_versions v "
            "JOIN ontology.value_sources s ON s.ver = v.ver JOIN ontology.value_cells c "
            "ON c.src = s.src JOIN ontology.value_kinds k ON k.kind = c.kind WHERE "
            "v.object_id COLLATE \"C\" >= %s AND v.object_id COLLATE \"C\" < %s AND "
            "s.binding_id = %s AND k.parser = %s ON CONFLICT DO NOTHING",
            (o["prefix"], o["end"], o["binding"], VALUE_PARSER))
        cur.execute(
            "DELETE FROM ontology.value_cells c USING ontology.value_kinds k WHERE "
            "k.kind = c.kind AND k.parser = %s AND c.src IN (SELECT src FROM step_srcs)",
            (VALUE_PARSER,))
        deleted += cur.rowcount
    cur.execute("DELETE FROM ontology.value_sources s WHERE s.src IN (SELECT src FROM "
                "step_srcs) AND NOT EXISTS (SELECT 1 FROM ontology.value_cells c WHERE "
                "c.src = s.src)")
    out["deleted.values"] = deleted
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        rows = conn.execute(f"SELECT {', '.join(store_ddl.VALUE_COLUMNS)} FROM object_values "
                            "ORDER BY object_id, l1_handle, binding_id, property").fetchall()
    finally:
        conn.close()
    col = {c: i for i, c in enumerate(store_ddl.VALUE_COLUMNS)}
    versions: dict[tuple[str, str], tuple[int, str, str]] = {}
    for r in rows:
        key = (r[col["object_id"]], r[col["l1_handle"]])
        if key in versions:
            continue
        cur.execute("SELECT ver, digest, lineage FROM ontology.object_versions WHERE "
                    "object_id = %s AND l1_handle = %s", key)
        found = cur.fetchall()
        if len(found) != 1:
            raise Refused("step_value_version_not_served")
        versions[key] = found[0]
    sources, fields, kinds = [], [], []
    for n, r in enumerate(rows):
        ver, digest, lineage = versions[(r[col["object_id"]], r[col["l1_handle"]])]
        # digest / lineage stored only where they are not the version's (migration 0007)
        sources.append((n, ver, r[col["binding_id"]],
                        None if r[col["digest"]] == digest else r[col["digest"]],
                        None if r[col["lineage"]] == lineage else r[col["lineage"]],
                        r[col["recorded_at"]], r[col["superseded_at"]]))
        fields.append((n, *(r[col[c]] for c in store_ddl.FIELD_COLUMNS)))
        kinds.append((n, *(r[col[c]] if c != "markings" else (r[col[c]] or "[]")
                           for c in store_ddl.KIND_COLUMNS)))
    src = _match(cur, "value_sources", "src", "value_sources_src",
                 ("ver", "binding_id", "digest", "lineage", "recorded_at", "superseded_at"),
                 sources)
    field = _match(cur, "value_fields", "field", "value_fields_field",
                   store_ddl.FIELD_COLUMNS, fields)
    kind = _match(cur, "value_kinds", "kind", "value_kinds_kind", store_ddl.KIND_COLUMNS,
                  kinds)
    out["copied.values"] = _copy(
        cur, "value_cells", ("src", "field", "kind", "value", "value_num", "currency",
                             "currency_citation"),
        ((src[n], field[n], kind[n], r[col["value"]],
          _number(r[col["value"]]) if r[col["value_type"]] in NUMERIC else None,
          r[col["currency"]], r[col["currency_citation"]]) for n, r in enumerate(rows)))


def _apply_units(cur: Any, layer: Path, record: Mapping[str, Any],
                 report: dict[str, Any]) -> None:
    """The unit migration part (module docstring)."""
    from ontology.project import event_id_of
    from operating.pg.ontology_delta import _DELETE, _insert

    started = time.monotonic()
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
        bindings = record["delete"]["bindings"]
        for spec in bindings.values():
            cur.execute("INSERT INTO owned SELECT object_id FROM ontology.objects WHERE "
                        "object_type = %s AND starts_with(object_id, %s)",
                        (spec["type"], spec["prefix"]))
        cur.execute("ANALYZE owned")
        # what the served rows were: dropped objects, audience moves, new versions
        cur.execute("CREATE TEMP TABLE prior ON COMMIT DROP AS SELECT o.object_id, "
                    "o.binding_id, o.audience_key FROM ontology.objects o JOIN owned "
                    "USING (object_id)")
        cur.execute("CREATE TEMP TABLE prior_versions ON COMMIT DROP AS SELECT DISTINCT "
                    "v.object_id, v.l1_handle FROM ontology.object_versions v JOIN owned "
                    "USING (object_id)")
        cur.execute("CREATE INDEX ON prior (object_id)")
        cur.execute("CREATE INDEX ON prior_versions (object_id, l1_handle)")
        cur.execute("ANALYZE prior")
        cur.execute("ANALYZE prior_versions")
        # the events the served schema made from these versions (``_delete_unit``)
        cur.execute("CREATE TEMP TABLE gone_events (event_id text PRIMARY KEY) "
                    "ON COMMIT DROP")
        for spec in bindings.values():
            if not spec["events"]:
                continue
            cur.execute("SELECT v.object_id, v.l1_handle FROM ontology.object_versions v "
                        "JOIN owned o ON o.object_id = v.object_id "
                        "WHERE starts_with(v.object_id, %s)", (spec["prefix"],))
            ids = {event_id_of(eid, handle, oid, part)
                   for oid, handle in cur.fetchall() for eid, part in spec["events"]}
            with cur.copy("COPY gone_events (event_id) FROM STDIN") as copy:
                for eid in ids:
                    copy.write_row((eid,))
        for table in ("event_objects", "events"):
            cur.execute(f"DELETE FROM ontology.{table} WHERE event_id IN "
                        "(SELECT event_id FROM gone_events)")
            report[f"deleted.{table}"] = cur.rowcount
        for table, where in _DELETE:
            if table in _TABLES:
                cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
                report[f"deleted.{table}"] = cur.rowcount
        links = 0
        for link in record["delete"]["links"]:
            cur.execute("DELETE FROM ontology.link_rows WHERE link_type = %s AND "
                        "binding_kind = %s AND starts_with(from_object, %s) AND "
                        "starts_with(to_object, %s)",
                        (link["link_type"], link["kind"], link["from"], link["to"]))
            links += cur.rowcount
        report["deleted.link_rows"] = links
        report["seconds.delete"] = round(time.monotonic() - started, 3)
        ids = json.dumps([r[0] for r in conn.execute("SELECT object_id FROM objects")])
        report.update(_insert(conn, str(layer), cur, ids, every_link=True))
        report["seconds.insert"] = round(time.monotonic() - started
                                         - report["seconds.delete"], 3)
        _dropped(cur, report)
        _text_index(cur, record, report)
        _search(cur, record, report)
        # newer than the snapshot: what a delta wrote in scope since it began
        report["reproject"] = reproject(cur, _newer(cur, record["scope"],
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
        report["version"] = version
    finally:
        conn.close()


def _dropped(cur: Any, report: dict[str, Any]) -> None:
    """The objects the migration drops (a binding filter, a binding gone): every link
    naming one goes, a step's too (a full build holds none), and their text fields."""
    cur.execute("CREATE TEMP TABLE gone ON COMMIT DROP AS SELECT p.object_id FROM prior p "
                "WHERE NOT EXISTS (SELECT 1 FROM ontology.objects o WHERE o.object_id = "
                "p.object_id)")
    cur.execute("ANALYZE gone")
    n = 0
    for end in ("from_object", "to_object"):
        cur.execute(f"DELETE FROM ontology.link_rows WHERE {end} IN "
                    "(SELECT object_id FROM gone)")
        n += cur.rowcount
    report["dropped.objects"] = cur.execute("SELECT count(*) FROM gone").fetchone()[0]
    report["dropped.link_rows"] = n


def _text_index(cur: Any, record: Mapping[str, Any], report: dict[str, Any]) -> None:
    """The text index of the rewritten objects (``ontology/text_index.py``: a field's
    markings are its version's and those its path's properties carry,
    ``Projector._marks_for_path``) brought under the new schema in this transaction: the
    fields of a version no longer held go, every other field is marked again. What the
    index would hold anew (text values on other paths of a binding holding text) needs
    the text index step: refused (``text_index_input``); an object holding a version the
    served rows did not is queued for the delta to project again (``ontology.reproject``:
    its text is indexed then)."""
    from ontology.step_migrate import SENSITIVE_MARKS
    from ontology.store import StoreError, markings_text
    bindings = record["delete"]["bindings"]
    cur.execute("SELECT DISTINCT p.binding_id FROM prior p JOIN ontology.text_fields f "
                "ON f.object_id = p.object_id")
    texted = {r[0] for r in cur.fetchall()} | set(record.get("document_bindings") or ())
    for bid in sorted(texted & set(bindings)):
        if bindings[bid].get("text_paths_changed"):
            raise Refused(f"text_index_input:{bid}:text_paths")
    report["text_index.reprojected"] = 0
    if texted:
        # a version the served rows did not hold (captured since, or an object the change
        # adds) has no text fields yet: its object is projected again by the next delta
        # cycle, which indexes its text (withheld until then, never shown unmarked)
        cur.execute("SELECT DISTINCT o.object_id FROM ontology.objects o JOIN "
                    "ontology.object_versions v ON v.object_id = o.object_id WHERE "
                    "o.binding_id = ANY(%s) AND NOT EXISTS (SELECT 1 FROM prior_versions p "
                    "WHERE p.object_id = v.object_id AND p.l1_handle = v.l1_handle)",
                    (sorted(texted),))
        report["text_index.reprojected"] = reproject(
            cur, sorted(r[0] for r in cur.fetchall()), record["id"])
    cur.execute("CREATE TEMP TABLE gone_fields ON COMMIT DROP AS SELECT f.field_id FROM "
                "ontology.text_fields f JOIN prior p ON p.object_id = f.object_id WHERE NOT "
                "EXISTS (SELECT 1 FROM ontology.object_versions v WHERE v.object_id = "
                "f.object_id AND v.l1_handle = f.version_handle)")
    cur.execute("DELETE FROM ontology.text_chunks WHERE field_id IN "
                "(SELECT field_id FROM gone_fields)")
    report["deleted.text_chunks"] = cur.rowcount
    cur.execute("DELETE FROM ontology.text_fields WHERE field_id IN "
                "(SELECT field_id FROM gone_fields)")
    report["deleted.text_fields"] = cur.rowcount
    cur.execute("SELECT f.field_id, o.binding_id, f.field_path, f.markings, "
                "array_agg(DISTINCT v.markings) FROM ontology.text_fields f "
                "JOIN prior p ON p.object_id = f.object_id "
                "JOIN ontology.objects o ON o.object_id = f.object_id "
                "JOIN ontology.object_versions v ON v.object_id = f.object_id "
                "AND v.l1_handle = f.version_handle "
                "GROUP BY f.field_id, o.binding_id, f.field_path, f.markings")
    changed = []
    for field_id, bid, path, held, versions in cur.fetchall():
        marks = bindings.get(bid, {}).get("marks")
        if marks is None:   # an object of a binding the migration does not rewrite
            raise Refused(f"text_index_markings:{bid}")
        want: set[str] = set(marks["type"])
        want.update(marks["paths"].get(path[len("row."):] if path.startswith("row.")
                                       else path, ()))
        try:
            for text in versions:
                want.update(json.loads(text))
            # a payroll or HR-confidential document's pair (the text index's, not its
            # version's or its path's) stays: stricter, never wider
            if set(SENSITIVE_MARKS) <= set(json.loads(held)):
                want.update(SENSITIVE_MARKS)
            new = markings_text(want)
        except (StoreError, TypeError, ValueError) as exc:
            raise Refused(f"text_index_markings:{bid}") from exc
        if new != held:
            changed.append((field_id, new))
    if changed:
        cur.execute("CREATE TEMP TABLE remarked (field_id bigint PRIMARY KEY, "
                    "markings text NOT NULL) ON COMMIT DROP")
        with cur.copy("COPY remarked (field_id, markings) FROM STDIN") as copy:
            for row in changed:
                copy.write_row(row)
        cur.execute("UPDATE ontology.text_fields f SET markings = r.markings FROM remarked r "
                    "WHERE r.field_id = f.field_id")
    report["remarked.text_fields"] = len(changed)


def _search(cur: Any, record: Mapping[str, Any], report: dict[str, Any]) -> None:
    """The search tables' copies of the rewritten objects' permissions
    (``search.chunks`` / ``search.entities``: each row keyed by its object's audience and
    the markings a reader of its text must hold), in this transaction: a dropped object's
    rows go, an object whose audience moved moves, and a marking the migration adds to a
    binding is added to its objects' rows (a removed one stays until the rows are written
    again: stricter, never wider)."""
    cur.execute("SELECT to_regclass('search.chunks') IS NOT NULL, "
                "to_regclass('search.entities') IS NOT NULL")
    present = [t for t, held in zip(("chunks", "entities"), cur.fetchone()) if held]
    for table in present:
        cur.execute(f"DELETE FROM search.{table} WHERE object_id IN "
                    "(SELECT object_id FROM gone)")
        report[f"deleted.search.{table}"] = cur.rowcount
        cur.execute(f"UPDATE search.{table} t SET audience_key = o.audience_key FROM prior p "
                    "JOIN ontology.objects o ON o.object_id = p.object_id WHERE t.object_id "
                    "= p.object_id AND t.audience_key = p.audience_key AND o.audience_key "
                    "<> p.audience_key")
        report[f"moved.search.{table}"] = cur.rowcount
        n = 0
        for bid, spec in sorted(record["delete"]["bindings"].items()):
            added = spec.get("added_marks") or []
            if not added:
                continue
            cur.execute(f"UPDATE search.{table} t SET markings = ARRAY(SELECT DISTINCT m "
                        "FROM unnest(t.markings || %s::text[]) m ORDER BY m) FROM prior p "
                        "WHERE t.object_id = p.object_id AND p.binding_id = %s AND NOT "
                        "(t.markings @> %s::text[])", (added, bid, added))
            n += cur.rowcount
        report[f"remarked.search.{table}"] = n
    if present:   # a reader's cached search scope is read again
        cur.execute("SELECT to_regclass('search.write_generation') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT nextval('search.write_generation')")


def check(dsn: str, digest: str | None = None) -> dict[str, Any]:
    """The deploy check (module docstring)."""
    import psycopg
    if digest is None:
        from ontology.schema import load_schema
        digest = load_schema().digest
    with psycopg.connect(dsn, connect_timeout=5) as pg, pg.cursor() as cur:
        served = served_schema(cur)
        whole = _whole(cur)
        steps = served_steps(cur)
    from ontology.step_migrate import steps_digest
    code_steps = steps_digest()
    out = {"code": digest, "served": served, "generation_whole": whole is not None,
           "steps_code": code_steps, "steps_served": steps}
    if whole is None:
        return {**out, "ok": False, "reason": "postgres_holds_no_whole_generation"}
    if steps is None:   # fail closed: which step code built the generation is not known
        return {**out, "ok": False, "reason": "steps_baseline_unknown",
                "next": "python3 -m operating.pg.ontology_migrate seed-steps --ref "
                        "<the commit that built the loaded generation>"}
    pending = []
    if served != digest:
        pending.append("python3 -m ontology.schema_migrate build --from-ref <served commit> "
                       "--state-dir /data/plx-state --out <layer>")
    if steps != code_steps or served != digest:
        pending.append("python3 -m ontology.step_migrate build --from-ref <served commit> "
                       "--state-dir /data/plx-state --out <steps layer> [--unit-layer <layer>]")
    if served == digest and steps == code_steps:
        return {**out, "ok": True}
    return {**out, "ok": False,
            "reason": "migration_pending" if served != digest else "step_migration_pending",
            "next": " && ".join(pending + ["python3 -m operating.pg.ontology_migrate apply "
                                           "[<layer>] --steps <steps layer>"])}


def seed_steps(dsn: str, ref: str | None = None, digest: str | None = None) -> dict[str, Any]:
    """Record which build-step code built the generation Postgres holds
    (``store_meta.steps_digest``: the step code of commit ``ref``, or ``digest``), for a
    generation loaded without it. Refused once a digest is held (a step migration moves
    it)."""
    import psycopg
    from ontology.step_migrate import steps_digest
    digest = digest or steps_digest(ref)
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute(LOCK)
        held = served_steps(cur)
        if held is not None:
            return {"seeded": False, "reason": "steps_digest_held", "served": held}
        cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES ('steps_digest', %s)",
                    (digest,))
    return {"seeded": True, "steps_digest": digest}


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.ontology_migrate")
    parser.add_argument("--dsn", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply")
    a.add_argument("layer", nargs="?", default=None, help="the unit migration layer")
    a.add_argument("--steps", default=None, help="the step migration layer (same transaction)")
    c = sub.add_parser("check")
    c.add_argument("--digest", default=None)
    sd = sub.add_parser("seed-steps", help="record the step code of the loaded generation")
    sd.add_argument("--ref", required=True, help="the commit that built the generation")
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.command == "apply":
        out: Mapping[str, Any] = apply_migration(args.layer, dsn, steps=args.steps)
    elif args.command == "seed-steps":
        out = seed_steps(dsn, args.ref)
    else:
        out = check(dsn, args.digest)
    print(json.dumps(out, sort_keys=True), flush=True)
    return 0 if out.get("applied") or out.get("ok") or out.get("seeded") else 1


if __name__ == "__main__":
    sys.exit(main())
