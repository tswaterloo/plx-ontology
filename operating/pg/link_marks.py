"""The link slice kept in Postgres (migration 0028, CC-P-07): ``refresh``, ``fold_layer``,
``status``, ``start_rebuild`` and ``undo``.

``ontology.link_marks`` holds, per link type and markings, the latest end of its rows and of
each gated evidence handle they cite: the facts ``ontology_tools_traverse._slices("link")``
reads, which every host's first read of a data version otherwise read off the whole of
``ontology.link_rows`` (45M rows, 76 GB). No trigger keeps them: a reader takes them only
while ``ontology.link_marks_vouched`` holds (the state kept for its gated schemes at the
data version, generation and link_rows change counter Postgres has now: migration 0028),
and reads link_rows as before otherwise.

**Refresh** (one refresher at a time, a session advisory lock ``LOCK``): a shadow build the
delta layers keep running beside, then a swap that holds them off for seconds.

1. *Start*, under the delta layers' lock (``WRITERS_LOCK``, the one
   ``ontology_delta.apply_layer`` and the mail purge take), held for one short transaction:
   the change counter, data version and generation read into the build record
   (``ontology.link_marks_build``, migration 0032; made here when missing), an empty
   catch-up table (``link_marks_catchup``) and staging table (``link_marks_parts``, no
   index) made, the state set ``building``.
2. *Read*, no lock held: the slice of link_rows, one link-id range at a time on ``workers``
   connections, each range's rows grouped and appended to the staging table. No worker
   writes a key another may write (no unique index there; the 11-15 5:57 AM ET build died
   of ``DeadlockDetected`` between its own workers' ``INSERT ... ON CONFLICT``).
   Meanwhile each delta layer, in its own transaction (``layer_start``/``fold_layer``),
   appends the records it owns to the catch-up table and moves the build's counter and
   data version on to its own when the counter it found was the build's (the chain); a
   counter it did not find there (a train, the purge, any write outside a layer) breaks
   the build (``broken``), which stops reading at once.
3. *Merge*, no lock held: the staged rows grouped into ``link_marks_next`` and its key
   added; the catch-up records so far folded into it.
4. *Swap*, under ``WRITERS_LOCK`` (seconds): the catch-up records the last layers added
   folded in; when the build's chain is whole (the counter and data version Postgres has
   now are the ones the last layer left, the generation the start's), in one transaction
   the old table dropped, ``link_marks_next`` renamed in its place (its owner and grants
   the old one's) and the state set ``kept``. Readers see the old state (not ``kept``)
   or the new table kept, never a mix.

A build that fails at any step (an error, a broken chain, a lock it could not get) stamps
``failed_at`` and ``why`` on the build record (columns, never text to parse) and drops its
tables; ``refresh_if_stale(retry_after=...)`` waits that long after it, and after the start
of a build no refresher still holds the lock for (killed).

**Fold** (``fold_layer``, in a delta layer's own transaction): when the facts were current
before the layer wrote anything, the links of the records it owns are added (raised, never
lowered) and the state moved to the layer's data version and the counter with its own
writes counted, so a delta keeps them current without a refresh.

**Who builds**: the first build is run on purpose (``python3 -m operating.pg.link_marks
refresh``; on the box ``/data/scratch/cfg/link_marks_build.sh``), never inside a delta
cycle. Once a build has kept it, a layer that finds it not current (a train, the purge)
starts one rebuild in a process of its own (``start_rebuild``: detached, at most once per
``RETRY_SECONDS`` after a failure) and goes on; trains' command lines refresh after they
apply (``after_apply``); an API start asks only when its role may write the rows.

Stdlib and psycopg. ``python3 -m operating.pg.link_marks status|refresh|undo [--workers N]
[--if-stale] [--retry-after S]``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_for
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Sequence

LOCK = "hashtext('ontology.link_marks')"
SETTLE_SECONDS = 11.0   # past PGSTAT_IDLE_INTERVAL (10 s): a commit's counts are flushed
# the delta layers' lock (``ontology_delta.apply_layer``, ``mail_exclusion_purge``): held by a
# refresh for its start and its swap only (seconds), never while it reads link_rows
WRITERS_LOCK = "hashtext('ontology_delta')"
WRITERS_WAIT_SECONDS = 600   # the longest a refresh waits for a layer under way to commit
SWAP_LOCK_TIMEOUT_MS = 3000  # the longest the swap waits for a reader of the old table
SWAP_TRIES = 10
LAYER_SETTLE_SECONDS = 2.0   # a layer's wait for the last commit's counts (no build: none)
RETRY_SECONDS = 3600         # a rebuild after one that failed: not sooner
# an active build record older than this is taken as dead even while its refresher's lock
# is held (a hung process); one whose lock nobody holds is dead at once (``_reset_dead``)
STALE_ACTIVE_SECONDS = 6 * 3600
DEAD = "build stopped without finishing (no refresher holds its lock)"
STALE = f"build still active after {STALE_ACTIVE_SECONDS} s: taken as stalled"
WORKERS = max(1, min(16, os.cpu_count() or 4))   # every core the server has (16 on the box)
BROKEN = "link_rows written outside a delta layer during the build"
FAILED = "link_rows or the data version changed during the refresh, or track_counts is off"

BUILD = "ontology.link_marks_build"
# the work tables' primary key (a train's undo capture refuses an ontology table without
# one): a generated id, never a natural key, so the workers' plain appends to the staging
# table still never wait on one another (one key from two ranges is two rows there)
WORK_ID = "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
PARTS = "ontology.link_marks_parts"
NEXT = "ontology.link_marks_next"
CATCHUP = "ontology.link_marks_catchup"

# the build record: migration 0032 makes it; this text only for a database without 0032
# (``_ensure_build``), the same columns
BUILD_SQL = f"""
CREATE TABLE IF NOT EXISTS {BUILD} (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    active boolean NOT NULL DEFAULT false,
    started_at timestamptz,
    counter text,
    data_version bigint,
    generation text,
    layers integer NOT NULL DEFAULT 0,
    broken text,
    failed_at timestamptz,
    why text,
    kept_at timestamptz,
    seconds double precision);
