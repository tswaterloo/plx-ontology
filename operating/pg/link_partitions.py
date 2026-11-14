"""``ontology.link_rows`` partitioned by link type: one type's links rebuilt in minutes.

Why: every link or step change rewrote rows of one 180 GB table with eight indexes (seven since GA-F7), chunk by
chunk (12-24 h on 2026-11-06..10). Partitioned by ``link_type`` (``PARTITION BY LIST``), a
change to some link types touches only the partitions holding them:

1. **build** (:func:`rebuild`): for each affected partition a new table beside it, with a
   ``CHECK`` naming the partition's types, filled set-based in Postgres by
   ``INSERT ... SELECT`` in disjoint block ranges (``ctid``), one connection per range, in
   parallel: the old partition's rows the change keeps, then the change's rows (a source
   table the caller filled, e.g. a step's output copied in, or any query). Then its
   indexes, one connection per index, side by side, and ``VACUUM (ANALYZE)`` (its
   visibility map set, so the readers' index-only scans stay index-only).
2. **swap** (:func:`swap`): one short transaction, under the delta's advisory lock (never
   in the middle of a delta apply), ``DETACH`` the old partitions and ``ATTACH`` the new
   ones. The ``CHECK`` implies the partition bound and the new table has every index of
   the parent, so the attach scans nothing and builds nothing: the swap holds its lock for
   milliseconds (it waits for running readers, ``lock_timeout``, and retries). A reader's
   statement sees the old partition or the new one, never a half; the old one is dropped
   after the commit.

Rows keep every column (versions, ``superseded_at``, ``markings``, reasons and evidence)
exactly as stored: the readers' visibility rules read those columns and do not change. A
row a writer adds to an affected partition while it is rebuilt is caught by a trigger
(:data:`LOG`): one of a type the change keeps is copied over inside the swap; one of a
type the change replaces fails the swap closed (:class:`Changed`): the change was derived
from a snapshot that no longer holds.

**One link id, one row.** The primary key holds the partition key, ``(link_id, link_type)``
(Postgres requires it). The id alone stays unique: the delta's upsert replaces a served row
of the same id and another type (``ontology_delta._copy``), a rebuild refuses new rows whose
id another type holds (:func:`_foreign_ids`), and :func:`audit` checks the whole table from
the primary key alone.

Small link types share partitions (``<parent>_b<k>_<gen>``: a stable hash of the type picks
the bucket, :func:`groups`); a type of at least :data:`OWN_MIN_SHARE` of the rows has its
own (``<parent>_t<digest>_<gen>``). Every partition costs each lookup by object
(``from_object`` or ``to_object`` without a type) one index probe and its planning. A type
no partition names goes to the ``DEFAULT`` partition until a rebuild places it: in its
bucket's partition, or its own when large.

**Budget** (:func:`plan`, :func:`gate`): every rebuild or conversion is projected from
measured rates (:data:`RATES`, calibrated by ``scripts/rehearse_link_partitions.py``) and
refused before any write when over :data:`BUDGET_S` (2 h) or over the free disk less a
reserve.

**Conversion** (:func:`convert`): the unpartitioned table copied into a partitioned one
built beside it (block ranges in parallel, yielding to the delta's applies, a trigger
logging the rows written meanwhile), indexes built per partition in parallel, then one
transaction under the delta's lock catches up the logged rows and renames the tables;
``ontology.links`` is made again over the new table. The old table stays as
``link_rows_unpartitioned`` with a trigger on the new one logging every write, so
:func:`undo` can put it back with those writes; :func:`finish` drops it.

Stdlib and psycopg. ``python3 -m operating.pg.link_partitions
layout|plan|rebuild|convert|audit|undo|finish ...``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# The schema whose ``link_rows`` this module changes. Only a rehearsal sets another one
# (``scripts/rehearse_link_partitions.py``); it is validated like any identifier.
SCHEMA = os.environ.get("PLX_LINK_PARTITIONS_SCHEMA", "ontology")
PARENT = "link_rows"
OLD = f"{PARENT}_unpartitioned"   # the conversion's old table, kept for undo
LOG = "link_rows_change_log"
BUDGET_S = 7200
DISK_RESERVE = 20 << 30
# The delta's advisory lock (``ontology_delta``, ``ontology_migrate.LOCK``): held for the
# whole of a layer's apply.
DELTA_LOCK = "hashtext('ontology_delta')"
# Measured on this host by the 10 % rehearsal (scripts/rehearse_link_partitions.py,
# 2026-11-12, 8 workers beside a running delta catch-up, two runs; the decision record),
# each from the slower run and rounded down: rows a second one connection fills
# (8.8 k; 16.1 k on the quieter run), index rows a second the side-by-side builds take in
# all (188 k; 338 k), rows a second the per-partition vacuums take (549 k), bytes a row
# takes on disk (heap and TOAST 1,791; its eight index entries 357), rows a second the
# link-id check probes (138 k over 9 partitions), the swap's wait, the disk's read plus
# write rate during the fill (450 MB/s on the quieter run).
RATES = {"fill_rows_s_per_worker": 8_000.0, "index_rows_s": 150_000.0,
         "vacuum_rows_s": 300_000.0, "bytes_per_row": 1850.0, "index_bytes_per_row": 400.0,
         "probe_rows_s": 80_000.0, "swap_s": 5.0, "io_bytes_s": 4e8}
# A link type holding at least this share of the rows has a partition of its own; smaller
# ones share one of BUCKETS partitions (the decision record has the probe costs).
OWN_MIN_SHARE = 0.04
BUCKETS = 8
COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
           "link_contract", "reason_id", "why_not", "evidence_id", "reviewed_by",
           "reviewed_at", "review_action_id", "ek", "slots", "markings", "recorded_at",
           "superseded_at", "reason", "evidence_handles", "row_no")
# The served table's indexes (0007, 0008, 0013; GA-F7's 0018 dropped the byte-order twin
# ``by_id``), the primary key with the partition key (a partitioned unique index must hold
# it). The key (link_id, link_type) also serves a step's prefix range where the database's
# default collation is byte order (``operating.pg.byte_order.key_collate``), so the types a
# step's prefix holds are read from it (:func:`step_change`), and :func:`audit` reads ids
# in order from it.
INDEXES = (("by_row_no", "btree (row_no)"),
           ("by_type", "btree (link_type, tier)"),
           ("from", "btree (from_object, link_type)"),
           ("to", "btree (to_object, link_type)"),
           ("lead_from", 'btree (from_object, link_id COLLATE "C") '
                         "WHERE tier = 'lead' AND superseded_at IS NULL"),
           ("contra_from", "btree (from_object) INCLUDE (to_object, link_type) "
                           "WHERE tier = 'contradicted'"),
           # CC-P-07 (ops-1113d, migration 0019 on the plain table): the odd-evidence-slot
           # read of every whole-store link slice, partial on its own predicate
           ("odd_slots", "gin ((slots[1:ek])) WHERE evidence_id IS NOT NULL"))
PKEY = ("link_id", "link_type")


class OverBudget(RuntimeError):
    """The projected change exceeds the time budget or the free disk: nothing written."""


class Changed(RuntimeError):
    """A writer changed links of a replaced type during the rebuild: nothing swapped."""


class Refused(RuntimeError):
    """The change cannot run as asked (not partitioned, unknown type, bad name)."""


def q(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise Refused(f"bad_identifier:{name!r}")
    return name


q(SCHEMA)


def lit(text: str) -> str:
    """A SQL string literal (types are data; never formatted unquoted)."""
    return "'" + text.replace("'", "''") + "'"


def bucket(link_type: str, buckets: int = BUCKETS) -> int:
    return int(hashlib.sha1(link_type.encode()).hexdigest()[:8], 16) % buckets


def groups(counts: Mapping[str, int], *, own_min: int | None = None,
           buckets: int = BUCKETS) -> list[tuple[tuple[str, ...], int | None]]:
    """The partitions' (types, bucket): each type of ``own_min`` rows or more (default
    :data:`OWN_MIN_SHARE` of all) alone (bucket None), the others in their hash bucket
    (stable: a type keeps its bucket as others come and go)."""
    if own_min is None:
        own_min = max(1, int(OWN_MIN_SHARE * sum(counts.values())))
    own = sorted(t for t, n in counts.items() if n >= own_min)
    shared: dict[int, list[str]] = {}
    for t in counts:
        if counts[t] < own_min:
            shared.setdefault(bucket(t, buckets), []).append(t)
    return ([((t,), None) for t in own] +
            [(tuple(sorted(v)), b) for b, v in sorted(shared.items())])


def part_name(types: Sequence[str] | None, gen: int, bucket_: int | None = None) -> str:
    if types is None:
        return f"{PARENT}_default_{gen}"
    if bucket_ is not None:
        return f"{PARENT}_b{bucket_}_{gen}"
    return f"{PARENT}_t{hashlib.sha1(chr(0).join(sorted(types)).encode()).hexdigest()[:10]}_{gen}"


_BOUND = re.compile(r"'((?:[^']|'')*)'")
_BUCKET = re.compile(rf"^{PARENT}_b(\d+)_\d+$")


@dataclass
class Partition:
    name: str
    types: tuple[str, ...] | None   # None: the DEFAULT partition
    rows: int = 0
    pages: int = 0
    bucket: int | None = None


def is_partitioned(cur: Any, table: str = PARENT) -> bool:
    cur.execute("SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = "
                "c.relnamespace WHERE n.nspname = %s AND c.relname = %s", (SCHEMA, table))
    row = cur.fetchone()
    return bool(row) and row[0] == "p"


def _exists(cur: Any, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{SCHEMA}.{table}",))
    return bool(cur.fetchone()[0])


def layout(cur: Any, table: str = PARENT) -> list[Partition]:
    cur.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid), greatest(c.reltuples, 0)::bigint, "
        "pg_relation_size(c.oid) / 8192 FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = %s::regclass ORDER BY 1", (f"{SCHEMA}.{table}",))
    out = []
    for name, bound, rows, pages in cur.fetchall():
        types = None if bound == "DEFAULT" else tuple(
            m.replace("''", "'") for m in _BOUND.findall(bound))
        b = _BUCKET.match(name)
        out.append(Partition(name, types, rows, pages, int(b.group(1)) if b else None))
    return out


def _gen(cur: Any) -> int:
    cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {SCHEMA}.link_partition_gen")
    cur.execute(f"SELECT nextval('{SCHEMA}.link_partition_gen')")
    return cur.fetchone()[0]


def _check(types: Sequence[str] | None, others: Sequence[str] = ()) -> str:
    """A row's test of being of ``types`` (None: of none of ``others``, the default)."""
    if types is None:   # the default partition: none of the listed types
        return ("link_type IS NOT NULL" if not others else
                f"NOT (link_type = ANY (ARRAY[{', '.join(lit(t) for t in others)}]::text[]))")
    return f"link_type = ANY (ARRAY[{', '.join(lit(t) for t in types)}]::text[])"


