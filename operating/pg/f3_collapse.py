"""F3: collapse the no-op link history already in Postgres, once, set-based (GROWTH-AUDIT-1111).

    python3 -m operating.pg.f3_collapse estimate [--dsn D] [--percent 0.05]   # read-only share
    python3 -m operating.pg.f3_collapse sightings [--dsn D] --source portal_api=PATH ...
                                                 [--workers 8] [--slices 4]   # stage A
    python3 -m operating.pg.f3_collapse plan     [--dsn D]            # batches, rows, no writes
    python3 -m operating.pg.f3_collapse run      [--dsn D] [--workers 8] [--types T,..]
                                                 [--budget-minutes 110] [--dry-run]

Since F2 (``Projector._write_links``) a link is one row per stretch: consecutive pairs of end
versions of one link (same type, ends, tier, kind, contract, markings and decided features
without the versions they cite) whose spans meet. Rows written before F2 hold one row per
pair. This folds them in place, the rule F2 applies, without re-projecting anything:

* Rows: the projector's own (``link_id`` ``L:...``; step links such as ``mention:...`` are
  ranged by their id prefix and are never touched) without a review.
* Identity (``IDENT_SQL``): md5 of type, from, to, tier, binding kind, contract, markings and
  the reason and why-not features with each feature's ``citations``, ``from_handle`` and
  ``to_handle`` left out (F2's ``_link_ident`` less the binding index and join record, which
  Postgres does not hold), and a property conflict's ``from``/``to`` handle of a version the
  object stage folded read as the version it folded into (``ontology.f3_handle_map``), as
  the projector cites the folded version: so objects run first, links after all of them. A group of one identity whose rows overlap in time (two bindings
  or join records giving the same link at once) is left alone, so the missing parts can
  never merge two links.
* Stretch: rows of one identity in start order, each starting where the one before ended.
  The stretch's last row is kept (its citations and evidence are the newest versions', as
  F2 writes) under its own id, starting at the first row's start; the others are deleted.
  An identity with a row another row names by id (a ``keyjoin:`` confirmed lead's
  ``stored_lead``) is left whole, like a reviewed one.
* Batches: per link type, per ``from_object`` range (the histogram bounds of the column),
  each its own transaction holding the delta's lock shared (``ontology_delta`` takes it
  exclusive: no layer applies while a batch runs, batches run side by side); a finished
  batch is marked in ``ontology.load_state`` (``f3:links:<type>:<n>``), so a run resumes
  and a re-run folds nothing twice. A run stops starting batches after ``--budget-minutes``.
* After: ``VACUUM (ANALYZE) ontology.link_rows`` (space reusable); returning it to the disk
  needs a rewrite (F4).

The delta's later apply of an object replaces its links with the projector's own rows (ids
from the binding index and join record too): the folded rows are a consistent state, not a
second history. No id is renamed, and no row another names is deleted: keyjoin step links
keep the ``L:`` id they lead from (``ontology/key_links.py``, read by
``ontology/link_keys.py``).
Undo: every batch writes its rows' before-images to ``--undo-dir`` first (``Undo``);
``undo`` puts them back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ontology import parent_watch

LOCK = "SELECT pg_advisory_xact_lock_shared(hashtext('ontology_delta'))"

# A link feature list without the versions it cites (F2 ``_VERSION_KEYS``); '' for none.
# Only a text naming a ``from``/``to`` object (a property conflict's end versions, stored
# compact or spaced) has handles to read through the handle map: the others skip the
# per-feature map lookups (most rows; the 10% rehearsal's links ran 2.5x slower with them).
BARE_FN = """
CREATE OR REPLACE FUNCTION pg_temp.f3_kept(h text) RETURNS text LANGUAGE sql STABLE AS $f$
  SELECT coalesce((SELECT kept FROM ontology.f3_handle_map WHERE dropped = h), h)
$f$;
CREATE OR REPLACE FUNCTION pg_temp.f3_side(e jsonb, side text) RETURNS jsonb
LANGUAGE sql STABLE AS $f$
  SELECT CASE WHEN jsonb_typeof(e -> side) = 'object' AND (e -> side) ? 'l1_handle'
              THEN jsonb_set(e, ARRAY[side, 'l1_handle'],
                             to_jsonb(pg_temp.f3_kept(e -> side ->> 'l1_handle')))
              ELSE e END
$f$;
CREATE OR REPLACE FUNCTION pg_temp.f3_bare(t text) RETURNS text LANGUAGE sql STABLE AS $f$
  SELECT CASE WHEN t IS NULL THEN '' WHEN t IS JSON ARRAY THEN coalesce((
    SELECT jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' AND remap
                          THEN pg_temp.f3_side(pg_temp.f3_side(
                                 e - 'citations' - 'from_handle' - 'to_handle' - '_cite',
                                 'from'), 'to')
                          WHEN jsonb_typeof(e) = 'object'
                          THEN e - 'citations' - 'from_handle' - 'to_handle' - '_cite'
                          ELSE e END ORDER BY o)::text
      FROM jsonb_array_elements(t::jsonb) WITH ORDINALITY AS a(e, o),
           (SELECT t ~ REMAP_GATE AS remap) g), '[]')
  ELSE t END
