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
         "probe_rows_s": 80_000.0, "swap_s": 5.0, "io_bytes_s": 4e8,
         # the conversion with its texts packed (GA-F11): PACKED_RATES
         "pack_rows_s_per_worker": 2_500.0, "packed_bytes_per_row": 900.0}


def packed_rates(rates: Mapping[str, float] = RATES) -> dict[str, float]:
    """``rates`` for a fill that packs: its rows a second a process, and the bytes a row
    takes with its share of the new texts and skeletons."""
    return {**rates, "fill_rows_s_per_worker": rates["pack_rows_s_per_worker"],
            "bytes_per_row": rates["packed_bytes_per_row"]}
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


# --- packing the texts (GA-F11) ---------------------------------------------------------
#
# A row's ``reason`` and ``evidence_handles`` stored plain (99 % of the served table) are
# written packed: a skeleton id each and the slot text ids (``slots``, the evidence's
# first ``ek``), the layout of migration 0007 that ``ontology.links`` formats back
# (``ontology.store.pack_link_texts``, the SQLite store's own packing, checks that the
# skeleton formatted with the slots gives the texts back exactly). Every reader reads the
# texts through the view or unpacks them the view's way (``migrate_chunks._served_links``,
# ``ontology_reader``'s slices), so no reader changes; a row the packing leaves plain is
# read as before.
#
# Many processes pack side by side, so a text's id is its hash, not a sequence number:
# the high 61 bits of its SHA-256 above 2^62 (never a sequence's id; a sequence set past
# them still has 2^61 left). A text whose id another text already holds (a hash collision,
# about 1 in 2,000 at 50 M texts) is never stored: its row stays plain. A slot text a
# slice reader would read the slow way (``ontology_reader._ODD_TEXT``: a quote, backslash
# or control character; more than ``LINK_ODD_CAP`` of them sends every whole-store link
# slice to the generic read) keeps its row plain too. Texts are only ever added: ``undo``
# copies packed rows into the old table, whose rows then need them.

PACK_ID_BASE = 1 << 62
PACK_BATCH = 5000
_ODD_TEXT = re.compile(r'["\\\x01-\x1f]')


def text_ids(text: str) -> tuple[int, int]:
    """A link text's or skeleton's id (its SHA-256's high 61 bits above 2^62) and the
    hash's next 64 bits (a check: two texts with one id differ there)."""
    h = hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()
    return PACK_ID_BASE | (int.from_bytes(h[:8], "big") >> 3), int.from_bytes(h[8:16], "big")


def text_id(text: str) -> int:
    return text_ids(text)[0]


_AT = {c: i for i, c in enumerate(COLUMNS)}


@dataclass
class Packed:
    row: tuple[Any, ...]                      # the row as written, texts packed
    skeletons: list[tuple[int, int, str]]     # (id, check, text): reason's, evidence's
    texts: list[tuple[int, int, str]]         # (id, check, text) of each slot, in order


def packed_row(row: Sequence[Any]) -> Packed | None:
    """``row`` (:data:`COLUMNS` order) with its texts packed; None when it stays as it is:
    already packed, a text not text, nothing to slot, the packing not exact, or a slot
    text that is odd."""
    from ontology import store
    a = _AT
    if row[a["reason_id"]] is not None or row[a["evidence_id"]] is not None:
        return None
    got = store.pack_link_texts(row[a["reason"]], row[a["evidence_handles"]])
    if got is None:
        return None
    reason, evidence, texts, used = got
    if any(_ODD_TEXT.search(t) for t in texts):
        return None
    skeletons = [(*text_ids(t), t) for t in (reason, evidence)]
    slots = [(*text_ids(t), t) for t in texts]
    out = list(row)
    out[a["reason_id"]], out[a["evidence_id"]] = skeletons[0][0], skeletons[1][0]
    out[a["ek"]], out[a["slots"]] = used, [x[0] for x in slots]
    out[a["reason"]] = out[a["evidence_handles"]] = None
    return Packed(tuple(out), skeletons, slots)