INSERT INTO {BUILD} (one) VALUES (true) ON CONFLICT DO NOTHING;
"""

UNDO_SQL = f"""
DROP TABLE IF EXISTS {PARTS};
DROP TABLE IF EXISTS {NEXT};
DROP TABLE IF EXISTS {CATCHUP};
DROP TABLE IF EXISTS {BUILD};
DROP FUNCTION IF EXISTS ontology.link_marks_vouched(text[]);
DROP FUNCTION IF EXISTS ontology.link_marks_stale(text);
DROP FUNCTION IF EXISTS ontology.link_marks_counter();
DROP FUNCTION IF EXISTS ontology.link_marks_generation();
DROP FUNCTION IF EXISTS ontology.link_marks_leaf_set();
DROP TABLE IF EXISTS ontology.link_marks_state;
DROP TABLE IF EXISTS ontology.link_marks;
"""

# one row's facts as the slice composes them: its markings, its gated handles, its end
_ROW_SLICE = ("SELECT l.link_type, l.markings, ontology.sq_l9_gated(%(schemes)s::text[], "
              "coalesce(l.evidence_handles, ontology.link_text(l.evidence_id, "
              "l.slots, 1, l.ek))), CASE WHEN l.superseded_at IS NULL OR "
              "ontology.sq_l9_time(l.superseded_at) IS NULL THEN '~' ELSE "
              "ontology.sq_l9_time(l.superseded_at) END FROM ontology.link_rows l ")

def _ends_slice(table: str) -> str:
    """``_ROW_SLICE`` of the links from or to the records in ``table`` (a keyed temp
    table): each record's links read by its ends' indexes (``link_rows_from``, then
    ``link_rows_to`` for a link from no such record), never one ``OR`` over both ends, which
    plans a sequential scan of every ``link_rows`` partition (82 GB served, 11-15) for a
    layer of any size; the lateral reads keep a large ``table`` on the indexes too (a join
    there plans whole-index merges or partition scans from the hub-skewed ``to_object``
    estimates; ``OFFSET 0`` keeps the planner from flattening them into that join). The
    "from no such record" test is an anti-join on the table's key, never ``NOT IN``
    (a large table outgrows hash_mem and ``NOT IN`` then scans it for every row). The
    grouped fold takes each group's greatest ``until``, so its rows are the
    ``OR``'s."""
    cols = _ROW_SLICE[:_ROW_SLICE.index(" FROM ontology.link_rows l ")]
    return (f"{cols} FROM {table} o CROSS JOIN LATERAL (SELECT * FROM ontology.link_rows l "
            f"WHERE l.from_object = o.object_id OFFSET 0) l UNION ALL {cols} FROM {table} o CROSS JOIN "
            f"LATERAL (SELECT * FROM ontology.link_rows l WHERE l.to_object = o.object_id AND "
            f"NOT EXISTS (SELECT 1 FROM {table} w WHERE w.object_id = l.from_object) OFFSET 0) l")


