"""Declared SQLite schemas: every store's tables, indexes and triggers live here, as
versioned steps, not inline in the module that opens the store.

One module per store (``operating/declared_schema/<store>.py``) holds ``STEPS``: an
ordered tuple of ``(version, name, ddl)``, versions 1, 2, ... with no gap. ``ddl`` is a
script (``str``) or a tuple of scripts or single statements. Version 1 is the store's DDL as it was
when it was declared; every statement is idempotent (``IF NOT EXISTS``), so a file made
before the declaration is adopted as it is: its tables are not touched.

* :func:`apply` brings a file to the latest step: it runs the steps above the file's
  ``PRAGMA user_version`` and records the new version. A file already at the latest
  version is opened without a statement or a write. A file made before the declaration
  (version 0) runs version 1, a no-op on its tables, and is stamped; a stamp that cannot
  be written (read-only file, another writer holding the lock) is skipped and tried at
  the next open. A file at a *higher* version than the code knows is refused.
* A store that gates its own open on the objects it already holds (``cases``,
  ``source_mirror``) or keeps its own ``user_version`` (``cases``, ``playbook_reports``)
  imports the DDL constants from its module and keeps its own logic.

Later schema changes are a new ``(version, name, ddl)`` step, never an edit of an earlier
one. ``tests/test_declared_schema.py`` pins every store's schema dump.
"""
from __future__ import annotations

import sqlite3
import zlib
from typing import Any, Sequence, Tuple, Union

Ddl = Union[str, Tuple[str, ...]]
Step = Tuple[int, str, Ddl]


class DeclaredSchemaError(RuntimeError):
    pass


def _check(steps: Sequence[Step]) -> None:
    if not steps or [s[0] for s in steps] != list(range(1, len(steps) + 1)):
        raise DeclaredSchemaError("declared_steps_not_contiguous")


def _statements(ddl: Ddl) -> Tuple[str, ...]:
    return (ddl,) if isinstance(ddl, str) else tuple(ddl)


def _tag(store: str) -> int:
    return (zlib.crc32(store.encode()) & 0x7FFF) << 16


def apply(conn: sqlite3.Connection, declared: Any) -> int:
    """Bring ``conn``'s file to the latest step of ``declared`` (a module with ``STORE``
    and ``STEPS``); return that version.

    The recorded version carries a tag of the store's name, so two stores that share one
    file (a form store and a confirmation store) never take each other's version for
    their own: a file whose tag is another store's counts as version 0 and runs the
    (idempotent) steps; the stamp then names this store."""
    steps, tag = declared.STEPS, _tag(declared.STORE)
    _check(steps)
    latest = steps[-1][0]
    raw = conn.execute("PRAGMA user_version").fetchone()[0]
    current = raw & 0xFFFF if raw & ~0xFFFF == tag else 0
    if raw & ~0xFFFF == tag and current > latest:
        raise DeclaredSchemaError("store_schema_newer_than_code")
    if current == latest:
        return latest
    for version, _name, ddl in steps:
        if version <= current:
            continue
        for statement in _statements(ddl):
            conn.executescript(statement)
    try:
        conn.execute(f"PRAGMA user_version={tag | latest}")
    except sqlite3.OperationalError:
        pass    # read-only file or a lock held elsewhere: stamped at a later open
    return latest
