"""Columns with type and nullability, the generated
tsv, the vector dimension against the configured embedding model, and every index with
its access method. ``python3 -m operating.pg check`` prints the problems; none: exit 0.
"""
from __future__ import annotations

from typing import Any

import psycopg

from operating.pg import config

DIM = "halfvec(DIM)"  # resolved to the configured embedding dimension

# table -> column -> (type as format_type prints it, NOT NULL)
CONTRACT_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "chunks": {
        "chunk_id": ("text", True), "source_handle": ("text", True),
        "object_id": ("text", False), "kind": ("text", True), "title": ("text", False),
        "author_role": ("text", False),
        "occurred_at": ("timestamp with time zone", False),
        "header": ("text", True), "body": ("text", True), "content_sha": ("text", True),
        "linked_ids": ("text[]", True), "audience_key": ("text", True),
        "markings": ("text[]", True), "dup_group": ("text", False),
        "authority": ("smallint", True), "tsv": ("tsvector", False),
        "embedding": (DIM, False), "embed_model": ("text", False),
        "updated_at": ("timestamp with time zone", True),
    },
    "entities": {
        "entity_id": ("text", True), "object_id": ("text", False), "type": ("text", False),
        "name": ("text", False), "alias": ("text[]", True), "normalized": ("text", False),
        # Stricter than the contract text (which leaves them nullable): a row without an
        # audience or markings could not be permission-filtered, so it is refused.
        "audience_key": ("text", True), "markings": ("text[]", True),
        "name_embedding": (DIM, False), "name_embed_model": ("text", False),
    },
    "audience_units": {
        "audience_key": ("text", True), "sharing": ("text", True), "label": ("text", False),
        "people": ("text[]", True), "updated_at": ("timestamp with time zone", True),
    },
    "query_log": {
        "id": ("bigint", True), "at": ("timestamp with time zone", True),
        "actor": ("text", False), "question": ("text", False), "intent": ("text", False),
        "entities": ("jsonb", False), "rewrites": ("jsonb", False),
        "results": ("jsonb", False), "timings": ("jsonb", False),
    },
    "feedback": {
        "id": ("bigint", True), "at": ("timestamp with time zone", True),
        "actor": ("text", False), "question": ("text", False), "answer_id": ("text", False),
        "rating": ("smallint", False), "note": ("text", False),
    },
}

# index name -> (table, access method, column / expression text it must mention)
CONTRACT_INDEXES: dict[str, tuple[str, str, str]] = {
    "chunks_pkey": ("chunks", "btree", "chunk_id"),
    "chunks_tsv_gin": ("chunks", "gin", "tsv"),
    "chunks_embedding_hnsw": ("chunks", "hnsw", "embedding halfvec_cosine_ops"),
    "chunks_audience_key": ("chunks", "btree", "audience_key"),
    "chunks_kind": ("chunks", "btree", "kind"),
    "chunks_object_id": ("chunks", "btree", "object_id"),
    "chunks_content_sha": ("chunks", "btree", "content_sha"),
    "chunks_linked_ids_gin": ("chunks", "gin", "linked_ids"),
    "entities_pkey": ("entities", "btree", "entity_id"),
    "entities_normalized_trgm": ("entities", "gin", "normalized gin_trgm_ops"),
    "entities_name_embedding_hnsw": ("entities", "hnsw", "name_embedding halfvec_cosine_ops"),
    "entities_object_id": ("entities", "btree", "object_id"),
    "entities_alias_gin": ("entities", "gin", "(alias)"),
    "entities_alias_text_trgm": ("entities", "gin", "search.alias_text(alias) gin_trgm_ops"),
    "entities_audience_key": ("entities", "btree", "audience_key"),
    "audience_units_pkey": ("audience_units", "btree", "audience_key"),
    "query_log_pkey": ("query_log", "btree", "id"),
    "feedback_pkey": ("feedback", "btree", "id"),
}

