"""Throwaway Postgres databases for the operating.pg tests (``operating.pg.scratch_db``).

Each test class creates its own database over the local socket and drops it afterwards;
the ``plx`` database is never written. Without psycopg or a reachable server the tests
skip, saying why.
"""
from __future__ import annotations

import unittest

from operating.pg import scratch_db

try:
    import psycopg
except ImportError:  # pragma: no cover - environment without psycopg
    psycopg = None

ADMIN_DSN = scratch_db.ADMIN_DSN


def _admin():
    if psycopg is None:
        raise unittest.SkipTest("psycopg not installed")
    try:
        return scratch_db.admin()
    except psycopg.OperationalError as exc:
        raise unittest.SkipTest(f"no Postgres server: {str(exc).splitlines()[0]}")


def create_db() -> tuple[str, str]:
    """(name, dsn) of a new empty database."""
    _admin().close()
    return scratch_db.create_db()


def drop_db(name: str) -> None:
    _admin().close()
    scratch_db.drop_db(name)