_GROUPED = ("SELECT r.name, r.markings, h.handle, max(r.until COLLATE \"C\") FROM ({slice}) "
            "r(name, markings, gated, until) CROSS JOIN LATERAL (SELECT ''::text AS handle "
            "UNION ALL SELECT json_array_elements_text(r.gated::json)) h GROUP BY 1, 2, 3")
# into a keyed table (one writer at a time: a layer's fold, the build's catch-up)
_ADD = ("INSERT INTO {table} AS k (link_type, markings, handle, until) " + _GROUPED +
        " ON CONFLICT (link_type, markings, handle) DO UPDATE "
        "SET until = GREATEST(k.until, EXCLUDED.until)")
# into the staging table: plain appends, nothing for two workers to wait on
_STAGE = (f"INSERT INTO {PARTS} (link_type, markings, handle, until) " + _GROUPED)


def present(cur: Any) -> bool:
    """Whether the store has migration 0028 (an older schema keeps none)."""
    cur.execute("SELECT to_regclass('ontology.link_marks_state') IS NOT NULL "
                "AND to_regprocedure('ontology.link_marks_vouched(text[])') IS NOT NULL")
    return bool(cur.fetchone()[0])


def _schemes() -> list[str]:
    from operating.ontology_tools_traverse import GATED_SCHEMES
    return sorted(GATED_SCHEMES)


def _build_row(cur: Any) -> dict[str, Any] | None:
    cur.execute(f"SELECT to_regclass('{BUILD}') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None
    cur.execute(f"SELECT active, started_at, failed_at, why, kept_at, layers, broken, seconds "
                f"FROM {BUILD}")
    row = cur.fetchone()
    if row is None:
        return None
    keys = ("active", "started_at", "failed_at", "why", "kept_at", "layers", "broken",
            "seconds")
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in zip(keys, row)}


def status(conn: Any) -> dict[str, Any]:
    """The kept slice's state: phase, why, rows, whether a reader would use it now for the
    reader's gated schemes, and the last build's record (``build``)."""
    with conn.cursor() as cur:
        if not present(cur):
            return {"phase": None, "why": "migration 0028 not applied", "rows": 0,
                    "readers_use_it": False}
        cur.execute("SELECT phase, why, built_at, data_version FROM ontology.link_marks_state")
        phase, why, built, version = cur.fetchone()
        cur.execute("SELECT count(*) FROM ontology.link_marks")
        rows = cur.fetchone()[0]
        cur.execute("SELECT ontology.link_marks_vouched(%s::text[])", (_schemes(),))
        usable = bool(cur.fetchone()[0])
        build = _build_row(cur)
    return {"phase": phase, "why": why, "built_at": built and built.isoformat(),
            "data_version": version, "rows": rows, "readers_use_it": usable, "build": build}


def _bounds(conn: Any, pieces: int) -> list[str | None]:
    from operating.ontology_tools_traverse import _pg_key_bounds
    with conn.cursor() as cur:
        return _pg_key_bounds(cur, "link", pieces)


class _Failed(Exception):
    """A build that cannot keep the slice: ``why`` goes on the build record."""


def _take_writers(cur: Any) -> None:
    import psycopg
    try:
        cur.execute(f"SET lock_timeout = '{int(WRITERS_WAIT_SECONDS * 1000)}ms'")
        cur.execute(f"SELECT pg_advisory_lock({WRITERS_LOCK})")
    except psycopg.errors.LockNotAvailable:
        raise _Failed("a delta layer held the writers' lock past "
                      f"{WRITERS_WAIT_SECONDS} s") from None
    finally:
        cur.execute("RESET lock_timeout")


def _drop_work(cur: Any) -> None:
    cur.execute(f"DROP TABLE IF EXISTS {PARTS}")
    cur.execute(f"DROP TABLE IF EXISTS {NEXT}")
    cur.execute(f"DROP TABLE IF EXISTS {CATCHUP}")


