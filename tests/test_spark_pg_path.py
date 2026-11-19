"""The Spark's Postgres path (deploy/spark): the relay a Spark run connects through, spread over
the box's parallel tunnels, and the tunnel script's refusal of anything but <spark>:<n>.
Checked against local stand-in tunnel sockets (no Spark, no Postgres)."""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "spark"))
import spark_pg_balance as bal  # noqa: E402


class _Upstream:
    """A stand-in tunnel socket: echoes what it gets, counts accepts and open connections,
    and can say something first (like a server refusing a connection)."""

    def __init__(self, path: str, greet: bytes = b""):
        self.path, self.greet = path, greet
        self.accepted = 0
        self.open = 0
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self):
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)

    async def _handle(self, reader, writer):
        self.accepted += 1
        self.open += 1
        self.writers.append(writer)
        try:
            if self.greet:
                writer.write(self.greet)
                await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.open -= 1
            writer.close()

    async def stop(self):
        self.server.close()
        for w in self.writers:
            w.close()


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pgb", dir="/tmp")
        self.listen = os.path.join(self.tmp, "run", ".s.PGSQL.5432")
        self.ups: list[_Upstream] = []
        self.task = None

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        for u in self.ups:
            await u.stop()
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    async def _start(self, n_up: int, *, missing: tuple[int, ...] = (), greet: bytes = b"",
                     **kw):
        paths = []
        for i in range(n_up):
            d = os.path.join(self.tmp, f"t{i}")
            os.makedirs(d)
            paths.append(d)
            if i not in missing:
                u = _Upstream(os.path.join(d, f"s-{time.time_ns()}"), greet)
                await u.start()
                self.ups.append(u)
        ready = asyncio.Event()
        self.task = asyncio.create_task(bal.serve(self.listen, paths, ready, connect_timeout=1.0,
                                                  **kw))
        await asyncio.wait_for(ready.wait(), 5)
        return paths

    async def _client(self):
        return await asyncio.open_unix_connection(self.listen)

    async def test_bytes_cross_intact_both_ways(self):
        await self._start(2, spares=0)
        payload = os.urandom(3 * 1024 * 1024)
        r, w = await self._client()

        async def send():
            w.write(payload)
            await w.drain()

        got, _ = await asyncio.wait_for(
            asyncio.gather(r.readexactly(len(payload)), send()), 20)
        self.assertEqual(got, payload)
        w.close()

    async def test_listen_socket_is_owner_only(self):
        await self._start(1, spares=0)
        self.assertEqual(stat.S_IMODE(os.stat(self.listen).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.listen)).st_mode), 0o700)

    async def test_connections_spread_over_tunnels(self):
        await self._start(4, spares=0)
        held = []
        for _ in range(12):
            r, w = await self._client()
            w.write(b"x")
            await r.readexactly(1)
            held.append(w)
        self.assertEqual(sorted(u.open for u in self.ups), [3, 3, 3, 3])
        # N=60: every one gets its own echo back, none lost or crossed.
        conns = [await self._client() for _ in range(60)]
        for i, (r, w) in enumerate(conns):
            w.write(f"{i:04d}".encode())
        for i, (r, w) in enumerate(conns):
            self.assertEqual(await asyncio.wait_for(r.readexactly(4), 10), f"{i:04d}".encode())
        self.assertEqual(sum(u.open for u in self.ups), 72)
        for w in held + [w for _, w in conns]:
            w.close()

    async def test_a_down_tunnel_is_skipped(self):
        await self._start(3, missing=(0, 2), spares=0)
        for _ in range(5):
            r, w = await self._client()
            w.write(b"ok")
            self.assertEqual(await asyncio.wait_for(r.readexactly(2), 5), b"ok")
            w.close()
        self.assertEqual(self.ups[0].accepted, 5)

    async def test_no_tunnel_up_closes_the_client(self):
        await self._start(2, missing=(0, 1), spares=1)
        r, w = await self._client()
        self.assertEqual(await asyncio.wait_for(r.read(10), 5), b"")
        w.close()

    async def test_spare_is_handed_out_and_replaced(self):
        await self._start(1, spares=1, refill_every=0.05)
        up = self.ups[0]
        await _until(lambda: up.accepted == 1)
        r, w = await self._client()
        w.write(b"hi")
        self.assertEqual(await asyncio.wait_for(r.readexactly(2), 5), b"hi")
        self.assertEqual(up.accepted, 1, "the client got the spare, not a new connection")
        await _until(lambda: up.accepted == 2)   # a new spare stands by
        w.close()

    async def test_stale_or_spoken_spares_are_never_handed_out(self):
        # A spare older than its age limit is closed and replaced, not given to a client.
        await self._start(1, spares=1, spare_max_age=0.2, refill_every=0.05)
        up = self.ups[0]
        await _until(lambda: up.accepted >= 3)
        self.assertLessEqual(up.open, 1)

    async def test_spare_whose_server_spoke_first_is_dropped(self):
        await self._start(1, spares=1, refill_every=0.05, greet=b"E")
        up = self.ups[0]
        await _until(lambda: up.accepted >= 1)
        await asyncio.sleep(0.1)
        r, w = await self._client()
        w.write(b"z")
        # The spare that received "E" before any client is not used: this client's stream
        # starts with its own server's greeting and then its echo, never a stale byte.
        self.assertEqual(await asyncio.wait_for(r.readexactly(2), 5), b"Ez")
        self.assertGreaterEqual(up.accepted, 2)
        w.close()

    async def test_newest_live_socket_of_a_restarted_tunnel_is_used(self):
        # A tunnel that restarted: its old socket file is left behind (nothing listens) and
        # the new start bound a newer name. Then the reverse: the newest is dead.
        d = os.path.join(self.tmp, "t0")
        os.makedirs(d)
        old = os.path.join(d, "s-1000000000000000000")
        new = os.path.join(d, "s-2000000000000000000")
        live = _Upstream(new)
        await live.start()
        self.ups.append(live)
        dead = await asyncio.start_unix_server(lambda r, w: None, path=old)
        dead.close()
        await dead.wait_closed()
        os.path.exists(old) or Path(old).touch()
        ready = asyncio.Event()
        self.task = asyncio.create_task(bal.serve(self.listen, [d], ready, spares=0))
        await asyncio.wait_for(ready.wait(), 5)
        r, w = await self._client()
        w.write(b"n")
        self.assertEqual(await asyncio.wait_for(r.readexactly(1), 5), b"n")
        w.close()
        await live.stop()
        os.unlink(new)
        newest_dead = os.path.join(d, "s-3000000000000000000")
        Path(newest_dead).touch()
        live2 = _Upstream(os.path.join(d, "s-2500000000000000000"))
        await live2.start()
        self.ups.append(live2)
        r, w = await self._client()
        w.write(b"m")
        self.assertEqual(await asyncio.wait_for(r.readexactly(1), 5), b"m")
        w.close()

    async def test_spares_are_capped_in_all_one_per_tunnel(self):
        await self._start(5, spares=3, refill_every=0.05)
        await _until(lambda: sum(u.accepted for u in self.ups) == 3)
        await asyncio.sleep(0.3)
        self.assertEqual(sorted(u.open for u in self.ups), [0, 0, 1, 1, 1])

    async def test_client_close_frees_the_tunnel_slot(self):
        await self._start(1, spares=0)
        up = self.ups[0]
        r, w = await self._client()
        w.write(b"a")
        await r.readexactly(1)
        w.close()
        await _until(lambda: up.open == 0)


