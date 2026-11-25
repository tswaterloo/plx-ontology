"""One change feed of the applied cycles (migration 0026), read incrementally by every
derived consumer.

Before the feed each consumer worked out on its own what changed since its last run:
the incremental proactive pass and the autopilot's live feed opened every delta layer the
pointer listed (each owns every record changed since the generation, 0.7-0.8 M on
2026-11-13) and fingerprinted them; the AI watcher scanned ``object_versions`` past its own
version mark; the vendor-mail pass and the truth check re-read everything. Now the delta
apply (``operating.pg.ontology_delta.apply_layer``) writes, in its own transaction, the
records the cycle really changed, and each consumer reads the rows past its cursor.

**Writer** (``snapshot`` before the apply replaces the owned rows, ``record_delta`` after,
both on the apply's cursor and its temp table ``owned``): a per-record signature (its
object row, its versions' numbers and supersession, its text fields, removals and
identifiers) before and after; a record whose signature moved is ``c`` (changed), one
that was not there ``n`` (new), one gone or under an open removal ``r``; the ends of every
link the apply deleted or inserted (the staged apply's undo, ``operating.pg.step_staging``)
whose own rows did not move are ``l``. A record re-owned unchanged gets no row. Set-based:
two grouped reads over the owned records and one ``INSERT ... SELECT``. A whole-steps run's
changed link ends (``record_ids``) and a schema migration's scope (``record_scope``: one
``m`` row per scope prefix, ``object_id`` ``'*'``) are written the same way. Every writer
holds the delta's advisory lock, so rows commit in ``seq`` order and a reader's newest seq
never jumps over a transaction still open.

**Reader** (``read_since(conn, consumer)``, then ``commit(conn, consumer, batch.end)`` once
the consumer has handled the batch): ``None`` when the feed does not exist (the migration
is not applied: the consumer keeps its own way); state ``start`` on a consumer's first
read and ``gap`` when rows past its cursor were pruned (it reads its own way once, then
commits ``end`` and follows the feed from there); else ``ok`` with the rows. A consumer
that fails before ``commit`` reads the same rows again (at least once).

**Retention** (``prune``, run by every writer): rows every active consumer (one that
committed within ``STALE_DAYS``) has passed go, and rows of cycles older than
``KEEP_DAYS`` go whatever the cursors say, so the table stays bounded when a consumer
stops for good.

``python3 -m operating.pg.change_feed status [--dsn DSN]``: each consumer's position and
lag, the rows held (counts only).

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import re
import sys
from typing import Any, Iterable, Sequence

FEED = "ontology.change_feed"
CYCLES = "ontology.change_cycles"
CURSORS = "ontology.change_cursors"
PRUNED = "_pruned"   # change_cursors row: the newest seq pruned
# every consumer that reads the feed; any other name is refused (a typo would otherwise
# start a cursor nobody reads and hold rows until they age out)
CONSUMERS = frozenset({"proactive_incremental", "ai_watcher", "autopilot_follow",
                       "vendor_watch", "truth_check"})
KINDS = {"n": "new", "c": "changed", "r": "removed", "l": "linked", "m": "migrated"}
SOURCES = ("delta", "whole_steps", "migration", "links", "purge")
KEEP_DAYS = float(os.environ.get("PLX_CHANGE_FEED_KEEP_DAYS", "7"))
STALE_DAYS = float(os.environ.get("PLX_CHANGE_FEED_STALE_DAYS", "3"))
CONNECT = {"connect_timeout": 10, "options": "-c statement_timeout=120000"}
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Migration 0026's text, which lands apart (after the migration train): the tests make the
# tables from it, and a check keeps the file and this the same once both are on one tree.
DDL = '''\
-- 0026_change_feed: one change feed of the applied cycles (operating/pg/change_feed.py).
--
-- * change_cycles: one row per applied cycle (a delta layer, a whole-steps run, a schema
--   or step migration, a link apply or undo, a people-exclusion purge): its name (the load_state key it is recorded under), the generation it
--   applied over, when it committed, and its feed rows' first and last seq.
-- * change_feed: the records a cycle really changed, in apply order (``seq``, the readers'
--   cursor): kind n (new), c (changed), r (removed), l (only a link at it changed), m (a
--   migration rewrote every record of the scope prefix in object_type; object_id '*').
--   A record a layer re-owns unchanged gets no row. Written in the apply's own
--   transaction, under the delta's advisory lock, so rows commit in seq order.
-- * change_cursors: each consumer's position (the last seq it handled); the row
--   '_pruned' is the newest seq pruned: a cursor below it has missed rows (a gap) and its
--   consumer reads its own way once, as before the feed existed.
-- Rows every active consumer passed, and rows older than the keep window, are pruned by
-- the writer. Not part of a generation: an ontology swap or reload leaves these tables
-- alone, and readers filter by generation.

CREATE TABLE IF NOT EXISTS ontology.change_cycles (
    cycle_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    source     text NOT NULL CHECK (source IN ('delta', 'whole_steps', 'migration', 'links',
                                               'purge')),
    generation text,
    applied_at timestamptz NOT NULL DEFAULT now(),
    first_seq  bigint,
    last_seq   bigint,
    rows       integer NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS change_cycles_by_last_seq ON ontology.change_cycles (last_seq);

CREATE TABLE IF NOT EXISTS ontology.change_feed (
    seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cycle_id    bigint NOT NULL,
    object_id   text NOT NULL,
    object_type text,
    kind        text NOT NULL CHECK (kind IN ('n', 'c', 'r', 'l', 'm')));

CREATE TABLE IF NOT EXISTS ontology.change_cursors (
    consumer   text PRIMARY KEY CHECK (consumer ~ '^_?[a-z][a-z0-9_]{0,62}$'),
    position   bigint NOT NULL CHECK (position >= 0),
    updated_at timestamptz NOT NULL DEFAULT now());

ALTER TABLE ontology.change_feed SET (autovacuum_vacuum_scale_factor = 0.05);
'''


def _consumer(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name) or name not in CONSUMERS:
        raise ValueError(f"change_feed_consumer_unknown:{name!r}")
    return name


def present(cur: Any) -> bool:
    """Whether the feed's tables exist (migration 0026 applied)."""
    row = cur.execute("SELECT to_regclass('ontology.change_feed') IS NOT NULL AND "
                      "to_regclass('ontology.change_cursors') IS NOT NULL AND "
                      "to_regclass('ontology.change_cycles') IS NOT NULL").fetchone()
    return bool(row and row[0])