def _intern(cur: Any, schema: str, table: str, texts: Mapping[int, str]) -> set[int]:
    """``texts`` (id -> text) added to ``<schema>.<table>`` where missing; the ids another
    text holds (collisions). Inserted in id order (two packers adding the same ids wait
    for each other in one order, never in a cycle)."""
    if not texts:
        return set()
    stage = f"pack_stage_{table}"
    cur.execute(f"CREATE TEMP TABLE IF NOT EXISTS {stage} (id bigint, text text) "
                "ON COMMIT DELETE ROWS")
    with cur.copy(f"COPY {stage} (id, text) FROM STDIN") as cp:
        for k, v in texts.items():
            cp.write_row((k, v))
    cur.execute(f"INSERT INTO {schema}.{table} (id, text) SELECT id, text FROM {stage} "
                "ORDER BY id ON CONFLICT (id) DO NOTHING")
    cur.execute(f"SELECT s.id FROM {stage} s JOIN {schema}.{table} t ON t.id = s.id "
                "WHERE t.text <> s.text")
    return {r[0] for r in cur.fetchall()}


@dataclass
class PackJob:
    dsn: str
    schema: str
    target: str            # schema-qualified table the rows go to
    source: str            # schema-qualified table they are read from
    lo: int
    hi: int | None
    yield_to_delta: bool = True
    yield_poll_s: float = 5.0
    batch: int = PACK_BATCH
    where: str = "TRUE"     # SQL over the source's rows: the ones copied


def pack_range(job: PackJob) -> dict[str, int]:
    """One block range of ``job.source`` copied into ``job.target`` with its texts packed
    (a pool task: its own connections). Each batch: its new texts interned and committed,
    then its rows copied and committed; a batch starts only while no delta applies."""
    import psycopg
    if job.schema != SCHEMA:
        raise Refused(f"pack_schema_differs:{job.schema}")
    counts = {"rows": 0, "packed": 0, "texts": 0, "collided": 0, "yielded_ms": 0}
    known: dict[int, int] = {}   # id -> check of the texts this process saw stored

    def connect(autocommit: bool) -> Any:
        conn = psycopg.connect(job.dsn, autocommit=autocommit)
        conn.execute("SET synchronous_commit = off")
        return conn

    with connect(False) as rconn, connect(False) as wconn, connect(True) as poll:
        cols = ", ".join(COLUMNS)
        read = rconn.cursor(name="pack_range")
        read.itersize = job.batch
        read.execute(f"SELECT {cols} FROM {job.source} s WHERE {_ctid(job.lo, job.hi)} "
                     f"AND ({job.where})")
        while True:
            rows = read.fetchmany(job.batch)
            if not rows:
                break
            if job.yield_to_delta:
                t = time.monotonic()
                with poll.cursor() as pc:
                    while delta_applying(pc):
                        time.sleep(job.yield_poll_s)
                counts["yielded_ms"] += int((time.monotonic() - t) * 1000)
            packs = [packed_row(r) for r in rows]
            want: dict[str, dict[int, str]] = {"link_skeletons": {}, "link_texts": {}}
            check: dict[int, int] = {}
            clash: set[int] = set()
            for got in packs:
                if got is None:
                    continue
                for table, items in (("link_skeletons", got.skeletons),
                                     ("link_texts", got.texts)):
                    for kid, chk, txt in items:
                        seen = known.get(kid, check.get(kid))
                        if seen is not None and seen != chk:
                            clash.add(kid)      # another text holds the id
                            continue
                        check[kid] = chk
                        if kid not in known:
                            want[table][kid] = txt
            with wconn.cursor() as cur:
                for attempt in range(1, 6):
                    try:
                        found = set()
                        for table, texts in want.items():
                            found |= _intern(cur, job.schema, table, texts)
                        wconn.commit()
                        break
                    except psycopg.errors.DeadlockDetected:
                        wconn.rollback()
                        if attempt == 5:
                            raise
                        time.sleep(0.1 * attempt)
                clash |= found
                counts["texts"] += sum(len(t) for t in want.values())
                if len(known) > 500_000:
                    known.clear()
                known.update((k, v) for k, v in check.items() if k not in clash)
                with cur.copy(f"COPY {job.target} ({cols}) FROM STDIN") as cp:
                    for row, got in zip(rows, packs):
                        if got is not None:
                            if not clash.intersection(
                                    x[0] for x in (*got.skeletons, *got.texts)):
                                cp.write_row(got.row)
                                counts["packed"] += 1
                                continue
                            counts["collided"] += 1
                        cp.write_row(row)
                wconn.commit()
            counts["rows"] += len(rows)
        rconn.rollback()
    return counts