$f$""".replace("REMAP_GATE", "'\"(from|to)\"[[:space:]]*:[[:space:]]*\\{'")

IDENT_SQL = ("md5(concat_ws(chr(31), l.link_type, l.from_object, l.to_object, l.tier, "
             "coalesce(l.binding_kind, ''), coalesce(l.link_contract, ''), "
             "coalesce(l.markings, ''), "
             "pg_temp.f3_bare(coalesce(l.reason, ontology.link_text(l.reason_id, l.slots, "
             "l.ek + 1, 64))), pg_temp.f3_bare(l.why_not)))")

# A row another row of its batch names by id (a confirmed lead's ``stored_lead``: a
# ``keyjoin:`` step link has its lead's type and ends, so it is read in the lead's batch;
# ``ontology/link_keys.py`` reads the lead's keys by that id) is held like a reviewed one:
# its identity is left whole, so no named id is ever deleted.
STAGE_SQL = f"""
CREATE TEMP TABLE f3_rows ON COMMIT DROP AS
WITH b AS (
  SELECT l.* FROM ontology.link_rows l
   WHERE l.link_type = %(t)s AND l.from_object >= %(lo)s
     AND (%(hi)s::text IS NULL OR l.from_object < %(hi)s)
), named AS (
  SELECT DISTINCT m[1] AS link_id
    FROM b, regexp_matches(coalesce(b.reason, ontology.link_text(b.reason_id, b.slots,
                                                                  b.ek + 1, 64), ''),
                           '"link_id": ?"([^"]+)"', 'g') m
   WHERE b.link_id NOT LIKE 'L:%%'
)
SELECT l.link_id, l.recorded_at, l.superseded_at, {IDENT_SQL} AS ident,
       (l.reviewed_by IS NOT NULL OR l.reviewed_at IS NOT NULL
        OR l.review_action_id IS NOT NULL
        OR l.link_id IN (SELECT link_id FROM named)) AS reviewed
  FROM b l
 WHERE l.link_id LIKE 'L:%%'"""

# Per row: its island (stretch) within its identity; identities that overlap or hold a
# review are left out whole.
PLAN_SQL = """
CREATE TEMP TABLE f3_plan ON COMMIT DROP AS
WITH o AS (
  SELECT r.*, lag(r.superseded_at) OVER w AS prev_end, lag(r.link_id) OVER w AS prev_id
    FROM f3_rows r WINDOW w AS (PARTITION BY ident ORDER BY recorded_at, link_id)
), ok AS (
  SELECT ident FROM o GROUP BY ident
  HAVING count(*) > 1 AND NOT bool_or(reviewed)
     AND bool_and(prev_id IS NULL OR (prev_end IS NOT NULL AND prev_end <= recorded_at))
), s AS (
  SELECT o.*, sum(CASE WHEN prev_id IS NOT NULL AND prev_end = recorded_at THEN 0 ELSE 1 END)
              OVER (PARTITION BY o.ident ORDER BY recorded_at, link_id) AS island
    FROM o JOIN ok USING (ident)
)
SELECT link_id, ident, island,
       first_value(recorded_at) OVER i AS start,
       row_number() OVER (PARTITION BY ident, island ORDER BY recorded_at DESC, link_id DESC) AS from_end,
       count(*) OVER (PARTITION BY ident, island) AS n
  FROM s WINDOW i AS (PARTITION BY ident, island ORDER BY recorded_at, link_id)"""

# Each step: ("sql", text) on temp tables; ("delete"/"update", table, alias, from, where[,
# set]) on an ontology table, its rows' before-images (and an update's new keys) written
# to the batch's undo file first (``_apply``); ("map", text): handle map rows added
# (their keys kept, so an undo takes them out again).
LINK_STEPS = (
    # the kept row keeps its id (only its start moves back): an id read anywhere else (a
    # gap's citation, a step link's lead, an action's undo row) still finds the link
    ("update", "link_rows", "l", "f3_plan p", "p.n > 1 AND p.from_end = 1 AND "
     "l.link_id = p.link_id", "recorded_at = p.start"),
    ("delete", "link_rows", "l", "f3_plan p",
     "p.n > 1 AND p.from_end > 1 AND l.link_id = p.link_id"),
)
COUNT_SQL = ("SELECT (SELECT count(*) FROM f3_rows), "
             "coalesce(sum(CASE WHEN n > 1 AND from_end > 1 THEN 1 ELSE 0 END), 0), "
             "coalesce(sum(CASE WHEN n > 1 AND from_end = 1 THEN 1 ELSE 0 END), 0), "
             "(SELECT count(*) FROM (SELECT ident FROM f3_rows GROUP BY ident "
             " HAVING count(*) > 1) g) - (SELECT count(DISTINCT ident) FROM f3_plan) "
             "FROM f3_plan")


# The working tables are logged (a crash keeps them with the marks that count on them;
# unlogged, they came back empty after a crash and the stages after folded nothing,
# silently); one left unlogged by an older run is made logged. The batch plan is saved
# once and read by every run after it (``_saved_plan``).
SIGHTINGS_DDL = ("CREATE TABLE IF NOT EXISTS ontology.f3_sightings ("
                 "handle text PRIMARY KEY, relation text NOT NULL, sighting text NOT NULL)")
HANDLE_MAP_DDL = ("CREATE TABLE IF NOT EXISTS ontology.f3_handle_map ("
                  "dropped text PRIMARY KEY, kept text NOT NULL)")
PLAN_DDL = ("CREATE TABLE IF NOT EXISTS ontology.f3_batches (phase text NOT NULL, "
            "n int NOT NULL, key text NOT NULL, lo text NOT NULL, hi text, "
            "PRIMARY KEY (phase, n))")


def _ensure_tables(conn: Any) -> None:
    for ddl in (SIGHTINGS_DDL, HANDLE_MAP_DDL, PLAN_DDL):
        conn.execute(ddl)
    for name in ("f3_sightings", "f3_handle_map"):
        if conn.execute("SELECT relpersistence FROM pg_class WHERE oid = %s::regclass",
                        (f"ontology.{name}",)).fetchone()[0] == "u":
            conn.execute(f"ALTER TABLE ontology.{name} SET LOGGED")


SIGHTINGS_MARK = "f3:sightings:"
SIGHTINGS_DONE = "f3:sightings:done"


class KeySlice:
    """Slice ``k`` of ``n`` of the raw rows a capture source's record queries step over
    (passed as its ``sample``, before any row is decoded): the rows whose first column
    hashes to ``k`` (the record key in the mirror's query; BC's is the capture id; Teams
    has no hook and is read whole by slice 0). A row's first column is the same in every
    worker's snapshot, so the slices never overlap; sightings are per row, so any such
    column partitions them."""

    def __init__(self, k: int, n: int) -> None:
        self.k, self.n, self.rows, self.kept = k, n, 0, 0

    def __call__(self, rows: Any) -> Any:
        import zlib
        for row in rows:
            self.rows += 1
            if zlib.crc32(str(row[0]).encode("utf-8")) % self.n == self.k:
                self.kept += 1
                yield row


def _open_capture(source: str, path: str) -> Any:
    from ontology import project
    return project.capture_source(source, path)


def sightings_tasks(declared: Any, paths: dict[str, str], slices: int,
                    skip: tuple[str, ...] = ()) -> list[tuple[str, str, str, int, int]]:
    """(source, path, relation, k, n) for every declared relation of the sources given:
    each relation cut into ``slices`` key slices. A declared source neither given nor
    skipped by name is refused (an incomplete stage A would leave its versions unfolded
    without saying so)."""
    if slices < 1:
        raise ValueError("f3_slices_invalid: --slices must be 1 or more")
    from ontology import project
    unknown = sorted(set(paths) - set(project.CAPTURE_SOURCES))
    if unknown:
        raise ValueError(f"f3_source_unknown: {', '.join(unknown)}")
    missing = sorted({src for src, _ in declared} - set(paths) - set(skip))
    if missing:
        raise ValueError(f"f3_sources_missing: {', '.join(missing)} (give a capture path "
                         "or skip it by name)")
    return [(src, paths[src], rel, k, slices) for (src, rel) in sorted(declared)
            if src in paths for k in range(slices)]


def load_sighting_slice(dsn: str, task: tuple[str, str, str, int, int],
                        opener: Any = None, declared: Any = None) -> dict[str, Any]:
    """One stage A slice in one transaction with its mark (``f3:sightings:<source>:
    <relation>:<k>/<n>``): a slice marked before is skipped, a failed one leaves no row.
    A source with no ``sample`` hook is read whole by slice 0 (the others hold nothing)."""
    import hashlib
    import psycopg
    from ontology import stamps as source_stamps
    source, path, relation, k, n = task
    stamps = (declared or source_stamps.load())[(source, relation)]
    mark = f"{SIGHTINGS_MARK}{source}:{relation}:{k}/{n}"
    t = time.monotonic()
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            if conn.execute("SELECT 1 FROM ontology.load_state WHERE name = %s",
                            (mark,)).fetchone():
                return {"slice": mark, "done_before": True}
            other = conn.execute(
                "SELECT name FROM ontology.load_state WHERE name LIKE %s AND name NOT LIKE %s "
                "LIMIT 1", (f"{SIGHTINGS_MARK}{source}:{relation}:%", f"%/{n}")).fetchone()
            if other:   # loaded under another slice count: its rows would be loaded twice
                raise RuntimeError(f"f3_slices_changed: {other[0]} (re-run with that count)")
            src = (opener or _open_capture)(source, path)
            rows = 0
            try:
                sliced = n > 1 and hasattr(src, "sample")
                if sliced:
                    src.sample = KeySlice(k, n)
                if sliced or k == 0:
                    with conn.cursor().copy("COPY ontology.f3_sightings (handle, relation, "
                                            "sighting) FROM STDIN") as copy:
                        for rec in src.records([relation])[relation]:
                            key = stamps.sighting_key(rec.row) + "|" + rec.audience_key
                            copy.write_row((rec.l1_handle, f"{source}:{relation}",
                                            hashlib.sha256(key.encode()).hexdigest()[:32]))
                            rows += 1
            finally:
                close = getattr(src, "close", None)
                if close is not None:
                    close()
            out = {"slice": mark, "rows": rows, "sliced": sliced}
            conn.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                         (mark, json.dumps(out)))
    out["seconds"] = round(time.monotonic() - t, 2)
    return out


def load_sightings(dsn: str, paths: dict[str, str], *, workers: int = 8, slices: int = 4,
                   skip: tuple[str, ...] = (), opener: Any = None, declared: Any = None,
                   budget_minutes: float = 110, log: Any = sys.stderr) -> dict[str, Any]:
    """Stage A (read-only on the captures): every captured version of every relation with
    declared stamps (``ontology.stamps``), as (handle, relation, sighting) in
    ``ontology.f3_sightings``, ``workers`` processes over (relation, key slice) tasks. The
    sighting is what the projector folds by (``Projector._sightings``): the row without
    its stamps and the record's audience. When every task is marked, ``f3:sightings:done``
    is written; the object stage refuses to start without it."""
    from concurrent.futures import ProcessPoolExecutor
    import psycopg
    from ontology import stamps as source_stamps
    declared = declared or source_stamps.load()
    tasks = sightings_tasks(declared, paths, slices, skip)
    with psycopg.connect(dsn, autocommit=True) as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM ontology.load_state WHERE name = %s", (SIGHTINGS_DONE,))
    done: list[dict[str, Any]] = []
    started = time.monotonic()
    stop_at = started + budget_minutes * 60
    todo = list(tasks)
    with ProcessPoolExecutor(max_workers=max(1, workers),
                             **parent_watch.pool_init()) as pool:
        pending: set[Any] = set()
        while todo or pending:
            # a slice starts only within the budget; the running ones finish (each its
            # own transaction), the rest wait for the next run
            while todo and len(pending) < max(1, workers) and time.monotonic() < stop_at:
                pending.add(pool.submit(load_sighting_slice, dsn, todo.pop(0), opener,
                                        declared))
            if not pending:
                break
            fut = next(as_completed(pending))
            pending.discard(fut)
            done.append(fut.result())   # a failed slice raises: no done mark
            print(json.dumps(done[-1]), file=log, flush=True)
    if todo:
        return {"tasks": len(tasks), "left": len(todo), "done": False,
                "seconds": round(time.monotonic() - started, 1)}
    with psycopg.connect(dsn, autocommit=True) as conn:
        loaded = conn.execute("SELECT count(*) FROM ontology.f3_sightings").fetchone()[0]
        marked = conn.execute("SELECT coalesce(sum((value->>'rows')::bigint), 0) FROM "
                              "ontology.load_state WHERE name LIKE %s AND name <> %s",
                              (f"{SIGHTINGS_MARK}%", SIGHTINGS_DONE)).fetchone()[0]
        per: dict[str, int] = {}
        for name, rows in conn.execute(
                "SELECT name, (value->>'rows')::bigint FROM ontology.load_state WHERE name "
                "LIKE %s AND name <> %s", (f"{SIGHTINGS_MARK}%", SIGHTINGS_DONE)):
            relation = ":".join(name[len(SIGHTINGS_MARK):].split(":")[:2])
            per[relation] = per.get(relation, 0) + int(rows or 0)
        if int(marked) != int(loaded):   # rows outside the marked slices: refuse
            raise RuntimeError(f"f3_sightings_mismatch: table {loaded}, marks {marked}")
        summary = {"tasks": len(tasks), "left": 0, "done": True, "rows": int(loaded),
                   "relations": per,
                   "skipped_sources": sorted(skip), "seconds":
                   round(time.monotonic() - started, 1)}
        conn.execute("ANALYZE ontology.f3_sightings")
        conn.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s) "
                     "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                     (SIGHTINGS_DONE, json.dumps(summary)))
    return summary


# Stage B, per object type and object id range: versions of one object from one relation
# with declared stamps, in start order; a version continues the one before when its
# sighting is the same and it starts where that one ended (``Projector._sightings``).
OBJ_STAGE_SQL = """
CREATE TEMP TABLE f3_v ON COMMIT DROP AS
SELECT v.ver, v.object_id, v.l1_handle, v.recorded_at, v.superseded_at, s.relation, s.sighting
  FROM ontology.object_versions v JOIN ontology.f3_sightings s ON s.handle = v.l1_handle
 WHERE v.object_id >= %(lo)s AND v.object_id < %(hi)s"""

OBJ_PLAN_SQL = """
CREATE TEMP TABLE f3_vp ON COMMIT DROP AS
WITH o AS (
  SELECT f.*, lag(sighting) OVER w AS prev_s, lag(superseded_at) OVER w AS prev_end,
         lag(ver) OVER w AS prev_ver
    FROM f3_v f WINDOW w AS (PARTITION BY object_id, relation ORDER BY recorded_at, ver)
), bad AS (   -- overlapping versions: the projector projects none of them; leave alone
  SELECT DISTINCT object_id, relation FROM o
   WHERE prev_ver IS NOT NULL AND (prev_end IS NULL OR prev_end > recorded_at)
), s AS (
  SELECT o.*, sum(CASE WHEN prev_ver IS NOT NULL AND prev_s = sighting
                        AND prev_end = recorded_at THEN 0 ELSE 1 END)
              OVER (PARTITION BY object_id, relation ORDER BY recorded_at, ver) AS island
    FROM o WHERE NOT EXISTS (SELECT 1 FROM bad b WHERE b.object_id = o.object_id
                                                   AND b.relation = o.relation)
)
SELECT ver, object_id, relation, island, l1_handle, recorded_at AS old_start,
       first_value(recorded_at) OVER i AS start,
       row_number() OVER (PARTITION BY object_id, relation, island
                          ORDER BY recorded_at DESC, ver DESC) AS from_end,
       count(*) OVER (PARTITION BY object_id, relation, island) AS n
  FROM s WINDOW i AS (PARTITION BY object_id, relation, island ORDER BY recorded_at, ver)"""

# ``ontology.project.event_id_of`` of an event row ``e`` as a version of ``p.object_id``
# yields it (a date event: ``part`` is its time path, ``field_path`` less the record's
# prefix ``row.``, ``key.`` or ``rows.<n>.``)
EVENT_ID_SQL = (
    "'E:' || left(encode(sha256(convert_to(e.event_type || '|' || e.l1_handle || '|' || "
    r"p.object_id || '|' || regexp_replace(e.field_path, '^(row|key|rows\.[0-9]+)\.', ''), "
    "'UTF8')), 'hex'), 40)")

OBJ_STEPS = (
    ("sql", "DELETE FROM f3_vp WHERE n = 1"),
    ("sql", "CREATE INDEX ON f3_vp (ver)"),
    # what a link cites of a dropped version reads as the version it folded into
    ("map", "INSERT INTO ontology.f3_handle_map (dropped, kept) SELECT d.l1_handle, "
     "k.l1_handle FROM f3_vp d JOIN f3_vp k ON k.object_id = d.object_id AND k.relation = "
     "d.relation AND k.island = d.island AND k.from_end = 1 WHERE d.from_end > 1 "
     "ON CONFLICT (dropped) DO NOTHING RETURNING dropped"),
    ("sql", "ANALYZE f3_vp"),
    # the dropped versions' value cells and sources
    ("delete", "value_cells", "c", "ontology.value_sources s, f3_vp p",
     "p.from_end > 1 AND s.ver = p.ver AND c.src = s.src"),
    ("delete", "value_sources", "s", "f3_vp p", "p.from_end > 1 AND s.ver = p.ver"),
    ("update", "value_sources", "s", "f3_vp p",
     "p.from_end = 1 AND s.ver = p.ver AND s.recorded_at = p.old_start",
     "recorded_at = p.start"),
    # identifiers cite the version's handle and span
    ("delete", "identifiers", "i", "f3_vp p", "p.from_end > 1 AND i.object_id = p.object_id "
     "AND i.l1_handle = p.l1_handle AND i.recorded_at = p.old_start"),
    ("update", "identifiers", "i", "f3_vp p", "p.from_end = 1 AND i.object_id = p.object_id "
     "AND i.l1_handle = p.l1_handle AND i.recorded_at = p.old_start", "recorded_at = p.start"),
    # events read off a version's values, of that object (not capture-observed changes,
    # which cite the capture that first held the value: the run's first, as before; nor
    # another object's event citing the same handle, whose versions may not be folded).
    # The object's own: its id is the one the projector gives a version of that object
    # (``ontology.project.event_id_of``: type, handle, object, time path), whatever other
    # objects it names through a link (``ItemLedgerEntry.posted``'s item): those fold too
    ("sql", "CREATE TEMP TABLE f3_ev ON COMMIT DROP AS SELECT e.event_id, p.from_end, "
     "p.start FROM ontology.events e JOIN f3_vp p ON e.l1_handle = p.l1_handle "
     "AND e.recorded_at = p.old_start WHERE e.time_basis <> 'capture_observed' "
     "AND e.event_id = " + EVENT_ID_SQL),
    ("delete", "event_objects", "o", "f3_ev x", "x.from_end > 1 AND o.event_id = x.event_id"),
    ("delete", "events", "e", "f3_ev x", "x.from_end > 1 AND e.event_id = x.event_id"),
    ("update", "events", "e", "f3_ev x", "x.from_end = 1 AND e.event_id = x.event_id",
     "recorded_at = x.start"),
    ("delete", "object_versions", "v", "f3_vp p", "p.from_end > 1 AND v.ver = p.ver"),
    ("update", "object_versions", "v", "f3_vp p", "p.from_end = 1 AND v.ver = p.ver",
     "recorded_at = p.start"),
)


class Undo:
    """A batch's undo file (``<dir>/<mark>.jsonl.zst``, zstd): one JSON line per row,
    ``{"t": table, "k": "pre"|"post", "s": step, "r": row}``: ``pre`` the row as it was
    before the step (to_jsonb), ``post`` the key an update or a map insert left. Written
    beside the batch's transaction, renamed into place only before it commits; a
    crashed batch leaves a ``.part`` and no mark (``undo`` reads marked batches only)."""

    def __init__(self, directory: str, mark: str) -> None:
        import subprocess
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", mark)
        self.path = os.path.join(directory, safe + ".jsonl.zst")
        self.part = self.path + ".part"
        self.out = open(self.part, "wb")
        self.proc = subprocess.Popen(["zstd", "-q", "-3", "-c"], stdin=subprocess.PIPE,
                                     stdout=self.out)
        self.rows = 0

    def write(self, table: str, kind: str, step: int, row: Any) -> None:
        self.proc.stdin.write((json.dumps({"t": table, "k": kind, "s": step, "r": row},
                                          separators=(",", ":"), default=str) + "\n"
                               ).encode("utf-8"))
        self.rows += 1

    def finish(self) -> dict[str, Any]:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError("f3_undo_write_failed: zstd")
        self.out.flush()
        os.fsync(self.out.fileno())
        self.out.close()
        os.replace(self.part, self.path)
        dir_fd = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return {"undo": os.path.basename(self.path), "undo_rows": self.rows,
                "undo_bytes": os.path.getsize(self.path)}

    def abandon(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait()
        self.out.close()
        if os.path.exists(self.part):
            os.remove(self.part)


def _key_columns(conn: Any, table: str) -> list[str]:
    row = conn.execute(
        "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_index i, unnest(i.indkey) WITH "
        "ORDINALITY AS k(attnum, ord) JOIN pg_attribute a ON a.attnum = k.attnum WHERE "
        "i.indrelid = %s::regclass AND a.attrelid = i.indrelid AND i.indisprimary",
        (f"ontology.{table}",)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"f3_undo_no_key: ontology.{table} has no primary key")
    return list(row[0])


def _apply(conn: Any, steps: tuple, undo: "Undo | None", out: dict[str, Any]) -> None:
    """Run ``steps`` in the batch's transaction, the undo written first (None: dry run
    accounting only is not supported here; the caller rolls back before)."""
    for n, step in enumerate(steps):
        kind = step[0]
        if kind == "sql":
            conn.execute(step[1])
            continue
        if kind == "map":
            for (key,) in conn.cursor().stream(step[1]):
                undo.write("f3_handle_map", "post", n, {"dropped": key})
                out["f3_handle_map.inserted"] = out.get("f3_handle_map.inserted", 0) + 1
            continue
        table, alias, source, where = step[1:5]
        if kind == "delete":
            sql = (f"DELETE FROM ontology.{table} {alias} USING {source} WHERE {where} "
                   f"RETURNING to_jsonb({alias})")
            count = 0
            for (row,) in conn.cursor().stream(sql):
                undo.write(table, "pre", n, row)
                count += 1
            out[f"{table}.deleted"] = out.get(f"{table}.deleted", 0) + count
            continue
        keys = _key_columns(conn, table)
        for (row,) in conn.cursor().stream(
                f"SELECT DISTINCT to_jsonb({alias}) FROM ontology.{table} {alias}, {source} "
                f"WHERE {where}"):
            undo.write(table, "pre", n, row)
        returning = ", ".join(f"{alias}.{k}" for k in keys)
        count = 0
        for row in conn.cursor().stream(
                f"UPDATE ontology.{table} {alias} SET {step[5]} FROM {source} WHERE {where} "
                f"RETURNING {returning}"):
            undo.write(table, "post", n, dict(zip(keys, row)))
            count += 1
        out[f"{table}.updated"] = out.get(f"{table}.updated", 0) + count


def object_batches(conn: Any, parts: int = 20) -> list[tuple[str, int, str, str]]:
    """(type, n, lo, hi): each object type's id range (``<Type>:`` to ``<Type>;``) cut at
    the histogram bounds of ``object_versions.object_id`` that fall inside it."""
    if conn.execute("SELECT to_regclass('ontology.f3_sightings')").fetchone()[0] is None:
        raise RuntimeError("f3_sightings_missing: load the sightings (stage A) first")
    types = [r[0] for r in conn.execute(
        "SELECT DISTINCT split_part(v.object_id, ':', 1) FROM ontology.object_versions v "
        "JOIN ontology.f3_sightings s ON s.handle = v.l1_handle")]
    bounds = conn.execute(
        "SELECT histogram_bounds::text::text[] FROM pg_stats WHERE schemaname = 'ontology' "
        "AND tablename = 'object_versions' AND attname = 'object_id'").fetchone()
    cuts = sorted(set(bounds[0] or [])) if bounds and bounds[0] else []
    out = []
    for t in sorted(types):
        lo, hi = t + ":", t + ";"
        inner = [c for c in cuts if lo < c < hi]
        step = max(1, len(inner) // parts) if inner else 1
        edges = [lo] + inner[step::step] + [hi]
        out += [(t, n, a, b) for n, (a, b) in enumerate(zip(edges, edges[1:]))]
    return out


def run_object_batch(dsn: str, batch: tuple[str, int, str, str], dry: bool,
                     undo_dir: str | None = None) -> dict[str, Any]:
    import psycopg
    object_type, n, lo, hi = batch
    mark = f"f3:objects:{object_type}:{n}"
    t = time.monotonic()
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(LOCK)
            if conn.execute("SELECT 1 FROM ontology.load_state WHERE name = %s",
                            (mark,)).fetchone():
                return {"batch": mark, "done_before": True}
            conn.execute(OBJ_STAGE_SQL, {"lo": lo, "hi": hi})
            conn.execute(OBJ_PLAN_SQL)
            versions, dropped = conn.execute(
                "SELECT (SELECT count(*) FROM f3_v), count(*) FILTER (WHERE n > 1 AND "
                "from_end > 1) FROM f3_vp").fetchone()
            out: dict[str, Any] = {"batch": mark, "versions": versions,
                                   "versions_deleted": int(dropped)}
            if dry:
                conn.rollback()
            else:
                _apply_marked(conn, OBJ_STEPS, undo_dir, mark, out)
    out["seconds"] = round(time.monotonic() - t, 2)
    return out


# The live share of link rows F3 deletes, read-only: objects sampled by block from
# ``ontology.objects`` (rows of near-equal size, so near-uniform over objects; sampling
# ``link_rows`` blocks picks objects in proportion to their rows and over-states the
# share), all their ``L:`` rows read by the from-object index, the plan computed in one
# READ ONLY transaction per chunk (no temp table, no function: plain CTEs) under a temp
# file and statement limit. The handle map is not read (objects not folded yet), so the
# share is a lower bound of the links' (conflicts over folded versions fold too).
def _bare_sql(expr: str) -> str:
    """``pg_temp.f3_bare`` of ``expr`` inline, less the handle map (read-only use)."""
    return ("(SELECT CASE WHEN x.t IS NULL THEN '' WHEN x.t IS JSON ARRAY THEN coalesce(("
            "SELECT jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' THEN e - 'citations' - "
            "'from_handle' - 'to_handle' - '_cite' ELSE e END ORDER BY o)::text FROM "
            "jsonb_array_elements(x.t::jsonb) WITH ORDINALITY AS a(e, o)), '[]') "
            f"ELSE x.t END FROM (SELECT {expr} AS t) x)")


ESTIMATE_SQL = f"""
WITH b AS (
  SELECT l.* FROM ontology.link_rows l WHERE l.from_object = ANY(%(ids)s)
), named AS (
  SELECT DISTINCT m[1] AS link_id
    FROM b, regexp_matches(coalesce(b.reason, ontology.link_text(b.reason_id, b.slots,
                                                                  b.ek + 1, 64), ''),
                           '"link_id": ?"([^"]+)"', 'g') m
   WHERE b.link_id NOT LIKE 'L:%%'
), r AS (
  SELECT l.from_object, l.link_id, l.recorded_at, l.superseded_at, pg_column_size(l.*) AS sz,
         md5(concat_ws(chr(31), l.link_type, l.from_object, l.to_object, l.tier,
             coalesce(l.binding_kind, ''), coalesce(l.link_contract, ''),
             coalesce(l.markings, ''),
             {_bare_sql("coalesce(l.reason, ontology.link_text(l.reason_id, l.slots, l.ek + 1, 64))")},
             {_bare_sql("l.why_not")})) AS ident,
         (l.reviewed_by IS NOT NULL OR l.reviewed_at IS NOT NULL
          OR l.review_action_id IS NOT NULL
          OR l.link_id IN (SELECT link_id FROM named)) AS reviewed
    FROM b l
   WHERE l.link_id LIKE 'L:%%'
), o AS (
  SELECT r.*, lag(superseded_at) OVER w AS prev_end, lag(link_id) OVER w AS prev_id
    FROM r WINDOW w AS (PARTITION BY ident ORDER BY recorded_at, link_id)
), ok AS (
  SELECT ident FROM o GROUP BY ident
  HAVING count(*) > 1 AND NOT bool_or(reviewed)
     AND bool_and(prev_id IS NULL OR (prev_end IS NOT NULL AND prev_end <= recorded_at))
), s AS (
  SELECT o.*, sum(CASE WHEN prev_id IS NOT NULL AND prev_end = recorded_at THEN 0 ELSE 1 END)
              OVER (PARTITION BY o.ident ORDER BY recorded_at, link_id) AS island
    FROM o JOIN ok USING (ident)
), p AS (
  SELECT sz, row_number() OVER (PARTITION BY ident, island
                                ORDER BY recorded_at DESC, link_id DESC) AS from_end FROM s
)
SELECT (SELECT count(*) FROM r), (SELECT coalesce(sum(sz), 0) FROM r),
       count(*) FILTER (WHERE from_end > 1), coalesce(sum(sz) FILTER (WHERE from_end > 1), 0),
       (SELECT count(DISTINCT from_object) FROM r)
  FROM p"""


def estimate(dsn: str, percent: float = 0.05, seed: int = 7, chunk: int = 500,
             temp_limit: str = "2GB", timeout: str = "10min",
             log: Any = sys.stderr) -> dict[str, Any]:
    """The share of ``L:`` link rows and row bytes F3 would delete, read-only (above)."""
    import psycopg
    if not 0 < percent <= 100:
        raise ValueError("f3_percent_invalid: 0 < percent <= 100")
    totals = [0, 0, 0, 0, 0]
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        ids = [r[0] for r in conn.execute(
            "SELECT object_id FROM ontology.objects TABLESAMPLE SYSTEM (%s) REPEATABLE (%s)",
            (percent, seed))]
        conn.rollback()
        started = time.monotonic()
        for n in range(0, len(ids), chunk):
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL temp_file_limit = '{temp_limit}'")
                conn.execute(f"SET LOCAL statement_timeout = '{timeout}'")
                row = conn.execute(ESTIMATE_SQL, {"ids": ids[n:n + chunk]}).fetchone()
                totals = [a + int(b or 0) for a, b in zip(totals, row)]
            if (n // chunk) % 20 == 0:
                print(json.dumps({"objects_read": min(n + chunk, len(ids)),
                                  "of": len(ids)}), file=log, flush=True)
    rows, size, gone, gone_size, linked = totals
    return {"sampled_objects": len(ids), "objects_with_links": linked, "rows": rows,
            "row_bytes": size, "deletable_rows": gone, "deletable_bytes": gone_size,
            "row_share": round(gone / rows, 4) if rows else None,
            "byte_share": round(gone_size / size, 4) if size else None,
            "percent": percent, "seconds": round(time.monotonic() - started, 1),
            "bound": "lower (objects not folded yet)"}


def _apply_marked(conn: Any, steps: tuple, undo_dir: str | None, mark: str,
                  out: dict[str, Any]) -> None:
    """The batch's steps with its undo file, then its mark (one transaction)."""
    if not undo_dir:
        raise RuntimeError("f3_undo_dir_missing: a run that writes keeps an undo")
    undo = Undo(undo_dir, mark)
    try:
        _apply(conn, steps, undo, out)
        out.update(undo.finish())
    except BaseException:
        undo.abandon()
        raise
    conn.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                 (mark, json.dumps(out)))


