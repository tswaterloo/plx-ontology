"""Throwaway Postgres databases (``plx_test_a_<pid>_<n>``) for the tests and the migration
proofs: created over the local socket (``PLX_PG_TEST_ADMIN_DSN``), dropped by their
creator. The ``plx`` database is never written. psycopg only."""
from __future__ import annotations

import itertools
import os
from typing import Any

ADMIN_DSN = os.environ.get("PLX_PG_TEST_ADMIN_DSN", "postgresql:///postgres")
_COUNTER = itertools.count()


def admin() -> Any:
    import psycopg
    return psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=5)


def create_db() -> tuple[str, str]:
    """(name, dsn) of a new empty database."""
    import psycopg
    from psycopg import sql
    name = f"plx_test_a_{os.getpid()}_{next(_COUNTER)}"
    with admin() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name, psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=name)


def drop_db(name: str) -> None:
    from psycopg import sql
    if not name.startswith("plx_test_a_"):
        raise ValueError("not a scratch database")
    with admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                     .format(sql.Identifier(name)))
