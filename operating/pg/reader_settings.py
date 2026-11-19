"""A stateless reader's gate state in session settings (``PgCompatConnection.stateless``).

A session that cannot make temporary tables (a hot standby) holds what
``pg_temp.sq_calls`` / ``sq_pending`` / ``sq_gates`` hold elsewhere in session settings,
which ``set_config`` changes in a read-only transaction and on a standby: the names and
values migration 0049's ``ontology.sq_call_s`` / ``sq_item_gate_s`` read. Each value carries
what it is for (fn, chr(31), arg or key), so a name collision reads as a miss, never as
another call's answer. A cleared setting is ''. Also the shared principal-key sets such a
reader reads in place of its own (``shared_sets``, migration 0050). Stdlib; the cursor is
psycopg's."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, Callable, Mapping

SEP = "\x1f"
# where a standby's reader asks the primary to keep a set (a libpq DSN to the primary)
KEYSET_DSN_ENV = "PLX_PG_KEYSET_DSN"
SHARED_SETS_READABLE = (
    "SELECT to_regproc('ontology.reader_key_set_put') IS NOT NULL "
    "AND has_table_privilege('ontology.reader_key_set_heads', 'SELECT') "
    "AND has_table_privilege('ontology.reader_key_sets', 'SELECT')")
_asked: set[str] = set()   # digests this process asked to keep (asked once)
_asked_lock = threading.Lock()
log = logging.getLogger(__name__)


def md5(text: str) -> str:
    """Postgres ``md5(text)`` of ``text`` (a UTF-8 database): a setting's name."""
    return hashlib.md5(text.encode("utf-8", "surrogatepass")).hexdigest()


def put(cur: Any, values: Mapping[str, str]) -> None:
    """Session settings ``values`` (name -> value) set in one statement; '' clears."""
    if values:
        cur.execute("SELECT count(set_config(n, v, false)) FROM unnest(%s::text[], "
                    "%s::text[]) AS s(n, v)", (list(values), list(values.values())))


def hold_gate(cur: Any, held: dict[str, list[str]], name: str, keys: tuple[str, ...],
              rosters: list[str] | tuple[str, ...]) -> None:
    """Gate ``name``'s keys and rosters held (``sq_item_gate_s``), replacing any held
    before (as the ``sq_gates`` row's upsert); ``held``: gate -> the names it set. The
    gate's own setting carries its key when it holds just one (a full reader's alone)."""
    values = dict.fromkeys(held.pop(name, ()), "")
    values["plx_sq.g" + md5(name)] = name + SEP + (keys[0] if len(keys) == 1 else "")
    for key in keys:
        values["plx_sq.k" + md5(name + SEP + key)] = name + SEP + key
    for roster in rosters:
        values["plx_sq.r" + md5(name + SEP + roster)] = name + SEP + roster
    put(cur, values)
    held[name] = [n for n, v in values.items() if v]


def answer(conn: Any, functions: Mapping[str, Callable[[Any], Any]],
           answered: dict[str, set[str]]) -> int:
    """The queued calls (``plx_sq.p1..pn``) answered in Python (a failing or unknown gate
    denies), each answer held and the queue cleared in one statement; how many were
    queued. ``answered``: fn -> the answer settings it holds (cleared when it goes)."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('plx_sq.p' || i) FROM generate_series(1, "
                    "coalesce(nullif(current_setting('plx_sq.pn', true), ''), '0')::integer)"
                    " AS i ORDER BY i")
        pending = [tuple(row[0].split(SEP, 1)) for row in cur.fetchall() if SEP in row[0]]
        if not pending:
            return 0
        values = {"plx_sq.pn": "0"}
        values.update((f"plx_sq.p{i}", "") for i in range(1, len(pending) + 1))
        for fn, arg in pending:
            func = functions.get(fn)
            try:
                result = bool(func(arg)) if func else False
            except Exception:  # noqa: BLE001 - a failing gate denies
                result = False
            call = fn + SEP + arg
            name = "plx_sq.a" + md5(call)
            values[name] = ("t" if result else "f") + SEP + call
            values["plx_sq.q" + md5(call)] = ""
            answered.setdefault(fn, set()).add(name)
        put(cur, values)
    return len(pending)


def digest(raw: str) -> str:
    """A list's set name: the SHA-256 of its JSON text (``reader_key_set_put``'s)."""
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def shared_sets(conn: Any, wanted: Mapping[str, int], *, idle: bool,
                primary: bool) -> dict[str, str]:
    """Of ``wanted`` (a list's JSON text -> how many distinct members it holds), the lists
    the shared sets hold whole (their size written, and equal): text -> set name. A list
    not held is asked for once a process, when ``conn`` is outside a transaction: on
    ``conn`` itself on a primary (then read at once), else on the primary named by
    ``PLX_PG_KEYSET_DSN`` (read once replayed); the caller binds it as an array meanwhile.
    The put names a set from the text it reads, so what the reader trusts here is only
    the size check against a set no caller can fill with another list's keys."""
    names = {raw: digest(raw) for raw in wanted}
    held = _held(conn, names, wanted)
    if not idle:
        return held
    with _asked_lock:
        ask = [raw for raw in names if raw not in held and names[raw] not in _asked]
        _asked.update(names[raw] for raw in ask)
    if not ask:
        return held
    if primary:
        try:
            _put(conn, ask)
        except Exception as exc:  # noqa: BLE001 - the array reads the same rows
            log.warning("reader key set not kept: %s", type(exc).__name__)
            return held
        return _held(conn, names, wanted)
    dsn = os.environ.get(KEYSET_DSN_ENV, "")
    if dsn:
        try:
            import psycopg

            with psycopg.connect(dsn, connect_timeout=10) as primary_conn:
                primary_conn.autocommit = True
                _put(primary_conn, ask)
        except Exception as exc:  # noqa: BLE001 - the array reads the same rows
            log.warning("reader key set not kept on the primary: %s", type(exc).__name__)
    return held


def _held(conn: Any, names: Mapping[str, str], wanted: Mapping[str, int]) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT digest, n FROM ontology.reader_key_set_heads "
                    "WHERE digest = ANY(%s::text[])", (list(set(names.values())),))
        sizes = dict(cur.fetchall())
    return {raw: name for raw, name in names.items() if sizes.get(name) == wanted[raw]}


def _put(conn: Any, raws: list[str]) -> None:
    """Each of ``raws`` kept as a shared set, one committed transaction each (``conn`` in
    autocommit, outside a transaction; a read-only login's transaction made read-write)."""
    for raw in raws:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("SELECT ontology.reader_key_set_put(%s)", (raw,))
