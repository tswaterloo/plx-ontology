"""``search.chunks`` / ``search.entities`` writes (Postgres 17 + pgvector).

Upserts go through a staged ``COPY`` and one ``INSERT ... ON CONFLICT``: a row whose
content (``content_sha``), keys, markings, links or citation changed is rewritten; its
embedding is kept only while ``content_sha`` is unchanged (re-embed only what changed).
A full pass deletes the rows of the kinds it covered that it did not produce, unless that
would remove more than ``SHRINK_GUARD`` of a kind (a broken read is not a deletion; the
operator passes ``allow_shrink``); rows of an audience unit the people exclusion now
withholds are always deleted.

Embeddings are written as ``halfvec`` text (``[x,y,...]``); a vector narrower than the
column is zero-padded (cosine unchanged; ``embed_model`` names the model, so search
compares like with like), a wider one is refused.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator, Sequence

import numpy as np

from operating.chunking import Chunk
from operating.chunking.entities import Entity

SHRINK_GUARD = 0.5
_CHUNK_COLUMNS = ("chunk_id", "source_handle", "object_id", "kind", "title", "author_role",
                  "occurred_at", "header", "body", "content_sha", "linked_ids", "audience_key",
                  "markings", "authority")
_ENTITY_COLUMNS = ("entity_id", "object_id", "type", "name", "alias", "normalized",
                   "audience_key", "markings")


class SchemaMissing(RuntimeError):
    pass


def connect(dsn: str) -> Any:
    import psycopg
    return psycopg.connect(dsn, autocommit=False)


def column_dim(conn: Any, table: str = "chunks", column: str = "embedding") -> int | None:
    row = conn.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'search' AND c.relname = %s AND a.attname = %s AND NOT a.attisdropped",
        (table, column)).fetchone()
    if row is None:
        return None
    found = re.search(r"\((\d+)\)", row[0] or "")
    return int(found.group(1)) if found else None


def require_schema(conn: Any) -> None:
    for table in ("chunks", "entities"):
        row = conn.execute("SELECT to_regclass(%s)", (f"search.{table}",)).fetchone()
        if row is None or row[0] is None:
            raise SchemaMissing(f"search.{table} absent: run python3 -m operating.pg migrate")


def vector_text(vector: np.ndarray, dim: int) -> str:
    if vector.shape[0] > dim:
        raise ValueError(f"embedding_wider_than_column:{vector.shape[0]}>{dim}")
    if vector.shape[0] < dim:
        vector = np.concatenate([vector, np.zeros(dim - vector.shape[0], dtype=vector.dtype)])
    return "[" + ",".join(f"{x:.6g}" for x in vector.tolist()) + "]"


class ChunkWriter:
    """One run's writes over one connection (``seen`` is a session temp table)."""

    def __init__(self, conn: Any, unit_of: Any = None) -> None:
        """``unit_of(audience_key)``: the ``search.audience_units`` row of a unit, written
        in the same transaction as, and before, the unit's first chunks or entities."""
        self.conn = conn
        self.unit_of = unit_of
        self.units_written: set[str] = set()
        self.units_recorded = 0
        self._has_units: bool | None = None
        self.inserted = 0
        self.updated = 0
        self.unchanged = 0
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_chunks (chunk_id text PRIMARY KEY)")
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_entities (entity_id text PRIMARY KEY)")
        # A new run on a reused connection starts with nothing seen.
        conn.execute("TRUNCATE seen_chunks, seen_entities")
        conn.commit()

    def _units(self, keys: Iterable[str]) -> None:
        """Upsert the units rows of ``keys`` not yet written this run (no commit: the
        caller's chunk or entity write commits them together)."""
        if self.unit_of is None:
            return
        if self._has_units is None:
            self._has_units = self.conn.execute(
                "SELECT to_regclass('search.audience_units')").fetchone()[0] is not None
        new = sorted(set(keys) - self.units_written)
        if not self._has_units or not new:
            return
        for key in new:
            row = self.unit_of(key)
            self.units_recorded += self.conn.execute(
                "INSERT INTO search.audience_units AS u (audience_key, sharing, label, people, "
                "updated_at) VALUES (%s, %s, %s, %s, now()) ON CONFLICT (audience_key) DO "
                "UPDATE SET sharing = EXCLUDED.sharing, label = EXCLUDED.label, people = "
                "EXCLUDED.people, updated_at = now() WHERE (u.sharing, u.label, u.people) IS "
                "DISTINCT FROM (EXCLUDED.sharing, EXCLUDED.label, EXCLUDED.people)",
                (row["audience_key"], row["sharing"], row["label"], row["people"])).rowcount
        self.units_written.update(new)

    def write(self, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        self._units(c.audience_key for c in chunks)
        cur = self.conn.cursor()
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS stage_chunks "
                    "(LIKE search.chunks INCLUDING DEFAULTS) ON COMMIT DELETE ROWS")
        # The stage holds the generated tsvector too: never copied into, defaults only.
        with cur.copy(f"COPY stage_chunks ({', '.join(_CHUNK_COLUMNS)}, updated_at) "
                      "FROM STDIN") as copy:
            for c in chunks:
                copy.write_row((c.chunk_id, c.source_handle, c.object_id, c.kind, c.title,
                                c.author_role, c.occurred_at, c.header, c.body, c.content_sha,
                                list(c.linked_ids), c.audience_key, list(c.markings),
                                c.authority, "now"))
        cols = ", ".join(_CHUNK_COLUMNS)
        changed = " OR ".join(f"t.{col} IS DISTINCT FROM EXCLUDED.{col}"
                              for col in _CHUNK_COLUMNS if col != "chunk_id")
        rows = cur.execute(
            f"INSERT INTO search.chunks AS t ({cols}, updated_at) "
            f"SELECT DISTINCT ON (chunk_id) {cols}, now() FROM stage_chunks "
            f"ORDER BY chunk_id "
            f"ON CONFLICT (chunk_id) DO UPDATE SET "
            + ", ".join(f"{col} = EXCLUDED.{col}" for col in _CHUNK_COLUMNS if col != "chunk_id")
            + ", embedding = CASE WHEN t.content_sha = EXCLUDED.content_sha THEN t.embedding END"
            ", embed_model = CASE WHEN t.content_sha = EXCLUDED.content_sha THEN t.embed_model "
            "END, updated_at = now() WHERE " + changed + " RETURNING (xmax = 0)").fetchall()
        inserted = sum(1 for r in rows if r[0])
        self.inserted += inserted
        self.updated += len(rows) - inserted
        self.unchanged += len({c.chunk_id for c in chunks}) - len(rows)
        cur.execute("INSERT INTO seen_chunks SELECT DISTINCT chunk_id FROM stage_chunks "
                    "ON CONFLICT DO NOTHING")
        self.conn.commit()

    @property
    def _name_vector_reset(self) -> str:
        """A changed name or alias drops the entity's name vector (and its model, when the
        schema records one: ``name_embed_model``)."""
        if not hasattr(self, "_has_name_model"):
            self._has_name_model = self.conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'search' AND "
                "table_name = 'entities' AND column_name = 'name_embed_model'").fetchone() \
                is not None
        same = "t.name = EXCLUDED.name AND t.alias = EXCLUDED.alias"
        out = f", name_embedding = CASE WHEN {same} THEN t.name_embedding END"
        if self._has_name_model:
            out += f", name_embed_model = CASE WHEN {same} THEN t.name_embed_model END"
        return out

    def write_entities(self, entities: Sequence[Entity]) -> None:
        if not entities:
            return
        self._units(e.audience_key for e in entities)
        cur = self.conn.cursor()
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS stage_entities "
                    "(LIKE search.entities INCLUDING DEFAULTS) ON COMMIT DELETE ROWS")
        with cur.copy(f"COPY stage_entities ({', '.join(_ENTITY_COLUMNS)}) FROM STDIN") as copy:
            for e in entities:
                copy.write_row((e.entity_id, e.object_id, e.type, e.name, list(e.alias),
                                e.normalized, e.audience_key, list(e.markings)))
        cols = ", ".join(_ENTITY_COLUMNS)
        changed = " OR ".join(f"t.{col} IS DISTINCT FROM EXCLUDED.{col}"
                              for col in _ENTITY_COLUMNS if col != "entity_id")
        cur.execute(
            f"INSERT INTO search.entities AS t ({cols}) SELECT DISTINCT ON (entity_id) {cols} "
            f"FROM stage_entities ORDER BY entity_id ON CONFLICT (entity_id) DO UPDATE SET "
            + ", ".join(f"{col} = EXCLUDED.{col}" for col in _ENTITY_COLUMNS
                        if col != "entity_id")
            + self._name_vector_reset + " WHERE " + changed)
        cur.execute("INSERT INTO seen_entities SELECT entity_id FROM stage_entities "
                    "ON CONFLICT DO NOTHING")
        self.conn.commit()

    def delete_units(self, audience_keys: Iterable[str]) -> int:
        """Delete every chunk and entity of audience units the exclusion withholds now."""
        keys = sorted(set(audience_keys))
        if not keys:
            return 0
        n = self.conn.execute("DELETE FROM search.chunks WHERE audience_key = ANY(%s)",
                              (keys,)).rowcount
        n += self.conn.execute("DELETE FROM search.entities WHERE audience_key = ANY(%s)",
                               (keys,)).rowcount
        self.conn.commit()
        return n

    def delete_stale(self, kinds: Sequence[str], *, allow_shrink: bool = False
                     ) -> dict[str, Any]:
        """Delete rows of ``kinds`` this full pass did not produce (module docstring)."""
        out: dict[str, Any] = {}
        for kind in kinds:
            total, stale = self.conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM seen_chunks "
                "s WHERE s.chunk_id = c.chunk_id)) FROM search.chunks c WHERE kind = %s",
                (kind,)).fetchone()
            if total and stale / total > SHRINK_GUARD and not allow_shrink:
                out[kind] = {"refused": "shrink_guard", "stale": stale, "total": total}
                continue
            deleted = self.conn.execute(
                "DELETE FROM search.chunks c WHERE kind = %s AND NOT EXISTS "
                "(SELECT 1 FROM seen_chunks s WHERE s.chunk_id = c.chunk_id)", (kind,)).rowcount
            out[kind] = {"deleted": deleted}
        self.conn.commit()
        return out

    def delete_stale_entities(self, types: Sequence[str], *, allow_shrink: bool = False
                              ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for etype in types:
            total, stale = self.conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM "
                "seen_entities s WHERE s.entity_id = e.entity_id)) FROM search.entities e "
                "WHERE type = %s", (etype,)).fetchone()
            if total and stale / total > SHRINK_GUARD and not allow_shrink:
                out[etype] = {"refused": "shrink_guard", "stale": stale, "total": total}
                continue
            out[etype] = {"deleted": self.conn.execute(
                "DELETE FROM search.entities e WHERE type = %s AND NOT EXISTS (SELECT 1 FROM "
                "seen_entities s WHERE s.entity_id = e.entity_id)", (etype,)).rowcount}
        self.conn.commit()
        return out