def pack_jobs(s: "Session", jobs: Sequence[PackJob]) -> dict[str, int]:
    """:func:`pack_range` of each job, ``s.workers`` processes side by side."""
    import multiprocessing
    from ontology import parent_watch
    total: dict[str, int] = {}
    if not jobs:
        return total
    with multiprocessing.get_context("spawn").Pool(
            max(1, min(s.workers, len(jobs))), **parent_watch.pool_init()) as pool:
        for got in pool.imap_unordered(pack_range, jobs):
            for k, v in got.items():
                total[k] = total.get(k, 0) + v
    s.yielded_s += total.get("yielded_ms", 0) / 1000
    return total


def _texts_sql(alias: str) -> tuple[str, str]:
    """The view's reason and evidence expressions over the rows ``alias``."""
    return (f"coalesce({alias}.reason, {SCHEMA}.link_text({alias}.reason_id, {alias}.slots, "
            f"{alias}.ek + 1, 64))",
            f"coalesce({alias}.evidence_handles, {SCHEMA}.link_text({alias}.evidence_id, "
            f"{alias}.slots, 1, {alias}.ek))")


def verify_sample(cur: Any, new: str, source: str, token: str, *, percent: float = 1.0
                  ) -> dict[str, int]:
    """A block sample of ``new``'s rows compared with ``source``'s rows of the same key:
    every column the view gives (the texts as the view formats them) equal. Rows a writer
    changed since the fill (logged under ``token``) are left to the catch-up. A table of
    under 10,000 blocks is compared whole (a sample of a small one may hold no block)."""
    if _pages(cur, f"{SCHEMA}.{q(new)}") < 10_000:
        percent = 100.0
    nr, ne = _texts_sql("n")
    orr, oe = _texts_sql("o")
    plain = [c for c in COLUMNS if c not in ("reason_id", "evidence_id", "ek", "slots",
                                             "reason", "evidence_handles")]
    same = " AND ".join([f"n.{c} IS NOT DISTINCT FROM o.{c}" for c in plain] +
                        [f"{nr} IS NOT DISTINCT FROM {orr}", f"{ne} IS NOT DISTINCT FROM {oe}"])
    cur.execute(
        f"SELECT count(*), count(*) FILTER (WHERE o.link_id IS NULL OR NOT ({same})), "
        "count(*) FILTER (WHERE n.reason_id IS NOT NULL) "
        f"FROM {SCHEMA}.{q(new)} n TABLESAMPLE SYSTEM (%s) LEFT JOIN {source} o "
        "ON o.link_id = n.link_id AND o.link_type = n.link_type "
        f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.{LOG} g WHERE g.token = %s "
        "AND g.link_id = n.link_id)", (percent, token))
    checked, differ, packed = cur.fetchone()
    return {"checked": checked, "differ": differ, "packed": packed}


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
    # GA-F11: the kept rows' texts packed on the way (:func:`pack_range`)
    pack: bool = False

    @property
    def replaces(self) -> tuple[str, ...]:
        """The types whose rows a writer may not change during the build: none when every
        row is kept and nothing added (a repack: a logged row is caught up)."""
        return () if self.source is None and self.keep.strip().upper() == "TRUE" \
            else self.types


