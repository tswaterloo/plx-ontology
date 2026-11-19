"""What the search index covers now: the newest searchable text per source system
(``search.chunks``), so a freshness label states what search can find, not only what was
captured (a capture can be days ahead of the index until the next refresh).

``coverage(dsn)`` -> {system: ISO time of the newest searchable chunk}; read in the
background and kept for ``CACHE_SECONDS`` per database, never waited on by a page past
``WAIT_SECONDS``; {} when the index cannot be read yet (nothing is claimed).
Dates in the future (a meeting scheduled ahead) never count as coverage.
"""
from __future__ import annotations

import threading
import time
from typing import Any

# chunk kind -> the system Home and answers name (``operator_api.backend.STORE_SYSTEMS``)
SYSTEMS = {"mail": "Email", "attachment": "Email", "teams": "Teams", "sharepoint": "File"}
CACHE_SECONDS = 600.0
# a page never waits on the read longer than this: the scan of the chunks takes 15 s on
# served data (11-18; no index on kind and time), so it runs in the background, a page
# shows what was read last (or claims nothing before the first read ends)
WAIT_SECONDS = 0.5
READ_SECONDS = 60
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_READING: dict[str, threading.Event] = {}


def coverage(dsn: str | None = None, *, wait: float | None = None) -> dict[str, str]:
    """The newest searchable time per system: the last read's at once (a read older than
    ``CACHE_SECONDS`` starts the next one in the background, one per database at a time);
    before any read ended, up to ``wait`` seconds (``WAIT_SECONDS``) for the first, else
    {} (nothing claimed)."""
    from operating.search.config import dsn as default_dsn
    target = dsn or default_dsn()
    with _LOCK:
        held = _CACHE.get(target)
        fresh = held is not None and time.monotonic() - held[0] < CACHE_SECONDS
        done = _READING.get(target)
        if not fresh and done is None:
            done = _READING[target] = threading.Event()
            threading.Thread(target=_refresh, args=(target, done), daemon=True,
                             name="search-coverage").start()
    if held is not None:
        return dict(held[1])
    if done is not None:
        done.wait(WAIT_SECONDS if wait is None else wait)
    with _LOCK:
        held = _CACHE.get(target)
    return dict(held[1]) if held is not None else {}


def _refresh(target: str, done: threading.Event) -> None:
    try:
        out = _read(target)
    except Exception:  # noqa: BLE001 - an unreadable index claims nothing
        out = {}
    with _LOCK:
        _CACHE[target] = (time.monotonic(), out)
        _READING.pop(target, None)
    done.set()


def _read(target: str) -> dict[str, str]:
    import psycopg
    with psycopg.connect(target, autocommit=True, connect_timeout=5,
                         options=f"-c statement_timeout={READ_SECONDS * 1000}") as conn:
        rows: list[Any] = conn.execute(
            "SELECT kind, max(occurred_at) FROM search.chunks WHERE kind = ANY(%s) "
            "AND occurred_at <= now() + interval '1 hour' GROUP BY kind",
            (sorted(SYSTEMS),)).fetchall()
    out: dict[str, str] = {}
    for kind, newest in rows:
        if newest is None:
            continue
        system = SYSTEMS[kind]
        stamp = newest.isoformat()
        if system not in out or stamp > out[system]:
            out[system] = stamp
    return out