def purge_excluded(conn: Any, exclusion: Any) -> dict[str, int]:
    """Delete, on every run, every chunk and entity of a unit the people exclusion no longer
    clears: its key (a mailbox, chat or OneDrive of an excluded or undecidable person) or a
    person its ``search.audience_units`` row names. Runs before anything is written."""
    keys = {r[0] for r in conn.execute(
        "SELECT DISTINCT audience_key FROM search.chunks UNION "
        "SELECT DISTINCT audience_key FROM search.entities")}
    bad = {k for k in keys if not exclusion.unit_clear(k)}
    if conn.execute("SELECT to_regclass('search.audience_units')").fetchone()[0] is not None:
        for key, people in conn.execute("SELECT audience_key, people FROM search.audience_units"):
            if any(not exclusion.identity_clear(p) for p in people or ()):
                bad.add(key)
    if not bad:
        return {"units": 0, "rows": 0}
    keys_list = sorted(bad)
    rows = conn.execute("DELETE FROM search.chunks WHERE audience_key = ANY(%s)",
                        (keys_list,)).rowcount
    rows += conn.execute("DELETE FROM search.entities WHERE audience_key = ANY(%s)",
                         (keys_list,)).rowcount
    conn.commit()
    return {"units": len(bad), "rows": rows}