def connect(dsn: str) -> Any:
    """A connection for a consumer's read and commit (autocommit: each statement alone)."""
    import psycopg
    return psycopg.connect(dsn, autocommit=True, **CONNECT)


# --------------------------------------------------------------------------- writer

_SIGNATURE = """
SELECT o.object_id, ob.object_id IS NOT NULL AS present, ob.object_type,
       md5(concat_ws('|', ob::text, v.s, t.s, r.s, i.s)) AS sig,
       coalesce(r.open, false) AS removed
FROM owned o
LEFT JOIN ontology.objects ob ON ob.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(ver::text || ':' || coalesce(superseded_at, '-'),
                                        ',' ORDER BY ver) AS s
           FROM ontology.object_versions WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) v ON v.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(field_id::text, ',' ORDER BY field_id) AS s
           FROM ontology.text_fields WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) t ON t.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(x::text, ',' ORDER BY x::text) AS s,
                  bool_or(x.until IS NULL) AS open
           FROM ontology.object_removals x WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) r ON r.object_id = o.object_id
LEFT JOIN (SELECT object_id, string_agg(x::text, ',' ORDER BY x::text) AS s
           FROM ontology.identifiers x WHERE object_id IN (SELECT object_id FROM owned)
           GROUP BY object_id) i ON i.object_id = o.object_id
"""


