"""The step build's held keys, copied only when something reads them.

A step build copies only its declared types' rows and, for every other object, the keys
of its values, identifiers and links (``step_migrate._copy_held_keys`` into the
``held_*`` tables of ``step_migrate.HELD``). Their only reader is the tripwire
``step_migrate.guard_inputs`` lays when ``PLX_STEP_TRIPWIRE=1`` (the step migration tests
run so): a view arm over the held keys that raises when a step's read reaches one.
Without the tripwire (every real build: no launcher, unit or env file sets it) the
guarded views are the declared rows alone and nothing reads a held key; they are copied,
indexed and thrown away with the input.

Measured on the 1% sample (11-09, plan b0d574b6bb445a21): the held keys were 51% of the
step input (4.83M value keys: 480 MB table + 488 MB object index + 94 MB property index,
~220 B a key, of a 2.09 GB input); the served store holds 511M value cells, so at full
scale they are the bulk of the input the lp2 build copied (89-105 GB), and their copy
is a DISTINCT over every undeclared object's served values.

:func:`skipped` (while ``ontology.step_layer_workers`` builds) copies them only when the
tripwire is laid; without it the ``held_*`` tables are made empty, as the copy would
leave them for a store holding no other object, and keep nothing inserted later (the
guard moves the unit layer's undeclared keys into them). The layer is the same row for row
(``ontology.tests.test_held_keys``: no view, trigger or index of the guarded input
names a held table without the tripwire). Nothing the fields key or a plan digest
covers imports this module; :data:`GUARD_DIGEST` pins the ``step_migrate`` code the
claim rests on.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import sqlite3
from typing import Any, Iterator, Sequence

from ontology import step_migrate as sm

# sha256 of the source of step_migrate._copy_held_keys, _held_tables, _tripwire, guard_inputs
GUARD_DIGEST = "c0b31f3339a9c6c1384d8124f36632c87713240f2920764f77632c73b812405b"


def source_digest() -> str:
    return hashlib.sha256("".join(inspect.getsource(f) for f in (
        sm._copy_held_keys, sm._held_tables, sm._tripwire, sm.guard_inputs)
    ).encode()).hexdigest()


def copy_held_keys(cur: Any, conn: sqlite3.Connection, drop_types: Sequence[str],
                   drop_prefixes: Sequence[str], drop_links: Sequence[Sequence[str]]) -> None:
    """``step_migrate._copy_held_keys`` when the tripwire is laid; else the held tables
    made empty (module docstring)."""
    if sm._tripwire(None):
        return _ORIGINAL(cur, conn, drop_types, drop_prefixes, drop_links)
    sm._held_tables(conn)


_ORIGINAL = sm._copy_held_keys
_ORIGINAL_TABLES = sm._held_tables


def held_tables(conn: sqlite3.Connection, indexes: bool = True) -> None:
    """``step_migrate._held_tables``; without the tripwire each held table also drops
    every row inserted into it (``guard_inputs`` moves the unit layer's undeclared keys
    there: on the 1% sample all 4.83M held value keys came that way)."""
    _ORIGINAL_TABLES(conn, indexes)
    if not sm._tripwire(None):
        for table in sorted({t for _v, t, _c, _k in sm.HELD}):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_unread BEFORE INSERT ON "
                         f"{table} BEGIN SELECT RAISE(IGNORE); END")


@contextlib.contextmanager
def skipped() -> Iterator[None]:
    """While open, a step build copies no held key nothing reads (module docstring)."""
    saved = sm._copy_held_keys, sm._held_tables
    sm._copy_held_keys, sm._held_tables = copy_held_keys, held_tables
    try:
        yield
    finally:
        sm._copy_held_keys, sm._held_tables = saved
