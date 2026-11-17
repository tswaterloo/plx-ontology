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

# The host's separate test cluster (``deploy/ops/test_pg_cluster.sh``): a DROP DATABASE on
# the served cluster waits for every backend there to absorb a ProcSignalBarrier, which one
# blocked writing to a slow client (ClientWrite) does not, so tests held their teardown for
# as long as a delta or proactive reader stalled (11-12). ``tools/testslot.py`` applies the
# same rule to a wrapped run; ``use_test_cluster`` applies it to a bare ``unittest`` run.
TEST_PG_PORT, PG_SOCKET_DIR = "5433", "/var/run/postgresql"


def test_cluster_port(environ: Any = None, socket_dir: str = PG_SOCKET_DIR) -> str | None:
    """The test cluster's port (``PLX_TEST_PG_PORT``, default 5433) when its socket exists
    and the caller chose no server (``PGPORT``, ``PGHOST``, ``PLX_PG_TEST_ADMIN_DSN``)."""
    environ = os.environ if environ is None else environ
    if any(environ.get(k) for k in ("PGPORT", "PGHOST", "PLX_PG_TEST_ADMIN_DSN")):
        return None
    port = environ.get("PLX_TEST_PG_PORT", TEST_PG_PORT)
    if port.isdigit() and Path(socket_dir, f".s.PGSQL.{port}").exists():
        return port
    return None


def use_test_cluster(environ: Any = None, socket_dir: str = PG_SOCKET_DIR) -> str | None:
    """Point this process (and every child it starts) at the test cluster when one runs:
    called at import by each test module that makes throwaway databases. Test code only;
    the migration proofs and rehearsals keep the server their caller chose."""
    environ = os.environ if environ is None else environ
    port = test_cluster_port(environ, socket_dir)
    if port is not None:
        environ["PGPORT"] = port
    return port
_COUNTER = itertools.count()


def superuser_dsn(dsn: str) -> str:
    """``dsn`` logged in as this OS user's own role (a superuser on the test cluster)
    instead of the run's ``PGUSER`` (plx_test, ARCH T7): only for the tests that prove a
    deploy file a superuser runs (the role files). ``PLX_PG_TEST_SUPERUSER`` overrides."""
    import getpass
    import psycopg
    user = os.environ.get("PLX_PG_TEST_SUPERUSER") or getpass.getuser()
    return psycopg.conninfo.make_conninfo(dsn, user=user)


def admin() -> Any:
    import psycopg
    return psycopg.connect(admin_dsn(), autocommit=True, connect_timeout=5)


def _byte_order(provider: Any, collate: Any) -> bool:
    """Whether a database of this locale provider and LC_COLLATE orders text by bytes
    (code points): libc C/POSIX (C.UTF-8 too), or the builtin provider."""
    if provider == "b":
        return True
    name = str(collate or "")
    return provider == "c" and (name in ("C", "POSIX") or name.startswith(("C.", "POSIX.")))


def create_db() -> tuple[str, str]:
    """(name, dsn) of a new empty database. It orders text by bytes as the served
    databases do (C.UTF-8), whatever locale the cluster was made with: a Spark's test
    cluster initialised under en_US.utf8 read key and citation ranges linguistically
    ('probe:zz' past 'probe;'), red there and green elsewhere (11-15). Tests of a
    linguistic database make one themselves (test_pg_byte_order, test_pg_link_keys)."""
    import psycopg
    from psycopg import sql
    name = f"plx_test_a_{os.getpid()}_{next(_COUNTER)}"
    with admin() as conn:
        provider, collate = conn.execute(
            "SELECT datlocprovider, datcollate FROM pg_database WHERE datname = 'template1'"
        ).fetchone()
        if _byte_order(provider, collate):
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        else:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 LOCALE_PROVIDER libc "
                                 "LC_COLLATE 'C'").format(sql.Identifier(name)))
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
