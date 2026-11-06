"""What the search index covers now: the newest searchable text per source system
(``search.chunks``), so a freshness label states what search can find, not only what was
captured (a capture can be days ahead of the index until the next refresh).

``coverage(dsn)`` -> {system: ISO time of the newest searchable chunk}; cached for
``CACHE_SECONDS`` per database; {} when the index cannot be read (nothing is claimed).
Dates in the future (a meeting scheduled ahead) never count as coverage.
"""
from __future__ import annotations

import threading
import time
from typing import Any

# chunk kind -> the system Home and answers name (``operator_api.backend.STORE_SYSTEMS``)
SYSTEMS = {"mail": "Email", "attachment": "Email", "teams": "Teams", "sharepoint": "File"}
CACHE_SECONDS = 600.0
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def coverage(dsn: str | None = None) -> dict[str, str]:
    from operating.search.config import dsn as default_dsn
    target = dsn or default_dsn()
    now = time.monotonic()
    with _LOCK:
        held = _CACHE.get(target)
        if held is not None and now - held[0] < CACHE_SECONDS:
            return dict(held[1])
    try:
        out = _read(target)
    except Exception:  # noqa: BLE001 - an unreadable index claims nothing
        out = {}
    with _LOCK:
        _CACHE[target] = (now, out)
    return dict(out)


def _read(target: str) -> dict[str, str]:
    import psycopg
    with psycopg.connect(target, autocommit=True, connect_timeout=5,
                         options="-c statement_timeout=5000") as conn:
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
