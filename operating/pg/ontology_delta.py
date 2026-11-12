"""Apply a row-delta layer (``ontology/delta.py``) to the Postgres ontology in place.

``python3 -m operating.pg.ontology_delta LAYER [LAYER ...] [--dsn DSN]``, or
``python3 -m operating.pg.ontology_delta --follow STATE_DIR`` (``follow``: every layer the
served delta lists, after loading the served generation when Postgres holds another).

A delta layer is a store of the generation's DDL whose ``delta_owned`` objects replace the
generation's (the objects of the records changed since it was built, and the served ones
that gained a link to them, each projected with every record its derivation reads). Here
each owned object's rows are replaced in ``ontology.*`` in one transaction: its object,
versions, removals, value sources and values, identifiers, document text fields and
chunks (and their words), the events naming it and every link with an end at it are deleted and the layer's
rows copied in, in the compact layout of ``operating.pg.ontology_load`` (migration 0007):
a layer's versions, value sources, text fields and chunks take new row numbers from the
ontology's sequences, its value fields and kinds are matched to the ontology's by content
(a new one is added), its packed values are unpacked (their sources recorded in
``packed_sources``: never word-indexed, migration 0010) and its links' texts stored plain.
A row whose key the store already holds (the delta follower wrote the same content-keyed row
a migration layer carries) is replaced by the layer's (``_copy``, ``UPSERT``).
Layers are applied oldest first, so an object owned by several ends with the newest
layer's rows.

A layer is applied only over the generation it was built on: ``ontology.load_state``
``generation.content_digest`` must equal the layer's ``content_digest`` (the delta keeps
the served generation's). Each applied layer is recorded under ``delta:<file name>`` and is
not applied twice; ``delta_layers`` lists them in order.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence

from ontology import store_ddl
from operating.pg import whole_steps as _whole_steps
from operating.pg.ontology_load import TABLES, _clean, _context, _number

_CHUNK = 200_000   # sources whose value cells are read at once
_OWNED = "(SELECT value FROM json_each(:ids))"
_OWNED_PG = "(SELECT object_id FROM owned)"
_EVENTS_PG = f"(SELECT event_id FROM ontology.event_objects WHERE object_id IN {_OWNED_PG})"
_VERS_PG = f"(SELECT ver FROM ontology.object_versions WHERE object_id IN {_OWNED_PG})"
_FIELDS_PG = f"(SELECT field_id FROM ontology.text_fields WHERE object_id IN {_OWNED_PG})"
# delete order (dependents first)
_DELETE = (
    ("events", f"event_id IN {_EVENTS_PG}"),
    ("event_objects", f"event_id IN {_EVENTS_PG}"),
    # two deletes, each on its own index (one with OR scans every link); the links of a
    # whole-store step are not the layer's to replace (``operating.pg.whole_steps``)
    ("link_rows", f"from_object IN {_OWNED_PG} AND {_whole_steps.pg_excluded()}"),
    ("link_rows", f"to_object IN {_OWNED_PG} AND {_whole_steps.pg_excluded()}"),
    ("value_cells", f"src IN (SELECT src FROM ontology.value_sources WHERE ver IN {_VERS_PG})"),
    ("packed_sources", f"src IN (SELECT src FROM ontology.value_sources WHERE ver IN "
                       f"{_VERS_PG})"),
    ("value_sources", f"ver IN {_VERS_PG}"),
    ("identifiers", f"object_id IN {_OWNED_PG}"),
    ("chunk_words", f"chunk_id IN (SELECT chunk_id FROM ontology.text_chunks WHERE "
                    f"field_id IN {_FIELDS_PG})"),
    ("text_chunks", f"field_id IN {_FIELDS_PG}"),
    ("text_fields", f"object_id IN {_OWNED_PG}"),
    ("object_versions", f"object_id IN {_OWNED_PG}"),
    ("object_removals", f"object_id IN {_OWNED_PG}"),
    ("objects", f"object_id IN {_OWNED_PG}"),
)
_PLAIN = {
    "objects": f"object_id IN {_OWNED}",
    "object_removals": f"object_id IN {_OWNED}",
    "identifiers": f"object_id IN {_OWNED}",
    "event_objects": f"event_id IN (SELECT event_id FROM event_objects WHERE object_id IN "
                     f"{_OWNED})",
    "events": f"event_id IN (SELECT event_id FROM event_objects WHERE object_id IN {_OWNED})",
}


# The follower's connections give up on a server that does not answer, and its
# statements and lock waits end in time: a hung connection or a load that never releases
# its lock fails this cycle (the next one tries again) instead of holding the follower.
FOLLOW_LIMITS = {"connect_timeout": 10,
                 "options": "-c statement_timeout=1800000 -c lock_timeout=600000"}


# Tables whose rows are keyed by their content or their object, so a layer can hold a row
# the served store already holds: a delta follower writes the same content-keyed rows
# (events, links, identifiers, text) a migration layer then copies. Their layer rows
# replace the served ones on the key. Every other table of ``_insert`` takes new row
# numbers from a sequence, or matches by content (``_match``), and cannot meet its own key.
UPSERT = frozenset({"objects", "events", "event_objects", "identifiers", "object_removals",
                    "link_rows", "text_fields", "text_chunks", "chunk_words"})
_ORDER = "_stage_order"
# columns of the compact layout (migration 0007) a layer's plain row leaves unset: a served
# row stored packed would otherwise keep them beside the layer's plain text (the table's
# check refuses both)
_UNPACK = {"link_rows": ("reason_id", "evidence_id", "ek", "slots")}


def _primary_key(cur: Any, table: str, keys: dict[str, list[str]] | None) -> list[str]:
    """``ontology.<table>``'s primary key columns, in key order. Read from the catalog once
    per ``keys`` (a transaction's cache, ``_insert``'s) when one is given."""
    if keys is not None and table in keys:
        return keys[table]
    cur.execute("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = "
                "i.indrelid AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass "
                "AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)",
                (f"ontology.{table}",))
    key = [row[0] for row in cur.fetchall()]
    if not key:
        raise RuntimeError(f"ontology.{table} has no primary key to upsert on")
    if keys is not None:
        keys[table] = key
    return key


def _copy(cur: Any, table: str, columns: Sequence[str], rows: Any, *,
          keys: dict[str, list[str]] | None = None) -> int:
    """Copy ``rows`` into ``ontology.<table>``; the number of rows read.

    A table of ``UPSERT`` is staged in a temporary table (dropped at commit) and inserted
    ``ON CONFLICT`` on its primary key, ``DO UPDATE`` of every other column: a row the
    store already holds is replaced by the layer's, and of several rows of one key in
    ``rows`` the last wins (``SELECT DISTINCT ON``), so neither fails the transaction.
    A replaced link row stored packed (migration 0007) becomes the layer's plain one
    (``_UNPACK``). Other tables are copied directly, and a duplicate key there still fails loudly.
    ``keys``: the primary keys read so far in this transaction."""
    cols = ", ".join(columns)
    count = 0
    key = _primary_key(cur, table, keys) if table in UPSERT else []
    if key and not set(key) <= set(columns):
        raise RuntimeError(f"ontology.{table}: the primary key {key} is not among {cols}")
    if not key:
        with cur.copy(f"COPY ontology.{table} ({cols}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(tuple(_clean(v) for v in row))
                count += 1
        return count
    stage = f"stage_{table}"
    cur.execute(f"CREATE TEMP TABLE IF NOT EXISTS {stage} ON COMMIT DROP AS SELECT {cols} "
                f"FROM ontology.{table} WITH NO DATA")
    cur.execute(f"ALTER TABLE {stage} ADD COLUMN IF NOT EXISTS {_ORDER} bigint "
                "GENERATED ALWAYS AS IDENTITY")
    cur.execute(f"TRUNCATE {stage}")
    with cur.copy(f"COPY {stage} ({cols}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(tuple(_clean(v) for v in row))
            count += 1
    on = ", ".join(key)
    sets = ", ".join([f"{c} = excluded.{c}" for c in columns if c not in key]
                     + [f"{c} = DEFAULT" for c in _UNPACK.get(table, ()) if c not in columns])
    cur.execute(f"INSERT INTO ontology.{table} ({cols}) SELECT DISTINCT ON ({on}) {cols} "
                f"FROM {stage} ORDER BY {on}, {_ORDER} DESC ON CONFLICT ({on}) "
                f"{'DO UPDATE SET ' + sets if sets else 'DO NOTHING'}")
    cur.execute(f"TRUNCATE {stage}")
    return count


def _numbers(cur: Any, sequence: str, count: int) -> list[int]:
    if not count:
        return []
    cur.execute(f"SELECT nextval('ontology.{sequence}') FROM generate_series(1, %s)", (count,))
    return [row[0] for row in cur.fetchall()]


def _match(cur: Any, table: str, key: str, sequence: str, columns: Sequence[str],
           rows: Sequence[tuple]) -> dict[int, int]:
    """Layer ids of ``rows`` (id, *columns) -> the ontology's ids of the same content;
    content the ontology does not hold yet is added with a new id."""
    out: dict[int, int] = {}
    match = " AND ".join(f"{c} IS NOT DISTINCT FROM %s" for c in columns)
    for layer_id, *content in rows:
        cur.execute(f"SELECT {key} FROM ontology.{table} WHERE {match}", content)
        found = cur.fetchone()
        if found is None:
            cur.execute(f"INSERT INTO ontology.{table} ({key}, {', '.join(columns)}) VALUES "
                        f"(nextval('ontology.{sequence}'), {', '.join(['%s'] * len(columns))}) "
                        f"RETURNING {key}", content)
            found = cur.fetchone()
        out[layer_id] = found[0]
    return out


def apply_layer(layer: str | Path, dsn: str, *, newer: Sequence[str | Path] = ()
                ) -> dict[str, Any]:
    """Replace the layer's owned objects' rows in ``ontology.*`` (module docstring).
    ``newer``: layers above this one (a fold applied after the layers added while it ran):
    the objects they own keep their rows."""
    import psycopg

    layer = Path(layer)
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        digest = conn.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                              ).fetchone()[0]
        above: set[str] = set()
        for path in newer:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as top:
                above.update(r[0] for r in top.execute("SELECT object_id FROM delta_owned"))
        owned = [r[0] for r in conn.execute("SELECT object_id FROM delta_owned ORDER BY 1")
                 if r[0] not in above]
        ids = json.dumps(owned)
        report: dict[str, Any] = {"layer": layer.name, "owned": len(owned)}
        with psycopg.connect(dsn, **FOLLOW_LIMITS) as pg, pg.cursor() as cur:
            # one layer at a time (a cycle and the fold may finish together); the
            # generation is read under the lock: a load emptying the tables waits for
            # this transaction, and a load in progress refuses the layer
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
            cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'")
            row = cur.fetchone()
            loaded = (row[0] or {}).get("content_digest") if row else None
            if loaded != digest:
                return {**report, "applied": False, "reason": "other_generation"}
            from operating.pg.ontology_load import OPTIONAL, ORDER
            cur.execute("SELECT substr(name, 7) FROM ontology.load_state "
                        "WHERE name LIKE 'table:%%'")
            done = {r[0] for r in cur.fetchall()}
            needed = set(ORDER) - set(OPTIONAL) - set((row[0] or {}).get("deferred") or ())
            if not needed <= done:
                return {**report, "applied": False, "reason": "generation_loading"}
            cur.execute("SELECT ontology.sq_data_version()")
            before = cur.fetchone()[0]   # the version this layer's kept facts fold over
            cur.execute("SELECT 1 FROM ontology.load_state WHERE name = %s",
                        (f"delta:{layer.name}",))
            if cur.fetchone() is not None:
                return {**report, "applied": False, "reason": "already_applied"}
            # a layer applies only under the schema Postgres serves; one of a schema a
            # migration moved from applies without the objects the migration rewrote
            # (those are projected again: ``ontology.reproject``)
            from operating.pg import ontology_migrate
            held_out = ontology_migrate.layer_scope(cur, _meta(conn, "schema_digest"))
            if held_out is None:
                return {**report, "applied": False, "reason": "other_schema",
                        "layer_digest": _meta(conn, "schema_digest"),
                        "served": ontology_migrate.served_schema(cur)}
            # likewise a layer of build-step code a step migration moved from
            steps_out = ontology_migrate.steps_scope(cur, _meta(conn, "steps_digest"))
            if steps_out is None:
                return {**report, "applied": False, "reason": "other_steps",
                        "layer_digest": _meta(conn, "steps_digest"),
                        "served": ontology_migrate.served_steps(cur)}
            if steps_out:
                held_out = {"id": held_out.get("id") or steps_out["id"],
                            "scope": sorted(set(held_out.get("scope") or ())
                                            | set(steps_out["scope"]))}
            if held_out:
                kept = [oid for oid in owned
                        if not any(oid.startswith(p) for p in held_out["scope"])]
                ontology_migrate.reproject(cur, sorted(set(owned) - set(kept)),
                                           held_out["id"])
                report["held_out"] = len(owned) - len(kept)
                owned = kept
                ids = json.dumps(owned)
            cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
            with cur.copy("COPY owned (object_id) FROM STDIN") as copy:
                for oid in owned:
                    copy.write_row((oid,))
            cur.execute("ANALYZE owned")
            cur.execute("SET LOCAL jit = off")   # many short statements: no JIT
            for table, where in _DELETE:
                cur.execute(f"DELETE FROM ontology.{table} WHERE {where}")
                report[f"deleted.{table}"] = report.get(f"deleted.{table}", 0) + cur.rowcount
            report.update(_insert(conn, str(layer), cur, ids))
            ontology_migrate.log_owned(cur, layer.name, owned)
            if not held_out:   # under the served schema: no longer to be projected again
                ontology_migrate.done(cur)
            report["meta"] = _freshness(conn, cur)
            cur.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                        (f"delta:{layer.name}", json.dumps({"owned": len(owned),
                                                            "content_digest": digest})))
            cur.execute("INSERT INTO ontology.load_state (name, value) VALUES "
                        "('delta_layers', %s) ON CONFLICT (name) DO UPDATE SET value = "
                        "ontology.load_state.value || excluded.value, updated_at = now()",
                        (json.dumps([layer.name]),))
        # committed: the traverse facts of the version it made, folded from the layer
        # (``fold_layer_facts``), so no host reads them in full on a request
        from operating import ontology_tools_traverse
        report["facts_kept"] = ontology_tools_traverse.fold_layer_facts(
            dsn, layer, owned, before, (f"delta:{layer.name}", "delta_layers"))
        return {**report, "applied": True}
    finally:
        conn.close()


def _relation_completeness(held: str | None, layer: str | None, families: set) -> str:
    """``relation_completeness`` after a layer that read ``families``: each such
    family's facts are the layer's, or none when the layer states none."""
    out = json.loads(held or "{}")
    mine = json.loads(layer or "{}")
    if not isinstance(out, dict) or not isinstance(mine, dict):
        raise ValueError("relation_completeness is not an object")
    for family in families:
        if isinstance(mine.get(family), dict):
            out[family] = mine[family]
        else:
            out.pop(family, None)
    return json.dumps(out, sort_keys=True)


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _freshness(conn: sqlite3.Connection, cur: Any) -> int:
    """The layer's capture freshness for the families it read (``delta_families``) into
    ``ontology.store_meta`` (``source_freshness`` per family, ``freshness`` per source,
    ``source_condition_unmet`` united), so a reader's freshness is the newest layer's;
    the keys written. ``relation_completeness`` follows per family: the layer's when it
    states one, else the family's is dropped (a stale per-relation fact never vouches
    for a newer partial read)."""
    mine = dict(conn.execute("SELECT key, value FROM store_meta"))
    try:
        families = set(json.loads(mine.get("delta_families") or "[]"))
    except ValueError:
        return 0
    if not families:
        return 0
    cur.execute("SELECT key, value FROM ontology.store_meta WHERE key IN "
                "('source_freshness', 'freshness', 'source_condition_unmet', "
                "'relation_completeness') FOR UPDATE")
    held = dict(cur.fetchall())
    out: dict[str, str] = {}
    try:
        fresh = json.loads(held.get("source_freshness") or "{}")
        layer_fresh = json.loads(mine.get("source_freshness") or "{}")
        for family in families & set(layer_fresh):
            fresh[family] = layer_fresh[family]
        out["source_freshness"] = json.dumps(fresh, sort_keys=True)
        entries = {e["source"]: e for e in json.loads(held.get("freshness") or "[]")}
        for entry in json.loads(mine.get("freshness") or "[]"):
            if entry.get("source") in families and entry.get("captured_at"):
                entries[entry["source"]] = entry
        out["freshness"] = json.dumps([entries[k] for k in sorted(entries)])
        unmet = json.loads(held.get("source_condition_unmet") or "{}")
        for where, handles in json.loads(mine.get("source_condition_unmet") or "{}").items():
            unmet[where] = sorted(set(unmet.get(where, ())) | set(handles))
        out["source_condition_unmet"] = json.dumps(unmet, sort_keys=True)
        out["relation_completeness"] = _relation_completeness(
            held.get("relation_completeness"), mine.get("relation_completeness"), families)
    except (ValueError, TypeError, AttributeError, KeyError):
        return 0
    for key, value in out.items():
        cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) ON CONFLICT "
                    "(key) DO UPDATE SET value = excluded.value", (key, value))
    return len(out)


def _insert(conn: sqlite3.Connection, path: str, cur: Any, ids: str, *,
            every_link: bool = False) -> dict[str, int]:
    """Copy the rows of the objects ``ids`` (a JSON list) from the layer at ``path``
    (``conn``) into ``ontology.*``; their links, or with ``every_link`` every link the
    layer holds (a migration's link units may join objects it does not hold)."""
    report: dict[str, int] = {}
    keys: dict[str, list[str]] = {}   # primary keys, read once per table in this transaction
    for table in ("objects", "object_removals", "identifiers", "events", "event_objects"):
        spec = TABLES[table]
        # a layer's row number is its own: the ontology numbers the row (column default)
        numbered = spec.columns[0] == "row_no"
        select = spec.select.replace("SELECT rowid, ", "SELECT ", 1) if numbered \
            else spec.select
        rows = conn.execute(f"SELECT * FROM ({select}) WHERE {_PLAIN[table]}", {"ids": ids})
        report[f"copied.{table}"] = _copy(cur, table, spec.columns[numbered:], rows, keys=keys)

    # versions and sources are streamed from the layer (6M rows each held as tuples were
    # part of the 11-08 OOM); only the layer-id -> ontology-id maps stay in memory
    owned_versions = (f"SELECT rowid, object_id, l1_handle, digest, lineage, markings, "
                      f"recorded_at, superseded_at FROM object_versions WHERE object_id IN "
                      f"{_OWNED} ORDER BY rowid")
    n_versions = conn.execute(f"SELECT count(*) FROM ({owned_versions})", {"ids": ids}).fetchone()[0]
    ver: dict[int, int] = {}
    ver_numbers = iter(_numbers(cur, "object_versions_ver", n_versions))

    def version_rows() -> Any:
        for v in conn.execute(owned_versions, {"ids": ids}):
            ver[v[0]] = next(ver_numbers)
            yield (ver[v[0]], *v[1:])
    report["copied.object_versions"] = _copy(
        cur, "object_versions", TABLES["object_versions"].columns, version_rows())

    owned_sources = ("SELECT src, ver, binding_id, digest, lineage, recorded_at, "
                     "superseded_at FROM value_sources WHERE ver IN (SELECT rowid FROM "
                     f"object_versions WHERE object_id IN {_OWNED}) ORDER BY src")
    n_sources = conn.execute(f"SELECT count(*) FROM ({owned_sources})", {"ids": ids}).fetchone()[0]
    src: dict[int, int] = {}
    src_numbers = iter(_numbers(cur, "value_sources_src", n_sources))

    def source_rows() -> Any:
        for s in conn.execute(owned_sources, {"ids": ids}):
            src[s[0]] = next(src_numbers)
            yield (src[s[0]], ver[s[1]], *s[2:])
    report["copied.value_sources"] = _copy(
        cur, "value_sources", TABLES["value_sources"].columns, source_rows())
    del src_numbers

    context = _context(path)
    numeric = context["numeric"]
    # value cells in chunks of sources (a whole migration's cells in one list took 25 GB)
    src_keys = list(src)
    copied = 0
    packed: set = set()
    for i0 in range(0, len(src_keys), _CHUNK):
        part = src_keys[i0:i0 + _CHUNK]
        cells = conn.execute(
            "SELECT src, field, layout, value, currency, currency_citation FROM value_cells "
            "WHERE src IN (SELECT value FROM json_each(:srcs))",
            {"srcs": json.dumps(part)}).fetchall()
        used_fields = {f for cell in cells for f, _ in context["layouts"][cell[2]]}
        used_kinds = {k for cell in cells for _, k in context["layouts"][cell[2]]}
        fields = _match(cur, "value_fields", "field", "value_fields_field",
                        ("property", "field_path"),
                        conn.execute("SELECT field, property, field_path FROM value_fields WHERE "
                                     "field IN (SELECT value FROM json_each(:f))",
                                     {"f": json.dumps(sorted(used_fields))}).fetchall())
        kinds = _match(cur, "value_kinds", "kind", "value_kinds_kind", store_ddl.KIND_COLUMNS,
                       conn.execute(f"SELECT kind, {', '.join(store_ddl.KIND_COLUMNS)} FROM "
                                    "value_kinds WHERE kind IN (SELECT value FROM json_each(:k))",
                                    {"k": json.dumps(sorted(used_kinds))}).fetchall())

        def values(cells=cells, fields=fields, kinds=kinds) -> Any:
            for cell in cells:
                for (s_, f, k, value, _num, currency, cite) in TABLES["value_cells"].shape(
                        context, cell):
                    yield (src[s_], fields[f], kinds[k], value,
                           _number(value) if k in numeric else None, currency, cite)
        copied += _copy(cur, "value_cells", TABLES["value_cells"].columns, values())
        packed.update(src[cell[0]] for cell in cells if cell[1] is None)
        del cells
    report["copied.value_cells"] = copied
    # its packed records' sources: their values are not word-indexed (migration 0010)
    report["copied.packed_sources"] = _copy(
        cur, "packed_sources", ("src",), sorted((p,) for p in packed))

    links = conn.execute(
        f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links" if every_link else
        f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links WHERE (from_object IN "
        f"{_OWNED} OR to_object IN {_OWNED}) AND {_whole_steps.sqlite_excluded()}",
        {} if every_link else {"ids": ids})
    report["copied.link_rows"] = _copy(cur, "link_rows", store_ddl.LINK_COLUMNS, links, keys=keys)

    try:
        text = conn.execute(
            "SELECT field_id, object_id, version_handle, citation, field_path, document_kind, "
            f"sender_class, markings, text_digest FROM text_fields WHERE object_id IN {_OWNED}",
            {"ids": ids}).fetchall()
    except sqlite3.OperationalError:   # a layer without the text index
        text = []
    field_id = dict(zip((t[0] for t in text), _numbers(cur, "text_fields_id", len(text))))
    report["copied.text_fields"] = _copy(cur, "text_fields", TABLES["text_fields"].columns,
                                         ((field_id[t[0]], *t[1:]) for t in text), keys=keys)
    chunks = conn.execute(
        "SELECT field_id, start, body FROM text_chunks WHERE field_id IN "
        "(SELECT value FROM json_each(:f))", {"f": json.dumps(list(field_id))}).fetchall() \
        if text else []
    numbers = _numbers(cur, "text_chunks_id", len(chunks))
    report["copied.text_chunks"] = _copy(
        cur, "text_chunks", TABLES["text_chunks"].columns,
        ((n, field_id[c[0]], c[1], c[2]) for n, c in zip(numbers, chunks)), keys=keys)
    # their words (migration 0010: FTS5's own tokenizer, as the layer's ``chunk_text``)
    from operating.pg.words import chunk_word_rows
    report["copied.chunk_words"] = _copy(
        cur, "chunk_words", ("chunk_id", "words"),
        chunk_word_rows((n, c[2]) for n, c in zip(numbers, chunks)), keys=keys)
    return report


def _keep_facts(dsn: str, report: dict[str, Any], text_store: Path | None = None,
                log: Any = None) -> bool:
    """The traverse facts of the version Postgres holds now kept (read in full here when no
    layer's fold kept them: after a load, a migration), never on a host's request. When
    none could be kept the reason is in ``report["facts_error"]`` and on ``log``: a
    host then reads them on its first request, minutes, so it is never silent."""
    try:
        from operating import ontology_tools_traverse
        kept = ontology_tools_traverse.prime_pg_facts(
            dsn, text_store=text_store if text_store and text_store.is_file() else None,
            log=log)
        report.pop("facts_error", None)
    except Exception as exc:  # noqa: BLE001 - reported: the cycle itself is done
        kept = False
        report["facts_error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        print(f"delta follower: traverse facts not kept: {report['facts_error']}",
              file=log or sys.stderr, flush=True)
    report["facts_kept"] = kept
    return kept


def apply_listed(stores: Sequence[str | Path], dsn: str, *, log: Any = None
                 ) -> dict[str, Any]:
    """Apply every layer of a delta pointer's list (``stores``, newest first) Postgres has
    not applied, oldest first, each with the layers above it as ``newer``."""
    stores = [str(s) for s in stores]
    results = []
    for index in range(len(stores) - 1, -1, -1):
        result = apply_layer(stores[index], dsn, newer=stores[:index])
        results.append(result)
        if log is not None:
            print(json.dumps(result, sort_keys=True), file=log, flush=True)
    newly = [r for r in results if r.get("applied")]
    out = {"applied": bool(newly), "layers": len(stores), "applied_now": len(newly),
           "owned": sum(r.get("owned", 0) for r in newly),
           "refused": sorted({r.get("reason") for r in results
                              if not r.get("applied") and r.get("reason")
                              != "already_applied"} - {None})}
    out["whole_steps"] = recompute_whole_steps(dsn, log=log)
    stalled = [r for r in results if r.get("reason") in STALLS]
    if stalled:
        out["stalled"] = stall(stalled[0])
        print(f"ONTOLOGY DELTA STALLED: {json.dumps(out['stalled'], sort_keys=True)}",
              file=sys.stderr, flush=True)
    return out


def recompute_whole_steps(dsn: str, *, log: Any = None) -> dict[str, Any]:
    """The whole-store steps brought up to date over the layers applied since their last
    run (``operating.pg.whole_steps``): their report, ``{"pending": False}`` when nothing
    is to be done, or ``{"error": ...}`` (the served links stay as they were and the next
    cycle tries again)."""
    try:
        report = _whole_steps.recompute(dsn)
    except Exception as exc:   # the layers are committed: report, keep the mark, go on
        report = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        print(f"ONTOLOGY WHOLE STEPS FAILED: {report['error']}", file=sys.stderr, flush=True)
    if log is not None and report != {"pending": False}:
        print(json.dumps({"whole_steps": report}, sort_keys=True), file=log, flush=True)
    return report


# a layer of code no migration leads from: every later layer waits behind it (the delta
# stalls until the migration of that code is applied or the code is deployed back)
STALLS = {"other_steps": "the build steps' code of this delta differs from what Postgres "
                         "serves and no step migration leads from it",
          "other_schema": "the schema of this delta differs from what Postgres serves and "
                          "no migration leads from it"}


def stall(result: Mapping[str, Any]) -> dict[str, Any]:
    """What a stalled delta reports (status and log): why, the digests, what to run."""
    return {"reason": result["reason"], "why": STALLS[result["reason"]],
            "layer": result.get("layer"), "layer_digest": result.get("layer_digest"),
            "served": result.get("served"),
            "action": "python3 -m operating.pg.ontology_migrate check (then build and apply "
                      "the migration it names), or deploy the code Postgres serves"}


# After a new generation is published, how long the older one stays in Postgres before
# an in-place reload empties it: a host moves to the SQLite generation at its next request
# (``ontology_reader.serves``); a request already running on Postgres (an answer's reads are
# pinned for up to 120 s) finishes first.
RELOAD_GRACE_SECONDS = 600


def _note_code(state_dir: Path) -> None:
    """The loader code the Postgres ontology was loaded by (``operating.derived_status``):
    other code's load is reported stale until the next load."""
    try:
        from operating import derived_status as ds
        ds.stamp("postgres-ontology", state_dir / ds.CODE_DIR / "postgres-ontology",
                 state_dir=state_dir)
    except Exception:  # noqa: BLE001 - unstamped: reported as older code's, never a failure
        pass


def follow(state_dir: str | Path, dsn: str, *, reload: bool = True, workers: int = 12,
           min_free: int = 0, log: Any = sys.stderr) -> dict[str, Any]:
    """Bring the Postgres ontology up to what is served now, once: when the served
    generation (``<state>/projection/ontology.sqlite``) is not the one Postgres holds, load
    it (``--replace``; ``reload=False``: report it and change nothing); then apply every
    layer the delta pointer (``<state>/projection/delta/CURRENT.json``) lists over that
    generation and Postgres has not applied, oldest first, each with the layers above it
    as ``newer``. Layers no longer listed were folded into a listed one, so nothing is
    missed whatever the interval between runs.

    Disk: every load keeps ``min_free`` and the next projection build's room free
    (``ontology_load.build_floor``: the next cold build's estimate, not a fixed floor). A
    load of a new generation starts only when it fits beside that room (``reload_room``):
    the bytes it writes (the held copy's size, else the store's), less the held copy an
    in-place reload empties first; the previous SQLite generation's files count as used
    until they are pruned (``waiting_for_prune``; else ``waiting_for_space``)."""
    import psycopg
    from operating.pg import ontology_load

    projection = Path(state_dir) / "projection"
    served = projection / "ontology.sqlite"
    from operating.pg import ontology_reader
    if ontology_reader.pg_only() and not served.is_file():
        # Postgres the only store, its SQLite generation retired: the generation is the
        # one Postgres holds (nothing to load), and only the delta layers follow
        with psycopg.connect(dsn, **FOLLOW_LIMITS) as pg:
            row = pg.execute("SELECT value->>'content_digest' FROM ontology.load_state "
                             "WHERE name = 'generation'").fetchone()
        if row is None or row[0] is None:
            return {"served": None, "held": None, "state": "no_generation_loaded"}
        digest = row[0]
    else:
        with sqlite3.connect(f"file:{served}?mode=ro", uri=True) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
    deferred = tuple(t for t in os.environ.get("PLX_ONTOLOGY_DEFER", "").split(",") if t)
    needed = set(ontology_load.ORDER) - set(ontology_load.OPTIONAL) - set(deferred)
    with psycopg.connect(dsn, **FOLLOW_LIMITS) as pg:
        row = pg.execute("SELECT value->>'content_digest' FROM ontology.load_state "
                         "WHERE name = 'generation'").fetchone()
        done = {r[0] for r in pg.execute("SELECT substr(name, 7) FROM ontology.load_state "
                                         "WHERE name LIKE 'table:%%'")}
        held_bytes = int(pg.execute(
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c JOIN "
            "pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ontology' "
            "AND c.relkind IN ('r', 'm')").fetchone()[0])
    report: dict[str, Any] = {"served": digest, "held": row[0] if row else None}
    if report["held"] != digest or not needed <= done:
        if not reload:
            return {**report, "state": "generation_differs"}
        if not served.is_file():   # retired: nothing to load from, never a partial store
            return {**report, "state": "not_whole_and_no_generation"}
        # the generation as a file of its own (the served path is swapped at a publish)
        source = served.resolve()
        for candidate in sorted((projection / "generations").glob("*/store.sqlite")):
            if candidate.stat().st_ino == served.stat().st_ino:
                source = candidate
        # A host reads Postgres only while it holds the served generation
        # (``ontology_reader.serves``): once a new one is served nobody reads the old one,
        # so it is replaced in place (half the disk). ``PLX_ONTOLOGY_RELOAD=beside``: load
        # beside it and swap in at once (``ontology_swap``), for readers that must keep
        # reading Postgres through the reload.
        beside = os.environ.get("PLX_ONTOLOGY_RELOAD") == "beside"
        published = served.stat().st_ctime   # the served link changed at the publish
        if not beside and report["held"] is not None and report["held"] != digest \
                and needed <= done and time.time() - published < RELOAD_GRACE_SECONDS:
            # a request that began on Postgres before the publish finishes on it first
            return {**report, "state": "waiting_for_readers",
                    "seconds_left": round(RELOAD_GRACE_SECONDS - (time.time() - published))}
        if report["held"] is not None and report["held"] != digest:
            room = reload_room(Path(state_dir), source, held_bytes=held_bytes,
                               in_place=not beside, min_free=min_free,
                               folder=ontology_load.free_path(dsn))
            report["room"] = room
            if not room["fits"]:
                return {**report, "state": room["state"]}
        if beside and report["held"] is not None \
                and report["held"] != digest and needed <= done:
            from operating.pg import ontology_swap
            ontology_swap.prepare_next(dsn, digest)
            import subprocess
            done = subprocess.run(   # the loader keeps the build's room too (state_of)
                [sys.executable, "-m", "operating.pg.ontology_load", str(source), "--dsn", dsn,
                 "--workers", str(workers), "--min-free-gb", str(min_free / 1024 ** 3),
                 *(["--defer", *deferred] if deferred else [])],
                env=dict(os.environ, PLX_ONTOLOGY_SCHEMA=ontology_swap.NEXT), check=False)
            if done.returncode != 0:
                return {**report, "state": "next_generation_loading",
                        "loader_exit": done.returncode}
            report["swapped"] = ontology_swap.swap(dsn)
        else:
            replace = report["held"] != digest
            report["loaded"] = ontology_load.load(source, dsn, workers=workers,
                                                  replace=replace, min_free=min_free, log=log,
                                                  deferred=deferred, state_dir=state_dir)
        report["held"] = digest
        _note_code(Path(state_dir))
    pointer = projection / "delta" / "CURRENT.json"
    try:
        body = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _keep_facts(dsn, report, projection / "ontology.sqlite", log)
        return {**report, "state": "no_delta"}
    if body.get("base_digest") != digest:
        return {**report, "state": "delta_of_another_generation"}
    stores = [item["store"] for item in body.get("layers") or []]   # newest first
    report["layers"] = apply_listed(stores, dsn, log=log)
    _keep_facts(dsn, report, projection / "ontology.sqlite", log)
    report["state"] = "stalled" if report["layers"].get("stalled") else "current"
    return report


def reload_room(state_dir: Path, source: Path, *, held_bytes: int, in_place: bool,
                min_free: int = 0, folder: str | None = None) -> dict[str, Any]:
    """Whether a load of the generation at ``source`` fits now: the free space of the
    database's file system (``folder``; the state directory's when None), plus the held
    copy an ``in_place`` reload empties first, must hold the bytes the load writes (the
    held copy's ``held_bytes``, else the store's size) and the floor every load keeps
    (``ontology_load.build_floor``: ``min_free`` or the next projection build's room). The
    files of SQLite generations other than ``source``'s (the previous one, kept until a
    later build prunes it) are named: while they are on disk and the load does not fit it
    is ``waiting_for_prune``, else ``waiting_for_space``."""
    from operating.pg import ontology_load
    folder, floor, reserve = ontology_load.build_floor(folder, min_free, state_dir)
    measured = folder or str(state_dir)
    try:
        free: int | None = ontology_load._free_bytes(measured)
    except OSError:
        free = None
    try:
        ino = source.stat().st_ino
        size = source.stat().st_size
    except OSError:
        ino, size = None, 0
    previous = 0
    for store in (state_dir / "projection" / "generations").glob("*/store.sqlite"):
        try:
            if store.stat().st_ino != ino:
                previous += sum(f.stat().st_size for f in store.parent.iterdir()
                                if f.is_file())
        except OSError:
            continue
    load_bytes = held_bytes or size
    freed = held_bytes if in_place else 0
    needed = load_bytes + floor
    fits = free is not None and free + freed >= needed
    return {"fits": fits, "free_bytes": free, "freed_bytes": freed, "load_bytes": load_bytes,
            "floor_bytes": floor, "needed_bytes": needed, "previous_bytes": previous,
            "build_reserve": reserve,
            "state": "fits" if fits else
            "waiting_for_prune" if previous else "waiting_for_space"}


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.ontology_delta")
    parser.add_argument("layers", nargs="*", help="delta layers, oldest first")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--follow", metavar="STATE_DIR", default=None,
                        help="apply what the state directory serves (``follow``)")
    parser.add_argument("--no-reload", action="store_true",
                        help="with --follow: never load a whole generation")
    parser.add_argument("--min-free-gb", type=float, default=0.0,
                        help="a floor below the next projection build's room, which every "
                             "load keeps free (ontology_load.build_floor)")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.follow:
        out = follow(args.follow, dsn, reload=not args.no_reload, workers=args.workers,
                     min_free=int(args.min_free_gb * 1024 ** 3))
        print(json.dumps(out, sort_keys=True,
                         default=str), flush=True)
        return 3 if out.get("state") == "stalled" else 0
    for layer in args.layers:
        print(json.dumps(apply_layer(layer, dsn), sort_keys=True), flush=True)
    if args.layers:   # the whole-store steps over what they applied (``whole_steps``)
        print(json.dumps({"whole_steps": recompute_whole_steps(dsn)}, sort_keys=True,
                         default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