def repack(types: Sequence[str]) -> "Rebuild":
    """The partitions holding ``types`` written again with every row kept, texts packed."""
    return Rebuild(tuple(types), keep="TRUE", pack=True)


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
    if _exists(cur, OLD) and change.replaces:
        # a change's rows would escape the conversion's undo log; a repack's do not: its
        # catch-up writes through the parent (logged), and the rows it copies read the same
        raise Refused("conversion_not_finished: undo or finish it first")
    unknown = [t for t in change.types if not isinstance(t, str) or not t]
    if unknown or not change.types:
        raise Refused("rebuild_without_types")
    parts = layout(cur)
    placed = _placement(cur, change, parts, gen, rates)
    pending = set(_undo_pending(cur))
    held = [p.name for p in placed.hit if p.name in pending]
    if held:
        raise Refused(f"rebuild_undo_open:{','.join(held)}: rebuild-finish or rebuild-undo first")
    if change.pack:
        rates = packed_rates(rates)
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
            lock_timeout_ms: int = 2000, tries: int = 30, keep_old: bool = False,
            verify_percent: float = 1.0) -> dict[str, Any]:
    """Rebuild the partitions holding ``change.types`` beside the served ones and swap
    them in (module docstring). Returns the plan, the timings and the counts. Refused,
    changed or failed, it leaves the served partitions as they were and drops what it
    built. ``change.pack``: the old partitions' kept rows go through :func:`pack_range`
    (``s.workers`` processes), then a ``verify_percent`` block sample of each new
    partition is compared with the old rows as the view reads them (one difference and
    nothing is swapped). ``keep_old``: the old partitions are kept detached and every
    write to the new ones logged, until :func:`rebuild_finish` (or :func:`rebuild_undo`
    swaps the old ones back with those writes)."""
    if change.pack and change.replaces:
        # a packed partition is checked row for row against the rows it was read from
        raise Refused("pack_is_for_a_repack_only")
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
        packs: list[PackJob] = []
        for new in built:
            own = _check(new.types, placed.named_after)
            for old in hit:
                # only an old partition that can hold the new one's rows
                if old.types is not None and new.types is not None and \
                        not set(old.types) & set(new.types):
                    continue
                if change.pack:
                    packs += [PackJob(s.dsn, SCHEMA, f"{SCHEMA}.{new.name}",
                                      f"{SCHEMA}.{old.name}", lo, hi, s.yield_to_delta,
                                      s.yield_poll_s, where=f"({keep}) AND ({own})")
                              for lo, hi in _ranges(pages[old.name], s.workers * 8)]
                    continue
                fills += fill_sqls(f"{SCHEMA}.{new.name}", f"{SCHEMA}.{old.name}",
                                   pages[old.name], s.workers, f"({keep}) AND ({own})")
            if change.source is not None:
                fills += fill_sqls(f"{SCHEMA}.{new.name}", change.source, source_pages,
                                   s.workers, own)
        packed = s.timed("fill", lambda: (pack_jobs(s, packs), s.run(fills))[0])
        # a row a writer changed while the ranges were read may have been copied twice (its
        # old and new version, two ranges' snapshots): every logged row made again once
        # from the old partitions before the unique keys are built (the swap catches up
        # what is written after)
        with s.connect() as conn, conn.cursor() as cur:
            s.timed("catch_up_1", lambda: _build_catch_up(cur, token, hit, built,
                                                          placed.named_after, keep))
        checked: dict[str, int] = {}
        if change.pack and verify_percent > 0:
            def check() -> None:
                with s.connect() as conn, conn.cursor() as cur:
                    for new in built:
                        got = verify_sample(cur, new.name, f"{SCHEMA}.{PARENT}", token,
                                            percent=verify_percent)
                        for k, v in got.items():
                            checked[k] = checked.get(k, 0) + v
            s.timed("verify", check)
            if checked.get("differ"):
                raise Refused(f"packed_rows_differ:{checked['differ']}")
        s.timed("index", lambda: s.run([x for n in built for x in _index_sqls(n.name)],
                                       workers=max(s.workers, 6)))
        s.timed("vacuum", lambda: s.run([(_pkey_sql(n.name), *_settle(n.name))
                                         for n in built]))
        foreign = s.timed("id_check", lambda: _foreign_ids(s, change, placed))
        if foreign:
            raise Refused(f"link_id_held_by_another_type:{foreign}")
        undo_token = f"undo-{gen}" if keep_old else None
        report = s.timed("swap", lambda: swap(s, [o.name for o in hit], built, token=token,
                                             replaced=change.replaces,
                                             lock_timeout_ms=lock_timeout_ms, tries=tries,
                                             undo_token=undo_token, gen=gen,
                                             old_types={o.name: o.types for o in hit}))
        swapped = True
    finally:
        with s.connect() as conn:
            conn.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (token,))
            # swapped: the old partitions go (kept detached for undo with keep_old), their
            # build triggers with them; else what was built
            if not (swapped and keep_old):
                for t in ([o.name for o in hit] if swapped else [n.name for n in built]):
                    _drop(conn, "table", t)
            if not swapped or keep_old:
                for old in hit:
                    _drop(conn, "trigger", _trigger(token), old.name)
            conn.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (token,))
    return {"plan": planned, "timings": dict(s.timings), "packed": packed,
            "verified": checked, "kept_old": keep_old, "gen": gen, **report}


