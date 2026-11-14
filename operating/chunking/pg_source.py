"""The served ontology in Postgres as the projection the chunkers read.

``ProjectionChunker`` (and the record and permission readers beside it) read a projection
store through a handful of SQLite queries on ``text_fields``, ``text_chunks``,
``objects``, ``object_versions``, ``object_values``, ``identifiers`` and ``link_rows``.
Postgres holds the same tables with the same columns in the ``ontology`` schema (the
``PLX_ONTOLOGY_PG=only`` store once the SQLite generation is retired), so
:class:`PgProjection` answers those queries there: each statement is translated by the
read tools' own translator (:func:`translate`, ``operating.pg.sqlite_compat``: the same
answers as SQLite, ``julianday`` included), run read-only on a server-side cursor (rows streamed, never
the whole table in memory) and its rows read by position or by name as ``sqlite3.Row``.
A statement it does not know how to rewrite is refused (``unsupported_statement``), never
run half-translated."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterator, Sequence

PG_PROJECTION = "postgres"   # the projection argument that names this store

# What may not survive translation: SQLite-only syntax sent to Postgres is refused.
_LEFT_OVER = re.compile(r"json_each|\bjulianday\s*\(|\bGLOB\b|\bPRAGMA\b|\?",
                        re.IGNORECASE)
_PLACEHOLDER = re.compile(r"%s(::text\[\])?")


class UnsupportedStatement(ValueError):
    def __init__(self) -> None:
        super().__init__("unsupported_statement")


def translate(sql: str) -> str:
    """One chunker statement in Postgres' dialect, by the read tools' own translator
    (``operating.pg.sqlite_compat.translate``: ``json_each`` lists as ``unnest`` of a
    text array, ``julianday`` as ``ontology.sq_julianday`` with SQLite's answers, index
    hints dropped, ``%s`` parameters), so a predicate means the same on both stores. A
    statement it cannot translate, or that still holds SQLite-only syntax, is refused."""
    from operating.pg.sqlite_compat import Untranslatable, translate as compat
    if re.match(r"(?is)^\s*(PRAGMA|EXPLAIN)\b", sql):   # never answered empty here
        raise UnsupportedStatement()
    try:
        out = compat(sql, arrays=True)
    except Untranslatable:
        raise UnsupportedStatement() from None
    if _LEFT_OVER.search(out.replace("%%", "")):
        raise UnsupportedStatement()
    return out


def bind(statement: str, params: Sequence[Any]) -> tuple[Any, ...]:
    """The parameters for ``statement``: a JSON list of strings bound where the
    translation reads a text array (``%s::text[]``), each other value as given."""
    slots = _PLACEHOLDER.findall(statement)
    if len(slots) != len(params):
        raise UnsupportedStatement()
    out = []
    for array, value in zip(slots, params):
        if array:
            items = json.loads(value) if isinstance(value, str) else value
            if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
                raise UnsupportedStatement()
            value = items
        out.append(value)
    return tuple(out)


class _Row(tuple):
    """A row by position (``row[0]``) and by column name (``row["object_id"]``)."""

    names: dict[str, int] = {}

    def __getitem__(self, key: Any) -> Any:   # type: ignore[override]
        if isinstance(key, str):
            return tuple.__getitem__(self, self.names[key])
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self.names)


class _Rows:
    """The rows of one statement, streamed from a named (server-side) cursor."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        names = [c.name for c in cursor.description or ()]
        self._row = type("Row", (_Row,), {"names": {n: i for i, n in enumerate(names)}})

    def __iter__(self) -> Iterator[_Row]:
        try:
            for raw in self._cursor:
                yield self._row(raw)
        finally:
            self._cursor.close()

    def fetchall(self) -> list[_Row]:
        return list(self)

    def fetchone(self) -> _Row | None:
        try:
            raw = self._cursor.fetchone()
            return None if raw is None else self._row(raw)
        finally:
            self._cursor.close()


class PgProjection:
    """A read-only Postgres connection to the ontology that the chunkers read as their
    projection (``execute``/``close``, as the SQLite connection they open)."""

    def __init__(self, dsn: str, *, connect: Any = None, itersize: int = 20_000) -> None:
        if connect is None:
            import psycopg
            connect = psycopg.connect
        self.conn = connect(dsn)
        self.conn.read_only = True
        with self.conn.cursor() as cur:
            cur.execute("SET search_path = ontology, public")
        self.itersize = itersize
        self._n = 0
        self._lock = threading.Lock()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Rows:
        statement = translate(sql)
        with self._lock:
            self._n += 1
            name = f"chunk_{self._n}"
        cursor = self.conn.cursor(name=name)
        cursor.itersize = self.itersize
        cursor.execute(statement, bind(statement, params))
        return _Rows(cursor)

    def close(self) -> None:
        self.conn.close()


def open_projection(projection: Any, dsn: str | None) -> Any:
    """The projection a chunking pass reads: the SQLite store at ``projection``, or the
    Postgres ontology when it is :data:`PG_PROJECTION` (``dsn`` required)."""
    if str(projection) == PG_PROJECTION:
        if not dsn:
            raise ValueError("pg_dsn_missing")
        return PgProjection(dsn)
    from .projection import open_ro
    return open_ro(projection)


__all__ = ["PG_PROJECTION", "PgProjection", "UnsupportedStatement", "bind",
           "open_projection", "translate"]