def _bound(types: Sequence[str] | None) -> str:
    return "DEFAULT" if types is None else f"FOR VALUES IN ({', '.join(lit(t) for t in types)})"


def _reloptions(cur: Any, table: str) -> str:
    """``WITH (...)`` of ``table``'s storage options (the autovacuum settings of 0014),
    for a partition made beside it; LIKE copies none, and a partitioned parent holds none."""
    cur.execute("SELECT reloptions FROM pg_class WHERE oid = %s::regclass", (table,))
    row = cur.fetchone()
    opts = [o for o in (row[0] if row and row[0] else ())
            if re.fullmatch(r"[a-z_]+=[0-9.]+", o)]
    return f" WITH ({', '.join(opts)})" if opts else ""


def _building(options: str) -> str:
    """``options`` with autovacuum off while a new partition fills (an insert-triggered
    autovacuum would read it again during the build); :func:`_settle` vacuums it once."""
    if not options:
        return " WITH (autovacuum_enabled = false)"
    return options[:-1] + ", autovacuum_enabled = false)"


def _settle(name: str) -> tuple[str, ...]:
    """A built partition made ready to serve, on one connection: ``VACUUM (ANALYZE)`` sets
    its visibility map (the readers' index-only scans) and statistics, then autovacuum on."""
    return (f"VACUUM (ANALYZE) {SCHEMA}.{q(name)}",
            f"ALTER TABLE {SCHEMA}.{q(name)} RESET (autovacuum_enabled)")


def _options_of(cur: Any) -> str:
    """The options the partitions carry: the served table's, or its partitions'."""
    if not is_partitioned(cur):
        return _reloptions(cur, f"{SCHEMA}.{PARENT}")
    parts = layout(cur)
    return _reloptions(cur, f"{SCHEMA}.{parts[0].name}") if parts else ""


def parent_indexes(cur: Any, table: str) -> None:
    """The parent's indexes; each partition's matching index is attached, none built."""
    cur.execute(f"ALTER TABLE {SCHEMA}.{q(table)} ADD CONSTRAINT {q(table)}_pkey "
                f"PRIMARY KEY ({', '.join(PKEY)})")
    for suffix, spec in INDEXES:
        cur.execute(f"CREATE INDEX IF NOT EXISTS {q(table + '_' + suffix)} ON "
                    f"{SCHEMA}.{q(table)} USING {spec}")


def _index_sqls(name: str) -> list[str]:
    """A partition's index builds (each on its own connection; ``CREATE INDEX`` takes a
    lock the others share, so they run side by side)."""
    return ([f"CREATE UNIQUE INDEX {name}_pkey ON {SCHEMA}.{name} ({', '.join(PKEY)})"] +
            [f"CREATE INDEX {name}_{s} ON {SCHEMA}.{name} USING {spec}" for s, spec in INDEXES])


def _pkey_sql(name: str) -> str:
    """The primary key over the built unique index (no build: the parent's key attaches it)."""
    return (f"ALTER TABLE {SCHEMA}.{name} ADD CONSTRAINT {name}_pkey PRIMARY KEY "
            f"USING INDEX {name}_pkey")


def _pages(cur: Any, table: str) -> int:
    cur.execute("SELECT pg_relation_size(%s::regclass) / current_setting('block_size')::int",
                (table,))
    return int(cur.fetchone()[0])


