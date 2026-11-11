"""The step input guarded (``step_migrate.guard_inputs``) without materializing the
rows it moves out of sight.

``guard_inputs`` deletes the undeclared objects' values and links through the
``object_values`` and ``links`` views. A DELETE on a view with an INSTEAD OF trigger makes
SQLite first copy every matching view row, all columns (value texts, decoded link
texts), into a temp table, then fire the trigger row by row. Measured 11-10: on the 1%
sample (s01m) the guard held a 2 GB deleted temp (input 1.2 GB) and took 110 s of the
165 s step build; at full scale the build's guard reached a 97 GB temp beside the 130 GB
input and the disk guard stopped the build. Its ``INSERT INTO held_*`` statements
also read every such row (a DISTINCT sort) although, without the tripwire,
``ontology.held_keys`` makes each held table ignore every row inserted.

:func:`guard_inputs` makes the same store, statement for statement where it matters:

* the values: the (version, field) of each view row the DELETE would have given its
  trigger, in the view's scan order, are listed (two integers a row) and the trigger's
  own statements are run for them in that order (the cells, then each touched version's
  empty sources), so the word index sees the same deletes in the same order;
* the links: ``DELETE FROM link_rows`` of the same rows (the view's trigger deletes each
  by its key, nothing else);
* the held keys' inserts are not run when each held table ignores every row (the
  ``held_keys`` triggers; no tripwire): they would insert nothing.

It runs ``step_migrate.guard_inputs`` itself when that code (:data:`GUARD_DIGEST`) or the
store's delete triggers (:data:`TRIGGER_DIGEST`) are not the ones followed, with the
tripwire, or when any value is a packed record (the trigger unpacks those first).

Why a module of its own: ``ontology.step_migrate`` and ``ontology.store_ddl`` are
derivation code (the fields key) and step code; nothing either covers imports this one.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import sqlite3
from typing import Iterable, Sequence

from ontology import step_migrate as sm

# sha256 of ``inspect.getsource(step_migrate.guard_inputs)`` this module follows.
GUARD_DIGEST = "274d5227ec994db7ea888c659418ecf8851f32920532e3af0e259f5a0b08557f"
# Rows a statement (a transaction) of the guard's deletes: bounds the in-memory journal.
CHUNK = 100_000
# sha256 of the SQL of the input's ``object_values_delete`` and ``links_delete`` triggers.
TRIGGER_DIGEST = "d8000f928c99c1d903ae72af33915ec914ddd57d8033c4ee2709623f9110d4b8"

def source_digest() -> str:
    return hashlib.sha256(inspect.getsource(sm.guard_inputs).encode()).hexdigest()


def trigger_digest(conn: sqlite3.Connection) -> str:
    sql = [conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                        (name,)).fetchone() for name in ("object_values_delete", "links_delete")]
    return hashlib.sha256(repr(sql).encode()).hexdigest()


def _ignored(conn: sqlite3.Connection) -> bool:
    """Whether every held table ignores every row inserted (``held_keys.held_tables``)."""
    tables = sorted({t for _v, t, _c, _k in sm.HELD})
    for table in tables:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                           (f"{table}_unread",)).fetchone()
        if row is None or not re.fullmatch(
                rf"CREATE TRIGGER {table}_unread BEFORE INSERT ON {table} BEGIN "
                r"SELECT RAISE\(IGNORE\); END", row[0]):
            return False
    return True


def follows(conn: sqlite3.Connection, tripwire: bool | None) -> bool:
    return (not sm._tripwire(tripwire) and source_digest() == GUARD_DIGEST
            and trigger_digest(conn) == TRIGGER_DIGEST
            and conn.execute("SELECT 1 FROM value_cells WHERE field IS NULL LIMIT 1"
                             ).fetchone() is None)


def _delete_values(conn: sqlite3.Connection, other: str) -> int:
    """``DELETE FROM object_values WHERE object_id IN (other)`` as its trigger runs it, no
    view row copied: each row's (version, field) listed in scan order, then the trigger's
    statements for each (no packed record: nothing to unpack)."""
    conn.execute("DROP TABLE IF EXISTS temp.guard_gone")
    conn.execute("CREATE TEMP TABLE guard_gone (ver INTEGER, field INTEGER)")
    # the view's own join (store_ddl._VALUE_JOIN) and filter: its rows in its order
    conn.execute("INSERT INTO temp.guard_gone (ver, field) SELECT v.rowid, f.field "
                 "FROM value_cells c JOIN value_sources s ON s.src = c.src "
                 "JOIN object_versions v ON v.rowid = s.ver "
                 "JOIN value_layout_fields lf ON lf.layout = c.layout "
                 "JOIN value_fields f ON f.field = lf.field JOIN value_kinds k ON k.kind = lf.kind "
                 f"WHERE v.object_id IN ({other})")
    # the view trigger's own deletes for each listed row, in order, by a temp trigger;
    # CHUNK rows a statement (each its own transaction): the builder's rollback journal
    # is in memory (journal_mode MEMORY) and holds every page a transaction changes, so
    # one statement over all rows held most of the input's value pages (build 7: 29 GB,
    # OOM-killed 11-10 9:50 AM ET; s01m: +310 MB on a 755 MB input)
    conn.execute("DROP VIEW IF EXISTS temp.guard_do")
    conn.execute("CREATE TEMP VIEW guard_do AS SELECT NULL AS ver, NULL AS field")
    conn.execute("CREATE TEMP TRIGGER guard_do_delete INSTEAD OF INSERT ON guard_do BEGIN "
                 "DELETE FROM value_cells WHERE rowid IN (SELECT c.rowid FROM "
                 "value_cells c WHERE c.field = NEW.field AND c.src IN (SELECT s.src "
                 "FROM value_sources s WHERE s.ver = NEW.ver)); "
                 "DELETE FROM value_sources WHERE ver IS NEW.ver AND NOT EXISTS "
                 "(SELECT 1 FROM value_cells c WHERE c.src = value_sources.src); "
                 "END")
    (last,) = conn.execute("SELECT coalesce(max(rowid), 0) FROM temp.guard_gone").fetchone()
    count = 0
    for low in range(0, last, CHUNK):
        count += conn.execute("INSERT INTO temp.guard_do (ver, field) SELECT ver, field FROM "
                              "temp.guard_gone WHERE rowid > ? AND rowid <= ? ORDER BY rowid",
                              (low, low + CHUNK)).rowcount
    conn.execute("DROP VIEW temp.guard_do")
    conn.execute("DROP TABLE temp.guard_gone")
    return count


def guard_inputs(conn: sqlite3.Connection, types: Sequence[str] | None,
                 step_prefixes: Iterable[str] = (), *, tripwire: bool | None = None) -> str:
    """``step_migrate.guard_inputs`` (module docstring); returns which ran."""
    if types is None or not follows(conn, tripwire):
        sm.guard_inputs(conn, types, step_prefixes, tripwire=tripwire)
        return "step_migrate"
    allowed = ", ".join("'" + t.replace("'", "''") + "'" for t in sorted(types)) or "NULL"
    conn.commit()
    sm._held_tables(conn)
    if not _ignored(conn):
        conn.rollback()
        sm.guard_inputs(conn, types, step_prefixes, tripwire=tripwire)
        return "step_migrate"
    other = f"SELECT object_id FROM objects WHERE object_type NOT IN ({allowed})"
    _delete_values(conn, other)
    # the undeclared objects listed once (an IN subquery is made again by each statement)
    conn.execute("DROP TABLE IF EXISTS temp.guard_other")
    conn.execute("CREATE TEMP TABLE guard_other (object_id TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute(f"INSERT INTO temp.guard_other {other}")
    held = "SELECT object_id FROM temp.guard_other"
    for table, where in (("identifiers", f"object_id IN ({held})"),
                         ("link_rows", f"from_object IN ({held}) AND to_object IN ({held})")):
        (last,) = conn.execute(f"SELECT coalesce(max(rowid), 0) FROM {table}").fetchone()
        for low in range(0, last, CHUNK):   # by row id ranges, a transaction each
            conn.execute(f"DELETE FROM {table} WHERE rowid > ? AND rowid <= ? AND {where}",
                         (low, low + CHUNK))
    conn.execute("DROP TABLE temp.guard_other")
    for prefix in step_prefixes:
        conn.execute("DELETE FROM held_links WHERE link_id >= ? AND link_id < ?",
                     (prefix, sm.prefix_end(prefix)))
    conn.execute("PRAGMA legacy_alter_table = ON")   # other tables keep naming ``objects``
    conn.execute("ALTER TABLE objects RENAME TO objects_held")
    conn.execute("CREATE INDEX IF NOT EXISTS objects_held_by_type ON "
                 "objects_held(object_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS objects_held_by_binding ON "
                 "objects_held(binding_id)")
    conn.execute("ALTER TABLE identifiers RENAME TO identifiers_declared")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute(f"CREATE VIEW objects AS SELECT * FROM objects_held WHERE object_type IN "
                 f"({allowed})")
    for view, _table, _cols, _key in sm.HELD:
        if view == "identifiers":
            names = [r[1] for r in conn.execute("PRAGMA table_info(identifiers_declared)")]
            select, triggers = f"SELECT {', '.join(names)} FROM identifiers_declared", []
        else:
            (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND "
                                  "name = ?", (view,)).fetchone()
            select = re.split(r"\bAS\s+(?=SELECT\b)", sql, maxsplit=1, flags=re.I)[1]
            triggers = [r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                (view,))]
            conn.execute(f"DROP VIEW {view}")
        conn.execute(f"CREATE VIEW {view} AS {select}")   # the declared rows alone
        for trigger in triggers:
            conn.execute(trigger)
    conn.commit()
    return "input_guard"