def snapshot(cur: Any) -> bool:
    """Before the apply replaces the owned records' rows: their signatures, in the temp
    table ``feed_before`` (dropped at commit). False (nothing done) without the feed."""
    if not present(cur):
        return False
    cur.execute("DROP TABLE IF EXISTS feed_before")
    cur.execute(f"CREATE TEMP TABLE feed_before ON COMMIT DROP AS {_SIGNATURE}")
    cur.execute("ALTER TABLE feed_before ADD PRIMARY KEY (object_id)")
    return True


def _cycle(cur: Any, name: str, source: str, generation: str | None) -> int:
    if source not in SOURCES:
        raise ValueError(f"change_feed_source_unknown:{source!r}")
    cur.execute(f"INSERT INTO {CYCLES} (name, source, generation) VALUES (%s, %s, %s) "
                "RETURNING cycle_id", (name, source, generation))
    return cur.fetchone()[0]


def _close_cycle(cur: Any, cycle: int, inserted: str, params: Sequence[Any]) -> dict[str, int]:
    """Run ``inserted`` (an ``INSERT INTO change_feed ... RETURNING seq``), record its
    first and last seq on the cycle, prune; the counts."""
    cur.execute(f"WITH ins AS ({inserted}) SELECT count(*), min(seq), max(seq) FROM ins",
                params)
    rows, first, last = cur.fetchone()
    cur.execute(f"UPDATE {CYCLES} SET rows = %s, first_seq = %s, last_seq = %s "
                "WHERE cycle_id = %s", (rows, first, last, cycle))
    return {"cycle": cycle, "rows": rows, **prune(cur)}


def record_delta(cur: Any, name: str, generation: str | None,
                 link_undo: tuple[str, str] | None = None) -> dict[str, Any] | None:
    """After the apply (same transaction, ``snapshot`` taken): the records it changed into
    the feed as one cycle. ``link_undo``: (stage schema, step) of the staged link apply,
    whose deleted and inserted links' ends are ``l`` when their own rows did not move.
    None without the feed or the snapshot."""
    cur.execute("SELECT to_regclass('pg_temp.feed_before') IS NOT NULL")
    if not cur.fetchone()[0] or not present(cur):
        return None
    cycle = _cycle(cur, name, "delta", generation)
    cur.execute(f"CREATE TEMP TABLE feed_after ON COMMIT DROP AS {_SIGNATURE}")
    cur.execute("ALTER TABLE feed_after ADD PRIMARY KEY (object_id)")
    cur.execute("CREATE TEMP TABLE feed_moved ON COMMIT DROP AS SELECT a.object_id, "
                "coalesce(a.object_type, b.object_type) AS object_type, "
                "CASE WHEN NOT a.present OR a.removed THEN 'r' WHEN NOT b.present THEN 'n' "
                "ELSE 'c' END AS kind FROM feed_after a JOIN feed_before b USING (object_id) "
                "WHERE a.sig IS DISTINCT FROM b.sig OR a.present <> b.present")
    cur.execute("ALTER TABLE feed_moved ADD PRIMARY KEY (object_id)")
    linked, steps = "SELECT NULL::text WHERE false", []
    if link_undo is not None:
        schema, step = link_undo
        sql = link_ends_sql(schema)
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL",
                    (f"{schema}.undo_deleted", f"{schema}.undo_inserted"))
        if cur.fetchone()[0]:
            # the links this apply deleted (kept as they were) and inserted (their ids)
            linked, steps = sql, [step] * 4
    inserted = (
        f"INSERT INTO {FEED} (cycle_id, object_id, object_type, kind) "
        "SELECT %s::bigint, object_id, object_type, kind FROM ("
        "SELECT object_id, object_type, kind FROM feed_moved "
        "UNION ALL SELECT e.object_id, ob.object_type, 'l' FROM "
        f"({linked}) e(object_id) JOIN ontology.objects ob ON ob.object_id = e.object_id "
        "WHERE NOT EXISTS (SELECT 1 FROM feed_moved m WHERE m.object_id = e.object_id)"
        ") x ORDER BY object_id RETURNING seq")
    out = _close_cycle(cur, cycle, inserted, [cycle, *steps])
    for table in ("feed_moved", "feed_after", "feed_before"):
        cur.execute(f"DROP TABLE {table}")
    return out


