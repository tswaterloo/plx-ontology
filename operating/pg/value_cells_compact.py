"""``ontology.value_cells`` rewritten compact beside itself and swapped in (GROWTH-AUDIT F13).

Why this and not a value side table: on the served table (11-14, 0.2 % block sample)
a cell is 51 bytes as a row with a 14-byte value on average; a deduplicated value table
keyed by ``value_id`` saves the value's bytes but adds the id, so a row stays one 8-byte
alignment step from today's (56 -> 48 bytes) while the side table and its hash key cost
about what that saves, and the word index would need a second key on ``value_id``. What
the served table carries beyond its rows is dead space: 39 GB of heap for ~28 GB of live
rows before GA-F3, and F3 deletes about half the rows, which a vacuum frees for reuse but
never gives back to the disk. Its two 16-17 GB keys are as bloated. So the table is
written again, whole and compact, with the same columns, rows and indexes: every reader
reads it as before, through its views, and no writer changes.

How (``convert``, each phase recorded and resumable):

1. **prepare**: the budget gate (:func:`plan`: the projection from :data:`RATES`, measured
   by ``scripts/rehearse_value_cells_compact.py`` at 10 %, refused before any write when
   over :data:`BUDGET_S` or when the new table would leave less than ``reserve`` of the
   disk free); a change log (``value_cells_compact_log``: the source id of every cell
   inserted, updated or deleted from now on, by statement triggers with transition
   tables); the new table ``value_cells_compact`` (same columns, defaults, constraints,
   storage options) with the word index already on it, so its words are cut by the fill's
   parallel workers (one build of it cuts every value's words in one process: 2.6 h of
   ``sq_words`` on the served table); the fill's block ranges.
2. **fill**: ``INSERT ... SELECT`` per block range of the old table, ``workers`` side by
   side, each range committed with its mark (``value_cells_compact_marks``): a stopped
   fill resumes with the ranges not marked. A row moved by a write while the fill runs may
   be copied twice or missed; its source is in the log either way.
3. **catch-up**: the logged sources' cells deleted from the new table and copied again
   from the old one, the log entries read removed (one transaction; again before the
   check and inside the swap).
4. **indexes**: every other index of the old table (its key, the production-only
   ``value_cells_by_field``, the document-name index; whatever the table has then), one
   connection each, side by side, with the old index's statistics targets; the key as a
   unique index made the primary key.
5. **vacuum**: ``VACUUM (ANALYZE)`` of the new table (visibility map for index-only reads,
   the planner's statistics and the word statistics the readers' windows read).
6. **check**: one exported snapshot shared by every worker; both tables read whole in
   block ranges, each row's order-free hash summed per source bucket (``src`` mod
   :data:`BUCKETS`), the sources still in the log left out of both; any bucket that
   differs refuses the swap (:class:`Mismatch`).
7. **swap** (``--swap`` or ``swap``): one transaction under the delta's advisory lock and
   an ``ACCESS EXCLUSIVE`` lock of the old table (``lock_timeout``, retried): the last
   catch-up, the old table and its indexes renamed ``*_precompact``, the new ones given
   the served names, the primary key's name, owner, grants and the views over the table
   (``object_values_typed``, ``value_words``: a view binds its table, not the name) made
   again from their definitions, so every reader reads the new table from its next
   statement. A trigger on the new table logs the sources written after the swap
   (``value_cells_compact_undo``).

**Undo** (:func:`undo`): the old table back under its names with the cells written since
the swap (the logged sources copied from the new table), its views made again; the new
table dropped. **Finish** (:func:`finish`): the check again, new table against the kept
old one (the sources written since the swap left out), and only when every bucket agrees
the old table, logs and triggers are dropped: the disk is given back then.

Stdlib and psycopg. ``python3 -m operating.pg.value_cells_compact
plan|convert|swap|undo|finish|status [--workers 4] [--reserve-gb 60] [--swap]``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating.pg.link_partitions import DELTA_LOCK, OverBudget, Refused, delta_applying, \
    free_bytes

# The schema whose value cells this module rewrites; only a test or a rehearsal sets
# another one. Validated as an identifier.
SCHEMA = os.environ.get("PLX_VALUE_CELLS_SCHEMA", "ontology")
TABLE = "value_cells"
NEW = "value_cells_compact"
OLD = "value_cells_precompact"
LOG = "value_cells_compact_log"
UNDO_LOG = "value_cells_compact_undo"
MARKS = "value_cells_compact_marks"
STATE = "value_cells_compact_state"
LOG_FN = "value_cells_compact_logged"
UNDO_FN = "value_cells_compact_undone"
REFUSE_FN = "value_cells_compact_no_truncate"
OLD_SUFFIX = "_precompact"
NEW_SUFFIX = "_cmp"
COLUMNS = ("src", "field", "kind", "value", "value_num", "currency", "currency_citation")
BUDGET_S = 7200
DISK_RESERVE = 60 << 30
BUCKETS = 4096
RANGE_PAGES = 65536          # a fill range: 512 MB of the old heap at most
# An index built before the fill (its rows written by the fill's workers, side by side):
# the word index, whose expression costs ~21 us a value (``sq_words``), so one build of it
# after the fill would cut 445 M values' words in one process.
DURING_FILL = re.compile(r"\bsq_words\s*\(")
# Estimates (from the 11-14 sample: 60 bytes a live row in a fresh heap, two btree keys
# of ~36 bytes an entry, 21 us a value for the words) until the 10 % rehearsal measures
# them: ``convert`` from the command line refuses without a rehearsal's measured rates
# (``--rates``, :func:`rehearsed_rates`), so these only show ``plan`` a first projection.
# Rows a second one fill worker writes with the word index on the new table, index rows a
# second the side-by-side builds take in all, rows a second the vacuum takes, rows a
# second the check hashes (both tables, all workers), bytes a live row takes in the new
# heap and in all its indexes, the swap's wait.
RATES = {"fill_rows_s_per_worker": 40_000.0, "index_rows_s": 600_000.0,
         "vacuum_rows_s": 1_000_000.0, "check_rows_s": 1_000_000.0,
         "heap_bytes_per_row": 60.0, "index_bytes_per_row": 75.0, "swap_s": 10.0}


REHEARSED_KEYS = tuple(RATES)


def rehearsed_rates(report: Mapping[str, Any]) -> dict[str, float]:
    """The rates a rehearsal report (``scripts/rehearse_value_cells_compact.py run``)
    measured; refused (:class:`Refused`) unless it ran every phase: two conversions with
    a writer, each read the same through the readers' indexes after its swap, the undo
    and the finish both checked clean, every rate a positive number."""
    runs = report.get("runs") or []
    if len(runs) < 2 or (report.get("writer") or {}).get("error"):
        raise Refused("rehearsal_incomplete:runs_or_writer")
    if not all(r.get("readers_same_after_swap") for r in runs):
        raise Refused("rehearsal_readers_differed")
    if not runs[0].get("readers_same_after_undo"):
        raise Refused("rehearsal_undo_differed")
    finish_check = (runs[-1].get("finish") or {}).get("check") or {}
    if finish_check.get("differ") != 0 or not finish_check.get("rows"):
        raise Refused("rehearsal_finish_unchecked")
    if not all((r.get("check") or {}).get("differ") == 0 for r in runs):
        raise Refused("rehearsal_check_unclean")
    rates = report.get("rates") or {}
    out = {}
    for key in REHEARSED_KEYS:
        value = rates.get(key)
        if not isinstance(value, (int, float)) or value != value or value <= 0:
            raise Refused(f"rehearsal_without_rate:{key}")
        out[key] = float(value)
    return out


class Mismatch(RuntimeError):
    """The new table does not hold the old table's rows (the check's buckets differ)."""


def q(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise Refused(f"bad_identifier:{name!r}")
    return name


q(SCHEMA)


def t(name: str) -> str:
    """``schema.name``."""
    return f"{SCHEMA}.{q(name)}"


def renamed(name: str, suffix: str) -> str:
    return q(name[:63 - len(suffix)] + suffix)


# --- what the table is ------------------------------------------------------------------

def exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{SCHEMA}.{name}",))
    return bool(cur.fetchone()[0])


def indexes(cur: Any, table: str) -> list[dict[str, Any]]:
    """Every index of ``table``: name, definition, whether it holds the primary key, and
    the statistics targets of its columns (``{attnum: target}``)."""
    cur.execute(
        "SELECT c.relname, pg_get_indexdef(i.indexrelid), "
        "EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND "
        "k.contype = 'p'), (SELECT coalesce(jsonb_object_agg(a.attnum, a.attstattarget), "
        "'{}') FROM pg_attribute a WHERE a.attrelid = i.indexrelid AND a.attnum > 0 AND "
        "a.attstattarget IS NOT NULL AND a.attstattarget >= 0) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indrelid = %s::regclass ORDER BY c.relname", (t(table),))
    return [{"name": n, "def": d, "pkey": bool(p),
             "stats": {int(k): int(v) for k, v in (s or {}).items()}}
            for n, d, p, s in cur.fetchall()]


def views(cur: Any, table: str) -> list[dict[str, Any]]:
    """The views reading ``table`` directly: name, definition (names schema-qualified),
    options."""
    cur.execute("SET LOCAL search_path = pg_catalog")
    cur.execute(
        "SELECT DISTINCT v.oid::regclass::text, pg_get_viewdef(v.oid), "
        "coalesce(array_to_string(v.reloptions, ', '), '') FROM pg_depend d "
        "JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class "
        "WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = %s::regclass "
        "AND v.oid <> %s::regclass AND v.relkind = 'v' ORDER BY 1", (t(table), t(table)))
    out = [{"name": n, "def": d, "options": o} for n, d, o in cur.fetchall()]
    cur.execute("RESET search_path")
    return out


def _unsupported(cur: Any, table: str) -> list[str]:
    """What this module would not carry over to the new table (fail closed): a trigger
    other than its own, row security, extended statistics, a materialized view, a rule,
    a foreign key, a function body bound to it."""
    cur.execute(
        "SELECT 'trigger:' || tgname FROM pg_trigger WHERE tgrelid = %(t)s::regclass "
        "AND NOT tgisinternal AND tgname NOT LIKE 'value_cells_compact_%%' "
        "UNION ALL SELECT 'row_security' FROM pg_class WHERE oid = %(t)s::regclass "
        "AND relrowsecurity UNION ALL SELECT 'statistics:' || stxname FROM "
        "pg_statistic_ext WHERE stxrelid = %(t)s::regclass UNION ALL SELECT "
        "'dependent:' || d.classid::regclass::text || ':' || d.objid FROM pg_depend d "
        "WHERE d.refobjid = %(t)s::regclass AND d.deptype = 'n' AND d.classid NOT IN "
        "('pg_rewrite'::regclass, 'pg_class'::regclass, 'pg_constraint'::regclass) "
        "UNION ALL SELECT 'materialized:' || v.relname FROM pg_depend d JOIN pg_rewrite r "
        "ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class WHERE "
        "d.classid = 'pg_rewrite'::regclass AND d.refobjid = %(t)s::regclass AND "
        "v.relkind <> 'v' AND v.oid <> %(t)s::regclass UNION ALL SELECT 'foreign_key:' || "
        "conname FROM pg_constraint WHERE (conrelid = %(t)s::regclass OR confrelid = "
        "%(t)s::regclass) AND contype = 'f'", {"t": t(table)})
    return [r[0] for r in cur.fetchall()]


def sizes(cur: Any, table: str) -> dict[str, int]:
    cur.execute("SELECT pg_table_size(%(t)s::regclass), pg_indexes_size(%(t)s::regclass), "
                "pg_relation_size(%(t)s::regclass) / current_setting('block_size')::int, "
                "greatest((SELECT reltuples FROM pg_class WHERE oid = %(t)s::regclass), 0)",
                {"t": t(table)})
    heap, idx, pages, rows = cur.fetchone()
    return {"heap_bytes": int(heap), "index_bytes": int(idx), "pages": int(pages),
            "rows": int(rows)}


# --- the budget -------------------------------------------------------------------------

def project(rows: int, *, workers: int, btrees: int, max_wal: int = 0,
            rates: Mapping[str, float] = RATES) -> dict[str, float]:
    """Projected seconds and disk of compacting a table of ``rows`` live cells with
    ``btrees`` indexes built after the fill. ``bytes``: the new table and its indexes plus
    the WAL they write that a checkpoint has not yet recycled (bounded by twice the
    server's ``max_wal_size``, ``max_wal``: a checkpoint starts at it, and segments wait
    for the next one)."""
    w = max(1, workers)
    fill = rows / (rates["fill_rows_s_per_worker"] * w)
    index = rows * max(1, btrees) / rates["index_rows_s"]
    vacuum = rows / rates["vacuum_rows_s"]
    check = 2 * rows / rates["check_rows_s"]
    total = fill + index + vacuum + check + rates["swap_s"]
    new = int(rows * (rates["heap_bytes_per_row"] + rates["index_bytes_per_row"]))
    wal = min(new, 2 * max(0, int(max_wal)))
    return {"fill_s": round(fill, 1), "index_s": round(index, 1),
            "vacuum_s": round(vacuum, 1), "check_s": round(check, 1),
            "total_s": round(total, 1), "new_bytes": new, "wal_bytes": wal,
            "bytes": new + wal}


def gate(projection: Mapping[str, float], *, budget_s: float, free: int | None,
         reserve: int) -> None:
    """Refuse (:class:`OverBudget`) a projection over the budget or one that would leave
    less than ``reserve`` of the disk free; an unknown free disk or a projection without
    its numbers is refused too (fail closed)."""
    for key in ("total_s", "bytes"):
        value = projection.get(key)
        if not isinstance(value, (int, float)) or value != value or value < 0:
            raise OverBudget(f"projection_without_{key}")
    if projection["total_s"] > budget_s:
        raise OverBudget(f"projected {projection['total_s']:.0f} s > budget {budget_s:.0f} s")
    if free is None:
        raise OverBudget("free_disk_unknown: set PLX_PG_DATA_DIR to a folder on the "
                         "database's file system")
    if free - projection["bytes"] < reserve:
        raise OverBudget(f"projected {projection['bytes'] / 2**30:.1f} GB new would leave "
                         f"{(free - projection['bytes']) / 2**30:.1f} GB free < reserve "
                         f"{reserve / 2**30:.0f} GB")


def plan(cur: Any, *, workers: int = 4, budget_s: float = BUDGET_S,
         reserve: int = DISK_RESERVE, rates: Mapping[str, float] = RATES,
         check: bool = True) -> dict[str, Any]:
    """The table now, the projection, the disk; gated when ``check``."""
    if not exists(cur, TABLE):
        raise Refused("no_value_cells")
    now = sizes(cur, TABLE)
    cur.execute(f"SELECT count(*) FROM {t(TABLE)} WHERE FALSE")   # readable at all
    cur.execute("SELECT n_live_tup FROM pg_stat_user_tables WHERE relid = %s::regclass",
                (t(TABLE),))
    row = cur.fetchone()
    live = int(row[0]) if row and row[0] else now["rows"]
    rows = max(now["rows"], live)
    idx = indexes(cur, TABLE)
    btrees = sum(1 for i in idx if not DURING_FILL.search(i["def"]))
    cur.execute("SELECT pg_size_bytes(current_setting('max_wal_size'))")
    proj = project(rows, workers=workers, btrees=btrees, max_wal=int(cur.fetchone()[0]),
                   rates=rates)
    free = free_bytes(cur)
    out = {"table": now, "rows": rows, "indexes": [i["name"] for i in idx],
           "projection": proj, "free_bytes": free, "reserve_bytes": reserve,
           "budget_s": budget_s, "workers": workers,
           "saving_bytes": now["heap_bytes"] + now["index_bytes"] - proj["new_bytes"]}
    if check:
        gate(proj, budget_s=budget_s, free=free, reserve=reserve)
    return out


# --- sessions ---------------------------------------------------------------------------

@dataclass
class Session:
    """Connections for one job's workers; a statement starts only while no delta apply
    holds its lock (``yield_to_delta``), so an apply never waits on the fill."""
    dsn: str
    workers: int = 4
    maintenance_mem: str = "1GB"
    log: Callable[[str], None] = lambda m: None
    timings: dict[str, float] = field(default_factory=dict)
    yield_to_delta: bool = True
    yield_poll_s: float = 5.0
    yielded_s: float = 0.0
    # tests: called after each committed fill range with its index (raise to stop there)
    after_range: Callable[[int], None] | None = None
    # the most logged sources the swap (or undo) copies while it holds the table locked;
    # more, and it lets go, catches up without the lock and tries again
    locked_catch_up_max: int = 20_000
    lock_timeout_ms: int = 5000
    # tests: the free disk the fill sees (default: the database's file system)
    free: Callable[[Any], int | None] | None = None

    def connect(self, autocommit: bool = True) -> Any:
        import psycopg
        conn = psycopg.connect(self.dsn, autocommit=autocommit)
        conn.execute(f"SET maintenance_work_mem = '{self.maintenance_mem}'")
        conn.execute("SET synchronous_commit = off")
        # the workers are the parallelism (the box's cap): no hidden helpers
        conn.execute("SET max_parallel_maintenance_workers = 0")
        conn.execute("SET max_parallel_workers_per_gather = 0")
        conn.execute("SET statement_timeout = 0")
        if not autocommit:
            conn.commit()
        return conn

    def wait_for_delta(self, conn: Any) -> None:
        if not self.yield_to_delta:
            return
        started = time.monotonic()
        with conn.cursor() as cur:
            while delta_applying(cur):
                time.sleep(self.yield_poll_s)
        self.yielded_s += time.monotonic() - started

    def pool(self, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        """``fn`` over ``items``, ``workers`` at a time; the first failure starts no
        further item (those running finish) and is raised."""
        import threading
        failed = threading.Event()

        def guarded(item: Any) -> Any:
            if failed.is_set():
                return None
            try:
                return fn(item)
            except BaseException:
                failed.set()
                raise

        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            futures = [pool.submit(guarded, item) for item in items]
            return [f.result() for f in futures]

    def timed(self, key: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        out = fn()
        self.timings[key] = round(self.timings.get(key, 0.0) + time.monotonic() - started, 3)
        self.log(f"{key}: {self.timings[key]} s")
        return out


# --- state ------------------------------------------------------------------------------

def _state(cur: Any) -> dict[str, Any]:
    if not exists(cur, STATE):
        return {}
    cur.execute(f"SELECT key, value FROM {t(STATE)}")
    return {k: v for k, v in cur.fetchall()}


def _put(cur: Any, key: str, value: Any) -> None:
    cur.execute(f"INSERT INTO {t(STATE)} (key, value) VALUES (%s, %s::jsonb) ON CONFLICT "
                "(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def status(cur: Any) -> dict[str, Any]:
    state = _state(cur)
    out: dict[str, Any] = {"phase": state.get("phase", "none"),
                           "tables": {n: sizes(cur, n) for n in (TABLE, NEW, OLD)
                                      if exists(cur, n)}}
    if exists(cur, MARKS):
        cur.execute(f"SELECT count(*), coalesce(sum(rows), 0) FROM {t(MARKS)}")
        done, rows = cur.fetchone()
        out["fill"] = {"ranges_done": int(done), "ranges": len(state.get("ranges", [])),
                       "rows": int(rows)}
    for name in (LOG, UNDO_LOG):
        if exists(cur, name):
            cur.execute(f"SELECT count(DISTINCT src) FROM {t(name)}")
            out[name] = int(cur.fetchone()[0])
    for key in ("check", "swap", "plan"):
        if key in state:
            out[key] = state[key]
    return out


# --- the change logs --------------------------------------------------------------------

def _log_fn_sql(fn: str, log: str) -> str:
    """A statement trigger's function logging the source of every cell written (both
    sides of an update). Runs as its owner: any writer of the cells may log."""
    return (f"CREATE OR REPLACE FUNCTION {t(fn)}() RETURNS trigger LANGUAGE plpgsql "
            "SECURITY DEFINER SET search_path = pg_catalog AS $f$ BEGIN "
            f"IF TG_OP <> 'DELETE' THEN INSERT INTO {t(log)} (src) SELECT DISTINCT src FROM "
            "changed_new; END IF; "
            f"IF TG_OP <> 'INSERT' THEN INSERT INTO {t(log)} (src) SELECT DISTINCT src FROM "
            "changed_old; END IF; RETURN NULL; END $f$")


def _watch(cur: Any, table: str, fn: str, prefix: str) -> None:
    # a TRUNCATE logs nothing: refused while the log is needed (fail closed)
    cur.execute(f"CREATE OR REPLACE FUNCTION {t(REFUSE_FN)}() RETURNS trigger LANGUAGE "
                "plpgsql AS $f$ BEGIN RAISE EXCEPTION 'value_cells is being compacted: "
                "TRUNCATE refused (operating.pg.value_cells_compact undo or finish first)'; "
                "END $f$")
    name = q(f"{prefix}_truncate")
    cur.execute(f"DROP TRIGGER IF EXISTS {name} ON {t(table)}")
    cur.execute(f"CREATE TRIGGER {name} BEFORE TRUNCATE ON {t(table)} FOR EACH STATEMENT "
                f"EXECUTE FUNCTION {t(REFUSE_FN)}()")
    for op, refs in (("INSERT", "NEW TABLE AS changed_new"),
                     ("UPDATE", "OLD TABLE AS changed_old NEW TABLE AS changed_new"),
                     ("DELETE", "OLD TABLE AS changed_old")):
        name = q(f"{prefix}_{op.lower()}")
        cur.execute(f"DROP TRIGGER IF EXISTS {name} ON {t(table)}")
        cur.execute(f"CREATE TRIGGER {name} AFTER {op} ON {t(table)} REFERENCING {refs} "
                    f"FOR EACH STATEMENT EXECUTE FUNCTION {t(fn)}()")


def _unwatch(cur: Any, table: str, prefix: str) -> None:
    for op in ("insert", "update", "delete", "truncate"):
        cur.execute(f"DROP TRIGGER IF EXISTS {q(prefix + '_' + op)} ON {t(table)}")


def _log_table(cur: Any, name: str) -> None:
    cur.execute(f"CREATE TABLE IF NOT EXISTS {t(name)} (id bigserial PRIMARY KEY, "
                "src bigint NOT NULL)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS {renamed(name, '_src')} ON {t(name)} (src)")


def _owner_and_grants(cur: Any, source: str, target: str) -> None:
    """``target`` owned by ``source``'s owner with ``source``'s grants."""
    cur.execute("SELECT relowner::regrole::text, coalesce(relacl::text[], '{}') FROM "
                "pg_class WHERE oid = %s::regclass", (t(source),))
    owner, acl = cur.fetchone()
    cur.execute(f"ALTER TABLE {t(target)} OWNER TO {owner}")
    cur.execute(f"REVOKE ALL ON {t(target)} FROM PUBLIC")
    cur.execute("SELECT a.grantee::regrole::text, a.privilege_type, a.is_grantable FROM "
                "pg_class c, aclexplode(c.relacl) a WHERE c.oid = %s::regclass AND "
                "a.grantee <> 0", (t(source),))
    for grantee, privilege, grantable in cur.fetchall():
        cur.execute(f"GRANT {privilege} ON {t(target)} TO {grantee}"
                    + (" WITH GRANT OPTION" if grantable else ""))
    cur.execute("SELECT a.privilege_type FROM pg_class c, aclexplode(c.relacl) a WHERE "
                "c.oid = %s::regclass AND a.grantee = 0", (t(source),))
    for (privilege,) in cur.fetchall():
        cur.execute(f"GRANT {privilege} ON {t(target)} TO PUBLIC")
    del acl


def _on_new(definition: str, name: str, target: str) -> str:
    """An index definition of the served table made for ``target`` under ``name``."""
    head = re.match(r"^CREATE (UNIQUE )?INDEX (\S+) ON (?:ONLY )?(\S+) ", definition)
    if head is None:
        raise Refused(f"index_definition_not_understood:{definition[:80]}")
    return (f"CREATE {head.group(1) or ''}INDEX IF NOT EXISTS {q(name)} ON {t(target)} "
            + definition[head.end():])


# --- the phases -------------------------------------------------------------------------

def f3_unfinished(cur: Any) -> str | None:
    """Why GA-F3's history collapse (``operating.pg.f3_collapse``) is not finished on this
    database, None when it is or was never started here. Its record: the planned batches
    (``ontology.f3_batches``) and one ``load_state`` mark ``f3:<phase>:<key>:<n>`` per
    batch done; sightings loaded (stage A) with no batch planned is unfinished too. F3
    deletes about half the cells: compacted before it ends, the table is sized for rows
    F3 then frees again, inside the new table."""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL, to_regclass(%s) IS NOT NULL",
                (f"{SCHEMA}.f3_batches", f"{SCHEMA}.f3_sightings"))
    batches, sightings = cur.fetchone()
    if not batches:
        return "f3_planned_no_batches: sightings loaded, no batch run" if sightings else None
    cur.execute(f"SELECT b.phase, count(*) FROM {SCHEMA}.f3_batches b WHERE NOT EXISTS "
                f"(SELECT 1 FROM {SCHEMA}.load_state m WHERE m.name = 'f3:' || b.phase || "
                "':' || b.key || ':' || b.n) GROUP BY 1 ORDER BY 1")
    left = cur.fetchall()
    cur.execute(f"SELECT count(*) FROM {SCHEMA}.f3_batches")
    if not cur.fetchone()[0]:
        return "f3_no_batches_planned"
    if left:
        return "f3_unfinished: " + ", ".join(f"{p} {n} batches left" for p, n in left)
    return None


def prepare(s: Session, *, budget_s: float = BUDGET_S, reserve: int = DISK_RESERVE,
            rates: Mapping[str, float] = RATES, without_f3: bool = False) -> dict[str, Any]:
    import psycopg
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase"):
            return {"prepared": "already", "phase": state["phase"]}
        if exists(cur, OLD) or exists(cur, NEW):
            raise Refused("leftover_tables: run undo or finish first")
        bad = _unsupported(cur, TABLE)
        if bad:
            raise Refused("not_carried_over:" + ",".join(bad))
        why = f3_unfinished(cur)
        if why and not without_f3:
            raise Refused(why + " (--without-f3-finished overrides)")
        planned = plan(cur, workers=s.workers, budget_s=budget_s, reserve=reserve,
                       rates=rates)
    for attempt in range(1, 61):
        try:
            return _prepare_txn(s, planned, reserve)
        except psycopg.errors.LockNotAvailable:
            # a writer holds the table (a delta apply, F3): nothing was made; again
            s.log(f"prepare: table busy (attempt {attempt}), retrying")
            time.sleep(min(10, attempt))
    raise Refused("prepare_lock_not_acquired")


def _prepare_txn(s: Session, planned: Mapping[str, Any], reserve: int) -> dict[str, Any]:
    with s.connect(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = {int(s.lock_timeout_ms)}")
        cur.execute(f"CREATE TABLE {t(STATE)} (key text PRIMARY KEY, value jsonb NOT NULL)")
        cur.execute(f"CREATE TABLE {t(MARKS)} (lo bigint PRIMARY KEY, hi bigint, rows bigint "
                    "NOT NULL, done_at timestamptz NOT NULL DEFAULT now())")
        _log_table(cur, LOG)
        cur.execute(_log_fn_sql(LOG_FN, LOG))
        cur.execute(_log_fn_sql(UNDO_FN, UNDO_LOG))
        # the log first: every write from here on is caught up, whatever the fill reads
        cur.execute(f"LOCK TABLE {t(TABLE)} IN SHARE ROW EXCLUSIVE MODE")
        _watch(cur, TABLE, LOG_FN, "value_cells_compact_log")
        cur.execute("SELECT pg_relation_size(%s::regclass) / current_setting('block_size')::int",
                    (t(TABLE),))
        pages = int(cur.fetchone()[0])
        cur.execute(f"CREATE TABLE {t(NEW)} (LIKE {t(TABLE)} INCLUDING DEFAULTS INCLUDING "
                    "CONSTRAINTS INCLUDING STORAGE INCLUDING COMPRESSION)")
        cur.execute("SELECT coalesce(array_to_string(reloptions, ', '), '') FROM pg_class "
                    "WHERE oid = %s::regclass", (t(TABLE),))
        options = cur.fetchone()[0]
        if options:
            cur.execute(f"ALTER TABLE {t(NEW)} SET ({options})")
        idx = indexes(cur, TABLE)
        for i in idx:
            if DURING_FILL.search(i["def"]):
                cur.execute(_on_new(i["def"], renamed(i["name"], NEW_SUFFIX), NEW))
                # its pending list merged in large batches while the workers fill
                cur.execute(f"ALTER INDEX {t(renamed(i['name'], NEW_SUFFIX))} SET "
                            "(gin_pending_list_limit = 65536)")
        step = RANGE_PAGES
        parts = max(s.workers * 4, 16, -(-pages // step))
        step = max(1, -(-pages // parts))
        ranges = [[lo, lo + step] for lo in range(0, max(pages, 1), step)]
        ranges[-1][1] = None   # open: rows written past the end are read too
        _put(cur, "plan", {k: planned[k] for k in ("table", "rows", "projection",
                                                   "free_bytes", "saving_bytes")})
        _put(cur, "indexes", idx)
        _put(cur, "ranges", ranges)
        _put(cur, "built", [])
        _put(cur, "reserve", reserve)
        _put(cur, "phase", "prepared")
        conn.commit()
    return {"prepared": True, "pages": pages, "ranges": len(ranges), **planned}


def _cols(alias: str = "") -> str:
    return ", ".join(f"{alias}{c}" for c in COLUMNS)


def _ctid(lo: int, hi: int | None) -> str:
    where = f"ctid >= '({int(lo)},0)'::tid"
    return where if hi is None else f"{where} AND ctid < '({int(hi)},0)'::tid"


class DiskLow(Refused):
    """The disk fell under the reserve while the job ran: stopped between steps; the
    marks kept, a later run resumes."""


def _disk_left(s: Session, cur: Any, reserve: int) -> None:
    free = s.free(cur) if s.free is not None else free_bytes(cur)
    if free is None or free < reserve:
        raise DiskLow(f"disk_below_reserve: {None if free is None else free >> 30} GB free "
                      f"< {reserve >> 30} GB; stopped resumable (run convert again)")


def fill(s: Session) -> dict[str, Any]:
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase") != "prepared":
            return {"fill": "skipped", "phase": state.get("phase")}
        ranges = state["ranges"]
        cur.execute(f"SELECT lo FROM {t(MARKS)}")
        done = {r[0] for r in cur.fetchall()}
    todo = [(i, lo, hi) for i, (lo, hi) in enumerate(ranges) if lo not in done]
    reserve = int(state.get("reserve", DISK_RESERVE))

    def one(item: tuple[int, int, int | None]) -> int:
        i, lo, hi = item
        with s.connect(autocommit=False) as conn, conn.cursor() as cur:
            _disk_left(s, cur, reserve)
            s.wait_for_delta(conn)
            cur.execute(f"INSERT INTO {t(NEW)} ({_cols()}) SELECT {_cols()} FROM {t(TABLE)} "
                        f"WHERE {_ctid(lo, hi)}")
            n = cur.rowcount
            cur.execute(f"INSERT INTO {t(MARKS)} (lo, hi, rows) VALUES (%s, %s, %s)",
                        (lo, hi, n))
            conn.commit()
        if s.after_range is not None:
            s.after_range(i)
        return n

    rows = sum(s.pool(one, todo))
    with s.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {t(MARKS)}")
        if int(cur.fetchone()[0]) != len(ranges):
            raise Refused("fill_incomplete")
        _put(cur, "phase", "filled")
    return {"filled_rows": rows, "ranges_resumed": len(done), "ranges": len(ranges)}


def catch_up(cur: Any, source: str, target: str, log: str) -> int:
    """The sources logged so far: their cells in ``target`` made ``source``'s again; then
    exactly the log entries read are removed. In the caller's transaction.

    Exactly those: a writer still in flight when the log is read has its entry (an id
    below newer ones) committed later, and that entry must stay for the next catch-up
    (deleting by "id <= the highest read" dropped it: the reviewer's race)."""
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS compact_ids (id bigint PRIMARY KEY, "
                "src bigint NOT NULL) ON COMMIT DROP")
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS compact_srcs (src bigint PRIMARY KEY) "
                "ON COMMIT DROP")
    cur.execute("TRUNCATE compact_ids, compact_srcs")
    cur.execute(f"INSERT INTO compact_ids SELECT id, src FROM {t(log)}")
    if not cur.rowcount:
        return 0
    cur.execute("INSERT INTO compact_srcs SELECT DISTINCT src FROM compact_ids")
    n = cur.rowcount
    cur.execute("ANALYZE compact_srcs")
    cur.execute(f"DELETE FROM {t(target)} c USING compact_srcs g WHERE c.src = g.src")
    cur.execute(f"INSERT INTO {t(target)} ({_cols()}) SELECT {_cols('c.')} FROM {t(source)} c "
                "JOIN compact_srcs g ON g.src = c.src")
    cur.execute(f"DELETE FROM {t(log)} l USING compact_ids i WHERE l.id = i.id")
    return n