def _ranges(pages: int, parts: int) -> list[tuple[int, int | None]]:
    """``parts`` disjoint block ranges covering a table of ``pages`` blocks; the last one
    open (rows added past the end while it runs)."""
    parts = max(1, min(parts, pages or 1))
    step = -(-max(pages, 1) // parts)
    return [(lo, None if lo + step >= pages else lo + step) for lo in range(0, max(pages, 1), step)]


def _ctid(lo: int, hi: int | None) -> str:
    where = f"ctid >= '({lo},0)'::tid"
    return where if hi is None else f"{where} AND ctid < '({hi},0)'::tid"


def delta_applying(cur: Any) -> bool:
    """True while a delta layer's apply holds its advisory lock (any session)."""
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND "
                "granted AND objsubid = 1 AND database = (SELECT oid FROM pg_database WHERE "
                f"datname = current_database()) AND ((classid::bigint << 32) | objid::bigint) "
                f"= {DELTA_LOCK}::bigint)")
    return bool(cur.fetchone()[0])


@dataclass
class Session:
    """Connections and settings for one job's workers. ``yield_to_delta``: a statement
    starts only while no delta apply runs (one already started goes on); the conversion's
    fill is many short statements, so a delta apply waits for none of it."""
    dsn: str
    workers: int = 8
    maintenance_mem: str = "512MB"
    log: Callable[[str], None] = lambda m: None
    timings: dict[str, float] = field(default_factory=dict)
    yield_to_delta: bool = True
    yield_poll_s: float = 5.0
    yielded_s: float = 0.0

    def connect(self, autocommit: bool = True) -> Any:
        import psycopg
        conn = psycopg.connect(self.dsn, autocommit=autocommit)
        conn.execute(f"SET maintenance_work_mem = '{self.maintenance_mem}'")
        conn.execute("SET synchronous_commit = off")
        conn.execute("SET max_parallel_maintenance_workers = 2")
        return conn

    def wait_for_delta(self, conn: Any) -> None:
        if not self.yield_to_delta:
            return
        t = time.monotonic()
        with conn.cursor() as cur:
            while delta_applying(cur):
                time.sleep(self.yield_poll_s)
        self.yielded_s += time.monotonic() - t

    def run(self, sqls: Sequence[str | tuple[str, ...]], workers: int | None = None
            ) -> list[int]:
        """Each statement (or tuple of statements, in order) on its own connection,
        ``workers`` at a time; row counts (of a tuple's last)."""
        def one(sql: str | tuple[str, ...]) -> int:
            with self.connect() as conn:
                self.wait_for_delta(conn)
                n = 0
                for part in ((sql,) if isinstance(sql, str) else sql):
                    n = conn.execute(part).rowcount
                return n
        with ThreadPoolExecutor(max_workers=workers or self.workers) as pool:
            return list(pool.map(one, sqls))

    def timed(self, key: str, fn: Callable[[], Any]) -> Any:
        t = time.monotonic()
        out = fn()
        self.timings[key] = round(self.timings.get(key, 0.0) + time.monotonic() - t, 3)
        self.log(f"{key}: {self.timings[key]} s")
        return out


def fill_sqls(target: str, source: str, pages: int | None, parts: int,
              where: str = "TRUE", *, ranges: Sequence[tuple[int, int | None]] | None = None
              ) -> list[str]:
    """``INSERT ... SELECT`` of ``source``'s rows matching ``where`` into ``target``, one
    statement per disjoint block range of ``source`` (a table of ``pages`` blocks; the last
    range open-ended, so rows past a stale size are read too; or the given ``ranges``);
    one statement when ``pages`` is None (a query)."""
    cols = ", ".join(COLUMNS)
    if pages is None and ranges is None:
        return [f"INSERT INTO {target} ({cols}) SELECT {cols} FROM {source} s WHERE ({where})"]
    return [f"INSERT INTO {target} ({cols}) SELECT {cols} FROM {source} s "
            f"WHERE {_ctid(lo, hi)} AND ({where})"
            for lo, hi in (ranges if ranges is not None else _ranges(pages or 0, parts))]


# --- the budget -------------------------------------------------------------------------

def project(rows_written: int, *, workers: int, read_bytes: int = 0, probes: int = 0,
            rates: Mapping[str, float] = RATES) -> dict[str, float]:
    """Projected seconds and bytes of writing ``rows_written`` rows into new partitions
    (fill in ``workers`` ranges side by side, reading ``read_bytes`` of old tables, then
    their indexes), checking ``probes`` new rows' ids, and swapping them in."""
    written = rows_written * (rates["bytes_per_row"] + rates["index_bytes_per_row"])
    fill = max(rows_written / (rates["fill_rows_s_per_worker"] * max(1, workers)),
               (read_bytes + rows_written * rates["bytes_per_row"]) / rates["io_bytes_s"])
    index = max(rows_written / rates["index_rows_s"],
                rows_written * rates["index_bytes_per_row"] / rates["io_bytes_s"])
    check = probes / rates["probe_rows_s"]
    vacuum = rows_written / rates["vacuum_rows_s"]
    total = fill + index + vacuum + check + rates["swap_s"]
    return {"fill_s": round(fill, 1), "index_s": round(index, 1), "vacuum_s": round(vacuum, 1),
            "check_s": round(check, 1),
            "total_s": round(total, 1), "bytes": int(written)}


def free_bytes(cur: Any) -> int | None:
    """Free bytes of the file system holding the database: ``PLX_PG_DATA_DIR``, else the
    server's data directory or its nearest readable parent (this host only); None when
    neither can be read (the gate then refuses)."""
    import shutil
    from pathlib import Path
    folder = os.environ.get("PLX_PG_DATA_DIR")
    candidates: list[Path] = [Path(folder)] if folder else []
    if not folder:
        try:
            cur.execute("SELECT current_setting('data_directory')")
            data = Path(cur.fetchone()[0])
            candidates = [data, *data.parents]
        except Exception:
            return None
    for candidate in candidates:
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None


def gate(projection: Mapping[str, float], *, budget_s: float = BUDGET_S,
         free: int | None, reserve: int = DISK_RESERVE) -> None:
    """Refuse (:class:`OverBudget`) a projection over the time budget or the free disk
    less the reserve. A projection without its numbers, or a free disk that could not be
    read (``free`` None), is refused too (fail closed)."""
    for key in ("total_s", "bytes"):
        value = projection.get(key)
        if not isinstance(value, (int, float)) or value != value or value < 0:
            raise OverBudget(f"projection_without_{key}")
    if projection["total_s"] > budget_s:
        raise OverBudget(f"projected {projection['total_s']:.0f} s > budget {budget_s:.0f} s")
    if free is None:
        raise OverBudget("free_disk_unknown: set PLX_PG_DATA_DIR to a folder on the "
                         "database's file system")
    if projection["bytes"] > free - reserve:
        raise OverBudget(f"projected {projection['bytes'] >> 30} GB > free {free >> 30} GB "
                         f"less {reserve >> 30} GB reserve")


# --- rebuild one link type --------------------------------------------------------------