# Statement triggers that bump search.write_generation (the permission scope cache key).
CONTRACT_TRIGGERS = tuple(f"{table}_{what}_generation" for table in
                          ("chunks", "entities", "audience_units")
                          for what in ("write", "truncate"))


def check(conn: psycopg.Connection, *, model: str | None = None) -> list[str]:
    """Every way the database differs from the contract (empty: it matches)."""
    problems: list[str] = []
    model = model or config.embed_model()
    try:
        want_dim = config.embed_dim(model)
    except ValueError as exc:
        return [str(exc)]
    rows = conn.execute(
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
        "a.attgenerated FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'search' "
        "AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped").fetchall()
    actual: dict[str, dict[str, tuple[str, bool, str]]] = {}
    for table, column, typ, notnull, generated in rows:
        actual.setdefault(table, {})[column] = (typ, notnull, generated)
    for table, columns in CONTRACT_COLUMNS.items():
        have = actual.get(table)
        if have is None:
            problems.append(f"table_missing:search.{table}")
            continue
        for column, (typ, notnull) in columns.items():
            if column not in have:
                problems.append(f"column_missing:search.{table}.{column}")
                continue
            want = f"halfvec({want_dim})" if typ == DIM else typ
            if have[column][0] != want:
                problems.append(f"column_type:search.{table}.{column}:{have[column][0]}!={want}")
            if have[column][1] != notnull:
                problems.append(f"column_nullability:search.{table}.{column}")
        for column in sorted(set(have) - set(columns)):
            problems.append(f"column_unexpected:search.{table}.{column}")
    if "chunks" in actual and actual["chunks"].get("tsv", ("", False, ""))[2] != "s":
        problems.append("column_not_generated:search.chunks.tsv")
    indexes = {name: (table, am, definition) for name, table, am, definition in conn.execute(
        "SELECT i.relname, t.relname, am.amname, pg_get_indexdef(i.oid) FROM pg_index x "
        "JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
        "JOIN pg_am am ON am.oid = i.relam JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = 'search'")}
    for name, (table, am, mentions) in CONTRACT_INDEXES.items():
        got = indexes.get(name)
        if got is None:
            problems.append(f"index_missing:search.{name}")
        elif got[0] != table or got[1] != am or mentions not in got[2]:
            problems.append(f"index_differs:search.{name}:{got[2]}")
    triggers = {name for (name,) in conn.execute(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'search' "
        "AND NOT t.tgisinternal")}
    for name in CONTRACT_TRIGGERS:
        if name not in triggers:
            problems.append(f"trigger_missing:search.{name}")
    if conn.execute("SELECT to_regclass('search.write_generation')").fetchone()[0] is None:
        problems.append("sequence_missing:search.write_generation")
    config_row = None
    if "embedding_config" in actual:
        config_row = conn.execute("SELECT model, dim FROM search.embedding_config").fetchone()
    if config_row is None:
        problems.append("embedding_config_missing")
    elif config_row != (model, want_dim):
        problems.append(f"embedding_config:{config_row[0]}/{config_row[1]}!={model}/{want_dim}")
    return problems


def summary(conn: psycopg.Connection) -> dict[str, Any]:
    """Row counts and the embedding config (status output)."""
    if conn.execute("SELECT to_regclass('search.chunks')").fetchone()[0] is None:
        return {"search_schema": "absent"}
    out: dict[str, Any] = {}
    for table in ("chunks", "entities", "query_log", "feedback"):
        out[table] = conn.execute(f"SELECT count(*) FROM search.{table}").fetchone()[0]
    out["chunks_embedded"] = conn.execute(
        "SELECT count(*) FROM search.chunks WHERE embedding IS NOT NULL").fetchone()[0]
    row = conn.execute("SELECT model, dim FROM search.embedding_config").fetchone()
    out["embedding"] = {"model": row[0], "dim": row[1]} if row else None
    return out