def generation(cur: Any) -> str | None:
    """The content digest of the generation Postgres holds (``load_state``)."""
    row = cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'"
                      ).fetchone()
    return (row[0] or {}).get("content_digest") if row else None


def record_select(cur: Any, name: str, source: str, gen: str | None, select: str,
                  params: Sequence[Any] = (), kind: str = "l") -> dict[str, Any] | None:
    """The records ``select`` names (one text column of object ids) as one cycle of
    ``kind``, set-based, in the caller's transaction; None without the feed."""
    if kind not in KINDS or kind == "m":
        raise ValueError(f"change_feed_kind_unknown:{kind!r}")
    if not present(cur):
        return None
    cycle = _cycle(cur, name, source, gen)
    return _close_cycle(
        cur, cycle,
        f"INSERT INTO {FEED} (cycle_id, object_id, object_type, kind) SELECT %s, x.id, "
        f"ob.object_type, %s FROM (SELECT DISTINCT id FROM ({select}) s(id) WHERE id IS NOT "
        "NULL) x LEFT JOIN ontology.objects ob ON ob.object_id = x.id ORDER BY x.id "
        "RETURNING seq", (cycle, kind, *params))


def record_ids(cur: Any, name: str, source: str, gen: str | None,
               ids: Iterable[str], kind: str = "l") -> dict[str, Any] | None:
    """Records changed outside a layer apply (a whole-steps run's changed link ends) as
    one cycle of ``kind``; None without the feed. Same transaction as the change."""
    return record_select(cur, name, source, gen, "SELECT unnest(%s::text[])",
                         (sorted(set(ids)),), kind)