@dataclass
class Rebuild:
    """``types``: the link types the change replaces. Their rows in the affected partitions
    are dropped except those matching ``keep`` (SQL over a row, e.g. the rows of other
    steps: ``NOT (link_id COLLATE "C" >= 'mention:' AND ...)``), and ``source``'s rows of
    the partitions' types are added (a table or ``(SELECT ...)`` with the link columns)."""
    types: tuple[str, ...]
    source: str | None = None
    source_pages: int | None = None
    keep: str = "FALSE"


def prefix_end(prefix: str) -> str:
    """The first text past every text starting with ``prefix`` (binary order)."""
    if not prefix:
        raise Refused("empty_prefix")
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _in_prefixes(prefixes: Sequence[str], c: str = ' COLLATE "C"') -> str:
    """The rows whose id starts with one of ``prefixes`` (byte order; ``c``: the key's
    collation clause, ``byte_order.key_collate``)."""
    return " OR ".join(f"(link_id{c} >= {lit(p)} AND link_id{c} < "
                       f"{lit(prefix_end(p))})" for p in prefixes) or "FALSE"


def step_change(cur: Any, prefixes: Sequence[str], source: str | None, *,
                from_types: Sequence[str] | None = None,
                writes: Iterable[str] = ()) -> Rebuild:
    """The rebuild of a build step's links (``ontology.step_migrate.Spec``): the step owns
    every link whose id starts with one of ``prefixes``; with ``from_types`` it re-derives
    only those starting at objects of these types (``<type>:`` ids). The replaced types
    are the ones its served links hold (read from the key), its declared
    ``writes`` and its new rows' (``source``); every other row of those types is kept."""
    from ontology.pg_byte_order import key_collate
    c = key_collate(cur)
    owned = _in_prefixes(prefixes, c)
    types = set(writes)
    for p in prefixes:
        cur.execute(f"SELECT DISTINCT link_type FROM {SCHEMA}.{PARENT} WHERE link_id{c} >= %s"
                    f" AND link_id{c} < %s", (p, prefix_end(p)))
        types |= {r[0] for r in cur.fetchall()}
    replaced = owned
    if from_types is not None:
        scoped = " OR ".join(f"left(from_object, {len(t) + 1}) = {lit(t + ':')}"
                             for t in from_types) or "FALSE"
        replaced = f"({owned}) AND ({scoped})"
    if source is not None:
        cur.execute(f"SELECT DISTINCT link_type FROM {source} s")
        types |= {r[0] for r in cur.fetchall()}
        # a new row outside what the step replaces would duplicate a kept one
        cur.execute(f"SELECT count(*) FROM {source} s WHERE NOT ({replaced})")
        if cur.fetchone()[0]:
            raise Refused("step_output_outside_what_it_replaces")
    if not types:
        raise Refused("step_owns_no_links")
    return Rebuild(tuple(sorted(types)), source, keep=f"NOT ({replaced})")


def _total_rows(parts: Sequence[Partition], rates: Mapping[str, float] = RATES) -> int:
    return sum(max(p.rows, int(p.pages * 8192 / rates["bytes_per_row"])) for p in parts)


@dataclass
class _Plan:
    hit: list[Partition]                       # the served partitions replaced
    news: list[Partition]                      # what replaces them
    named_after: list[str]                     # every type a partition names after the swap
    new_types: list[str]
    report: dict[str, Any]


def _placement(cur: Any, change: Rebuild, parts: Sequence[Partition], gen: int,
               rates: Mapping[str, float]) -> _Plan:
    """The partitions a change rebuilds and the ones replacing them. A type no partition
    names joins its hash bucket's partition (rebuilt with it), or gets one of its own when
    its new rows reach :data:`OWN_MIN_SHARE`; the default partition is then rebuilt
    without it (its ``CHECK`` names every placed type, so attaching never scans it)."""
    want = set(change.types)
    named = {t for p in parts if p.types for t in p.types}
    new_types = sorted(want - named)
    hit = {p.name: p for p in parts if p.types is not None and want & set(p.types)}
    extra: dict[str, list[str]] = {}            # partition name -> new types it takes
    own_new: list[str] = []
    if new_types:
        default = [p for p in parts if p.types is None]
        if not default:
            raise Refused("no_default_partition")
        hit[default[0].name] = default[0]
        counts: dict[str, int] = {}
        if change.source is not None:
            cur.execute(f"SELECT link_type, count(*) FROM {change.source} s WHERE "
                        f"{_check(new_types)} GROUP BY 1")
            counts = dict(cur.fetchall())
        own_min = max(1, int(OWN_MIN_SHARE * _total_rows(parts, rates)))
        buckets = {p.bucket: p for p in parts if p.bucket is not None}
        for t in new_types:
            home = buckets.get(bucket(t))
            if counts.get(t, 0) >= own_min or home is None:
                own_new.append(t)
            else:
                extra.setdefault(home.name, []).append(t)
                hit[home.name] = home
    news: list[Partition] = []
    for old in hit.values():
        if old.types is None:
            continue
        types = tuple(sorted({*old.types, *extra.get(old.name, ())}))
        news.append(Partition(part_name(types, gen, old.bucket), types, bucket=old.bucket))
    for t in own_new:
        news.append(Partition(part_name((t,), gen), (t,)))
    if any(p.types is None for p in hit.values()):
        news.append(Partition(part_name(None, gen), None))
    named_after = sorted(named | set(new_types))
    return _Plan(list(hit.values()), news, named_after, new_types, {})


def plan(cur: Any, change: Rebuild, *, workers: int = 8, budget_s: float = BUDGET_S,
         rates: Mapping[str, float] = RATES, gen: int = 0) -> dict[str, Any]:
    """The partitions ``change`` rebuilds, projected; :class:`OverBudget` past the budget,
    :class:`Refused` on a table that is not partitioned or still holds the conversion's
    undo (a rebuild would not be logged for it)."""
    if not is_partitioned(cur):
        raise Refused("link_rows_not_partitioned")
    if _exists(cur, OLD):
        raise Refused("conversion_not_finished: undo or finish it first")
    unknown = [t for t in change.types if not isinstance(t, str) or not t]
    if unknown or not change.types:
        raise Refused("rebuild_without_types")
    parts = layout(cur)
    placed = _placement(cur, change, parts, gen, rates)
    rows = _total_rows(placed.hit, rates)
    source_rows = 0
    if change.source is not None:
        cur.execute(f"SELECT count(*) FROM {change.source} s")
        source_rows = cur.fetchone()[0]
    proj = project(rows + source_rows, workers=workers, rates=rates, probes=source_rows,
                   read_bytes=sum(p.pages for p in placed.hit) * 8192)
    gate(proj, budget_s=budget_s, free=free_bytes(cur))
    placed.report = {"partitions": [p.name for p in placed.hit],
                     "new": [p.name for p in placed.news], "new_types": placed.new_types,
                     "rows": rows + source_rows, **proj}
    return placed.report