def purge_objects(conn: Any, object_ids: Iterable[str]) -> int:
    """Delete every chunk of these objects (an uncleared sender's messages and their
    attachments, ``projection.withheld_messages``), in any unit."""
    ids = sorted(set(object_ids))
    if not ids:
        return 0
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS purge_ids (object_id text PRIMARY KEY) "
                "ON COMMIT DELETE ROWS")
    with cur.copy("COPY purge_ids (object_id) FROM STDIN") as copy:
        for oid in ids:
            copy.write_row((oid,))
    n = cur.execute("DELETE FROM search.chunks c USING purge_ids p WHERE c.object_id = "
                    "p.object_id").rowcount
    conn.commit()
    return n


def reuse_embeddings(conn: Any, model: str) -> int:
    """Rows whose content another row already embedded with ``model`` take that vector."""
    n = conn.execute(
        "UPDATE search.chunks c SET embedding = d.embedding, embed_model = d.embed_model "
        "FROM (SELECT DISTINCT ON (content_sha) content_sha, embedding, embed_model FROM "
        "search.chunks WHERE embed_model = %s AND embedding IS NOT NULL) d "
        "WHERE c.content_sha = d.content_sha AND (c.embedding IS NULL OR "
        "c.embed_model IS DISTINCT FROM %s)", (model, model)).rowcount
    conn.commit()
    return n


