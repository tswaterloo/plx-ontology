"""Ordered SQL migrations of the Postgres platform (``operating/pg/migrations/NNNN_*.sql``).

A recorded migration whose file changed afterwards is drift: migrate
and check refuse it instead of silently diverging.

Template parameters: ``{{embed_dim}}`` (an int) and ``{{embed_model}}`` (a quoted SQL
literal), from ``operating/pg/config.py``. Any other ``{{...}}`` fails closed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

import psycopg
from psycopg import sql

from operating.pg import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
LOCK_KEY = 0x504C58_5047  # "PLXPG": one migrate at a time per database

_META_DDL = """
CREATE SCHEMA IF NOT EXISTS plx_meta;
CREATE TABLE IF NOT EXISTS plx_meta.migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    sha256     text NOT NULL,
    params     jsonb NOT NULL DEFAULT '{}',
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


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


def applied(conn: psycopg.Connection) -> dict[int, dict[str, Any]]:
    row = conn.execute("SELECT to_regclass('plx_meta.migrations')").fetchone()
    if row[0] is None:
        return {}
    return {v: {"name": n, "sha256": s, "params": p, "applied_at": a}
            for v, n, s, p, a in conn.execute(
                "SELECT version, name, sha256, params, applied_at FROM plx_meta.migrations "
                "ORDER BY version")}


def status(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> dict[str, Any]:
    with conn.transaction():  # read-only; leaves the connection as it found it
        done = applied(conn)
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
            directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration in order; return the names applied (empty when the
    database is current). Idempotent; refuses drift."""
    values = values or params()
    ran: list[str] = []
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise MigrationError("migrate_needs_idle_connection")
    for version, name, path in migration_files(directory):
        template = path.read_text("utf-8")
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            conn.execute(_META_DDL)
            row = conn.execute("SELECT sha256 FROM plx_meta.migrations WHERE version = %s",
                               (version,)).fetchone()
            if row is not None:
                if row[0] != _sha(template):
                    raise MigrationError(f"migration_drift:{name}")
                continue
            conn.execute(render(template, values))
            conn.execute(
                "INSERT INTO plx_meta.migrations (version, name, sha256, params) "
                "VALUES (%s, %s, %s, %s)",
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
        conn.execute("CREATE INDEX entities_name_embedding_hnsw ON search.entities USING hnsw "
                     "(name_embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)")
        conn.execute("UPDATE search.embedding_config SET model = %s, dim = %s, changed_at = now()",
                     (model, dim))
    return {"changed": True, "model": model, "dim": dim, "cleared": cleared}

