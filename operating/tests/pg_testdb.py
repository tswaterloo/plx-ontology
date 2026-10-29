"""Throwaway Postgres databases for the operating.pg tests (``plx_test_a_<pid>_<n>``).

Each test class creates its own database over the local socket and drops it afterwards;
the ``plx`` database is never written. Without psycopg or a reachable server the tests
skip, saying why.
"""
from __future__ import annotations

import itertools
import os
import unittest

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - environment without psycopg
    psycopg = None

ADMIN_DSN = os.environ.get("PLX_PG_TEST_ADMIN_DSN", "postgresql:///postgres")
_COUNTER = itertools.count()


def _admin():
    if psycopg is None:
        raise unittest.SkipTest("psycopg not installed")
    try:
        return psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        raise unittest.SkipTest(f"no Postgres server: {str(exc).splitlines()[0]}")


def create_db() -> tuple[str, str]:
    """(name, dsn) of a new empty database."""
    name = f"plx_test_a_{os.getpid()}_{next(_COUNTER)}"
    with _admin() as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name, psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=name)


def drop_db(name: str) -> None:
    with _admin() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                      .format(sql.Identifier(name)))