def _columns(conn: Any, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
        "AND NOT attisdropped AND attgenerated = '' ORDER BY attnum", (f"ontology.{table}",))]


def undo(dsn: str, undo_dir: str, *, after_delta: bool = False,
         log: Any = sys.stderr) -> dict[str, Any]:
    """Put back every marked F3 batch, newest first, from its undo file: an update's new
    rows (and a map insert's) are taken out, the before-images put in, set-based, one
    transaction per batch holding the delta's lock exclusive, and the mark removed. A
    delta layer applied since the first batch would be overwritten by the before-images:
    refused unless ``after_delta``."""
    import subprocess
    import psycopg
    undone: list[dict[str, Any]] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        marks = conn.execute(
            "SELECT name, value FROM ontology.load_state WHERE name LIKE 'f3:objects:%%' OR "
            "name LIKE 'f3:links:%%' ORDER BY updated_at DESC, name DESC").fetchall()
        if not marks:
            return {"undone": 0}
        first = conn.execute(
            "SELECT min(updated_at) FROM ontology.load_state WHERE name LIKE 'f3:objects:%%' "
            "OR name LIKE 'f3:links:%%'").fetchone()[0]
        later = conn.execute(
            "SELECT count(*) FROM ontology.load_state WHERE name LIKE 'delta:%%' AND "
            "updated_at > %s", (first,)).fetchone()[0]
        if later and not after_delta:
            raise RuntimeError(f"f3_undo_after_delta: {later} delta layer(s) applied since "
                               "the first F3 batch")
        for name, value in marks:
            path = os.path.join(undo_dir, str((value or {}).get("undo") or ""))
            if not (value or {}).get("undo") or not os.path.isfile(path):
                raise RuntimeError(f"f3_undo_file_missing: {name}")
        for name, value in marks:
            path = os.path.join(undo_dir, value["undo"])
            raw = subprocess.run(["zstd", "-q", "-dc", path], check=True,
                                 capture_output=True).stdout
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
                conn.execute("CREATE TEMP TABLE f3_u (s int, t text, k text, r jsonb) "
                             "ON COMMIT DROP")
                with conn.cursor().copy("COPY f3_u (s, t, k, r) FROM STDIN") as copy:
                    for line in raw.splitlines():
                        item = json.loads(line)
                        copy.write_row((item["s"], item["t"], item["k"],
                                        json.dumps(item["r"])))
                steps = conn.execute("SELECT DISTINCT s, t FROM f3_u ORDER BY s DESC"
                                     ).fetchall()
                for step, table in steps:
                    if table == "f3_handle_map":
                        conn.execute("DELETE FROM ontology.f3_handle_map m USING f3_u u "
                                     "WHERE u.s = %s AND m.dropped = u.r->>'dropped'", (step,))
                        continue
                    keys = _key_columns(conn, table) if conn.execute(
                        "SELECT 1 FROM f3_u WHERE s = %s AND k = 'post' LIMIT 1",
                        (step,)).fetchone() else []
                    if keys:
                        match = " AND ".join(f"x.{k} = p.{k}" for k in keys)
                        conn.execute(
                            f"DELETE FROM ontology.{table} x USING (SELECT (jsonb_populate_"
                            f"record(NULL::ontology.{table}, r)).* FROM f3_u WHERE s = %s AND "
                            f"k = 'post') p WHERE {match}", (step,))
                    cols = ", ".join(_columns(conn, table))
                    conn.execute(
                        f"INSERT INTO ontology.{table} ({cols}) SELECT {cols} FROM f3_u u, "
                        f"jsonb_populate_record(NULL::ontology.{table}, u.r) WHERE u.s = %s "
                        f"AND u.k = 'pre'", (step,))
                conn.execute("DELETE FROM ontology.load_state WHERE name = %s", (name,))
            undone.append({"batch": name, "rows": len(raw.splitlines())})
            print(json.dumps(undone[-1]), file=log, flush=True)
    return {"undone": len(undone), "rows": sum(u["rows"] for u in undone)}