def link_ends_sql(schema: str, step_column: str = "step", part: bool = False) -> str:
    """Both ends of the links an undo-kept apply deleted (``undo_deleted``, the rows as
    they were) and inserted (``undo_inserted`` joined to the served rows): one column of
    ids. Parameters: the step (and the part) four times."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema) or step_column not in ("step",
                                                                             "link_type"):
        raise ValueError(f"change_feed_stage_schema:{schema}")
    where = f"{{a}}.{step_column} = %s" + (f" AND {{a}}.part = %s" if part else "")
    d, u = where.format(a="d"), where.format(a="u")
    return (f"SELECT d.from_object FROM {schema}.undo_deleted d WHERE {d} "
            f"UNION SELECT d.to_object FROM {schema}.undo_deleted d WHERE {d} "
            f"UNION SELECT l.from_object FROM {schema}.undo_inserted u JOIN "
            f"ontology.link_rows l ON l.link_id = u.link_id WHERE {u} "
            f"UNION SELECT l.to_object FROM {schema}.undo_inserted u JOIN "
            f"ontology.link_rows l ON l.link_id = u.link_id WHERE {u}")


def record_link_ends(cur: Any, name: str, schema: str, key: Sequence[Any], *,
                     step_column: str = "step", source: str = "links"
                     ) -> dict[str, Any] | None:
    """The ends of the links an undo-kept apply (``step_staging``'s bits, ``link_apply``'s
    kinds) wrote or is about to take back, as one ``l`` cycle; read while both undo
    tables and the inserted rows are there (after an apply, before an undo). ``key``: the
    step (and part), or the link type. None without the feed."""
    if not present(cur):
        return None
    sql = link_ends_sql(schema, step_column, part=len(key) == 2)
    return record_select(cur, name, source, generation(cur), sql, list(key) * 4)


def record_scope(cur: Any, name: str, gen: str | None, scope: Iterable[str],
                 source: str = "migration") -> dict[str, Any] | None:
    """A change known by its scope (the object id prefixes a migration rewrote, the types
    a purge removed records of) as one ``m`` row per prefix: a consumer judges those
    records whole. No record id is written. None without the feed."""
    if not present(cur):
        return None
    cycle = _cycle(cur, name, source, gen)
    return _close_cycle(
        cur, cycle,
        f"INSERT INTO {FEED} (cycle_id, object_id, object_type, kind) SELECT %s, '*', "
        "x.p, 'm' FROM unnest(%s::text[]) x(p) ORDER BY x.p RETURNING seq",
        (cycle, sorted({str(p) for p in scope if str(p)} or {""})))


def prune(cur: Any, *, keep_days: float | None = None,
          stale_days: float | None = None) -> dict[str, int]:
    """Drop the rows every active consumer passed and those older than the keep window;
    record the newest seq dropped (``_pruned``). Counts."""
    keep = KEEP_DAYS if keep_days is None else keep_days
    stale = STALE_DAYS if stale_days is None else stale_days
    cur.execute(
        f"SELECT greatest("
        f"  (SELECT min(position) FROM {CURSORS} WHERE consumer <> %s "
        f"   AND updated_at > now() - make_interval(secs => %s)),"
        f"  (SELECT max(last_seq) FROM {CYCLES} WHERE applied_at < now() - "
        f"   make_interval(secs => %s)), 0), "
        f"(SELECT position FROM {CURSORS} WHERE consumer = %s)",
        (PRUNED, stale * 86400, keep * 86400, PRUNED))
    through, done = cur.fetchone()
    done = done or 0
    if through <= done:
        return {"pruned": 0}
    cur.execute(f"DELETE FROM {FEED} WHERE seq > %s AND seq <= %s", (done, through))
    pruned = cur.rowcount
    cur.execute(f"DELETE FROM {CYCLES} WHERE (last_seq IS NOT NULL AND last_seq <= %s) OR "
                "(last_seq IS NULL AND applied_at < now() - make_interval(secs => %s))",
                (through, keep * 86400))
    cur.execute(f"INSERT INTO {CURSORS} (consumer, position) VALUES (%s, %s) ON CONFLICT "
                "(consumer) DO UPDATE SET position = greatest(excluded.position, "
                f"{CURSORS}.position), updated_at = now()", (PRUNED, through))
    return {"pruned": pruned}


# --------------------------------------------------------------------------- reader

@dataclass(frozen=True)
class Change:
    seq: int
    object_id: str
    object_type: str | None
    kind: str          # new, changed, removed, linked, migrated
    cycle: str
    applied_at: datetime | None


@dataclass(frozen=True)
class Batch:
    """What one read returned. ``state``: ``ok`` (``changes`` are every row past
    ``start`` up to ``end`` of the asked generation and types), ``start`` (no cursor yet)
    or ``gap`` (rows past the cursor were pruned): then ``changes`` is empty and the
    consumer reads its own way once before committing ``end``. ``overflow``: more rows
    than ``limit``; ``end`` is then the last one returned (the rest is read next)."""
    consumer: str
    state: str
    start: int | None
    end: int
    changes: tuple[Change, ...] = ()
    overflow: bool = False
    generations: frozenset = field(default_factory=frozenset)
    head: int | None = None   # the newest row when read (``end`` unless ``overflow``)

    def skipped(self) -> "Batch":
        """This batch moved to ``head``: a consumer that sets an overflow aside (counted,
        not handled) commits this one and goes on past it."""
        from dataclasses import replace
        return replace(self, end=max(self.end, self.head or 0), changes=(), overflow=False)

    @property
    def ready(self) -> bool:
        return self.state == "ok"

    def ids(self) -> set[str]:
        """The changed records (a migration's scope rows are in ``migrated``)."""
        return {c.object_id for c in self.changes if c.kind != "migrated"}

    @property
    def migrated(self) -> list[str]:
        """The scope prefixes a migration rewrote since the cursor."""
        return sorted({c.object_type or "" for c in self.changes if c.kind == "migrated"})

    @property
    def newest(self) -> Change | None:
        return self.changes[-1] if self.changes else None

    def summary(self) -> dict[str, Any]:
        """Counts only (a consumer's report)."""
        kinds: dict[str, int] = {}
        for c in self.changes:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        return {"state": self.state, "start": self.start, "end": self.end,
                "rows": len(self.changes), "kinds": dict(sorted(kinds.items())),
                "overflow": self.overflow}


def position(cur: Any, consumer: str) -> int | None:
    row = cur.execute(f"SELECT position FROM {CURSORS} WHERE consumer = %s",
                      (_consumer(consumer),)).fetchone()
    return None if row is None else int(row[0])


def read_since(cur: Any, consumer: str, *, types: Iterable[str] | None = None,
               generation: str | None = None, limit: int | None = None) -> Batch | None:
    """The rows past ``consumer``'s cursor (module docstring); None without the feed.
    ``types``: only rows of these object types (a migration's rows and rows of no known
    type always); ``generation``:
    only cycles applied over it (the cursor still moves past the others)."""
    consumer = _consumer(consumer)
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("change_feed_limit_invalid")
    if not present(cur):
        return None
    start = position(cur, consumer)
    pruned, head = cur.execute(
        f"SELECT coalesce((SELECT position FROM {CURSORS} WHERE consumer = %s), 0), "
        f"coalesce((SELECT max(seq) FROM {FEED}), 0)", (PRUNED,)).fetchone()
    head = max(int(head), int(pruned))
    if start is None:
        return Batch(consumer, "start", None, head, head=head)
    if start < pruned:
        return Batch(consumer, "gap", start, head, head=head)
    if start >= head:
        return Batch(consumer, "ok", start, start, head=head)
    where, params = ["f.seq > %s", "f.seq <= %s"], [start, head]
    if types is not None:
        # a row of no known type (a record gone before it was typed) may be of any
        where.append("(f.object_type = ANY(%s) OR f.object_type IS NULL OR f.kind = 'm')")
        params.append(sorted({str(t) for t in types}))
    if generation is not None:
        where.append("c.generation IS NOT DISTINCT FROM %s")
        params.append(generation)
    sql = (f"SELECT f.seq, f.object_id, f.object_type, f.kind, c.name, c.applied_at "
           f"FROM {FEED} f JOIN {CYCLES} c ON c.cycle_id = f.cycle_id WHERE "
           + " AND ".join(where) + " ORDER BY f.seq")
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit + 1)
    rows = cur.execute(sql, params).fetchall()
    overflow = limit is not None and len(rows) > limit
    if overflow:
        rows = rows[:limit]
    gens = cur.execute(f"SELECT DISTINCT generation FROM {CYCLES} WHERE last_seq > %s AND "
                       "first_seq <= %s", (start, head)).fetchall()
    changes = tuple(Change(int(r[0]), r[1], r[2], KINDS.get(r[3], r[3]), r[4], r[5])
                    for r in rows)
    end = changes[-1].seq if overflow else head
    return Batch(consumer, "ok", start, end, changes, overflow,
                 frozenset(g[0] for g in gens), head)


