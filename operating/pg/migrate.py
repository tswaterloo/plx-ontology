"""Ordered SQL migrations of the Postgres platform (``operating/pg/migrations/NNNN_*.sql``).

Each migration runs once, in its own transaction, under a transaction advisory lock, and is
recorded in ``plx_meta.migrations`` with the sha256 of its template text and the parameters
it was rendered with. A recorded migration whose file changed afterwards is drift: migrate
and check refuse it instead of silently diverging.

Template parameters: ``{{embed_dim}}`` (an int) and ``{{embed_model}}`` (a quoted SQL
literal), from ``operating/pg/config.py``. Any other ``{{...}}`` fails closed.

Another database's migrations (the job queue's ``operating/jobq/migrations``) use the same
runner with their own ``directory``, ledger schema and lock key.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from operating.pg import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
LOCK_KEY = 0x504C58_5047  # "PLXPG": one migrate at a time per database

# search.chunks' bit-quantized HNSW index (migration 0034); the embedding switch below
# rebuilds it at the new dimension. operating.pg.bulk re-exports both.
CHUNKS_BQ_HNSW = "chunks_embedding_bq_hnsw"


def chunks_bq_hnsw_ddl(dim: int) -> str:
    """The bit index's definition at the column's dimension (migration 0034's)."""
    if not isinstance(dim, int) or isinstance(dim, bool) or not 1 <= dim <= 64000:
        raise ValueError(f"bq_dim_invalid:{dim!r}")
    return (f"CREATE INDEX IF NOT EXISTS {CHUNKS_BQ_HNSW} ON search.chunks USING hnsw "
            f"((binary_quantize(embedding)::bit({dim})) bit_hamming_ops) "
            "WITH (m = 16, ef_construction = 64)")

# One partial halfvec HNSW per chunk kind (migration 0036): a question far from every chunk
# (a long eval question sits at cosine distance 0.35-0.5 from all of them) makes one walk
# over the whole graph stop in the big kinds' region; a walk per kind reaches each kind's
# nearest rows. operating.pg.dense finds the valid ones at run time (PLX_SEARCH_KINDS).
CHUNKS_KIND_HNSW_PREFIX = "chunks_embedding_k_"
CHUNKS_KIND_HNSW_KINDS = ("attachment", "bc_text", "list_item", "mail", "map_stage", "mc_task",
                          "meeting", "original", "portal_text", "record_text", "record_words",
                          "sharepoint", "sheet_row", "teams")
_KIND_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def chunks_kind_hnsw_ddl(kind: str) -> str:
    """The partial halfvec HNSW over one kind's rows (migration 0036's definition)."""
    if not isinstance(kind, str) or not _KIND_NAME.match(kind):
        raise ValueError(f"kind_invalid:{kind!r}")
    return (f"CREATE INDEX IF NOT EXISTS {CHUNKS_KIND_HNSW_PREFIX}{kind} ON search.chunks "
            "USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64) "
            f"WHERE kind = '{kind}'")


KINDS_FLAG = "PLX_SEARCH_KINDS"


def kinds_flag_on(env: Any = None) -> bool:
    """``PLX_SEARCH_KINDS``: on only for ``1``/``on``/``true`` (any case). A bulk rebuild or
    an embedding switch rebuilds the per-kind indexes only when it is on (they about double
    a rebuild's time and size); off, they stay dropped and search uses the whole-table one."""
    import os
    value = (os.environ if env is None else env).get(KINDS_FLAG, "")
    return str(value).strip().lower() in ("1", "on", "true")


def drop_chunks_kind_hnsw(conn: Any) -> None:
    """Drop every per-kind chunk HNSW (any kind, listed or not)."""
    for (name,) in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'search' "
            "AND tablename = 'chunks' AND indexname LIKE %s",
            (CHUNKS_KIND_HNSW_PREFIX.replace("_", r"\_") + "%",)).fetchall():
        conn.execute(sql.SQL("DROP INDEX IF EXISTS search.{}").format(sql.Identifier(name)))

LEDGER = "plx_meta"
_META_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    sha256     text NOT NULL,
    params     jsonb NOT NULL DEFAULT '{{}}',
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def _meta_ddl(ledger: str) -> sql.Composed:
    return sql.SQL(_META_DDL).format(schema=sql.Identifier(ledger))


class MigrationError(RuntimeError):
    pass


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, str, Path]]:
    out, seen = [], set()
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _NAME.match(path.name)
        if not match:
            raise MigrationError(f"migration_name_invalid:{path.name}")
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(f"migration_version_duplicate:{version}")
        seen.add(version)
        out.append((version, path.stem, path))
    return out


def params(model: str | None = None, dim: int | None = None) -> dict[str, Any]:
    model = model or config.embed_model()
    return {"embed_model": model, "embed_dim": dim if dim is not None else config.embed_dim(model)}