def _saved_plan(conn: Any, phase: str, make: Any) -> list[tuple[str, int, str, str | None]]:
    """The phase's batches as saved by the first run (``ontology.f3_batches``); none
    saved: ``make()``'s, saved now. Every later run reads the same ranges, so a batch
    marked done names the rows it folded whatever the statistics say since."""
    rows = conn.execute("SELECT key, n, lo, hi FROM ontology.f3_batches WHERE phase = %s "
                        "ORDER BY n", (phase,)).fetchall()
    if rows:
        return [tuple(r) for r in rows]
    made = [(key, n, lo, hi) for n, (key, _i, lo, hi) in enumerate(make())]
    with conn.transaction():
        for key, n, lo, hi in made:
            conn.execute("INSERT INTO ontology.f3_batches VALUES (%s, %s, %s, %s, %s)",
                         (phase, n, key, lo, hi))
    return made


def _check_sightings(conn: Any) -> None:
    done = conn.execute("SELECT value FROM ontology.load_state WHERE name = %s",
                        (SIGHTINGS_DONE,)).fetchone()
    if done is None:
        raise RuntimeError("f3_sightings_incomplete: stage A did not finish every slice")
    held = conn.execute("SELECT count(*) FROM ontology.f3_sightings").fetchone()[0]
    want = (done[0] or {}).get("rows")
    if want is not None and int(want) != int(held):
        raise RuntimeError(f"f3_sightings_lost: {held} rows, stage A loaded {want}")