def _pending(cur: Any, log: str) -> int:
    cur.execute(f"SELECT count(DISTINCT src) FROM {t(log)}")
    return int(cur.fetchone()[0])


def _catch_up_txn(s: Session, source: str, target: str, log: str) -> int:
    with s.connect(autocommit=False) as conn, conn.cursor() as cur:
        s.wait_for_delta(conn)
        n = catch_up(cur, source, target, log)
        conn.commit()
    return n


def build_indexes(s: Session) -> dict[str, Any]:
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase") != "filled":
            return {"indexes": "skipped", "phase": state.get("phase")}
    caught = _catch_up_txn(s, TABLE, NEW, LOG)   # no twice-copied row before the key
    idx = [i for i in state["indexes"] if not DURING_FILL.search(i["def"])]
    built = set(state.get("built", []))

    def one(i: Mapping[str, Any]) -> str:
        name = renamed(i["name"], NEW_SUFFIX)
        if name in built:
            return name
        with s.connect() as conn, conn.cursor() as cur:
            _disk_left(s, cur, int(state.get("reserve", DISK_RESERVE)))
            s.wait_for_delta(conn)
            cur.execute(_on_new(i["def"], name, NEW))
        return name

    s.pool(one, idx)
    with s.connect(autocommit=False) as conn, conn.cursor() as cur:
        for i in state["indexes"]:
            name = renamed(i["name"], NEW_SUFFIX)
            if DURING_FILL.search(i["def"]):
                # the served index's own options again (as its definition has them)
                cur.execute(f"ALTER INDEX {t(name)} RESET (gin_pending_list_limit)")
                opts = re.search(r"\) WITH \(([^)]*)\)", i["def"])
                if opts:
                    cur.execute(f"ALTER INDEX {t(name)} SET ({opts.group(1)})")
            for attnum, target in i["stats"].items():
                cur.execute(f"ALTER INDEX {t(name)} ALTER COLUMN {int(attnum)} SET "
                            f"STATISTICS {int(target)}")
            if i["pkey"]:
                cur.execute("SELECT 1 FROM pg_constraint WHERE conrelid = %s::regclass AND "
                            "contype = 'p'", (t(NEW),))
                if cur.fetchone() is None:
                    cur.execute(f"ALTER TABLE {t(NEW)} ADD CONSTRAINT {name} PRIMARY KEY "
                                f"USING INDEX {name}")
        _put(cur, "built", [renamed(i["name"], NEW_SUFFIX) for i in state["indexes"]])
        _put(cur, "phase", "indexed")
        conn.commit()
    return {"indexes": len(idx), "caught_up_before_key": caught}


