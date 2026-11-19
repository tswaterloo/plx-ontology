"""The Portal read replica's connection (``integrations.portal_replica``) and the host's
tunnel to it (``deploy/ops/portal_replica_tunnel.py``): through the tailnet's loopback
SOCKS5 proxy the tunnel carries each stream to the replica by name; libpq is pointed at it
by ``hostaddr`` with the replica's own name kept for TLS; an off-loopback address, a
proxy refusal or a missing setting fails closed, never with a secret in the reason.

A fake SOCKS5 proxy and a fake server on loopback; no network. Evidence: isolated-fixture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import struct
import threading
import unittest
from unittest import mock

from integrations import portal_replica as pr

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "portal_replica_tunnel", ROOT / "deploy" / "ops" / "portal_replica_tunnel.py")
tunnel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tunnel)  # type: ignore[union-attr]

PARTS = {"PLX_PORTAL_REPLICA_HOST": "replica.example.internal", "PLX_PORTAL_REPLICA_PORT": "5432",
         "PLX_PORTAL_REPLICA_DB": "plxportal", "PLX_PORTAL_REPLICA_USER": "wb_reader",
         "PLX_PORTAL_REPLICA_PASSWORD": "synthetic-secret", "PLX_PORTAL_REPLICA_SSLMODE": "require"}


def listen() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    s.settimeout(5)
    return s


class FakeSocks:
    """SOCKS5, no auth, CONNECT by name; answers ``code``; connects every accepted stream
    to the fake server whatever it is asked for, and records the name and port asked."""

    def __init__(self, server_port: int, code: int = 0) -> None:
        self.sock, self.server_port, self.code, self.asked = listen(), server_port, code, []
        threading.Thread(target=self.serve, daemon=True).start()

    @property
    def address(self) -> tuple[str, int]:
        return "127.0.0.1", self.sock.getsockname()[1]

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(conn,), daemon=True).start()

    def one(self, conn: socket.socket) -> None:
        assert conn.recv(3) == b"\x05\x01\x00"
        conn.sendall(b"\x05\x00")
        head = conn.recv(5)
        name = conn.recv(head[4]).decode()
        port = struct.unpack(">H", conn.recv(2))[0]
        self.asked.append((name, port))
        conn.sendall(bytes([5, self.code, 0, 1, 127, 0, 0, 1, 0, 0]))
        if self.code:
            conn.close()
            return
        up = socket.create_connection(("127.0.0.1", self.server_port))
        threading.Thread(target=tunnel._pipe, args=(up, conn), daemon=True).start()
        tunnel._pipe(conn, up)


class Upper:
    """The 'replica': answers every chunk upper-cased, any number of connections."""

    def __init__(self) -> None:
        self.sock = listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(conn,), daemon=True).start()

    @staticmethod
    def one(conn: socket.socket) -> None:
        while True:
            data = conn.recv(1024)
            if not data:
                break
            conn.sendall(data.upper())
        conn.close()


def started(proxy: tuple[str, int]) -> int:
    ready, bound = threading.Event(), []
    threading.Thread(target=tunnel.serve, args=(("127.0.0.1", 0), proxy,
                                                "replica.example.internal", 5432, ready, bound),
                     daemon=True).start()
    ready.wait(5)
    return bound[0]


class Tunnel(unittest.TestCase):
    def test_each_stream_reaches_the_replica_by_name_through_the_proxy(self):
        socks = FakeSocks(Upper().sock.getsockname()[1])
        port = started(socks.address)
        for n in (1, 10):                       # one and several connections, each carried
            with self.subTest(n=n):
                conns = [socket.create_connection(("127.0.0.1", port), timeout=5)
                         for _ in range(n)]
                for i, c in enumerate(conns):
                    c.sendall(f"select {i}".encode())
                self.assertEqual([c.recv(64) for c in conns],
                                 [f"SELECT {i}".encode() for i in range(n)])
                for c in conns:
                    c.close()
        self.assertEqual(set(socks.asked), {("replica.example.internal", 5432)})
        self.assertEqual(len(socks.asked), 11)

    def test_a_proxy_refusal_closes_the_stream_and_off_loopback_is_refused(self):
        port = started(FakeSocks(Upper().sock.getsockname()[1], code=5).address)
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.assertEqual(c.recv(16), b"")       # closed, nothing carried
        for bad in ("10.0.0.5:1055", "proxy.example:1055", "127.0.0.1", "127.0.0.1:0",
                    "127.0.0.1:99999", "0.0.0.0:15433"):
            with self.subTest(bad):
                with self.assertRaises(tunnel.Refused):
                    tunnel.loopback(bad)
        self.assertEqual(tunnel.main(["--listen", "0.0.0.0:15433"]), 2)


class ConnectArgs(unittest.TestCase):
    def test_the_tunnel_is_used_by_address_and_the_name_kept_for_tls(self):
        args = pr.connect_args({**PARTS, pr.HOSTADDR_ENV: "127.0.0.1",
                                pr.TUNNEL_PORT_ENV: "15433"})
        self.assertEqual((args["host"], args["hostaddr"], args["port"], args["sslmode"]),
                         ("replica.example.internal", "127.0.0.1", 15433, "require"))
        self.assertEqual(pr.connect_args(dict(PARTS))["port"], 5432)
        self.assertEqual(pr.connect_args({pr.DSN_ENV: "host=x dbname=y"}),
                         {"conninfo": "host=x dbname=y"})

    def test_off_loopback_a_bad_port_a_dsn_with_a_tunnel_or_nothing_fails_closed(self):
        bad = [{**PARTS, pr.HOSTADDR_ENV: "10.0.0.5", pr.TUNNEL_PORT_ENV: "15433"},
               {**PARTS, pr.HOSTADDR_ENV: "127.0.0.1", pr.TUNNEL_PORT_ENV: "x"},
               {**PARTS, pr.HOSTADDR_ENV: "127.0.0.1"},
               {**PARTS, "PLX_PORTAL_REPLICA_PORT": "70000"},
               {pr.DSN_ENV: "host=x", pr.HOSTADDR_ENV: "127.0.0.1"},
               {"PLX_PORTAL_REPLICA_HOST": "h"}, {}]
        for env in bad:
            with self.subTest(env=sorted(env)):
                with self.assertRaises(pr.ReplicaUnavailable):
                    pr.connect_args(env)

    def test_a_failed_connection_names_its_kind_never_the_secret(self):
        boom = RuntimeError("could not connect: password=synthetic-secret host=...")
        with mock.patch("psycopg.connect", side_effect=boom):
            with self.assertRaises(pr.ReplicaUnavailable) as got:
                pr.connect(dict(PARTS))
        self.assertEqual(str(got.exception), "RuntimeError")


if __name__ == "__main__":
    unittest.main()