def newest_cycle(cur: Any, through: int | None = None) -> dict[str, Any] | None:
    """The newest applied cycle (up to the seq ``through``): its name, source and when it
    committed; None without the feed or a cycle."""
    if not present(cur):
        return None
    row = cur.execute(
        f"SELECT name, source, applied_at FROM {CYCLES} WHERE last_seq IS NOT NULL AND "
        "(%s::bigint IS NULL OR last_seq <= %s::bigint) ORDER BY last_seq DESC LIMIT 1",
        (through, through)).fetchone()
    return None if row is None else {"cycle": row[0], "source": row[1],
                                     "applied_at": row[2].isoformat()}


def commit(cur: Any, consumer: str, end: int) -> None:
    """``consumer`` handled every row up to ``end`` (never moves back)."""
    consumer = _consumer(consumer)
    if not isinstance(end, int) or isinstance(end, bool) or end < 0:
        raise ValueError("change_feed_position_invalid")
    cur.execute(f"INSERT INTO {CURSORS} (consumer, position) VALUES (%s, %s) ON CONFLICT "
                "(consumer) DO UPDATE SET position = greatest(excluded.position, "
                f"{CURSORS}.position), updated_at = now()", (consumer, end))


def read(dsn: str, consumer: str, *, require_writable: bool = True, **kwargs: Any) -> Batch | None:
    """``read_since`` on a connection of its own; None also when Postgres cannot be read
    (the consumer keeps its own way, which is complete). ``require_writable=False`` lets a
    read-only compute pass (a Spark hot standby) read its batch without failing on the
    cursor privilege check; the box apply commits the cursor."""
    try:
        with connect(dsn) as conn:
            if require_writable and present(conn) and not writable(conn):
                # it could never move its cursor: the same rows again on every run
                _loud(consumer, "cannot write its cursor as this database user; reading "
                      "its own way (grant INSERT, UPDATE on ontology.change_cursors)")
                return None
            return read_since(conn, consumer, **kwargs)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreachable or refused: the consumer's own way
        _loud(consumer, f"feed unreadable ({type(exc).__name__}); reading its own way")
        return None