def _build_catch_up(cur: Any, token: str, olds: Sequence[Partition],
                    news: Sequence[Partition], named_after: Sequence[str], keep: str) -> int:
    """Before a rebuild's indexes: each logged (id, type) taken out of the new partitions
    and copied again from the old ones as they hold it now (as the fill would: the kept
    rows of each new partition's types). The log is kept for the swap's catch-up."""
    cur.execute(f"SELECT DISTINCT link_id, link_type FROM {SCHEMA}.{LOG} WHERE token = %s",
                (token,))
    logged = cur.fetchall()
    if not logged:
        return 0
    ids, types = [i for i, _ in logged], [t for _, t in logged]
    pairs = "(SELECT * FROM unnest(%s::text[], %s::text[]) AS g(link_id, link_type))"
    cols = ", ".join(COLUMNS)
    for new in news:
        cur.execute(f"DELETE FROM {SCHEMA}.{q(new.name)} l USING {pairs} g WHERE "
                    "l.link_id = g.link_id AND l.link_type = g.link_type", (ids, types))
        own = _check(new.types, named_after)
        for old in olds:
            # the old partition's own columns only in the filters (``keep`` and ``own`` name
            # them unqualified)
            cur.execute(f"INSERT INTO {SCHEMA}.{q(new.name)} ({cols}) SELECT {cols} FROM "
                        f"{SCHEMA}.{q(old.name)} WHERE (link_id, link_type) IN (SELECT "
                        f"link_id, link_type FROM {pairs} x) AND ({keep}) AND ({own})",
                        (ids, types))
    return len(ids)


ACTIVE = "link_rows_rebuilds"     # the tokens of the rebuilds running
RETIRED = "link_rows_retired"    # what a cleanup could not drop yet (a reader held it)


