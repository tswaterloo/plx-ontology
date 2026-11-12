"""Throwaway Postgres databases (``plx_test_a_<pid>_<n>``) for the tests and the migration
proofs: created over the local socket (``PLX_PG_TEST_ADMIN_DSN``), dropped by their
creator. The ``plx`` database is never written. psycopg only."""
from __future__ import annotations

import itertools
import os
import re
from pathlib import Path
from typing import Any



def admin_dsn() -> str:
    """The server for throwaway databases, read at call time: ``PLX_PG_TEST_ADMIN_DSN`` when
    set, else the local socket, where libpq honours ``PGPORT`` (testslot sets it to the
    separate test cluster so a test run never touches the production cluster)."""
    return os.environ.get("PLX_PG_TEST_ADMIN_DSN") or "postgresql:///postgres"


ADMIN_DSN = admin_dsn()  # kept for importers of the old constant
_COUNTER = itertools.count()


def admin() -> Any:
    import psycopg
    return psycopg.connect(admin_dsn(), autocommit=True, connect_timeout=5)


def create_db() -> tuple[str, str]:
    """(name, dsn) of a new empty database."""
    import psycopg
    from psycopg import sql
    name = f"plx_test_a_{os.getpid()}_{next(_COUNTER)}"
    with admin() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name, psycopg.conninfo.make_conninfo(admin_dsn(), dbname=name)


def drop_db(name: str) -> None:
    from psycopg import sql
    if not name.startswith("plx_test_a_"):
        raise ValueError("not a scratch database")
    with admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                     .format(sql.Identifier(name)))


# Every throwaway name: plx_test_<kind>_<pid>[_<n>] (a: scratch_db, b: chunk embeds, d: the
# search fixture, qu: query understanding, appapi: the app API fixture)
_THROWAWAY = re.compile(r"^plx_(?:test_[a-z]+|restore_test)_(\d+)(?:_\d+)?$")


def stale(names: list[str], alive: Any = None) -> list[str]:
    """The throwaway databases among ``names`` whose creating process no longer runs: a
    killed test run never dropped them (REL-R-16). ``alive(pid)`` defaults to /proc."""
    alive = alive or (lambda pid: Path(f"/proc/{pid}").exists())
    return sorted(n for n in names if (m := _THROWAWAY.match(n)) and not alive(int(m.group(1))))


def sweep(apply: bool = False) -> list[str]:
    """Drop (``apply``) or just list the stale throwaway databases of the admin server.
    Only names of the throwaway shape are ever touched; ``plx`` never is."""
    from psycopg import sql
    with admin() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'plx\\_test\\_%' OR datname LIKE 'plx\\_restore\\_test\\_%'")]
        found = stale(names)
        if apply:
            for name in found:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                             .format(sql.Identifier(name)))
    return found


if __name__ == "__main__":
    import sys
    done = sweep(apply="--apply" in sys.argv[1:])
    print(f"{'dropped' if '--apply' in sys.argv[1:] else 'stale'} {len(done)}: "
          + " ".join(done))