def vacuum(s: Session) -> dict[str, Any]:
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase") != "indexed":
            return {"vacuum": "skipped"}
        cur.execute(f"VACUUM (ANALYZE) {t(NEW)}")
        _put(cur, "phase", "vacuumed")
    return {"vacuumed": True}


_HASH = ("SELECT (c.src % {b})::int, count(*), sum(hashtextextended(row({cols})::text, 0)"
         "::numeric) FROM {table} c WHERE {where} AND NOT EXISTS (SELECT 1 FROM {log} l "
         "WHERE l.src = c.src) GROUP BY 1")


def compare(s: Session, a: str, b: str, log: str) -> dict[str, Any]:
    """Tables ``a`` and ``b`` in one snapshot every worker shares, the sources in ``log``
    left out: (count, hash sum) per source bucket. Raises :class:`Mismatch`."""
    import psycopg
    with s.connect(autocommit=False) as lead, lead.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute("SELECT pg_export_snapshot()")
        snap = cur.fetchone()[0]
        cur.execute(f"SELECT count(DISTINCT src) FROM {t(log)}")
        skipped = int(cur.fetchone()[0])
        work: list[tuple[str, int, int | None]] = []
        for name in (a, b):
            cur.execute("SELECT pg_relation_size(%s::regclass) / "
                        "current_setting('block_size')::int", (t(name),))
            pages = int(cur.fetchone()[0])
            parts = max(1, min(s.workers * 4, pages))
            step = max(1, -(-max(pages, 1) // parts))
            spans = [[lo, lo + step] for lo in range(0, max(pages, 1), step)]
            spans[-1][1] = None
            work += [(name, lo, hi) for lo, hi in spans]

        def one(item: tuple[str, int, int | None]) -> tuple[str, list[tuple]]:
            name, lo, hi = item
            with psycopg.connect(s.dsn) as conn, conn.cursor() as wc:
                wc.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                wc.execute("SET TRANSACTION SNAPSHOT %s" % _lit(snap))
                wc.execute("SET LOCAL max_parallel_workers_per_gather = 0")
                wc.execute(_HASH.format(b=BUCKETS, cols=_cols("c."), table=t(name),
                                        where=_ctid(lo, hi), log=t(log)))
                return name, wc.fetchall()

        got: dict[str, dict[int, list]] = {a: {}, b: {}}
        for name, rows in s.pool(one, work):
            for bucket, n, h in rows:
                acc = got[name].setdefault(int(bucket), [0, 0])
                acc[0] += int(n)
                acc[1] += int(h)
        lead.rollback()
    differ = sorted(k for k in set(got[a]) | set(got[b])
                    if got[a].get(k, [0, 0]) != got[b].get(k, [0, 0]))
    rows = sum(v[0] for v in got[a].values())
    out = {"rows": rows, "rows_other": sum(v[0] for v in got[b].values()),
           "buckets": len(got[a]), "differ": len(differ), "sources_left_out": skipped}
    if differ:
        raise Mismatch(json.dumps({**out, "first": differ[:10]}))
    return out


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def check(s: Session) -> dict[str, Any]:
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase") not in ("vacuumed", "checked"):
            return {"check": "skipped", "phase": state.get("phase")}
    caught = _catch_up_txn(s, TABLE, NEW, LOG)
    out = {"caught_up": caught, **compare(s, TABLE, NEW, LOG)}
    with s.connect() as conn, conn.cursor() as cur:
        _put(cur, "check", out)
        _put(cur, "phase", "checked")
    return out


class _Backlog(Exception):
    """More logged sources than a locked catch-up may copy: release, catch up, retry."""


def _retry(s: Session, what: str, attempt: Callable[[Any], dict[str, Any]], *,
           lock_timeout_ms: int, tries: int,
           outside: Callable[[], Any] | None = None) -> dict[str, Any]:
    """``attempt(cur)`` in one transaction holding the delta's advisory lock and a
    ``lock_timeout``; retried while a delta applies or a reader holds the table, and
    after ``outside()`` (a catch-up without the lock) when the attempt found more logged
    work than it may do under the lock (:class:`_Backlog`)."""
    import psycopg
    waited = 0
    for n in range(1, tries + 1):
        try:
            with s.connect(autocommit=False) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
                cur.execute(f"SELECT pg_try_advisory_xact_lock({DELTA_LOCK})")
                if not cur.fetchone()[0]:
                    conn.rollback()
                    waited += 1
                    s.log(f"{what}: a delta applies (attempt {n}), retrying")
                    time.sleep(min(10, n))
                    continue
                started = time.monotonic()
                out = attempt(cur)
                held = time.monotonic() - started
                conn.commit()
                return {f"{what}_attempts": n, f"{what}_locked_s": round(held, 3),
                        f"{what}_waited_for_delta": waited, **out}
        except psycopg.errors.LockNotAvailable:
            s.log(f"{what}: table busy (attempt {n}), retrying")
            time.sleep(min(10, n))
        except _Backlog as exc:
            s.log(f"{what}: {exc} logged sources, caught up outside the lock first")
            if outside is not None:
                outside()
    raise Refused(f"{what}_lock_not_acquired")


def _rename_indexes(cur: Any, table: str, names: Iterable[tuple[str, str]]) -> None:
    for old, new in names:
        cur.execute(f"ALTER INDEX {t(old)} RENAME TO {q(new)}")
    del table


def _remake_views(cur: Any, defs: Sequence[Mapping[str, str]]) -> None:
    for v in defs:
        name = v["name"]
        if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", name):
            raise Refused(f"bad_view_name:{name!r}")
        opts = f" WITH ({v['options']})" if v["options"] else ""
        cur.execute(f"CREATE OR REPLACE VIEW {name}{opts} AS {v['def']}")


def swap(s: Session, *, lock_timeout_ms: int = 3000, tries: int = 60) -> dict[str, Any]:
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase") != "checked":
            raise Refused(f"not_checked:{state.get('phase')}")
        names = [i["name"] for i in state["indexes"]]
    # most of what is left, outside the lock
    _catch_up_txn(s, TABLE, NEW, LOG)

    def attempt(cur: Any) -> dict[str, Any]:
        cur.execute(f"LOCK TABLE {t(TABLE)} IN ACCESS EXCLUSIVE MODE")
        cur.execute(f"LOCK TABLE {t(NEW)} IN ACCESS EXCLUSIVE MODE")
        now = [i["name"] for i in indexes(cur, TABLE)]
        if sorted(now) != sorted(names):
            raise Refused(f"indexes_changed_since_prepare:{sorted(now)}")
        defs = views(cur, TABLE)
        backlog = _pending(cur, LOG)
        if backlog > s.locked_catch_up_max:
            raise _Backlog(backlog)
        caught = catch_up(cur, TABLE, NEW, LOG)
        if _pending(cur, LOG):   # the table is locked: no writer can be in flight
            raise Refused("log_not_empty_under_lock")
        _unwatch(cur, TABLE, "value_cells_compact_log")
        cur.execute(f"ALTER TABLE {t(TABLE)} RENAME TO {OLD}")
        _rename_indexes(cur, OLD, [(n, renamed(n, OLD_SUFFIX)) for n in names])
        cur.execute(f"ALTER TABLE {t(NEW)} RENAME TO {TABLE}")
        _rename_indexes(cur, TABLE, [(renamed(n, NEW_SUFFIX), n) for n in names])
        _owner_and_grants(cur, OLD, TABLE)
        _remake_views(cur, defs)
        _log_table(cur, UNDO_LOG)
        _watch(cur, TABLE, UNDO_FN, "value_cells_compact_undo")
        _put(cur, "phase", "swapped")
        _put(cur, "views", [v["name"] for v in defs])
        return {"caught_up": caught, "views": [v["name"] for v in defs]}

    out = _retry(s, "swap", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries,
                 outside=lambda: _catch_up_txn(s, TABLE, NEW, LOG))
    with s.connect() as conn, conn.cursor() as cur:
        _put(cur, "swap", out)
        cur.execute(f"ANALYZE {t(TABLE)}")
    return out


def undo(s: Session, *, lock_timeout_ms: int = 3000, tries: int = 60) -> dict[str, Any]:
    """Before the swap: the change log and the new table dropped. After it: the old table
    back with the cells written since (the logged sources copied from the new table), its
    views made again, then the new table dropped."""
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if not state and not exists(cur, NEW) and not exists(cur, OLD):
            return {"undo": "nothing"}
        names = [i["name"] for i in state.get("indexes", [])]
    if state.get("phase") == "swapped" or exists_old(s):
        _catch_up_txn(s, TABLE, OLD, UNDO_LOG)

        def attempt(cur: Any) -> dict[str, Any]:
            cur.execute(f"LOCK TABLE {t(TABLE)} IN ACCESS EXCLUSIVE MODE")
            cur.execute(f"LOCK TABLE {t(OLD)} IN ACCESS EXCLUSIVE MODE")
            defs = views(cur, TABLE)
            backlog = _pending(cur, UNDO_LOG)
            if backlog > s.locked_catch_up_max:
                raise _Backlog(backlog)
            caught = catch_up(cur, TABLE, OLD, UNDO_LOG)
            if _pending(cur, UNDO_LOG):
                raise Refused("undo_log_not_empty_under_lock")
            _unwatch(cur, TABLE, "value_cells_compact_undo")
            cur.execute(f"ALTER TABLE {t(TABLE)} RENAME TO {NEW}")
            _rename_indexes(cur, NEW, [(n, renamed(n, NEW_SUFFIX)) for n in names])
            cur.execute(f"ALTER TABLE {t(OLD)} RENAME TO {TABLE}")
            _rename_indexes(cur, TABLE, [(renamed(n, OLD_SUFFIX), n) for n in names])
            _remake_views(cur, defs)
            _put(cur, "phase", "undone")   # a rerun only cleans up
            return {"caught_up": caught}
        out = _retry(s, "undo", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries,
                     outside=lambda: _catch_up_txn(s, TABLE, OLD, UNDO_LOG))
    else:
        out = {"swapped": False}
    with s.connect(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = {int(s.lock_timeout_ms)}")
        _unwatch(cur, TABLE, "value_cells_compact_log")
        _unwatch(cur, TABLE, "value_cells_compact_undo")
        _drop_work(cur, NEW)
        _drop_functions(cur)
        conn.commit()
    return {"undone": True, **out}


def exists_old(s: Session) -> bool:
    with s.connect() as conn, conn.cursor() as cur:
        return exists(cur, OLD)


def _drop_work(cur: Any, table: str) -> None:
    """``table`` and the work tables; the trigger functions once no trigger uses them
    (the caller drops the triggers in the same transaction)."""
    cur.execute(f"DROP TABLE IF EXISTS {t(table)}")
    for name in (LOG, UNDO_LOG, MARKS, STATE):
        cur.execute(f"DROP TABLE IF EXISTS {t(name)}")


def _drop_functions(cur: Any) -> None:
    for fn in (LOG_FN, UNDO_FN, REFUSE_FN):
        cur.execute(f"DROP FUNCTION IF EXISTS {t(fn)}()")


def finish(s: Session) -> dict[str, Any]:
    """The check again (the served table against the kept old one, the sources written
    since the swap left out); only when it agrees the old table and the work tables go."""
    with s.connect() as conn, conn.cursor() as cur:
        state = _state(cur)
        if state.get("phase") != "swapped" or not exists(cur, OLD):
            raise Refused(f"not_swapped:{state.get('phase')}")
        before = sizes(cur, OLD)
    out = compare(s, TABLE, OLD, UNDO_LOG)
    # one transaction: the old table goes with the undo triggers and logs, never the
    # triggers first (a crash between would leave an undo restoring a table that missed
    # the writes made since)
    with s.connect(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = {int(s.lock_timeout_ms)}")
        _drop_work(cur, OLD)
        _unwatch(cur, TABLE, "value_cells_compact_undo")
        _drop_functions(cur)
        conn.commit()
    return {"finished": True, "check": out,
            "freed_bytes": before["heap_bytes"] + before["index_bytes"]}


def convert(s: Session, *, budget_s: float = BUDGET_S, reserve: int = DISK_RESERVE,
            do_swap: bool = False, rates: Mapping[str, float] = RATES,
            without_f3: bool = False) -> dict[str, Any]:
    """Every phase not done yet, in order (module docstring); the swap only when asked."""
    out: dict[str, Any] = {}
    out["prepare"] = s.timed("prepare", lambda: prepare(s, budget_s=budget_s,
                                                        reserve=reserve, rates=rates,
                                                        without_f3=without_f3))
    out["fill"] = s.timed("fill", lambda: fill(s))
    out["indexes"] = s.timed("indexes", lambda: build_indexes(s))
    out["vacuum"] = s.timed("vacuum", lambda: vacuum(s))
    out["check"] = s.timed("check", lambda: check(s))
    if do_swap:
        out["swap"] = s.timed("swap", lambda: swap(s))
    out["timings"] = dict(s.timings)
    out["yielded_to_delta_s"] = round(s.yielded_s, 1)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.value_cells_compact")
    ap.add_argument("command", choices=("plan", "convert", "swap", "undo", "finish",
                                        "status"))
    ap.add_argument("--dsn", default=None, help="default: PLX_PG_DSN")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--budget-s", type=float, default=BUDGET_S)
    ap.add_argument("--reserve-gb", type=float, default=DISK_RESERVE / 2**30)
    ap.add_argument("--swap", action="store_true", help="convert: swap once checked")
    ap.add_argument("--without-f3-finished", action="store_true",
                    help="convert even while GA-F3's collapse is unfinished here")
    ap.add_argument("--rates", default=None,
                    help="the 10 %% rehearsal's report (JSON): its measured rates; convert "
                         "refuses without it")
    a = ap.parse_args(argv)
    s = Session(a.dsn or config.dsn(), workers=a.workers,
                log=lambda m: print(time.strftime("%H:%M:%S"), m, file=sys.stderr,
                                    flush=True))
    reserve = int(a.reserve_gb * 2**30)
    try:
        rates: Mapping[str, float] = RATES
        if a.rates:
            with open(a.rates, encoding="utf-8") as fh:
                rates = rehearsed_rates(json.load(fh))
        elif a.command == "convert":
            raise Refused("no_rehearsal: run scripts/rehearse_value_cells_compact.py and "
                          "pass its report as --rates")
        if a.command == "plan":
            with s.connect() as conn, conn.cursor() as cur:
                f3 = f3_unfinished(cur)
                out: Any = plan(cur, workers=a.workers, budget_s=a.budget_s, reserve=reserve,
                                rates=rates, check=False)
            out["rates"] = "rehearsed" if a.rates else "estimated (no rehearsal yet)"
            out["f3"] = f3 or "finished or not started here"
            try:
                gate(out["projection"], budget_s=a.budget_s, free=out["free_bytes"],
                     reserve=reserve)
                out["gate"] = "pass"
            except OverBudget as exc:
                out["gate"] = f"refused: {exc}"
        elif a.command == "convert":
            out = convert(s, budget_s=a.budget_s, reserve=reserve, do_swap=a.swap,
                          rates=rates, without_f3=a.without_f3_finished)
        elif a.command == "swap":
            out = swap(s)
        elif a.command == "undo":
            out = undo(s)
        elif a.command == "finish":
            out = finish(s)
        else:
            with s.connect() as conn, conn.cursor() as cur:
                out = status(cur)
    except (OverBudget, Refused, Mismatch) as exc:
        print(json.dumps({"refused": type(exc).__name__, "why": str(exc)}))
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