# Embedding priority (review 2026-10-29): curated documents first, mail last (newest first).
PRIORITY = ("map_stage", "original", "sharepoint", "sheet_row", "teams", "mc_task",
            "record_text", "portal_text", "bc_text", "attachment", "mail")


def pending(conn: Any, model: str, kinds: Sequence[str] | None = None,
            limit: int | None = None, order: str = "recent") -> list[str]:
    """Content hashes still to embed with ``model``. ``recent``: newest first, system of
    record and documents before chat at equal dates. ``priority``: by ``PRIORITY`` kind
    (a hash shared by kinds takes its best), newest first within a kind."""
    where = "(embedding IS NULL OR embed_model IS DISTINCT FROM %s)"
    params: list[Any] = [model]
    if kinds:
        where += " AND kind = ANY(%s)"
        params.append(list(kinds))
    if order == "priority":
        ranking = ("min(coalesce(array_position(%s::text[], kind), 99)), "
                   "max(occurred_at) DESC NULLS LAST, content_sha")
        params.append(list(PRIORITY))
    elif order == "recent":
        ranking = "max(occurred_at) DESC NULLS LAST, max(authority) DESC, content_sha"
    else:
        raise ValueError(f"embed_order_unknown:{order}")
    sql = f"SELECT content_sha FROM search.chunks WHERE {where} GROUP BY content_sha " \
          f"ORDER BY {ranking}"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def texts_for(conn: Any, shas: Sequence[str]) -> list[tuple[str, str]]:
    """(content_sha, header + body) of each hash (one row per hash: equal hashes are
    equal texts)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (content_sha) content_sha, header, body FROM search.chunks "
        "WHERE content_sha = ANY(%s) ORDER BY content_sha", (list(shas),)).fetchall()
    return [(sha, header + "\n\n" + body) for sha, header, body in rows]


def store_embeddings(conn: Any, model: str, dim: int, shas: Sequence[str],
                     vectors: np.ndarray) -> int:
    texts = [vector_text(v, dim) for v in vectors]
    n = conn.execute(
        f"UPDATE search.chunks c SET embedding = u.vec::halfvec({dim}), embed_model = %s "
        "FROM unnest(%s::text[], %s::text[]) AS u(sha, vec) WHERE c.content_sha = u.sha",
        (model, list(shas), texts)).rowcount
    conn.commit()
    return n


def bodies(conn: Any, kinds: Sequence[str] | None = None) -> Iterator[tuple[str, str]]:
    """(chunk_id, body) of every chunk (server-side cursor)."""
    with conn.cursor(name="dedup_bodies") as cur:
        if kinds:
            cur.execute("SELECT chunk_id, body FROM search.chunks WHERE kind = ANY(%s)",
                        (list(kinds),))
        else:
            cur.execute("SELECT chunk_id, body FROM search.chunks")
        for row in cur:
            yield row[0], row[1]


def write_groups(conn: Any, groups: dict[str, str], kinds: Sequence[str] | None = None) -> int:
    """Set ``dup_group`` from ``groups`` (chunk_id -> group); every other chunk of the
    covered kinds gets none."""
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS stage_groups (chunk_id text PRIMARY KEY, "
                "dup_group text) ON COMMIT DELETE ROWS")
    with cur.copy("COPY stage_groups (chunk_id, dup_group) FROM STDIN") as copy:
        for chunk_id, group in groups.items():
            copy.write_row((chunk_id, group))
    scope = "AND c.kind = ANY(%s)" if kinds else ""
    params = [list(kinds)] if kinds else []
    n = cur.execute(
        "UPDATE search.chunks c SET dup_group = g.dup_group FROM stage_groups g WHERE "
        "g.chunk_id = c.chunk_id AND c.dup_group IS DISTINCT FROM g.dup_group").rowcount
    n += cur.execute(
        f"UPDATE search.chunks c SET dup_group = NULL WHERE c.dup_group IS NOT NULL {scope} "
        "AND NOT EXISTS (SELECT 1 FROM stage_groups g WHERE g.chunk_id = c.chunk_id)",
        params).rowcount
    conn.commit()
    return n


__all__ = ["ChunkWriter", "SchemaMissing", "bodies", "column_dim", "connect", "pending",
           "purge_excluded", "purge_objects",
           "require_schema", "reuse_embeddings", "store_embeddings", "texts_for",
           "vector_text", "write_groups"]
