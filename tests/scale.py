"""The 1/10/60 scale case (speed specs 1.5): every serving piece runs its read or build for
N = 1, 10 and 60 (records, readers, weeks, items: the piece says what N is) and proves the
number of SQL statements does not grow with N and the time stays flat. This is the test that
catches a per-item round trip (the 600-statements-per-record pages).

    from tests.scale import assert_flat
    assert_flat(lambda n: build(pg, keys[:n]), pg=pg)

Statements are counted on ``pg`` (a psycopg connection: every cursor it makes, server-side ones
included, counts each ``execute`` / ``executemany`` / ``copy``), so the code under test must run
its statements on that connection; no pg_stat_statements is needed in the throwaway Postgres.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Iterator, Sequence

NS = (1, 10, 60)
SLACK_SECONDS = 0.05


@contextlib.contextmanager
def counting(pg: Any) -> Iterator[list[int]]:
    """Count the statements run on ``pg`` while the block runs: ``got[0]``."""
    got = [0]

    def counted(base: type) -> type:
        class Counted(base):  # type: ignore[misc, valid-type]
            def execute(self, *a: Any, **k: Any) -> Any:
                got[0] += 1
                return super().execute(*a, **k)

            def executemany(self, *a: Any, **k: Any) -> Any:
                got[0] += 1
                return super().executemany(*a, **k)

            def copy(self, *a: Any, **k: Any) -> Any:
                got[0] += 1
                return super().copy(*a, **k)
        return Counted

    plain, server = pg.cursor_factory, getattr(pg, "server_cursor_factory", None)
    pg.cursor_factory = counted(plain)
    if server is not None:
        pg.server_cursor_factory = counted(server)
    try:
        yield got
    finally:
        pg.cursor_factory = plain
        if server is not None:
            pg.server_cursor_factory = server


def measure(run: Callable[[int], Any], *, pg: Any, ns: Sequence[int] = NS
            ) -> dict[int, tuple[int, float]]:
    """{n: (statements, seconds)} of ``run(n)`` for each n."""
    out = {}
    for n in ns:
        with counting(pg) as got:
            t = time.perf_counter()
            run(n)
            out[n] = (got[0], time.perf_counter() - t)
    return out


def assert_flat(run: Callable[[int], Any], *, pg: Any, ns: Sequence[int] = NS) -> dict:
    """``run(n)`` for n in 1, 10, 60: the same statement count for every n, and the time at
    the largest n at most 3 x the time at the smallest + 50 ms. Returns what it measured."""
    got = measure(run, pg=pg, ns=ns)
    counts = {n: c for n, (c, _) in got.items()}
    if len(set(counts.values())) != 1:
        raise AssertionError(f"statements grow with N: {counts} (a per-item round trip)")
    if counts[ns[0]] == 0:
        raise AssertionError("no statement ran on the connection given: the count proves nothing")
    small, large = got[ns[0]][1], got[ns[-1]][1]
    if large > 3 * small + SLACK_SECONDS:
        raise AssertionError(f"time not flat: {large * 1000:.0f} ms at N={ns[-1]} over 3 x "
                             f"{small * 1000:.0f} ms at N={ns[0]} + {SLACK_SECONDS * 1000:.0f} ms")
    return got