def _foreign_ids(s: Session, change: Rebuild, plan_: _Plan) -> int:
    """New rows (``change.source``) whose link id a row of another type holds after the
    swap would hold: in a partition the change leaves alone, in a new one, or in the
    source itself. Probed by the primary keys, one connection per block range of the
    source (a query source in one)."""
    if change.source is None:
        return 0
    others = [p.name for p in plan_.news]
    with s.connect() as conn, conn.cursor() as cur:
        untouched = [p.name for p in layout(cur) if p.name not in {h.name for h in plan_.hit}]
        cur.execute(f"SELECT count(*) FROM (SELECT link_id FROM {change.source} s GROUP BY 1 "
                    "HAVING count(DISTINCT link_type) > 1) d")
        twice = cur.fetchone()[0]
        pages = None
        if re.fullmatch(r"[A-Za-z_][\w.]*", change.source):
            pages = _pages(cur, change.source)
    probe = " OR ".join(f"EXISTS (SELECT 1 FROM {SCHEMA}.{q(t)} o WHERE o.link_id = s.link_id "
                        "AND o.link_type <> s.link_type)" for t in [*untouched, *others])
    if not probe:
        return twice
    sqls = ([f"SELECT count(*) FROM {change.source} s WHERE {_ctid(lo, hi)} AND ({probe})"
             for lo, hi in _ranges(pages, s.workers)] if pages is not None else
            [f"SELECT count(*) FROM {change.source} s WHERE {probe}"])

    def one(sql: str) -> int:
        with s.connect() as conn:
            return conn.execute(sql).fetchone()[0]
    with ThreadPoolExecutor(max_workers=s.workers) as pool:
        return twice + sum(pool.map(one, sqls))


def rebuild(s: Session, change: Rebuild, *, budget_s: float = BUDGET_S,
            lock_timeout_ms: int = 2000, tries: int = 30) -> dict[str, Any]:
    """Rebuild the partitions holding ``change.types`` beside the served ones and swap
    them in (module docstring). Returns the plan, the timings and the counts. Refused,
    changed or failed, it leaves the served partitions as they were and drops what it
    built."""
    with s.connect() as conn, conn.cursor() as cur:
        gen = _gen(cur)
        planned = plan(cur, change, workers=s.workers, budget_s=budget_s, gen=gen)
        placed = _placement(cur, change, layout(cur), gen, RATES)
        options = _options_of(cur)
        _log_table(cur)
        sweep(conn)
    token = f"rebuild-{gen}"
    hit, built = placed.hit, placed.news
    swapped = False
    try:
        with s.connect() as conn, conn.cursor() as cur:
            cur.execute(f"INSERT INTO {SCHEMA}.{ACTIVE} VALUES (%s)", (token,))
            # the trigger's lock waits on writers only (readers share it), not forever
            cur.execute(f"SET lock_timeout = {int(lock_timeout_ms) * 5}")
            for old in hit:
                _watch(cur, old.name, token)
            for new in built:
                others = placed.named_after if new.types is None else ()
                cur.execute(f"CREATE TABLE {SCHEMA}.{new.name} (LIKE {SCHEMA}.{PARENT} "
                            "INCLUDING DEFAULTS INCLUDING CONSTRAINTS, "
                            f"CHECK ({_check(new.types, others)})){_building(options)}")
            pages = {old.name: _pages(cur, f"{SCHEMA}.{old.name}") for old in hit}
            source_pages = None
            if change.source is not None and re.fullmatch(r"[A-Za-z_][\w.]*", change.source):
                source_pages = _pages(cur, change.source)
        replaced = _check(change.types)
        keep = f"NOT ({replaced}) OR ({change.keep})"
        fills: list[str] = []
        for new in built:
            own = _check(new.types, placed.named_after)
            for old in hit:
                # only an old partition that can hold the new one's rows
                if old.types is not None and new.types is not None and \
                        not set(old.types) & set(new.types):
                    continue
                fills += fill_sqls(f"{SCHEMA}.{new.name}", f"{SCHEMA}.{old.name}",
                                   pages[old.name], s.workers, f"({keep}) AND ({own})")
            if change.source is not None:
                fills += fill_sqls(f"{SCHEMA}.{new.name}", change.source, source_pages,
                                   s.workers, own)
        s.timed("fill", lambda: s.run(fills))
        s.timed("index", lambda: s.run([x for n in built for x in _index_sqls(n.name)],
                                       workers=max(s.workers, 6)))
        s.timed("vacuum", lambda: s.run([(_pkey_sql(n.name), *_settle(n.name))
                                         for n in built]))
        foreign = s.timed("id_check", lambda: _foreign_ids(s, change, placed))
        if foreign:
            raise Refused(f"link_id_held_by_another_type:{foreign}")
        report = s.timed("swap", lambda: swap(s, [o.name for o in hit], built, token=token,
                                             replaced=change.types,
                                             lock_timeout_ms=lock_timeout_ms, tries=tries))
        swapped = True
    finally:
        with s.connect() as conn:
            conn.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (token,))
            # swapped: the old partitions (and their triggers) go; else what was built
            for t in ([o.name for o in hit] if swapped else [n.name for n in built]):
                _drop(conn, "table", t)
            if not swapped:
                for old in hit:
                    _drop(conn, "trigger", _trigger(token), old.name)
            conn.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (token,))
    return {"plan": planned, "timings": dict(s.timings), **report}


ACTIVE = "link_rows_rebuilds"     # the tokens of the rebuilds running
RETIRED = "link_rows_retired"    # what a cleanup could not drop yet (a reader held it)


def _log_table(cur: Any) -> None:
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.{LOG} (
        token text NOT NULL, link_id text NOT NULL, link_type text NOT NULL)""")
    cur.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{ACTIVE} (token text PRIMARY KEY)")
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.{RETIRED} (
        kind text NOT NULL, name text NOT NULL, on_table text NOT NULL DEFAULT '',
        PRIMARY KEY (kind, name, on_table))""")
    # a trigger left behind (its drop waited on a reader) logs nothing once its rebuild ended
    cur.execute(f"""CREATE OR REPLACE FUNCTION {SCHEMA}.link_rows_log() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.{ACTIVE} WHERE token = TG_ARGV[0]) THEN
                RETURN NULL;
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                INSERT INTO {SCHEMA}.{LOG} VALUES (TG_ARGV[0], OLD.link_id, OLD.link_type);
            END IF;
            IF TG_OP IN ('UPDATE', 'INSERT') THEN
                INSERT INTO {SCHEMA}.{LOG} VALUES (TG_ARGV[0], NEW.link_id, NEW.link_type);
            END IF;
            RETURN NULL;
        END $$""")
    # a TRUNCATE logs no row: it is marked (an empty id), and undo refuses after one
    cur.execute(f"""CREATE OR REPLACE FUNCTION {SCHEMA}.link_rows_log_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM {SCHEMA}.{ACTIVE} WHERE token = TG_ARGV[0]) THEN
                INSERT INTO {SCHEMA}.{LOG} VALUES (TG_ARGV[0], '', '');
            END IF;
            RETURN NULL;
        END $$""")