def writable(cur: Any) -> bool:
    """Whether this session may write a cursor."""
    row = cur.execute("SELECT has_table_privilege('ontology.change_cursors', 'INSERT') AND "
                      "has_table_privilege('ontology.change_cursors', 'UPDATE')").fetchone()
    return bool(row and row[0])


def _loud(consumer: str, what: str) -> None:
    """A consumer off the feed says so in its unit's log (``ops_health`` ``change_feed``
    pages a consumer that stopped following)."""
    print(f"CHANGE FEED: {consumer}: {what}", file=sys.stderr, flush=True)


def commit_to(dsn: str, batch: Batch | None) -> bool:
    """``commit`` of ``batch.end`` on a connection of its own; False when not written
    (the batch is read again next time)."""
    if batch is None:
        return False
    try:
        with connect(dsn) as conn:
            commit(conn, batch.consumer, batch.end)
        return True
    except Exception as exc:  # noqa: BLE001 - read again next time (at least once)
        _loud(batch.consumer, f"cursor not written ({type(exc).__name__}); the same rows "
              "are read again")
        return False


def status(cur: Any) -> dict[str, Any]:
    if not present(cur):
        return {"present": False}
    head, rows, cycles, oldest = cur.execute(
        f"SELECT coalesce(max(seq), 0), count(*), (SELECT count(*) FROM {CYCLES}), "
        f"(SELECT min(applied_at) FROM {CYCLES}) FROM {FEED}").fetchone()
    cursors = {r[0]: {"position": r[1], "behind": max(0, head - r[1]),
                      "updated_at": r[2].isoformat()}
               for r in cur.execute(f"SELECT consumer, position, updated_at FROM {CURSORS} "
                                    "ORDER BY consumer")}
    return {"present": True, "head": head, "rows": rows, "cycles": cycles,
            "oldest_cycle_at": oldest.isoformat() if oldest else None, "cursors": cursors}


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.change_feed")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="each consumer's position and lag (counts only)")
    s.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    with connect(args.dsn or config.dsn()) as conn:
        print(json.dumps(status(conn), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