def _dsn() -> str:
    return os.environ.get("PLX_PG_DSN") or "dbname=plx"


def batches(conn: Any, types: list[str] | None = None, parts: int = 100
            ) -> list[tuple[str, int, str, str | None]]:
    """(link type, n, lo, hi) per link type and ``from_object`` range: the histogram bounds
    of the column cut into ``parts`` (an unanalyzed table: one range per type)."""
    rows = conn.execute(
        "SELECT link_type, count(*) FROM ontology.link_rows WHERE link_id LIKE 'L:%' "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    bounds = conn.execute(
        "SELECT histogram_bounds::text::text[] FROM pg_stats WHERE schemaname = 'ontology' "
        "AND tablename = 'link_rows' AND attname = 'from_object'").fetchone()
    cuts = sorted(set(bounds[0] or [])) if bounds and bounds[0] else []
    step = max(1, len(cuts) // parts) if cuts else 1
    edges = [""] + cuts[step::step]
    out = []
    for link_type, _ in rows:
        if types and link_type not in types:
            continue
        for n, lo in enumerate(edges):
            hi = edges[n + 1] if n + 1 < len(edges) else None
            out.append((link_type, n, lo, hi))
    return out


def run_batch(dsn: str, batch: tuple[str, int, str, str | None], dry: bool,
              undo_dir: str | None = None) -> dict[str, Any]:
    import psycopg
    link_type, n, lo, hi = batch
    mark = f"f3:links:{link_type}:{n}"
    t = time.monotonic()
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(LOCK)
            if conn.execute("SELECT 1 FROM ontology.load_state WHERE name = %s",
                            (mark,)).fetchone():
                return {"batch": mark, "done_before": True}
            conn.execute(BARE_FN)
            conn.execute(STAGE_SQL, {"t": link_type, "lo": lo, "hi": hi})
            conn.execute(PLAN_SQL)
            rows, dropped, kept, left = conn.execute(COUNT_SQL).fetchone()
            out = {"batch": mark, "rows": rows, "deleted": int(dropped), "kept": int(kept),
                   "groups_left_overlapping_or_reviewed": int(left)}
            if dry:
                conn.rollback()
            else:
                _apply_marked(conn, LINK_STEPS, undo_dir, mark, out)
    out["seconds"] = round(time.monotonic() - t, 2)
    return out


def run(dsn: str, workers: int = 8, types: list[str] | None = None, budget_minutes: float = 110,
        dry: bool = False, what: str = "all", log: Any = sys.stderr,
        undo_dir: str | None = None) -> dict[str, Any]:
    """Every batch of ``what`` (objects, links or all), ``workers`` at a time, until the
    budget: one transaction per batch, its undo file written first, marked when done (a
    later run resumes over the saved batches). ``types``: link types only. Links run only
    once every object batch is marked (the handle map is whole)."""
    import psycopg
    if not dry and not undo_dir:
        raise RuntimeError("f3_undo_dir_missing: a run that writes keeps an undo")
    if undo_dir and not dry:
        os.makedirs(undo_dir, exist_ok=True)
    phases: list[list[tuple[Any, tuple[Any, ...]]]] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('ontology.f3_sightings')").fetchone()[0] is None:
            raise RuntimeError("f3_sightings_missing: load the sightings (stage A) first")
        _ensure_tables(conn)
        _check_sightings(conn)
        objects = _saved_plan(conn, "objects", lambda: object_batches(conn))
        links = _saved_plan(conn, "links", lambda: batches(conn))
        if what in ("objects", "all"):
            phases.append([(run_object_batch, b) for b in objects])
        if what in ("links", "all"):
            if what == "links":
                marked = conn.execute("SELECT count(*) FROM ontology.load_state WHERE name "
                                      "LIKE 'f3:objects:%%'").fetchone()[0]
                if marked < len(objects):
                    raise RuntimeError(f"f3_objects_unfinished: {len(objects) - marked} "
                                       "object batches not done; links wait for them")
            phases.append([(run_batch, b) for b in links if not types or b[0] in types])
    stop_at = time.monotonic() + budget_minutes * 60
    done: list[dict[str, Any]] = []
    planned = sum(len(p) for p in phases)
    started = time.monotonic()
    for index, plan in enumerate(phases):   # every object batch before any link batch
        if len(done) < sum(len(p) for p in phases[:index]):
            break   # the phase before did not finish within the budget: links wait
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending: dict[Any, Any] = {}
            todo = list(plan)
            while todo or pending:
                while todo and len(pending) < workers and time.monotonic() < stop_at:
                    fn, b = todo.pop(0)
                    pending[pool.submit(fn, dsn, b, dry, undo_dir)] = b
                if not pending:
                    break
                fut = next(as_completed(pending))
                pending.pop(fut)
                done.append(fut.result())
                print(json.dumps(done[-1]), file=log, flush=True)
    total: dict[str, int] = {}
    for d in done:
        for k, v in d.items():
            if isinstance(v, int) and k not in ("seconds",):
                total[k] = total.get(k, 0) + v
    return {"batches": planned, "ran": len(done), "left": planned - len(done),
            "seconds": round(time.monotonic() - started, 1), "totals": total, "dry_run": dry}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "run", "sightings", "estimate", "undo"))
    parser.add_argument("--undo-dir", default=None,
                        help="run/undo: where each batch's undo file goes (required to write)")
    parser.add_argument("--after-delta", action="store_true",
                        help="undo: even when delta layers applied since the first batch")
    parser.add_argument("--source", action="append", default=[],
                        help="sightings: SOURCE=CAPTURE_PATH, once per declared source")
    parser.add_argument("--skip-source", action="append", default=[])
    parser.add_argument("--slices", type=int, default=4)
    parser.add_argument("--percent", type=float, default=0.05)
    parser.add_argument("--what", choices=("objects", "links", "all"), default="all")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--types", default=None)
    parser.add_argument("--budget-minutes", type=float, default=110)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    dsn = args.dsn or _dsn()
    types = args.types.split(",") if args.types else None
    if args.command == "sightings":
        paths = {}
        for item in args.source:
            name, sep, path = item.partition("=")
            if not sep or not name or not path:
                parser.error(f"--source wants SOURCE=PATH, got {item!r}")
            paths[name] = path
        print(json.dumps(load_sightings(dsn, paths, workers=args.workers, slices=args.slices,
                                        skip=tuple(args.skip_source),
                                        budget_minutes=args.budget_minutes)))
        return 0
    if args.command == "undo":
        if not args.undo_dir:
            parser.error("undo wants --undo-dir")
        print(json.dumps(undo(dsn, args.undo_dir, after_delta=args.after_delta)))
        return 0
    if args.command == "estimate":
        print(json.dumps(estimate(dsn, args.percent)))
        return 0
    if args.command == "plan":
        import psycopg
        with psycopg.connect(dsn) as conn:
            plan = batches(conn, types)
        print(json.dumps({"batches": len(plan), "types": len({b[0] for b in plan})}))
        return 0
    print(json.dumps(run(dsn, args.workers, types, args.budget_minutes, args.dry_run,
                         args.what, undo_dir=args.undo_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
