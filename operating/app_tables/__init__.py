"""Typed rows of the Workbench's small app tables, and keyed reads of them.

``rows.py`` is generated (``python3 tools/gen_models.py``) from the declared SQLite
stores (``operating/declared_schema``) that hold approvals and write intents
(``action_path``, ``confirmation_forms``), sessions (``session_journal``,
``session_replay``) and work items (``work_log``, ``autopilot_queue``): one frozen
dataclass per table, a typed field per column, and a :class:`Table` per table.

:class:`Table` builds only keyed reads (``select``/``first``/``exists``): equality on named
columns, an order, a limit. Column names are checked against the row type and quoted;
every value is a bound parameter. A row also answers ``row["column"]`` and ``keys()``, so
it replaces a ``sqlite3.Row`` wherever the code reads columns by name. Writes, the
append-only hash chains and anything with JSON paths or joins stay as written SQL: they
are the stores' own invariants (docs/data-model.md).
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import sqlite3
from typing import Any, ClassVar, Generic, Mapping, Optional, Sequence, TypeVar

R = TypeVar("R", bound="AppRow")


@dataclass(frozen=True)
class AppRow:
    """One row; ``TABLE``/``STORE`` name where it lives."""
    TABLE: ClassVar[str] = ""
    STORE: ClassVar[str] = ""

    def __getitem__(self, key: str) -> Any:
        if not isinstance(key, str) or key not in self.columns():
            raise KeyError(key)
        return getattr(self, key)

    def keys(self) -> list[str]:
        return list(self.columns())

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        cached = cls.__dict__.get("_columns")
        if cached is None:
            cached = tuple(f.name for f in fields(cls))
            setattr(cls, "_columns", cached)
        return cached


class Table(Generic[R]):
    def __init__(self, row: type[R]) -> None:
        self.row = row
        self.name = row.TABLE

    def _column(self, name: str) -> str:
        if name not in self.row.columns():
            raise KeyError(f"{self.name} has no column {name!r}")
        return '"' + name + '"'

    def _where(self, where: Mapping[str, Any]) -> tuple[str, list[Any]]:
        if not where:
            return "", []
        parts, params = [], []
        for name, value in where.items():
            if value is None:
                parts.append(f"{self._column(name)} IS NULL")
            else:
                parts.append(f"{self._column(name)} = ?")
                params.append(value)
        return " WHERE " + " AND ".join(parts), params

    def select(self, conn: sqlite3.Connection, where: Mapping[str, Any] | None = None, *,
               order_by: Sequence[str] = (), descending: bool = False,
               limit: Optional[int] = None) -> list[R]:
        """Rows whose named columns equal the given values, in ``order_by`` order."""
        columns = ", ".join(self._column(c) for c in self.row.columns())
        clause, params = self._where(where or {})
        sql = f'SELECT {columns} FROM "{self.name}"{clause}'
        if order_by:
            sql += " ORDER BY " + ", ".join(
                self._column(c) + (" DESC" if descending else "") for c in order_by)
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise ValueError("limit must be a non-negative integer")
            sql += f" LIMIT {limit}"
        cursor = conn.execute(sql, params)
        return [self.row(*tuple(r)) for r in cursor.fetchall()]

    def first(self, conn: sqlite3.Connection, where: Mapping[str, Any] | None = None, *,
              order_by: Sequence[str] = (), descending: bool = False) -> Optional[R]:
        found = self.select(conn, where, order_by=order_by, descending=descending, limit=1)
        return found[0] if found else None

    def exists(self, conn: sqlite3.Connection, where: Mapping[str, Any]) -> bool:
        clause, params = self._where(where)
        return conn.execute(f'SELECT 1 FROM "{self.name}"{clause} LIMIT 1',
                            params).fetchone() is not None
