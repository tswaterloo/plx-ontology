"""Runs ON a Spark: the runs' Postgres socket, spread over the box's parallel reverse tunnels.

The box reaches a Spark through Petra's tailnet with a user-space Tailscale (no TUN device on
the box). One TCP stream through it carries about 1 MB/s from the box to the Spark, and every
``ssh -R`` tunnel is one stream, so all of a Spark's connections on the one old tunnel queued
behind each other's results: a connect took 0.3-17 s while one run read a large result, and
the role's connection limit filled with the waiting. The box now keeps 16 tunnels to each
Spark (``plx-spark-pg-tunnel@<spark>:<n>``, compressed; 12 streams measured ~9 MB/s together).
This relay listens where runs always connected (~/plx-ai/run/.s.PGSQL.5432, bound at
/run/postgresql in a spark_run sandbox) and hands each new connection to the least busy
tunnel: bytes it brought back lately plus a share per open connection, so a new connection
never waits behind another run's large read; a tunnel that is down is skipped.

A connect over a new tunnel channel costs two round trips (channel, then Postgres) before the
client's first byte. The relay keeps a few spares open on its least busy tunnels
(``--spares`` in all, one per tunnel at most) and hands one to the next client, so a connect
costs only the client's own start-up exchange. A spare carries no byte until a client gets it
(the server waits for the start-up packet, which the client sends itself, with its own user,
database and options), is closed after ``SPARE_MAX_AGE`` seconds unused (inside the server's
authentication_timeout of 60 s), and is never handed out once its far end has closed or
spoken. A spare holds a server process, not a login: it counts against max_connections, not
against plx_eval's connection limit.

Not PgBouncer: runs keep per-session state (the sq_gates item gates in
operating/pg/sqlite_compat.py, temp tables) and pass ``options=-c statement_timeout=...``,
which PgBouncer 1.22 refuses; this relay never reads the protocol. Access is unchanged: the
socket is 0600 in a 0700 folder of the Spark login, the tunnels' sockets are the same, and the
box end maps every login to ``plx_eval``. A connection no tunnel accepts is closed at once.

    python3 spark_pg_balance.py --listen ~/plx-ai/run/.s.PGSQL.5432 --tunnels ~/plx-ai/pgtun 16

Tunnel n binds ~/plx-ai/pgtun/t<n>/s-<start time>: a new name per start, because the sshd
session of a tunnel that just restarted unlinks its own socket path when it ends, which with
one fixed name removed the new session's socket and left that tunnel silently unreachable
(seen live: 2 of 16). The newest socket in the folder is used.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

CONNECT_TIMEOUT = 5.0
CHUNK = 1 << 16
SPARES = 6            # spare connections kept in all (one per tunnel at most)
BUSY_SKIP = 200_000.0 # bytes/s: a tunnel this busy is not used for a spare hand-out
SPARE_MAX_AGE = 25.0
REFILL_EVERY = 1.0
BUSY_HALF_LIFE = 5.0          # seconds
PER_CONNECTION = 20_000.0     # bytes/s an open connection counts for when placing a new one
MAX_TUNNELS = 16      # spark_pg_tunnel.sh accepts <spark>:1 .. <spark>:16


def tunnel_dirs(folder: str, count: int) -> list[str]:
    return [os.path.join(folder, f"t{n}") for n in range(1, count + 1)]


def sockets_newest_first(up: str) -> list[str]:
    """The tunnel's sockets, newest start first (``s-<nanoseconds>``, same width)."""
    try:
        names = [n for n in os.listdir(up) if n.startswith("s-")]
    except OSError:
        return []
    return [os.path.join(up, n) for n in sorted(names, reverse=True)[:2]]


async def open_tunnel(up: str, timeout: float):
    for path in sockets_newest_first(up):
        try:
            return await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)
        except (OSError, asyncio.TimeoutError):
            continue
    raise OSError(f"no live socket in {up}")