def _drop(conn: Any, kind: str, name: str, on_table: str = "",
          lock_timeout_ms: int = 2000) -> bool:
    """Drop a table or trigger without queueing readers behind the drop's lock: a reader
    holding it (an open transaction) makes it wait in ``RETIRED`` for :func:`sweep`."""
    import psycopg
    sql = (f"DROP TABLE IF EXISTS {SCHEMA}.{q(name)}" if kind == "table" else
           f"DROP TRIGGER IF EXISTS {q(name)} ON {SCHEMA}.{q(on_table)}")
    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
            conn.execute(sql)
            conn.execute(f"DELETE FROM {SCHEMA}.{RETIRED} WHERE kind = %s AND name = %s AND "
                         "on_table = %s", (kind, name, on_table))
        return True
    except psycopg.errors.LockNotAvailable:
        conn.execute(f"INSERT INTO {SCHEMA}.{RETIRED} VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                     (kind, name, on_table))
        return False
    except psycopg.errors.UndefinedTable:   # a trigger's table already gone
        conn.execute(f"DELETE FROM {SCHEMA}.{RETIRED} WHERE kind = %s AND name = %s AND "
                     "on_table = %s", (kind, name, on_table))
        return True


def sweep(conn: Any) -> int:
    """Drop what earlier cleanups left in ``RETIRED``; the number still held."""
    rows = conn.execute(f"SELECT kind, name, on_table FROM {SCHEMA}.{RETIRED}").fetchall()
    return sum(1 for k, n, t in rows if not _drop(conn, k, n, t))


def _trigger(token: str) -> str:
    return q("log_" + token.replace("-", "_"))


def _watch(cur: Any, table: str, token: str) -> None:
    cur.execute(f"CREATE TRIGGER {_trigger(token)} AFTER INSERT OR UPDATE "
                f"OR DELETE ON {SCHEMA}.{q(table)} FOR EACH ROW EXECUTE FUNCTION "
                f"{SCHEMA}.link_rows_log({lit(token)})")


def _delta_lock(cur: Any) -> bool:
    """The delta's advisory lock for this transaction, without waiting: False while a
    delta layer applies (the caller retries after it)."""
    cur.execute(f"SELECT pg_try_advisory_xact_lock({DELTA_LOCK})")
    return bool(cur.fetchone()[0])


def _catch_up(cur: Any, token: str, olds: Sequence[str], replaced: Sequence[str]) -> int:
    """Under the swap's lock, after the attach: the rows logged in the old partitions since
    the build began made again through the parent (routed to their partition); a logged row
    of a replaced type is :class:`Changed` (the change came from an older snapshot)."""
    cur.execute(f"SELECT DISTINCT link_id, link_type FROM {SCHEMA}.{LOG} WHERE token = %s",
                (token,))
    logged = cur.fetchall()
    if not logged:
        return 0
    if any(t in replaced for _, t in logged):
        raise Changed(f"{sum(1 for _, t in logged if t in replaced)} rows of replaced types "
                      "written during the rebuild")
    ids, types = [i for i, _ in logged], [t for _, t in logged]
    # the logged (id, type) pairs only: never a row of a partition this swap left alone
    pairs = "(SELECT * FROM unnest(%s::text[], %s::text[]) AS g(link_id, link_type))"
    cur.execute(f"DELETE FROM {SCHEMA}.{PARENT} l USING {pairs} g WHERE l.link_id = g.link_id "
                "AND l.link_type = g.link_type", (ids, types))
    for old in olds:
        cur.execute(f"INSERT INTO {SCHEMA}.{PARENT} ({', '.join(COLUMNS)}) SELECT "
                    f"{', '.join('o.' + c for c in COLUMNS)} FROM {SCHEMA}.{q(old)} o "
                    f"JOIN {pairs} g ON g.link_id = o.link_id AND g.link_type = o.link_type",
                    (ids, types))
    return len(ids)


def _retry(s: Session, what: str, attempt_fn: Callable[[Any, Any], dict[str, Any]], *,
           lock_timeout_ms: int, tries: int) -> dict[str, Any]:
    """``attempt_fn(conn, cur)`` in one transaction holding the delta's lock and a
    ``lock_timeout``; retried while a delta applies or a reader holds the table. A delta
    apply waits on it for the transaction's milliseconds at most."""
    import psycopg
    waited = 0
    for attempt in range(1, tries + 1):
        try:
            with s.connect(autocommit=False) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
                if not _delta_lock(cur):
                    conn.rollback()
                    waited += 1
                    s.log(f"{what}: a delta applies (attempt {attempt}), retrying")
                    time.sleep(min(10, attempt))
                    continue
                t = time.monotonic()
                out = attempt_fn(conn, cur)
                held = time.monotonic() - t
                conn.commit()
                return {f"{what}_attempts": attempt, f"{what}_locked_s": round(held, 3),
                        f"{what}_waited_for_delta": waited, **out}
        except psycopg.errors.LockNotAvailable:
            s.log(f"{what}: table busy (attempt {attempt}), retrying")
            time.sleep(min(10, attempt))
    raise Refused(f"{what}_lock_not_acquired")


def swap(s: Session, olds: Sequence[str], news: Sequence[Partition], *, token: str,
         replaced: Sequence[str] = (), lock_timeout_ms: int = 2000,
         tries: int = 30) -> dict[str, Any]:
    """One transaction: the old partitions detached, the new ones attached (implied
    bounds, matching indexes: no scan, no build), the logged rows caught up."""
    def attempt(conn: Any, cur: Any) -> dict[str, Any]:
        for old in olds:
            cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} DETACH PARTITION {SCHEMA}.{q(old)}")
        for new in news:
            cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} ATTACH PARTITION "
                        f"{SCHEMA}.{q(new.name)} {_bound(new.types)}")
        return {"caught_up": _catch_up(cur, token, olds, replaced)}
    return _retry(s, "swap", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries)


# --- conversion of the served table -----------------------------------------------------

def type_counts(cur: Any, table: str | None = None) -> dict[str, int]:
    """Exact rows per link type, from the type index alone (``link_rows_by_type``, ~0.5 %
    of the table): the planner's sample misses rare types."""
    cur.execute("SET enable_seqscan = off")
    cur.execute(f"SELECT link_type, count(*) FROM {table or SCHEMA + '.' + PARENT} GROUP BY 1")
    out = dict(cur.fetchall())
    cur.execute("RESET enable_seqscan")
    return out


