"""One read-only connection to the Portal's read replica (11-17: ``read_only=on``), for the prepared-writes desk's "record as
read" (``operating.prepared_read.SystemReader.portal``).

Where it is: ``PLX_PORTAL_REPLICA_DSN``, else the parts the host keeps in
``/etc/plx/portal-replica.env`` (``PLX_PORTAL_REPLICA_HOST``, ``_PORT``, ``_DB``, ``_USER``,
``_PASSWORD``, ``_SSLMODE``). The replica is reachable from this host only through the
Petra tailnet's SOCKS proxy, which the host's tunnel service carries to a loopback port
(``deploy/ops/portal_replica_tunnel.py``): ``PLX_PORTAL_REPLICA_HOSTADDR`` (loopback only)
and ``PLX_PORTAL_REPLICA_TUNNEL_PORT`` name it, and libpq connects there by ``hostaddr``
while ``host`` stays the replica's own name, so TLS verifies that name when the sslmode
asks for it. The connection is read-only (``read_only`` before any statement); nothing
here writes. A failure is reported by its kind only, never with the DSN or a password.
Evidence: isolated-fixture (``integrations/tests/test_portal_replica.py``).
"""
from __future__ import annotations

import ipaddress
from typing import Any, Mapping

DSN_ENV = "PLX_PORTAL_REPLICA_DSN"
HOSTADDR_ENV = "PLX_PORTAL_REPLICA_HOSTADDR"
TUNNEL_PORT_ENV = "PLX_PORTAL_REPLICA_TUNNEL_PORT"
PARTS = {"host": "PLX_PORTAL_REPLICA_HOST", "port": "PLX_PORTAL_REPLICA_PORT",
         "dbname": "PLX_PORTAL_REPLICA_DB", "user": "PLX_PORTAL_REPLICA_USER",
         "password": "PLX_PORTAL_REPLICA_PASSWORD", "sslmode": "PLX_PORTAL_REPLICA_SSLMODE"}
CONNECT_SECONDS = 10


class ReplicaUnavailable(Exception):
    """No replica configured, or it cannot be reached now (a short code, never a secret)."""


def _value(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name) or "").strip()


def configured(environ: Mapping[str, str]) -> bool:
    return bool(_value(environ, DSN_ENV)) or all(
        _value(environ, PARTS[k]) for k in ("host", "dbname", "user"))


def connect_args(environ: Mapping[str, str]) -> dict[str, Any]:
    """The libpq keyword arguments (module doc); raises ``ReplicaUnavailable``."""
    dsn = _value(environ, DSN_ENV)
    tunnel = _value(environ, HOSTADDR_ENV)
    if dsn:
        if tunnel:
            raise ReplicaUnavailable("dsn_with_tunnel")   # name the parts for the tunnel
        return {"conninfo": dsn}
    if not configured(environ):
        raise ReplicaUnavailable("not_configured")
    args: dict[str, Any] = {k: _value(environ, v) for k, v in PARTS.items()
                            if _value(environ, v)}
    try:
        args["port"] = int(args.get("port") or 5432)
        if tunnel:
            if not ipaddress.ip_address(tunnel).is_loopback:
                raise ValueError
            args["hostaddr"], args["port"] = tunnel, int(_value(environ, TUNNEL_PORT_ENV))
        if not 0 < args["port"] < 65536:
            raise ValueError
    except ValueError:
        raise ReplicaUnavailable("bad_address") from None
    return args


def connect(environ: Mapping[str, str]) -> Any:
    """A read-only psycopg connection to the replica; raises ``ReplicaUnavailable``."""
    args = connect_args(environ)
    try:
        import psycopg
        conn = psycopg.connect(**args, connect_timeout=CONNECT_SECONDS)
    except Exception as error:  # noqa: BLE001 - never the message: it may carry the DSN
        raise ReplicaUnavailable(type(error).__name__) from None
    conn.read_only = True
    return conn
