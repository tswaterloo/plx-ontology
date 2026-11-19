"""Read-only truth reads: role ``plx_eval``, a read-only transaction, a 60 s statement cap.

Connections come from one bounded pool (``QBANK_PG_CONNS``, default 6: the server's slots are
shared with the live API), whatever the number of threads; credential-shaped variables never
reach a child process.
``query`` returns rows as dicts with JSON-safe values (Decimal stays Decimal until
``jsonable``; money is compared as Decimal by the grader).
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import queue
import threading
from typing import Any, Mapping

STATEMENT_TIMEOUT_S = 60
ROLE = os.environ.get("QBANK_PG_ROLE", "plx_eval")
DBNAME = os.environ.get("QBANK_PG_DB", "plx")
HOST = os.environ.get("QBANK_PG_HOST", "")

MAX_CONNS = int(os.environ.get("QBANK_PG_CONNS", "6"))
_idle: "queue.LifoQueue[Any]" = queue.LifoQueue()
_slots = threading.BoundedSemaphore(MAX_CONNS)


class TruthError(RuntimeError):
    """A truth read failed (timeout, missing table, bad SQL): the item is not answerable."""


def _conninfo() -> str:
    parts = [f"dbname={DBNAME}", f"user={ROLE}", "application_name=qbank"]
    if HOST:
        parts.append(f"host={HOST}")
    return " ".join(parts)


def connect() -> Any:
    import psycopg  # optional dependency already used by the served ontology

    conn = psycopg.connect(_conninfo(), autocommit=False)
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
        cur.execute("SET default_transaction_read_only = on")
    conn.commit()
    return conn


def query(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    with _slots:
        try:
            conn = _idle.get_nowait()
        except queue.Empty:
            conn = None
        if conn is None or conn.closed:
            try:
                conn = connect()
            except Exception as exc:  # noqa: BLE001 - no slot or no server: unanswerable now
                raise TruthError(f"connect: {type(exc).__name__}: {str(exc)[:200]}") from exc
        try:
            with conn.cursor() as cur:
                cur.execute(sql, dict(params or {}))
                cols = [d.name for d in cur.description or ()]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.rollback()
            return rows
        except Exception as exc:  # noqa: BLE001 - any failure makes the item unanswerable
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                conn.close()
            raise TruthError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
        finally:
            if not conn.closed:
                _idle.put(conn)


def jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def scrub_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a child gets: no credential-shaped variable."""
    marks = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "DSN")
    return {k: v for k, v in environ.items() if not any(m in k.upper() for m in marks)}