def _catch_up(conn: Any, schemes: list[str]) -> int:
    """The records the delta layers appended to the catch-up table, taken off it and their
    links folded into ``link_marks_next`` (raised, never lowered), in one transaction."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS caught (object_id text PRIMARY KEY) "
                    "ON COMMIT DELETE ROWS")
        cur.execute(f"WITH taken AS (DELETE FROM {CATCHUP} RETURNING object_id) "
                    "INSERT INTO caught SELECT DISTINCT object_id FROM taken "
                    "ON CONFLICT DO NOTHING")
        if cur.rowcount == 0:
            return 0
        cur.execute("ANALYZE caught")
        cur.execute(_ADD.format(table=NEXT, slice=_ends_slice("caught")),
                    {"schemes": schemes})
        return cur.rowcount


def _copy_owner_and_grants(cur: Any, table: str = NEXT) -> None:
    """``table`` owned and granted as ``link_marks`` is (the API's role reads it)."""
    from psycopg import sql
    target = sql.SQL(table)   # one of this module's constants, never input
    cur.execute("SELECT pg_get_userbyid(relowner), current_user FROM pg_class "
                "WHERE oid = 'ontology.link_marks'::regclass")
    owner, me = cur.fetchone()
    cur.execute("SELECT CASE WHEN a.grantee = 0 THEN NULL ELSE pg_get_userbyid(a.grantee) END, "
                "a.privilege_type FROM pg_class c, aclexplode(c.relacl) a "
                "WHERE c.oid = 'ontology.link_marks'::regclass AND a.grantee <> c.relowner")
    grants = cur.fetchall()
    if owner != me:
        cur.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(target, sql.Identifier(owner)))
    for grantee, privilege in grants:
        cur.execute(sql.SQL("GRANT {} ON {} TO {}").format(
            sql.SQL(privilege), target, sql.SQL("PUBLIC") if grantee is None
            else sql.Identifier(grantee)))


def _ensure_build(cur: Any) -> None:
    """The build record there (migration 0032 makes it; a database without 0032 gets it
    here, owned and granted as ``link_marks``), with its one row. No DDL when it exists:
    ``CREATE TABLE IF NOT EXISTS`` still needs CREATE on the schema."""
    cur.execute(f"SELECT to_regclass('{BUILD}') IS NOT NULL")
    if not cur.fetchone()[0]:
        cur.execute(BUILD_SQL)
        _copy_owner_and_grants(cur, BUILD)
        return
    cur.execute(f"SELECT EXISTS (SELECT 1 FROM {BUILD})")
    if not cur.fetchone()[0]:
        cur.execute(f"INSERT INTO {BUILD} (one) VALUES (true) ON CONFLICT DO NOTHING")