def render(template: str, values: dict[str, Any]) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "embed_dim":
            dim = values["embed_dim"]
            if not isinstance(dim, int) or isinstance(dim, bool) or not (
                    1 <= dim <= config.MAX_HALFVEC_INDEX_DIM):
                raise MigrationError(f"embed_dim_invalid:{dim!r}")
            return str(dim)
        if name == "embed_model":
            model = values["embed_model"]
            if not isinstance(model, str) or not model:
                raise MigrationError("embed_model_invalid")
            return "'" + model.replace("'", "''") + "'"
        raise MigrationError(f"migration_parameter_unknown:{name}")
    return _PLACEHOLDER.sub(sub, template)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def applied(conn: psycopg.Connection, ledger: str = LEDGER) -> dict[int, dict[str, Any]]:
    cur = conn.cursor(row_factory=tuple_row)
    row = cur.execute("SELECT to_regclass(%s)", (f'"{ledger}".migrations',)).fetchone()
    if row[0] is None:
        return {}
    return {v: {"name": n, "sha256": s, "params": p, "applied_at": a}
            for v, n, s, p, a in cur.execute(
                sql.SQL("SELECT version, name, sha256, params, applied_at FROM {}.migrations "
                        "ORDER BY version").format(sql.Identifier(ledger))).fetchall()}


def status(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR,
           ledger: str = LEDGER) -> dict[str, Any]:
    with conn.transaction():  # read-only; leaves the connection as it found it
        done = applied(conn, ledger)
    files = migration_files(directory)
    drift = [name for version, name, path in files
             if version in done and done[version]["sha256"] != _sha(path.read_text("utf-8"))]
    known = {version for version, _, _ in files}
    return {
        "applied": [done[v]["name"] for v in sorted(done)],
        "pending": [name for version, name, _ in files if version not in done],
        "drift": drift,
        "unknown_applied": [done[v]["name"] for v in sorted(done) if v not in known],
    }


def migrate(conn: psycopg.Connection, *, values: dict[str, Any] | None = None,
            directory: Path = MIGRATIONS_DIR, ledger: str = LEDGER,
            lock_key: int = LOCK_KEY) -> list[str]:
    """Apply every pending migration in order; return the names applied (empty when the
    database is current). Idempotent; refuses drift. ``ledger`` is the schema holding the
    ``migrations`` record; ``values`` defaults to the embedding parameters (``{}`` for a
    directory whose templates take none)."""
    values = params() if values is None else values
    ran: list[str] = []
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise MigrationError("migrate_needs_idle_connection")
    cur = conn.cursor(row_factory=tuple_row)   # whatever row factory the connection has
    for version, name, path in migration_files(directory):
        template = path.read_text("utf-8")
        with conn.transaction():
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(_meta_ddl(ledger))
            row = cur.execute(sql.SQL("SELECT sha256 FROM {}.migrations WHERE version = %s")
                              .format(sql.Identifier(ledger)), (version,)).fetchone()
            if row is not None:
                if row[0] != _sha(template):
                    raise MigrationError(f"migration_drift:{name}")
                continue
            cur.execute(render(template, values))
            cur.execute(
                sql.SQL("INSERT INTO {}.migrations (version, name, sha256, params) "
                        "VALUES (%s, %s, %s, %s)").format(sql.Identifier(ledger)),
                (version, name, _sha(template), json.dumps(values, sort_keys=True)))
            ran.append(name)
    return ran


def set_embedding(conn: psycopg.Connection, model: str, dim: int | None = None) -> dict[str, Any]:
    """Switch the vector columns to ``model``'s dimension in one transaction: drop the
    HNSW indexes, null every embedding of another model (they must be re-embedded),
    retype both vector columns, rebuild the indexes, record the model. Same model and
    dimension: a no-op."""
    dim = dim if dim is not None else config.embed_dim(model)
    render("{{embed_dim}}{{embed_model}}", {"embed_dim": dim, "embed_model": model})
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        current = conn.execute("SELECT model, dim FROM search.embedding_config").fetchone()
        if current == (model, dim):
            return {"changed": False, "model": model, "dim": dim, "cleared": 0}
        conn.execute("DROP INDEX IF EXISTS search.chunks_embedding_hnsw")
        conn.execute("DROP INDEX IF EXISTS search.chunks_embedding_bq_hnsw")
        drop_chunks_kind_hnsw(conn)
        conn.execute("DROP INDEX IF EXISTS search.entities_name_embedding_hnsw")
        cleared = conn.execute(
            "UPDATE search.chunks SET embedding = NULL, embed_model = NULL "
            "WHERE embedding IS NOT NULL").rowcount
        conn.execute("UPDATE search.entities SET name_embedding = NULL, name_embed_model = NULL "
                     "WHERE name_embedding IS NOT NULL")
        conn.execute(sql.SQL("ALTER TABLE search.chunks ALTER COLUMN embedding TYPE halfvec({})")
                     .format(sql.Literal(dim)))
        conn.execute(sql.SQL("ALTER TABLE search.entities ALTER COLUMN name_embedding "
                             "TYPE halfvec({})").format(sql.Literal(dim)))
        conn.execute("CREATE INDEX chunks_embedding_hnsw ON search.chunks USING hnsw "
                     "(embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)")
        conn.execute(chunks_bq_hnsw_ddl(dim))
        if kinds_flag_on():
            for kind in CHUNKS_KIND_HNSW_KINDS:
                conn.execute(chunks_kind_hnsw_ddl(kind))
        conn.execute("CREATE INDEX entities_name_embedding_hnsw ON search.entities USING hnsw "
                     "(name_embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)")
        conn.execute("UPDATE search.embedding_config SET model = %s, dim = %s, changed_at = now()",
                     (model, dim))
    return {"changed": True, "model": model, "dim": dim, "cleared": cleared}