async def _until(cond, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


class BalancerArgsTests(unittest.TestCase):
    def test_new_connections_avoid_a_tunnel_streaming_a_large_result(self):
        b = bal.Balancer(["/a", "/b", "/c"], spares=0)
        b.active.update({"/a": 1, "/b": 4, "/c": 2})
        b.carried("/a", 20_000_000, now=100.0)          # /a is busy with one big read
        self.assertEqual(b.order(now=101.0)[0], "/c")
        self.assertEqual(b.order(now=101.0)[-1], "/a")
        # Once it has gone quiet, its single connection makes it the first choice again.
        self.assertEqual(b.order(now=200.0)[0], "/a")

    def test_refuses_no_tunnel_or_bad_spares(self):
        with self.assertRaises(ValueError):
            bal.Balancer([])
        with self.assertRaises(ValueError):
            bal.Balancer(["/x"], spares=-1)


class TunnelScriptTests(unittest.TestCase):
    """spark_pg_tunnel.sh takes exactly <spark>:<1-9>; anything else stops before any ssh."""

    def _run(self, inst: str):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "ssh.log")
            fake = Path(tmp, "ssh")
            fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\necho /home/x\n")
            fake.chmod(0o755)
            env = {"PATH": f"{tmp}:/usr/bin:/bin", "PLX_SPARK_SSH_CONFIG": "/dev/null"}
            p = subprocess.run([str(ROOT / "deploy/spark/spark_pg_tunnel.sh"), inst], env=env,
                               capture_output=True, text=True, timeout=20)
            return p.returncode, (log.read_text() if log.exists() else "")

    def test_bad_instances_never_reach_ssh(self):
        for inst in ("", "s7d3d", "s7d3d:0", "a;b:1", "-oProxyCommand=x:1",
                     "s7d3d:1:2", "../x:1", "s7d3d:17", "s7d3d:01"):
            code, calls = self._run(inst)
            self.assertEqual((code, calls), (2, ""), inst)

    def test_good_instance_forwards_its_own_socket_compressed(self):
        self.assertEqual(self._run("s7d3d:16")[0], 0)
        code, calls = self._run("s7d3d:3")
        self.assertEqual(code, 0)
        first, second = calls.strip().splitlines()
        self.assertIn("plx-ai/pgtun/t3", first)
        self.assertIn("Compression=yes", second)
        self.assertRegex(second, r"-R /home/x/plx-ai/pgtun/t3/s-[0-9]{19}:"
                                 r"/run/plx-spark-pg/\.s\.PGSQL\.5432 ")
        self.assertTrue(second.rstrip().endswith("s7d3d"))


class UnitsTests(unittest.TestCase):
    def test_spark_relay_listens_where_runs_connect_over_all_tunnels(self):
        unit = (ROOT / "deploy/spark/spark-user/plx-spark-pg-balance.service").read_text()
        self.assertIn("--listen %h/plx-ai/run/.s.PGSQL.5432 --tunnels %h/plx-ai/pgtun 16", unit)
        self.assertEqual(bal.tunnel_dirs("/h/pgtun", 16)[-1], "/h/pgtun/t16")
        box = (ROOT / "deploy/spark/systemd/plx-spark-pg-tunnel@.service").read_text()
        self.assertIn("spark_pg_tunnel.sh %i", box)


if __name__ == "__main__":
    unittest.main()