def _log_table(cur: Any) -> None:
    # Keyed by a surrogate ``seq`` (TRAIN2-B2): a migration's undo log
    # (``migrate_chunks._logged_tables``) refuses a keyless table in ``ontology``, and
    # no natural key fits, since an UPDATE logs its OLD and NEW (token, id, type), often
    # the same triple. BY DEFAULT, not ALWAYS, so that an undo can put a deleted log row
    # back with its own ``seq``. The table stays in ``ontology``: the writers' grants
    # cover it and their triggers insert into it.
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.{LOG} (
        token text NOT NULL, link_id text NOT NULL, link_type text NOT NULL,
        seq bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY)""")
    # A keyless table made before the key (DI-03) gets it once. The catalog is read
    # first so that later calls take no ALTER lock, which would queue the writers'
    # trigger inserts.
    cur.execute("SELECT NOT EXISTS (SELECT 1 FROM pg_index WHERE indisprimary AND "
                "indrelid = %s::regclass)", (f"{SCHEMA}.{LOG}",))
    if cur.fetchone()[0]:
        cur.execute(f"ALTER TABLE {SCHEMA}.{LOG} ADD COLUMN IF NOT EXISTS seq bigint "
                    "GENERATED BY DEFAULT AS IDENTITY")
        cur.execute(f"ALTER TABLE {SCHEMA}.{LOG} ADD PRIMARY KEY (seq)")
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
                INSERT INTO {SCHEMA}.{LOG} (token, link_id, link_type)
                    VALUES (TG_ARGV[0], OLD.link_id, OLD.link_type);
            END IF;
            IF TG_OP IN ('UPDATE', 'INSERT') THEN
                INSERT INTO {SCHEMA}.{LOG} (token, link_id, link_type)
                    VALUES (TG_ARGV[0], NEW.link_id, NEW.link_type);
            END IF;
            RETURN NULL;
        END $$""")
    # a TRUNCATE logs no row: it is marked (an empty id), and undo refuses after one
    cur.execute(f"""CREATE OR REPLACE FUNCTION {SCHEMA}.link_rows_log_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM {SCHEMA}.{ACTIVE} WHERE token = TG_ARGV[0]) THEN
                INSERT INTO {SCHEMA}.{LOG} (token, link_id, link_type)
                    VALUES (TG_ARGV[0], '', '');
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
         tries: int = 30, undo_token: str | None = None, gen: int = 0,
         old_types: Mapping[str, tuple[str, ...] | None] | None = None) -> dict[str, Any]:
    """One transaction: the old partitions detached, the new ones attached (implied
    bounds, matching indexes: no scan, no build), the logged rows caught up. With
    ``undo_token``: every later write to the new partitions logged and the swap recorded
    (:data:`UNDOS`) for :func:`rebuild_undo` / :func:`rebuild_finish`."""
    def attempt(conn: Any, cur: Any) -> dict[str, Any]:
        for old in olds:
            cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} DETACH PARTITION {SCHEMA}.{q(old)}")
        for new in news:
            cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} ATTACH PARTITION "
                        f"{SCHEMA}.{q(new.name)} {_bound(new.types)}")
        caught = _catch_up(cur, token, olds, replaced)
        if undo_token is not None:
            _undo_table(cur)
            cur.execute(f"INSERT INTO {SCHEMA}.{ACTIVE} VALUES (%s) ON CONFLICT DO NOTHING",
                        (undo_token,))
            for new in news:
                _watch(cur, new.name, undo_token)
                cur.execute(f"CREATE TRIGGER {_trigger(undo_token)}_truncate AFTER TRUNCATE ON "
                            f"{SCHEMA}.{q(new.name)} FOR EACH STATEMENT EXECUTE FUNCTION "
                            f"{SCHEMA}.link_rows_log_truncate({lit(undo_token)})")
            cur.execute(f"INSERT INTO {SCHEMA}.{UNDOS} VALUES (%s, %s, %s, %s, %s)",
                        (gen, undo_token, list(olds), [n.name for n in news],
                         json.dumps({o: (list(t) if t is not None else None)
                                     for o, t in (old_types or {}).items()})))
        return {"caught_up": caught}
    return _retry(s, "swap", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries)


UNDOS = "link_rows_rebuild_undo"   # rebuilds whose old partitions are kept for undo


def _undo_table(cur: Any) -> None:
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.{UNDOS} (
        gen bigint PRIMARY KEY, token text NOT NULL, olds text[] NOT NULL,
        news text[] NOT NULL, bounds jsonb NOT NULL)""")


def _undo_pending(cur: Any) -> list[str]:
    """The partitions an open rebuild undo names (its new ones served, its old ones kept)."""
    if not _exists(cur, UNDOS):
        return []
    cur.execute(f"SELECT coalesce(array_agg(n), '{{}}') FROM {SCHEMA}.{UNDOS}, "
                "unnest(news || olds) n")
    return list(cur.fetchone()[0])


def _undo_of(cur: Any, gen: int) -> tuple[str, list[str], list[str]]:
    if not _exists(cur, UNDOS):
        raise Refused("no_rebuild_undo")
    cur.execute(f"SELECT token, olds, news FROM {SCHEMA}.{UNDOS} WHERE gen = %s", (gen,))
    row = cur.fetchone()
    if row is None:
        raise Refused(f"no_rebuild_undo:{gen}")
    return row[0], list(row[1]), list(row[2])