class Balancer:
    def __init__(self, upstreams: list[str], connect_timeout: float = CONNECT_TIMEOUT,
                 spares: int = SPARES, spare_max_age: float = SPARE_MAX_AGE):
        if not upstreams:
            raise ValueError("at least one tunnel socket is needed")
        if spares < 0 or spare_max_age <= 0:
            raise ValueError("spares must be >= 0 and their age > 0")
        self.upstreams = list(upstreams)
        self.active = {u: 0 for u in self.upstreams}
        self.spare = {u: [] for u in self.upstreams}   # (reader, writer, opened at)
        self.spares = spares
        self.spare_max_age = spare_max_age
        self.connect_timeout = connect_timeout
        self.busy = {u: 0.0 for u in self.upstreams}
        self.busy_at = {u: 0.0 for u in self.upstreams}
        self._turn = 0

    def carried(self, up: str, n: int, now: float | None = None) -> None:
        """Count bytes a tunnel brought back (a decaying rate, half-life BUSY_HALF_LIFE)."""
        now = time.monotonic() if now is None else now
        self.busy[up] = self.rate(up, now) + n / BUSY_HALF_LIFE
        self.busy_at[up] = now

    def rate(self, up: str, now: float) -> float:
        return self.busy[up] * 0.5 ** ((now - self.busy_at[up]) / BUSY_HALF_LIFE)

    def order(self, now: float | None = None) -> list[str]:
        """Least busy tunnels first: bytes/s each brought back lately, plus a share per open
        connection (an idle one may start a large read). A tunnel streaming a large result
        delays every connection on it, so new ones go where nothing is queued. Ties rotate."""
        now = time.monotonic() if now is None else now
        n = len(self.upstreams)
        start = self._turn % n
        self._turn += 1
        rotated = self.upstreams[start:] + self.upstreams[:start]
        return sorted(rotated, key=lambda u: self.rate(u, now) + self.active[u] * PER_CONNECTION)

    def _fresh_spare(self, up: str):
        """A spare of this tunnel that is young and whose far end is still open, else None
        (stale ones are closed on the way)."""
        now = time.monotonic()
        while self.spare[up]:
            reader, writer, opened = self.spare[up].pop(0)
            if _usable(reader, writer, opened, now, self.spare_max_age):
                return reader, writer
            writer.close()
        return None

    async def refill(self) -> None:
        """Close stale spares, then open the missing ones (``spares`` in all, at most one per
        tunnel) on the least busy tunnels; a tunnel that is down is skipped."""
        now = time.monotonic()
        for up in self.upstreams:
            keep = []
            for reader, writer, opened in self.spare[up]:
                if _usable(reader, writer, opened, now, self.spare_max_age):
                    keep.append((reader, writer, opened))
                else:
                    writer.close()
            self.spare[up] = keep
        missing = self.spares - sum(len(v) for v in self.spare.values())
        for up in self.order(now):
            if missing <= 0:
                break
            if self.spare[up]:
                continue
            try:
                reader, writer = await open_tunnel(up, self.connect_timeout)
            except (OSError, asyncio.TimeoutError):
                continue
            self.spare[up].append((reader, writer, time.monotonic()))
            missing -= 1

    async def keep_spares(self, every: float = REFILL_EVERY) -> None:
        while True:
            await self.refill()
            await asyncio.sleep(every)

    async def _open(self):
        now = time.monotonic()
        order = self.order(now)
        for up in order:
            if self.rate(up, now) >= BUSY_SKIP:
                continue          # a spare behind a large result is no faster than a new one
            got = self._fresh_spare(up)
            if got is not None:
                return (up, *got)
        for up in order:
            try:
                reader, writer = await open_tunnel(up, self.connect_timeout)
            except (OSError, asyncio.TimeoutError):
                continue
            return up, reader, writer
        return None

    async def handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter):
        opened = await self._open()
        if opened is None:
            print("spark_pg_balance: no tunnel accepted a connection", file=sys.stderr, flush=True)
            c_writer.close()
            return
        up, u_reader, u_writer = opened
        self.active[up] += 1
        try:
            await asyncio.gather(_pipe(c_reader, u_writer),
                                 _pipe(u_reader, c_writer, lambda n: self.carried(up, n)))
        finally:
            self.active[up] -= 1
            for w in (u_writer, c_writer):
                w.close()


def _usable(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, opened: float,
            now: float, max_age: float) -> bool:
    """A spare may be handed out: young, far end open, and nothing received yet (a server
    that spoke first, e.g. an error before the start-up packet, is not a fresh session)."""
    return (now - opened < max_age and not reader.at_eof() and not writer.is_closing()
            and not getattr(reader, "_buffer", b""))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                count=None) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            if count is not None:
                count(len(data))
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        # Half-close keeps the other direction flowing until its own end; a closed peer
        # ends both (the gather in handle waits for both pipes).
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (ConnectionError, OSError, RuntimeError):
            pass


async def serve(listen: str, upstreams: list[str], ready: asyncio.Event | None = None,
                connect_timeout: float = CONNECT_TIMEOUT, spares: int = SPARES,
                spare_max_age: float = SPARE_MAX_AGE, refill_every: float = REFILL_EVERY) -> None:
    path = Path(listen)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.is_socket():
        path.unlink()
    balancer = Balancer(upstreams, connect_timeout, spares, spare_max_age)
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(balancer.handle, path=str(path))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    keeper = asyncio.create_task(balancer.keep_spares(refill_every)) if spares else None
    if ready is not None:
        ready.set()
    try:
        async with server:
            await server.serve_forever()
    finally:
        if keeper is not None:
            keeper.cancel()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", required=True)
    ap.add_argument("--spares", type=int, default=SPARES, help="spare connections kept open in all")
    ap.add_argument("--tunnels", nargs=2, metavar=("DIR", "COUNT"), required=True,
                    help="folder of the tunnel folders t1..tCOUNT and their count")
    args = ap.parse_args(argv)
    folder, count = os.path.expanduser(args.tunnels[0]), int(args.tunnels[1])
    if not 1 <= count <= MAX_TUNNELS:
        ap.error(f"tunnel count must be 1..{MAX_TUNNELS}")
    asyncio.run(serve(os.path.expanduser(args.listen), tunnel_dirs(folder, count),
                      spares=args.spares))
    return 0


if __name__ == "__main__":
    sys.exit(main())