def _reset_dead(cur: Any) -> str | None:
    """In a delta layer's transaction (the writers' lock held): an ``active`` build record
    no refresher can finish taken off. Dead when nobody holds the refresher's lock (killed
    without cleanup: a refresh holds it from before it marks the record active until after
    it clears it) or active past ``STALE_ACTIVE_SECONDS`` (hung). Marked failed (``why``,
    ``failed_at``: the usual back-off applies), so the layer neither extends its chain nor
    keeps filling its catch-up table, and the delta starts a rebuild; a dead one's work
    tables dropped (a hung one's are its own: it drops them when it fails). The reason, or
    None when there was nothing to reset."""
    cur.execute(f"SELECT active, started_at < now() - make_interval(secs => %s) FROM {BUILD}",
                (STALE_ACTIVE_SECONDS,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    cur.execute(f"SELECT pg_try_advisory_lock({LOCK})")
    free = bool(cur.fetchone()[0])
    if free:
        cur.execute(f"SELECT pg_advisory_unlock({LOCK})")
    elif not row[1]:
        return None   # its refresher is alive and within its time
    why = DEAD if free else STALE
    cur.execute(f"UPDATE {BUILD} SET active = false, failed_at = now(), why = %s", (why,))
    cur.execute("UPDATE ontology.link_marks_state SET why = %s WHERE phase <> 'kept'",
                (f"build failed: {why}",))
    if free:
        _drop_work(cur)
    return why


def refresh(dsn: str, *, workers: int = WORKERS, log: Any = None,
            settle: float | None = None) -> dict[str, Any]:
    """Rebuild the kept link slice beside the delta layers and swap it in (module
    docstring); the final ``status``, with ``busy`` when another refresh holds the lock.
    ``settle``: how long the swap waits for the counts of the last layer's commit to reach
    the shared counters (under the writers' lock)."""
    import psycopg
    settle = SETTLE_SECONDS if settle is None else settle
    schemes = _schemes()
    say = (lambda text: print(text, file=log, flush=True)) if log is not None \
        else (lambda text: None)
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            if not present(cur):
                return status(conn)
            cur.execute(f"SELECT pg_try_advisory_lock({LOCK})")
            if not cur.fetchone()[0]:
                return {**status(conn), "busy": True}
        began = time.monotonic()
        try:
            try:
                _build(conn, dsn, schemes, max(1, workers), settle, say)
            except BaseException as error:   # any failure stamped: the back-off holds
                why = str(error) if isinstance(error, _Failed) else \
                    f"{type(error).__name__}: {(str(error).splitlines() or [''])[0][:300]}"
                _fail(dsn, why, time.monotonic() - began)
                say(f"failed: {why}")
                if not isinstance(error, Exception):
                    raise   # interrupted: stamped, then on its way
            out = status(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute(f"SELECT pg_advisory_unlock({LOCK})")
    say(json.dumps(out, sort_keys=True, default=str))
    return out


def _fail(dsn: str, why: str, seconds: float) -> None:
    """The failure on the build record (its own connection: the builder's may be broken)
    and the build's tables dropped; the state left as it is (not kept)."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        _ensure_build(cur)
        with conn.transaction():
            cur.execute(f"UPDATE {BUILD} SET active = false, failed_at = now(), why = %s, "
                        "seconds = %s", (why, round(seconds, 1)))
            cur.execute("UPDATE ontology.link_marks_state SET why = %s "
                        "WHERE phase <> 'kept'", (f"build failed: {why}",))
        _drop_work(cur)


def _build(conn: Any, dsn: str, schemes: list[str], workers: int, settle: float,
           say: Any) -> None:
    import psycopg
    from operating.pg.ontology_reader import LINK_ODD_CAP, LINK_ODD_ROWS_SQL, LINK_ODD_SQL, \
        link_slice_sql
    began = time.monotonic()
    # 1. start: the chain's first link, under the writers' lock for one short transaction
    with conn.cursor() as cur:
        _ensure_build(cur)
        _drop_work(cur)   # a killed build's leftovers
        cur.execute(f"CREATE UNLOGGED TABLE {PARTS} ({WORK_ID}, LIKE ontology.link_marks "
                    "INCLUDING DEFAULTS)")
        cur.execute(f"CREATE UNLOGGED TABLE {CATCHUP} ({WORK_ID}, object_id text NOT NULL)")
        _take_writers(cur)
        try:
            with conn.transaction():
                cur.execute(f"UPDATE {BUILD} SET active = true, started_at = now(), "
                            "counter = ontology.link_marks_counter(), "
                            "data_version = ontology.sq_data_version(), "
                            "generation = ontology.link_marks_generation(), layers = 0, "
                            "broken = NULL, failed_at = NULL, why = NULL, seconds = NULL "
                            "RETURNING counter")
                if cur.fetchone()[0] is None:
                    raise _Failed(FAILED)
                cur.execute("UPDATE ontology.link_marks_state SET phase = 'building', "
                            "why = NULL WHERE phase <> 'building'")
        finally:
            cur.execute(f"SELECT pg_advisory_unlock({WRITERS_LOCK})")
    say("start: building beside the delta layers")
    # 2. read, no lock: each range appended to the staging table by one worker
    with conn.cursor() as cur:
        cur.execute(LINK_ODD_SQL, {"cap": LINK_ODD_CAP + 1})
        texts = cur.fetchone()[0] or []
        odd: list[str] | None = []
        if len(texts) > LINK_ODD_CAP:
            odd = None
        elif texts:
            cur.execute(LINK_ODD_ROWS_SQL, {"texts": texts})
            odd = list(cur.fetchone()[0] or [])
    if odd is None or len(odd) > LINK_ODD_CAP:
        # too many odd evidence texts for the joined read: every row composed
        slice_sql = (_ROW_SLICE + "WHERE (%(lo)s::text IS NULL OR l.link_id COLLATE "
                     "\"C\" >= %(lo)s) AND (%(hi)s::text IS NULL OR l.link_id "
                     "COLLATE \"C\" < %(hi)s)")
        odd = []
    else:
        slice_sql = link_slice_sql(ranged=True)
    cuts = _bounds(conn, workers * 4)
    spans = list(zip(cuts, cuts[1:]))
    stop = threading.Event()

    def add(span: tuple[str | None, str | None]) -> int:
        if stop.is_set():
            return 0
        with psycopg.connect(dsn, autocommit=True) as part:
            with part.transaction(), part.cursor() as cur:
                cur.execute("SET LOCAL enable_nestloop = off")
                cur.execute("SET LOCAL enable_sort = off")
                cur.execute(_STAGE.format(slice=slice_sql),
                            {"schemes": schemes, "odd": odd, "lo": span[0], "hi": span[1]})
                return cur.rowcount

    def broken() -> str | None:
        with conn.cursor() as cur:
            cur.execute(f"SELECT broken FROM {BUILD}")
            return cur.fetchone()[0]

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        pending = {pool.submit(add, span) for span in spans}
        done = 0
        while pending:
            finished, pending = wait_for(pending, timeout=30, return_when=FIRST_COMPLETED)
            for future in finished:
                future.result()   # a worker's error fails the build
                done += 1
            why = broken()
            if why:
                raise _Failed(why)
            if finished:
                say(f"read {done}/{len(spans)} ranges, {time.monotonic() - began:.0f} s")
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    # 3. merge, no lock: one keyed table, then the layers' records so far
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {NEXT} (LIKE ontology.link_marks INCLUDING DEFAULTS)")
        cur.execute(f"SET LOCAL max_parallel_workers_per_gather = {min(workers, 8)}")
        cur.execute(f"INSERT INTO {NEXT} (link_type, markings, handle, until) "
                    f"SELECT link_type, markings, handle, max(until) FROM {PARTS} "
                    "GROUP BY 1, 2, 3")
        cur.execute(f"SET LOCAL max_parallel_maintenance_workers = {min(workers, 8)}")
        cur.execute(f"ALTER TABLE {NEXT} ADD CONSTRAINT link_marks_next_pkey "
                    "PRIMARY KEY (link_type, markings, handle)")
        cur.execute(f"DROP TABLE {PARTS}")
        _copy_owner_and_grants(cur)
    say(f"merged, {time.monotonic() - began:.0f} s")
    _catch_up(conn, schemes)
    # 4. swap, under the writers' lock: the last layers' records, the chain checked
    for attempt in range(1, SWAP_TRIES + 1):
        if _swap(conn, schemes, settle, began, say):
            return
        if attempt == SWAP_TRIES:
            raise _Failed(f"readers held the old table through {SWAP_TRIES} swaps")
        say(f"swap {attempt}: a reader holds the old table; again")
        time.sleep(1)


def _swap(conn: Any, schemes: list[str], settle: float, began: float, say: Any) -> bool:
    """The swap under the writers' lock (released on return); False: a reader held the
    old table past ``SWAP_LOCK_TIMEOUT_MS`` (nothing changed)."""
    import psycopg
    with conn.cursor() as cur:
        _take_writers(cur)
    held = time.monotonic()
    try:
        caught = _catch_up(conn, schemes)
        with conn.cursor() as cur:
            deadline = time.monotonic() + settle
            while True:
                cur.execute(f"SELECT broken, counter = ontology.link_marks_counter(), "
                            "data_version = ontology.sq_data_version(), "
                            "generation IS NOT DISTINCT FROM "
                            f"ontology.link_marks_generation(), layers FROM {BUILD}")
                why, same_counter, same_version, same_generation, layers = cur.fetchone()
                if why:
                    raise _Failed(why)
                if same_counter and same_version and same_generation:
                    break
                if time.monotonic() >= deadline:
                    raise _Failed(FAILED)
                time.sleep(0.5)
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT_MS}ms'")
                cur.execute("DROP TABLE ontology.link_marks")
                cur.execute(f"ALTER TABLE {NEXT} RENAME TO link_marks")
                cur.execute("ALTER TABLE ontology.link_marks RENAME CONSTRAINT "
                            "link_marks_next_pkey TO link_marks_pkey")
                cur.execute("UPDATE ontology.link_marks_state SET phase = 'kept', "
                            "schemes = %s::text[], data_version = ontology.sq_data_version(), "
                            "generation = ontology.link_marks_generation(), "
                            "counter = ontology.link_marks_counter(), built_at = now(), "
                            "why = NULL", (schemes,))
                cur.execute(f"UPDATE {BUILD} SET active = false, kept_at = now(), "
                            "seconds = %s, why = NULL, failed_at = NULL",
                            (round(time.monotonic() - began, 1),))
                cur.execute(f"DROP TABLE {CATCHUP}")
        except psycopg.errors.LockNotAvailable:
            return False
        say(f"kept: {layers} layers caught up ({caught} rows at the swap), writers held "
            f"{time.monotonic() - held:.1f} s, {time.monotonic() - began:.0f} s in all")
        return True
    finally:
        with conn.cursor() as cur:
            cur.execute(f"SELECT pg_advisory_unlock({WRITERS_LOCK})")


def layer_start(cur: Any) -> dict[str, bool]:
    """In a delta layer's transaction (it holds the writers' lock), before it writes:
    whether the kept slice is current (``current``: ``fold_layer`` carries it) and whether
    a build under way may count this layer in its chain (``build``: the counter Postgres
    has now is the one the build or the last layer left; else the build is broken)."""
    out = {"current": vouched_now(cur), "build": False}
    if not present(cur):
        return out
    # this layer's counts shared as it commits, so the next layer (or the swap) finds them
    cur.execute("SELECT pg_stat_force_next_flush()")
    cur.execute(f"SELECT to_regclass('{BUILD}') IS NOT NULL")
    if not cur.fetchone()[0]:
        return out
    if _reset_dead(cur):
        return out
    cur.execute(f"SELECT to_regclass('{CATCHUP}') IS NOT NULL")
    if not cur.fetchone()[0]:
        return out
    deadline = time.monotonic() + LAYER_SETTLE_SECONDS
    while True:
        cur.execute(f"SELECT active AND broken IS NULL, counter IS NOT DISTINCT FROM "
                    f"ontology.link_marks_counter() AND counter IS NOT NULL FROM {BUILD} "
                    "FOR UPDATE")
        row = cur.fetchone()
        if not row or not row[0]:
            return out
        if row[1]:
            out["build"] = True
            return out
        if time.monotonic() >= deadline:   # a write outside the layers: the build is lost
            cur.execute(f"UPDATE {BUILD} SET broken = %s", (BROKEN,))
            return out
        time.sleep(0.25)   # the last commit's counts on their way to the shared counters


def vouched_now(cur: Any) -> bool:
    """Whether the kept slice is current before a layer writes (``fold_layer``)."""
    if not present(cur):
        return False
    cur.execute("SELECT ontology.link_marks_vouched(%s::text[])", (_schemes(),))
    return bool(cur.fetchone()[0])


def fold_layer(cur: Any, start: dict[str, bool] | bool,
               owned_table: str = "owned") -> int:
    """In a delta layer's transaction, after its writes and its data version moved: when
    the kept slice was current before the layer wrote (``start["current"]``), the links
    from or to the records in ``owned_table`` added to it and the state moved to the
    version and counter Postgres has now (this transaction's writes counted); else nothing
    (the state stays as it is and readers read link_rows). With a build under way that
    counted this layer (``start["build"]``), the records go to its catch-up table and its
    chain moves to this layer's counter and version. Rows added to the kept slice."""
    if isinstance(start, bool):
        start = {"current": start, "build": False}
    if start.get("build"):
        cur.execute(f"INSERT INTO {CATCHUP} (object_id) SELECT object_id FROM {owned_table}")
        cur.execute(f"UPDATE {BUILD} SET counter = ontology.link_marks_counter(), "
                    "data_version = ontology.sq_data_version(), layers = layers + 1")
    if not start.get("current"):
        return 0
    cur.execute(_ADD.format(table="ontology.link_marks", slice=_ends_slice(owned_table)),
                {"schemes": _schemes()})
    added = cur.rowcount
    cur.execute("UPDATE ontology.link_marks_state SET data_version = "
                "ontology.sq_data_version(), counter = ontology.link_marks_counter() "
                "WHERE phase = 'kept'")
    return added


def refresh_if_stale(dsn: str, *, retry_after: float | None = None,
                     **kw: Any) -> dict[str, Any]:
    """``refresh`` when a reader would not take the kept slice now; else its ``status``.
    Never by a role that may not write the kept rows (``"not_writable"``: the API's
    read-only role). ``retry_after``: not within that many seconds of a build that failed,
    or of the start of one no refresher holds any more (``"retry_later"``)."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        now = status(conn)
        if now["readers_use_it"] or now["phase"] is None:
            return now
        with conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege('ontology.link_marks', 'DELETE') AND "
                        "has_table_privilege('ontology.link_marks_state', 'UPDATE') AND "
                        "has_schema_privilege('ontology', 'CREATE')")
            if not cur.fetchone()[0]:
                return {**now, "not_writable": True}
            if retry_after is not None and _backing_off(cur, retry_after):
                return {**now, "retry_later": True}
    return refresh(dsn, **kw)


def _backing_off(cur: Any, retry_after: float) -> bool:
    """A build failed (``failed_at``) or began without finishing (``started_at`` of an
    ``active`` record) less than ``retry_after`` seconds ago."""
    cur.execute(f"SELECT to_regclass('{BUILD}') IS NOT NULL")
    if not cur.fetchone()[0]:
        return False
    cur.execute(f"SELECT extract(epoch FROM now() - greatest(failed_at, CASE WHEN active "
                f"THEN started_at END)) FROM {BUILD}")
    row = cur.fetchone()
    return bool(row and row[0] is not None and float(row[0]) < retry_after)


def after_apply(dsn: str, what: str, **kw: Any) -> dict[str, Any] | None:
    """The kept slice brought current at the end of an apply that wrote link_rows or moved
    the data version (``what``: train, steps, link_apply, link_partitions), so readers take
    it again without waiting for an API start. Same switch as ``start_refresh_if_stale``;
    errors go to stderr, never raised (the apply already committed; readers read
    link_rows until a refresh)."""
    if _off():
        return None
    try:
        out = refresh_if_stale(dsn, **kw)
    except Exception as error:  # noqa: BLE001 - the slice is read from link_rows
        print(f"link_marks after {what}: refresh failed: {type(error).__name__}",
              file=sys.stderr)
        return None
    print(f"link_marks after {what}: {json.dumps(out, sort_keys=True, default=str)}",
          file=sys.stderr)
    return out


def _off() -> bool:
    return os.environ.get("PLX_LINK_MARKS_REFRESH", "").strip().lower() in ("off", "0", "no")


_last_start = 0.0


def start_rebuild(dsn: str, *, retry_after: float = RETRY_SECONDS) -> str:
    """From a delta layer that found the kept slice not current, after it committed: one
    rebuild started in a detached process of its own (``refresh --if-stale``), never in
    the delta cycle; only once a build has kept it (the first one is run on purpose), not
    while one runs, and not within ``retry_after`` seconds of a failed one. What it did."""
    global _last_start
    import psycopg
    if _off():
        return "off"
    if _last_start and time.monotonic() - _last_start < 60:
        return "started_lately"
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        if not present(cur):
            return "absent"
        build = _build_row(cur)
        if build is None or build["kept_at"] is None:
            return "never_built"
        if _backing_off(cur, retry_after):
            return "retry_later"
        cur.execute(f"SELECT pg_try_advisory_lock({LOCK})")
        if not cur.fetchone()[0]:
            return "busy"
        cur.execute(f"SELECT pg_advisory_unlock({LOCK})")
    log_path = os.environ.get("PLX_LINK_MARKS_LOG") or os.path.join(
        tempfile.gettempdir(), "plx-link-marks-refresh.log")
    with open(log_path, "a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, "-m", "operating.pg.link_marks", "refresh", "--if-stale",
             "--retry-after", str(int(retry_after))],
            env={**os.environ, "PLX_PG_DSN": dsn}, stdin=subprocess.DEVNULL, stdout=log,
            stderr=log, start_new_session=True, close_fds=True,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    _last_start = time.monotonic()
    return "started"


def start_refresh_if_stale(dsn: str | None = None) -> threading.Thread | None:
    """An API start's background ``refresh_if_stale`` (one per database: the lock), unless
    ``PLX_LINK_MARKS_REFRESH=off``. Errors are logged, never raised: readers read
    link_rows meanwhile."""
    if _off():
        return None
    if dsn is None:
        from operating.pg import config
        dsn = config.dsn()

    def run() -> None:
        try:
            out = refresh_if_stale(dsn, retry_after=RETRY_SECONDS)
            print(f"link_marks: {json.dumps(out, sort_keys=True, default=str)}",
                  file=sys.stderr)
        except Exception as error:  # noqa: BLE001 - the slice is read from link_rows
            print(f"link_marks: refresh failed: {type(error).__name__}", file=sys.stderr)

    thread = threading.Thread(target=run, daemon=True, name="plx-link-marks")
    thread.start()
    return thread


def undo(dsn: str) -> None:
    """Drop what migration 0028 and the builds made (``UNDO_SQL``): readers read link_rows
    as before."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute(UNDO_SQL)


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.link_marks")
    parser.add_argument("command", choices=("status", "refresh", "undo"))
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--if-stale", action="store_true",
                        help="refresh only when readers would not take it now")
    parser.add_argument("--retry-after", type=float, default=None,
                        help="with --if-stale: not within this many seconds of a failure")
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.command == "undo":
        undo(dsn)
        print("link_marks: undone")
        return 0
    if args.command == "status":
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            print(json.dumps(status(conn), sort_keys=True, default=str))
        return 0
    if args.if_stale:
        out = refresh_if_stale(dsn, retry_after=args.retry_after, workers=args.workers,
                               log=sys.stderr)
    else:
        out = refresh(dsn, workers=args.workers, log=sys.stderr)
    print(json.dumps(out, sort_keys=True, default=str))
    return 0 if out["readers_use_it"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
