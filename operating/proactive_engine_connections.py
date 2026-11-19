"""Store connections for the proactive engine: SQLite or Postgres connections, the per-thread
connection wrapper and the parallel link-row reader ``_Store`` uses."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any, Callable, Mapping, Sequence

from ontology import store as ostore

from .proactive_engine_config import SCAN_THREADS_ENV
from .proactive_engine_budget import statement_timeout_ms


def scan_threads(environ: Mapping[str, str] | None = None) -> int:
    """Threads a link scan reads with: every core (``PLX_PROACTIVE_SCAN_THREADS``
    overrides; 1: one connection, no thread)."""
    raw = (os.environ if environ is None else environ).get(SCAN_THREADS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return max(1, os.cpu_count() or 1)


def _connect(path: Path, file_only: bool = False) -> sqlite3.Connection:
    """A read-only connection to the store at ``path``: Postgres when it serves it, else
    the file. ``file_only``: the file itself, whatever Postgres serves (a scratch store
    of a few records, ``operating.autopilot_fast``)."""
    from operating.pg import ontology_reader
    if not file_only and ontology_reader.pg_only():
        # Postgres is the only store: read it whole or not at all (an error, never an
        # empty run over a store being loaded)
        problem = ontology_reader.whole()
        if problem is not None:
            raise ostore.StoreError("store_not_whole", problem)
    if not file_only and ontology_reader.serves(path):
        # PLX_ONTOLOGY_PG: the Postgres ontology (current to the last delta layer), the
        # detectors' SQL run as written (operating/pg/sqlite_compat.py)
        from operating.pg.sqlite_compat import PgCompatConnection
        return PgCompatConnection(                        # type: ignore[return-value]
            ontology_reader.dsn(), statement_timeout_ms=statement_timeout_ms(),
            link_guard=True)
    # read only, one thread at a time (``_Store.conn`` is per thread); the run's own thread
    # may still set its progress handler and close it
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # No query of a detector may build a transient index over a whole table: on a
    # store of millions of value cells that index is a multi-GB temp file and minutes.
    conn.execute("PRAGMA automatic_index = OFF")
    return conn


def _link_row(row: Sequence[Any]) -> tuple:
    """A link row as kept (``_LinkRows``): its type, tier and markings shared strings."""
    return (row[0], row[1], sys.intern(row[2]), row[3], row[4], sys.intern(row[5]),
            sys.intern(row[6]), bool(row[7]))


class _ThreadConn:
    """A thread's store connection, closed when the thread's locals are freed (the thread
    ended). The finalizer holds the connection and the release callback, never this object.

    ``owner``: the store whose ``_release`` the finalizer calls, held weakly: a strong
    reference (a bound method) from the finalizer registry kept every store whose run
    thread lives on (the main thread's locals) alive with all it read, so each rule group
    of a whole pass kept its populations and links to the end of the process (11-17: the
    whole pass grew to 14.5 GB and was OOM-killed). A store already freed has its
    connection closed here."""

    def __init__(self, conn: Any, release: Callable[[Any], None] | None = None, *,
                 owner: Any = None) -> None:
        import weakref
        self.conn = conn
        if owner is not None:
            weakref.finalize(self, _release_weakly, weakref.ref(owner), conn)
        else:
            weakref.finalize(self, release, conn)


def _release_weakly(owner: Any, conn: Any) -> None:
    store = owner()
    if store is not None:
        store._release(conn)
        return
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - a connection already closed or lost
        pass


class _LinkRows:
    """The run's link rows by link type, read once (``_Store.links``): each row
    (rowid, link_id, link_type, from_object, to_object, tier, markings, gone), ``gone``
    when either end is removed; every current link not contradicted, in rowid order.
    Kept up to ``LINK_CACHE_ROWS`` rows."""

    def __init__(self) -> None:
        self.by_type: dict[str, list[tuple]] = {}
        self.kept = 0
        self.lock = threading.Lock()
        # each kept type, least recently asked first (``PassCache``: a type the asking
        # store never asked for makes room when a new one does not fit)
        self.asked: dict[str, None] = {}


class PassCache:
    """What one pass of rule groups (``proactive_engine.refresh_pass``) reads once and
    shares across its groups, each run on a store of its own (``_Store(shared=)``): the
    link rows by type (``_LinkRows``, the same ``LINK_CACHE_ROWS`` bound as one whole
    run), the records removed (each row's ``gone`` read against them) and the
    test-record population with its digest (``TEST_POPULATION``). One pass reads one
    view: a group never scans again what an earlier one read (the link table scan was
    20-25 min a group on the served store, 2026-11-16). Only a whole-store run uses it
    (never an incremental pass's scope), on the guarded connection of each group's store
    (the person-link guard filters the rows before they are kept). ``prefetch``: the
    first group reads every link type a whole run reads in one scan
    (``_run_link_types``), as a whole run does."""

    def __init__(self, *, prefetch: bool = False) -> None:
        self.link_rows = _LinkRows()
        self.removed_of: dict[str, frozenset[str]] = {}
        self.removed_all: frozenset[str] | None = None
        self.removed_lock = threading.Lock()
        self.populations: dict[str, tuple[Any, str]] = {}   # key -> (made, digest)
        self.prefetch = prefetch
        self.prefetched = False
        self.stamp: str | None = None   # the data the held reads are of (``current``)
        self.bound = False   # a group's store took it (``current``)
        self.epoch = 0   # moves with every ``clear``: a store writes only into its own epoch

    def current(self, stamp: str | None) -> None:
        """Called by each group's store, under that group's pass lock, before it reads:
        ``stamp`` is the data the store reads now (``_Store.data_stamp``). The held reads
        are kept only while the stamp they were read at still stands; when it moved (a
        delta layer applied and the incremental pass folded between two groups) or
        cannot be told (None), every held read is let go (link rows, records removed,
        test records), so a group never folds, as current, what an earlier data version
        held. Every type is dropped, not only the moved ones: the change feed names the
        records a cycle changed, not the link types, and the deleted links are gone from
        the table, so which held types moved is not known without a scan. A later group
        then reads only the types it asks for (no second prefetch)."""
        if self.bound and (stamp is None or stamp != self.stamp):
            self.clear()
            self.prefetched = True
        self.bound = True
        self.stamp = stamp

    def clear(self) -> None:
        """Let go of every held read (a moved stamp, ``current``; the pass's end). The
        held objects are emptied in place (an earlier group's store, alive past its group,
        holds them, so dropping the names would free nothing) and new ones put in their
        place, and ``epoch`` moves: a thread of an earlier group still reading (one given
        up at its budget) fills only the old objects, never what a later group reads."""
        old = self.link_rows
        self.link_rows = _LinkRows()
        with old.lock:
            old.by_type.clear()
            old.asked.clear()
            old.kept = 0
        with self.removed_lock:
            self.removed_of.clear()
            self.removed_of = {}
            self.removed_all = None
            self.populations.clear()
            self.populations = {}
            self.epoch += 1
        self.stamp = None


# the populations a pass's groups share (``PassCache``): key prefixes of ``_Store.population``
TEST_POPULATION = "test_records:"


# ``_Store.values`` on the Postgres ontology: the ``object_values`` view's columns read from
# its tables (a record's versions, their current value sources, the cells), the same rows
_PG_VALUES_SQL = (
    "SELECT o.object_id, o.object_type, f.property, k.value_type, c.value, k.unit, "
    "k.quantity_role, v.l1_handle, k.markings, COALESCE(s.lineage, v.lineage) AS lineage "
    "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
    "JOIN value_sources s ON s.ver = v.rowid AND s.superseded_at IS NULL "
    "JOIN value_cells c ON c.src = s.src JOIN value_kinds k ON k.kind = c.kind "
    "JOIN value_fields f ON f.field = c.field WHERE ")