def build_target(s: Session, new: str, counts: Mapping[str, int], *, source: str,
                 ranges: Sequence[tuple[int, int | None]], own_min: int | None = None,
                 buckets: int = BUCKETS, options: str = "",
                 between: Callable[[], Any] | None = None) -> list[Partition]:
    """The partitioned table ``new`` beside the served one, filled from ``source``'s block
    ``ranges`` (routed by the parent to the partitions), its indexes built per partition
    side by side, primary keys, parent indexes (attached, none built) and statistics.
    ``between``: called after the fill, before the indexes (the conversion's first
    catch-up)."""
    with s.connect() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{q(new)} CASCADE")
        cur.execute(f"CREATE TABLE {SCHEMA}.{q(new)} (LIKE {SCHEMA}.{PARENT} INCLUDING DEFAULTS "
                    "INCLUDING CONSTRAINTS) PARTITION BY LIST (link_type)")
        gen = _gen(cur)
        parts = [Partition(part_name(t, gen, b), t, bucket=b)
                 for t, b in groups(counts, own_min=own_min, buckets=buckets)]
        parts.append(Partition(part_name(None, gen), None))
        named = [t for p in parts if p.types for t in p.types]
        for p in parts:
            cur.execute(f"CREATE TABLE {SCHEMA}.{p.name} PARTITION OF {SCHEMA}.{new} "
                        f"{_bound(p.types)}{_building(options)}")
            if p.types is None:
                cur.execute(f"ALTER TABLE {SCHEMA}.{p.name} ADD CONSTRAINT {p.name}_ck "
                            f"CHECK ({_check(None, named)})")
    s.timed("fill", lambda: s.run(fill_sqls(f"{SCHEMA}.{new}", source, None, 0,
                                            ranges=ranges)))
    if between is not None:
        between()
    s.timed("index", lambda: s.run([x for p in parts for x in _index_sqls(p.name)],
                                   workers=max(s.workers, 6)))
    s.timed("pkey", lambda: s.run([_pkey_sql(p.name) for p in parts]))
    with s.connect() as conn, conn.cursor() as cur:
        s.timed("parent_index", lambda: parent_indexes(cur, new))
    s.timed("vacuum", lambda: s.run([_settle(p.name) for p in parts] +
                                    [f"ANALYZE {SCHEMA}.{new}"]))
    return parts


def convert_plan(cur: Any, *, workers: int = 8, budget_s: float = BUDGET_S,
                 counts: Mapping[str, int] | None = None,
                 rates: Mapping[str, float] = RATES) -> dict[str, Any]:
    """The conversion projected (rows, the whole heap read, the new copy's bytes beside
    the old table) and gated, nothing written; :class:`OverBudget` past the time budget
    or the free disk less the reserve, :class:`Refused` when there is nothing to convert."""
    if is_partitioned(cur):
        raise Refused("already_partitioned")
    if _exists(cur, OLD):
        raise Refused(f"{OLD}_exists")
    counts = dict(counts) if counts is not None else type_counts(cur)
    total = sum(counts.values())
    pages = _pages(cur, f"{SCHEMA}.{PARENT}")
    proj = project(total, workers=workers, read_bytes=pages * 8192, rates=rates)
    free = free_bytes(cur)
    out = {"rows": total, "types": len(counts), "partitions": len(groups(counts)) + 1,
           "read_bytes": pages * 8192, "free_bytes": free, **proj}
    gate(proj, budget_s=budget_s, free=free)
    return out


def convert(s: Session, *, own_min: int | None = None, buckets: int = BUCKETS,
            budget_s: float = BUDGET_S, lock_timeout_ms: int = 5000, tries: int = 120,
            counts: Mapping[str, int] | None = None, parts_per_worker: int = 32
            ) -> dict[str, Any]:
    """The served unpartitioned ``link_rows`` copied into a partitioned table and renamed
    into its place (module docstring). Readers read the old table until the rename;
    writers may keep writing (their rows are logged and caught up in the rename's
    transaction). The fill is ``workers * parts_per_worker`` statements, each starting only
    while no delta applies."""
    new = f"{PARENT}_part"
    token = "convert"
    with s.connect() as conn, conn.cursor() as cur:
        if is_partitioned(cur):
            raise Refused("already_partitioned")
        if _exists(cur, OLD):
            raise Refused(f"{OLD}_exists")
        # the new table would not carry grants or row security (LIKE copies neither)
        cur.execute("SELECT relacl IS NOT NULL OR relrowsecurity FROM pg_class WHERE oid = "
                    "%s::regclass", (f"{SCHEMA}.{PARENT}",))
        if cur.fetchone()[0]:
            raise Refused("grants_or_row_security_not_copied")
        counts = dict(counts) if counts is not None else type_counts(cur)
        proj = convert_plan(cur, workers=s.workers, budget_s=budget_s, counts=counts)
        total, pages = proj["rows"], proj["read_bytes"] // 8192
        options = _reloptions(cur, f"{SCHEMA}.{PARENT}")
        _log_table(cur)
        cur.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (token,))
        cur.execute(f"INSERT INTO {SCHEMA}.{ACTIVE} VALUES (%s) ON CONFLICT DO NOTHING", (token,))
        # writers queue for the trigger's lock only as long as a writer holds the table
        cur.execute(f"SET lock_timeout = {int(lock_timeout_ms) * 6}")
        _watch(cur, PARENT, token)

    def first_catch_up() -> None:
        # a row updated while the ranges were read may have been copied twice (its old
        # and new version); its logged id is copied again once
        with s.connect() as conn, conn.cursor() as cur:
            s.timed("catch_up_1", lambda: _convert_catch_up(cur, new, token))
    try:
        parts = build_target(s, new, counts, source=f"{SCHEMA}.{PARENT}",
                             ranges=_ranges(pages, s.workers * parts_per_worker),
                             own_min=own_min, buckets=buckets, options=options,
                             between=first_catch_up)
        report = s.timed("rename", lambda: _rename(s, new, token, lock_timeout_ms, tries))
    except BaseException:
        with s.connect() as conn:
            conn.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (token,))
            _drop(conn, "trigger", _trigger(token), PARENT)
            _drop(conn, "table", new)
            conn.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (token,))
        raise
    return {"partitions": len(parts), "rows": total, "projected": proj,
            "timings": dict(s.timings), "yielded_to_delta_s": round(s.yielded_s, 1), **report}


def _convert_catch_up(cur: Any, new: str, token: str, *, source: str | None = None) -> int:
    """The logged ids taken out of the log and made again in ``new`` from ``source`` (the
    served table): every row of each id, whatever its type."""
    source = source or f"{SCHEMA}.{PARENT}"
    cur.execute(f"WITH g AS (DELETE FROM {SCHEMA}.{LOG} WHERE token = %s RETURNING link_id) "
                "SELECT coalesce(array_agg(DISTINCT link_id), '{}') FROM g", (token,))
    ids = cur.fetchone()[0]
    if ids:
        cur.execute(f"DELETE FROM {SCHEMA}.{new} WHERE link_id = ANY(%s)", (ids,))
        cur.execute(f"INSERT INTO {SCHEMA}.{new} ({', '.join(COLUMNS)}) SELECT "
                    f"{', '.join(COLUMNS)} FROM {source} WHERE link_id = ANY(%s)", (ids,))
    return len(ids)


def _indexes_of(cur: Any, table: str) -> list[str]:
    cur.execute("SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = %s::regclass ORDER BY 1", (f"{SCHEMA}.{table}",))
    return [r[0] for r in cur.fetchall()]


def _rename_indexes(cur: Any, table: str, old_prefix: str, new_prefix: str) -> None:
    """Every index of ``table`` named ``<old_prefix>_...`` renamed ``<new_prefix>_...``
    (the primary key's constraint with its index)."""
    for name in _indexes_of(cur, table):
        if name.startswith(old_prefix + "_"):
            cur.execute(f"ALTER INDEX {SCHEMA}.{q(name)} RENAME TO "
                        f"{q(new_prefix + name[len(old_prefix):])}")


UNDO = "undo"   # the token of the writes logged on the partitioned table while undo holds