def rebuild_undo(s: Session, gen: int, *, lock_timeout_ms: int = 2000,
                 tries: int = 30) -> dict[str, Any]:
    """A kept rebuild taken back: its old partitions attached again in place of the new
    ones, with every write logged on the new ones since the swap; the new ones dropped."""
    with s.connect() as conn, conn.cursor() as cur:
        token, olds, news = _undo_of(cur, gen)
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.{LOG} WHERE token = %s AND "
                    "link_id = '')", (token,))
        if cur.fetchone()[0]:
            raise Refused("truncated_since_the_rebuild: undo would lose rows")
        cur.execute(f"SELECT bounds FROM {SCHEMA}.{UNDOS} WHERE gen = %s", (gen,))
        bounds = {k: (tuple(v) if v is not None else None)
                  for k, v in cur.fetchone()[0].items()}
        if set(bounds) != set(olds):
            raise Refused("rebuild_undo_bounds_unknown")
        for old in olds:
            # the old partition's bound as a validated CHECK, so attaching it back scans
            # nothing under the swap's lock (it is detached: validating it holds no reader;
            # the default partition keeps the check naming the others)
            if bounds[old] is not None:
                ck = q(old[:50] + "_undo_ck")
                cur.execute(f"ALTER TABLE {SCHEMA}.{q(old)} DROP CONSTRAINT IF EXISTS {ck}")
                cur.execute(f"ALTER TABLE {SCHEMA}.{q(old)} ADD CONSTRAINT {ck} "
                            f"CHECK ({_check(bounds[old])}) NOT VALID")
                cur.execute(f"ALTER TABLE {SCHEMA}.{q(old)} VALIDATE CONSTRAINT {ck}")

    def attempt(conn: Any, cur: Any) -> dict[str, Any]:
        cur.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (token,))
        for new in news:
            cur.execute(f"DROP TRIGGER IF EXISTS {_trigger(token)} ON {SCHEMA}.{q(new)}")
            cur.execute(f"DROP TRIGGER IF EXISTS {_trigger(token)}_truncate ON "
                        f"{SCHEMA}.{q(new)}")
            cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} DETACH PARTITION {SCHEMA}.{q(new)}")
        for old in olds:
            cur.execute(f"ALTER TABLE {SCHEMA}.{PARENT} ATTACH PARTITION "
                        f"{SCHEMA}.{q(old)} {_bound(bounds[old])}")
        caught = _catch_up(cur, token, news, ())
        cur.execute(f"DELETE FROM {SCHEMA}.{UNDOS} WHERE gen = %s", (gen,))
        return {"caught_up": caught}
    out = _retry(s, "undo", attempt, lock_timeout_ms=lock_timeout_ms, tries=tries)
    with s.connect() as conn:
        for new in news:
            _drop(conn, "table", new)
        conn.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (token,))
    return out


def rebuild_finish(s: Session, gen: int) -> dict[str, Any]:
    """A kept rebuild made final: its old partitions, its triggers and log dropped."""
    with s.connect() as conn, conn.cursor() as cur:
        token, olds, news = _undo_of(cur, gen)
        cur.execute(f"DELETE FROM {SCHEMA}.{ACTIVE} WHERE token = %s", (token,))
        dropped = True
        for new in news:
            dropped &= _drop(conn, "trigger", _trigger(token), new)
            dropped &= _drop(conn, "trigger", _trigger(token) + "_truncate", new)
        for old in olds:
            dropped &= _drop(conn, "table", old)
        cur.execute(f"DELETE FROM {SCHEMA}.{LOG} WHERE token = %s", (token,))
        cur.execute(f"DELETE FROM {SCHEMA}.{UNDOS} WHERE gen = %s", (gen,))
    return {"gen": gen, "dropped": dropped}


REPACKED = "link_rows_repacked"   # partitions a repack wrote (a rerun skips them)


def kept_rebuilds(cur: Any) -> list[int]:
    if not _exists(cur, UNDOS):
        return []
    cur.execute(f"SELECT gen FROM {SCHEMA}.{UNDOS} ORDER BY gen")
    return [r[0] for r in cur.fetchall()]


