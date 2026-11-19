"""Host service: a loopback TCP port that carries each connection to the Portal's read
replica through the Petra tailnet's SOCKS5 proxy (``plx-portal-replica-tunnel.service``).

The replica (11-17,
``read_only=on``) is reachable from this host only through the userspace tailnet node,
which offers SOCKS5 on loopback (``--socks``, 127.0.0.1:1055). libpq cannot speak SOCKS,
so this service listens on ``--listen`` (loopback only) and, for every connection, opens a
SOCKS5 CONNECT to the replica by name (``PLX_PORTAL_REPLICA_HOST`` /
``PLX_PORTAL_REPLICA_PORT`` from /etc/plx/portal-replica.env; the unit drops the login's
user and password from its environment) and copies bytes both ways. It never sees a
credential and never speaks Postgres: TLS and the login run end to end between the
Workbench (``integrations.portal_replica``, ``hostaddr`` this port, ``host`` the
replica's own name so TLS verifies it) and the replica.

Loopback only, both ends: a ``--listen`` or ``--socks`` address off loopback is refused.
Evidence: isolated-fixture (``tests/test_portal_replica_tunnel.py``: a fake proxy and a
fake server).
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import struct
import sys
import threading
from typing import Optional, Sequence

TIMEOUT = 10.0


class Refused(Exception):
    """A setting or a proxy answer the tunnel will not use (a short code)."""


def loopback(raw: str) -> tuple[str, int]:
    host, sep, port = str(raw or "").strip().rpartition(":")
    host = host.strip("[]")
    try:
        if not sep or not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        number = int(port)
        if not 0 < number < 65536:
            raise ValueError
    except ValueError:
        raise Refused("not_loopback") from None
    return host, number


def _exactly(sock: socket.socket, n: int) -> bytes:
    got = b""
    while len(got) < n:
        part = sock.recv(n - len(got))
        if not part:
            raise Refused("socks_closed")
        got += part
    return got


def socks_connect(proxy: tuple[str, int], host: str, port: int) -> socket.socket:
    """A stream to ``host:port`` through the SOCKS5 proxy (no auth, CONNECT by name)."""
    name = host.encode("idna")
    if not 0 < len(name) < 256 or not 0 < port < 65536:
        raise Refused("bad_target")
    up = socket.create_connection(proxy, timeout=TIMEOUT)
    try:
        up.sendall(b"\x05\x01\x00")
        if _exactly(up, 2) != b"\x05\x00":
            raise Refused("socks_auth")
        up.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        head = _exactly(up, 4)
        if head[0] != 5 or head[1] != 0:
            raise Refused(f"socks_refused_{head[1]}")
        kind = head[3]
        size = {1: 4, 4: 16}.get(kind)
        if size is None:
            size = _exactly(up, 1)[0] if kind == 3 else -1
        if size < 0:
            raise Refused("socks_reply")
        _exactly(up, size + 2)
        up.settimeout(None)
        return up
    except BaseException:
        up.close()
        raise


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def carry(local: socket.socket, proxy: tuple[str, int], host: str, port: int) -> None:
    """One connection: through the proxy to the target, both ways, then both closed."""
    try:
        up = socks_connect(proxy, host, port)
    except (Refused, OSError) as why:
        print(f"portal-replica-tunnel: not connected ({type(why).__name__}: {why})",
              file=sys.stderr, flush=True)
        local.close()
        return
    back = threading.Thread(target=_pipe, args=(up, local), daemon=True)
    back.start()
    _pipe(local, up)
    back.join()
    local.close()
    up.close()


def serve(listen: tuple[str, int], proxy: tuple[str, int], host: str, port: int,
          ready: Optional[threading.Event] = None, bound: Optional[list] = None) -> None:
    server = socket.create_server(listen, reuse_port=False)
    if bound is not None:
        bound.append(server.getsockname()[1])
    if ready is not None:
        ready.set()
    with server:
        while True:
            local, _peer = server.accept()
            threading.Thread(target=carry, args=(local, proxy, host, port),
                             daemon=True).start()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", default="127.0.0.1:15433")
    parser.add_argument("--socks", default="127.0.0.1:1055")
    args = parser.parse_args(argv)
    host = str(os.environ.get("PLX_PORTAL_REPLICA_HOST") or "").strip()
    try:
        port = int(str(os.environ.get("PLX_PORTAL_REPLICA_PORT") or "5432"))
        listen, proxy = loopback(args.listen), loopback(args.socks)
        if not host:
            raise Refused("no_replica_host")
    except (Refused, ValueError) as why:
        print(f"portal-replica-tunnel: not started ({why})", file=sys.stderr)
        return 2
    serve(listen, proxy, host, port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