def _rename(s: Session, new: str, token: str, lock_timeout_ms: int, tries: int) -> dict[str, Any]:
    def attempt(conn: Any, cur: Any) -> dict[str, Any]:
        cur.execute(f"LOCK TABLE {SCHEMA}.{PARENT} IN EXCLUSIVE MODE")
        caught = _convert_catch_up(cur, new, token)
        cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (f"{SCHEMA}.links",))
        view = cur.fetchone()[0]
        cur.execute(f"DROP TRIGGER IF EXISTS {_trigger(token)} ON {SCHEMA}.{PARENT}")
        cur.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (token,))
        cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} RENAME TO {OLD}")
        _rename_indexes(cur, OLD, PARENT, OLD)
        cur.execute(f"ALTER TABLE {SCHEMA}.{new} RENAME TO {PARENT}")
        _rename_indexes(cur, PARENT, new, PARENT)
        cur.execute(f"CREATE OR REPLACE VIEW {SCHEMA}.links AS {view}")
        # every write from now on is logged until finish(), so undo() keeps it
        cur.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (UNDO,))
        cur.execute(f"INSERT INTO {SCHEMA}.{ACTIVE} VALUES (%s) ON CONFLICT DO NOTHING", (UNDO,))
        _watch(cur, PARENT, UNDO)
        cur.execute(f"CREATE TRIGGER {_trigger(UNDO)}_truncate AFTER TRUNCATE ON "
                    f"{SCHEMA}.{PARENT} FOR EACH STATEMENT EXECUTE FUNCTION "
                    f"{SCHEMA}.link_rows_log_truncate({lit(UNDO)})")
        return {"caught_up": caught}
    return _retry(s, "rename", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries)


def undo(s: Session, *, lock_timeout_ms: int = 5000, tries: int = 120) -> dict[str, Any]:
    """The conversion taken back while ``link_rows_unpartitioned`` is kept: the writes
    logged on the partitioned table since the rename copied into it, then the tables
    renamed back in one transaction under the delta's lock. The partitioned table is kept
    as ``link_rows_part`` (drop it by hand once the old table serves again)."""
    new = f"{PARENT}_part"

    def attempt(conn: Any, cur: Any) -> dict[str, Any]:
        if not is_partitioned(cur) or not _exists(cur, OLD):
            raise Refused("nothing_to_undo")
        cur.execute(f"LOCK TABLE {SCHEMA}.{PARENT} IN EXCLUSIVE MODE")
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.{LOG} WHERE token = %s AND "
                    "link_id = '')", (UNDO,))
        if cur.fetchone()[0]:
            # a reload truncated the table since: its rows are not in the log
            raise Refused("truncated_since_the_conversion: undo would lose rows")
        cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (f"{SCHEMA}.links",))
        view = cur.fetchone()[0]
        cur.execute(f"DROP TRIGGER IF EXISTS {_trigger(UNDO)} ON {SCHEMA}.{PARENT}")
        cur.execute(f"DROP TRIGGER IF EXISTS {_trigger(UNDO)}_truncate ON {SCHEMA}.{PARENT}")
        cur.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (UNDO,))
        # the old table takes the logged ids' rows as the partitioned one holds them now
        caught = _convert_catch_up(cur, OLD, UNDO, source=f"{SCHEMA}.{PARENT}")
        cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} RENAME TO {new}")
        _rename_indexes(cur, new, PARENT, new)
        cur.execute(f"ALTER TABLE {SCHEMA}.{OLD} RENAME TO {PARENT}")
        _rename_indexes(cur, PARENT, OLD, PARENT)
        cur.execute(f"CREATE OR REPLACE VIEW {SCHEMA}.links AS {view}")
        return {"caught_up": caught}
    return _retry(s, "undo", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries)


def finish(s: Session) -> dict[str, Any]:
    """The conversion made final: the undo log's trigger and rows and the old table
    dropped (its sequence ownership is none; the row numbers' sequence stays)."""
    with s.connect() as conn, conn.cursor() as cur:
        if not is_partitioned(cur) or not _exists(cur, OLD):
            raise Refused("nothing_to_finish")
        cur.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (UNDO,))
        dropped_trigger = _drop(conn, "trigger", _trigger(UNDO), PARENT)
        dropped_trigger &= _drop(conn, "trigger", _trigger(UNDO) + "_truncate", PARENT)
        cur.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (UNDO,))
        dropped = _drop(conn, "table", OLD)
    return {"dropped": dropped, "trigger_dropped": dropped_trigger}


def audit(cur: Any) -> dict[str, Any]:
    """Link ids held by more than one row (any types), read in id order from the
    primary keys; and rows sitting in the default partition."""
    cur.execute(f"SELECT count(*) FROM (SELECT 1 FROM {SCHEMA}.{PARENT} GROUP BY link_id "
                f"HAVING count(*) > 1) d")
    dup = cur.fetchone()[0]
    default = 0
    if is_partitioned(cur):
        for p in layout(cur):
            if p.types is None:
                cur.execute(f"SELECT count(*) FROM {SCHEMA}.{q(p.name)}")
                default += cur.fetchone()[0]
    return {"ids_held_twice": dup, "default_rows": default}


# --- command line -----------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.link_partitions")
    ap.add_argument("command", choices=("layout", "plan", "rebuild", "convert", "audit",
                                        "undo", "finish"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--type", action="append", default=[], dest="types")
    ap.add_argument("--source", default=None, help="table or (SELECT ...) of the new rows")
    ap.add_argument("--keep", default="FALSE", help="SQL: replaced-type rows to keep")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget-s", type=float, default=BUDGET_S)
    ap.add_argument("--plan-only", action="store_true", help="convert: project and gate only")
    a = ap.parse_args(argv)
    dsn = a.dsn or config.dsn()
    s = Session(dsn, workers=a.workers, log=lambda m: print(m, file=sys.stderr))
    try:
        if a.command == "layout":
            with s.connect() as conn, conn.cursor() as cur:
                out: Any = [p.__dict__ for p in layout(cur)]
        elif a.command == "plan":
            with s.connect() as conn, conn.cursor() as cur:
                out = plan(cur, Rebuild(tuple(a.types), a.source, keep=a.keep),
                           workers=a.workers, budget_s=a.budget_s)
        elif a.command == "rebuild":
            if not a.types:
                raise Refused("no_type")
            if not a.source:
                # an empty type is asked for by an empty source, never by omission
                raise Refused("no_source")
            out = rebuild(s, Rebuild(tuple(a.types), a.source, keep=a.keep), budget_s=a.budget_s)
        elif a.command == "convert" and a.plan_only:
            with s.connect() as conn, conn.cursor() as cur:
                out = convert_plan(cur, workers=a.workers, budget_s=a.budget_s)
        elif a.command == "convert":
            out = convert(s, budget_s=a.budget_s)
        elif a.command == "audit":
            with s.connect() as conn, conn.cursor() as cur:
                out = audit(cur)
        elif a.command == "undo":
            out = undo(s)
        else:
            out = finish(s)
    except (OverBudget, Refused, Changed) as exc:
        print(json.dumps({"refused": type(exc).__name__, "why": str(exc)}))
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