def repack_all(s: Session, *, types: Sequence[str] = (), every: bool = False,
               budget_s: float = BUDGET_S, keep_old: bool = True,
               verify_percent: float = 1.0) -> dict[str, Any]:
    """GA-F11 on the partitioned table: each partition holding ``types`` (``every``: each
    partition naming types; the default holds none after the conversion) rebuilt alone
    with its texts packed (:func:`repack`), smallest first, each gated and swapped on its
    own; one already repacked (:data:`REPACKED`) is skipped, so a stopped pass resumes.
    ``keep_old``: each old partition kept detached for :func:`rebuild_undo` until
    :func:`rebuild_finish`."""
    if not types and not every:
        raise Refused("repack_without_types")
    with s.connect() as conn, conn.cursor() as cur:
        if not is_partitioned(cur):
            raise Refused("link_rows_not_partitioned")
        cur.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{REPACKED} (name text PRIMARY KEY, "
                    "gen bigint NOT NULL, at timestamptz NOT NULL DEFAULT now())")
        cur.execute(f"SELECT name FROM {SCHEMA}.{REPACKED}")
        done = {r[0] for r in cur.fetchall()}
        parts = [p for p in layout(cur) if p.types is not None]
    want = set(types)
    todo = [p for p in sorted(parts, key=lambda p: (p.pages, p.name))
            if (every or want & set(p.types or ())) and p.name not in done]
    unknown = want - {t for p in parts for t in (p.types or ())}
    if unknown:
        raise Refused(f"types_no_partition_names:{sorted(unknown)[:5]}")
    out: list[dict[str, Any]] = []
    for p in todo:
        s.timings.clear()
        got = rebuild(s, repack(p.types or ()), budget_s=budget_s, keep_old=keep_old,
                      verify_percent=verify_percent)
        with s.connect() as conn:
            conn.execute(f"INSERT INTO {SCHEMA}.{REPACKED} (name, gen) SELECT n, %s FROM "
                         "unnest(%s::text[]) n ON CONFLICT DO NOTHING",
                         (got["gen"], got["plan"]["new"]))
        s.log(f"repacked {p.name}: {got['packed']} {got['timings']}")
        out.append({"partition": p.name, "new": got["plan"]["new"], "gen": got["gen"],
                    "packed": got["packed"], "verified": got["verified"],
                    "timings": got["timings"], "swap_locked_s": got.get("swap_locked_s")})
    return {"repacked": out, "skipped": sorted(done & {p.name for p in parts})}


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
                                        "undo", "finish", "repack", "rebuild-undo",
                                        "rebuild-finish"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--type", action="append", default=[], dest="types")
    ap.add_argument("--source", default=None, help="table or (SELECT ...) of the new rows")
    ap.add_argument("--keep", default="FALSE", help="SQL: replaced-type rows to keep")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget-s", type=float, default=BUDGET_S)
    ap.add_argument("--plan-only", action="store_true", help="convert: project and gate only")
    ap.add_argument("--all", action="store_true",
                    help="repack: every partition naming types, one rebuild each; "
                         "rebuild-undo/-finish: every kept rebuild")
    ap.add_argument("--gen", type=int, action="append", default=[],
                    help="rebuild-undo/-finish: the kept rebuild's generation")
    ap.add_argument("--no-keep-old", action="store_true",
                    help="repack: drop the old partitions at the swap (no undo)")
    ap.add_argument("--verify-percent", type=float, default=1.0,
                    help="repack: block sample of each packed partition compared")
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
        elif a.command == "repack":
            out = repack_all(s, types=a.types, every=a.all, budget_s=a.budget_s,
                             keep_old=not a.no_keep_old, verify_percent=a.verify_percent)
        elif a.command in ("rebuild-undo", "rebuild-finish"):
            gens = list(a.gen)
            if a.all:
                with s.connect() as conn, conn.cursor() as cur:
                    gens = kept_rebuilds(cur)
            if not gens:
                raise Refused("no_gen")
            fn = rebuild_undo if a.command == "rebuild-undo" else rebuild_finish
            # undo newest first: a later rebuild's old partitions are an earlier one's new
            out = [fn(s, g) for g in sorted(gens, reverse=True)]
        elif a.command == "undo":
            out = undo(s)
        else:
            out = finish(s)
    except (OverBudget, Refused, Changed) as exc:
        print(json.dumps({"refused": type(exc).__name__, "why": str(exc)}))
        return 2
    if a.command in ("rebuild", "convert", "undo", "finish") and not a.plan_only:
        from operating.pg import link_marks
        link_marks.after_apply(dsn, f"link_partitions {a.command}")
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
