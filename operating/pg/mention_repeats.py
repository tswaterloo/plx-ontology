"""CC-C-04: a document's repeated mentions of one record, marked in
``ontology.mention_repeats`` (migration 0029) so a Postgres reader skips them.

``ontology/mentions.py`` stores one row per occurrence of an identifier in a document, so a
document naming an order three times holds three live rows for one (document, order)
pair. The read side (``operating.ontology_tools.mention_pair_key`` and
``collapse_mention_rows``) shows each pair once; this marker lets the reads that cut at a
row count (the 5,000 links an evidence read takes) see the pairs, not the repeats.

The marker moves no digest: the build steps never read the table, and ``link_rows`` is
never written (no tier, ``superseded_at``, id or delete changes; a step re-judges from the
live leads, so shrinking those would change its output). Which rows it marks:

* the keeper of a pair is the read side's, by the same functions: ``mention_pair_key``,
  then ``mention_keeper_order`` over the stored tier (strongest, then least link id);
* a row is marked only when the read side would never show it in the keeper's place, for
  any reader: same markings, evidence handles and recorded time as the keeper (so the
  same readers see both, at every time), and the keeper wins under ``reader_view`` (a
  corroborated row is a lead to a reader who may not read what it cites) whether or not
  the reader may read either's corroboration;
* a lead a firm row (``crb-mid:``/``crb-mcid:``) left unmarked cites is never marked (the
  firm row's evidence names it).

Anything else stays unmarked and the read side's dedupe handles it. A Postgres reader
reads ``ontology_seen.links`` (``ontology.links_seen()``) for ``links`` only when the
switch ``PLX_PG_MENTION_REPEATS=1`` is on (``search_path``); that read hides a marked row
only while its keeper still stands in its place, so a stale marker shows a repeat and
never hides a pair's only link.

``python3 -m operating.pg.mention_repeats build|refresh|verify|clear|rows-hash``:
``build`` marks every pair (TRUNCATE, then the partitions' ranges side by side);
``refresh`` re-marks the pairs with an end at the given objects (the delta follower calls
it after each layer, ``operating.pg.ontology_delta``); ``verify`` checks the invariants and
that a recompute marks the same rows; ``clear`` empties the table (the undo, after the
switch is off); ``rows-hash`` is the stored rows' per-partition count and hash of
(link_id, tier, superseded_at), to show the build changed none.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed, wait
from typing import Any

from ontology import parent_watch

ENV = "PLX_PG_MENTION_REPEATS"
TABLE = "ontology.mention_repeats"
PLAIN_PATH = "ontology, public"
SEEN_PATH = "ontology_seen, ontology, public"
FIRM_PREFIXES = ("crb-mid:", "crb-mcid:")
CORROBORATED, LEAD = "corroborated", "lead"
# link rows a build task's range holds (pg_class estimate): each range of a partition scans
# its heap, so few (served 11-14: 67.8 GB heap, 47M rows in 13 partitions, the largest 6.3M)
ROWS_PER_TASK = 4_000_000
BUDGET_S = 1800             # a build or verify stops here (the plan's abort at 30 min)
BATCH = 5_000
LOCK = "mention_repeats"


def switch_on(env: Mapping[str, str] | None = None) -> bool:
    """Whether Postgres readers skip the marked repeats (``PLX_PG_MENTION_REPEATS=1``)."""
    return (os.environ if env is None else env).get(ENV) == "1"


def search_path(env: Mapping[str, str] | None = None) -> str:
    """A Postgres reader's search_path: ``links`` is ``ontology_seen.links`` when the
    switch is on, ``ontology.links`` otherwise."""
    return SEEN_PATH if switch_on(env) else PLAIN_PATH


# ------------------------------------------------------------------ the decision

def _reason(row: Mapping[str, Any]) -> list[Any]:
    try:
        value = json.loads(row.get("reason") or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def cited_leads(row: Mapping[str, Any]) -> list[str]:
    """The mention link ids a firm row's ``corroborates`` features cite."""
    return [str(f["link_id"]) for f in _reason(row) if isinstance(f, Mapping)
            and f.get("feature") == "corroborates" and isinstance(f.get("link_id"), str)]


def _handles(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The records a corroborated row's corroboration cites (``reader_view``'s readable
    test reads these); None: one is unnamed (never readable)."""
    from ontology.mentions import corroboration_handles
    found = corroboration_handles(_reason(row))
    return None if found is None else tuple(sorted(found))


def _shown(row: Mapping[str, Any], readable: bool) -> tuple[int, str]:
    from operating.ontology_tools_policy import mention_keeper_order
    tier = row["tier"]
    if tier == CORROBORATED and not readable:   # ontology.mentions.reader_view
        tier = LEAD
    return mention_keeper_order(tier, row["link_id"])


def keeps_over(keeper: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """Whether the read side keeps ``keeper`` over ``row`` for every reader: whatever the
    reader may read of either's corroboration (the same records cited: the same answer
    for both)."""
    both = keeper["tier"] == CORROBORATED and row["tier"] == CORROBORATED
    hk = _handles(keeper) if keeper["tier"] == CORROBORATED else None
    hr = _handles(row) if row["tier"] == CORROBORATED else None
    same = both and hk is not None and hk == hr
    for rk in (True, False):
        for rr in ((rk,) if same else (True, False)):
            if not _shown(keeper, rk) < _shown(row, rr):
                return False
    return True


def decide(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{marked link id: its keeper's link id} for ``rows``: live stored mention links
    (``binding_kind`` 'mention', not superseded; ``evidence_handles`` decoded; ``reason``
    at least for corroborated and firm rows), every row of each (type, from, to) they
    touch. The keeper of a pair is ``collapse_mention_rows``' over the stored tiers."""
    from operating.ontology_tools_policy import mention_keeper_order, mention_pair_key
    groups: dict[tuple, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = mention_pair_key(row)
        if key is not None:
            groups[key].append(row)
    marked: dict[str, str] = {}
    firm: list[Mapping[str, Any]] = []
    for items in groups.values():
        if len(items) < 2:
            continue
        keeper = min(items, key=lambda r: mention_keeper_order(r["tier"], r["link_id"]))
        for row in items:
            if row is keeper:
                continue
            if (row["markings"] == keeper["markings"]
                    and row["evidence_handles"] == keeper["evidence_handles"]
                    and row["recorded_at"] == keeper["recorded_at"]
                    and keeps_over(keeper, row)):
                marked[row["link_id"]] = keeper["link_id"]
        firm += [r for r in items if str(r["link_id"]).startswith(FIRM_PREFIXES)]
    # the leads an unmarked firm row cites stay (no firm row's evidence goes missing)
    for row in firm:
        if row["link_id"] not in marked:
            for lead in cited_leads(row):
                marked.pop(lead, None)
    return marked


# ------------------------------------------------------------------ Postgres reads

_COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
            "markings", "recorded_at", "evidence_handles", "reason")
# the live mention rows of every (type, from, to) holding two or more, in that order
# (evidence and, for corroborated and firm rows, reason decoded as ``ontology.links`` does)
_DECODE = ("SELECT link_id, link_type, from_object, to_object, tier, binding_kind, markings, "
           "recorded_at, coalesce(evidence_handles, "
           "ontology.link_text(evidence_id, slots, 1, ek)) AS evidence_handles, "
           "CASE WHEN wants THEN coalesce(reason_plain, "
           "ontology.link_text(reason_id, slots, ek + 1, 64)) END AS reason ")
_INNER = ("SELECT link_id, link_type, from_object, to_object, tier, binding_kind, markings, "
          "recorded_at, evidence_handles, evidence_id, slots, ek, reason_id, "
          "(tier = 'corroborated' OR link_id LIKE 'crb-%%') AS wants, "
          "CASE WHEN tier = 'corroborated' OR link_id LIKE 'crb-%%' THEN reason END "
          "AS reason_plain")


def _range_sql(table: str, lo: str | None, hi: str | None) -> tuple[str, dict[str, Any]]:
    where = ["binding_kind = 'mention'", "superseded_at IS NULL"]
    params: dict[str, Any] = {}
    if lo is not None:
        where.append("from_object >= %(lo)s")
        params["lo"] = lo
    if hi is not None:
        where.append("from_object < %(hi)s")
        params["hi"] = hi
    sql = (_DECODE + "FROM (" + _INNER + ", count(*) OVER (PARTITION BY link_type, "
           f"from_object, to_object) AS n FROM {table} WHERE {' AND '.join(where)}) s "
           "WHERE n > 1 ORDER BY link_type, from_object, to_object")
    return sql, params


def _objects_sql() -> str:
    """The live mention rows with an end at ``%(ids)s`` (by the from and the to index)."""
    inner = (_INNER + " FROM ontology.link_rows WHERE binding_kind = 'mention' AND "
             "superseded_at IS NULL AND {end}")
    return (_DECODE + "FROM (" + inner.format(end="from_object = ANY(%(ids)s::text[])")
            + " UNION ALL " + inner.format(end="to_object = ANY(%(ids)s::text[]) AND NOT "
                                               "from_object = ANY(%(ids)s::text[])") + ") s")


def _session(conn: Any, timeout_s: int = BUDGET_S) -> None:
    conn.execute("SET jit = off")
    conn.execute(f"SET statement_timeout = '{int(timeout_s)}s'")


def _connect(dsn: str) -> Any:
    import psycopg
    conn = psycopg.connect(dsn)
    _session(conn)
    return conn


def _grouped(cur: Any) -> Iterable[list[dict[str, Any]]]:
    """Rows of a cursor ordered by (type, from, to), one list per (type, from, to)."""
    held: list[dict[str, Any]] = []
    at: tuple | None = None
    for raw in cur:
        row = dict(zip(_COLUMNS, raw))
        key = (row["link_type"], row["from_object"], row["to_object"])
        if key != at and held:
            yield held
            held = []
        at = key
        held.append(row)
    if held:
        yield held


def _insert(cur: Any, marks: Mapping[str, tuple[str, str, str, str]]) -> int:
    items = sorted(marks.items())
    for i in range(0, len(items), BATCH):
        part = items[i:i + BATCH]
        cur.execute(
            f"INSERT INTO {TABLE} (link_id, keeper_id, link_type, from_object, to_object) "
            "SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], "
            "%s::text[]) ON CONFLICT (link_id) DO NOTHING",
            ([k for k, _ in part], [v[0] for _, v in part], [v[1] for _, v in part],
             [v[2] for _, v in part], [v[3] for _, v in part]))
    return len(items)


def _marks_of(groups: Iterable[list[dict[str, Any]]], check: dict[str, int] | None = None
              ) -> dict[str, tuple[str, str, str, str]]:
    out: dict[str, tuple[str, str, str, str]] = {}
    for rows in groups:
        marked = decide(rows)
        for row in rows:
            keeper = marked.get(row["link_id"])
            if keeper is not None:
                out[row["link_id"]] = (keeper, row["link_type"], row["from_object"],
                                       row["to_object"])
        if check is not None:
            check["rows"] = check.get("rows", 0) + len(rows)
    return out


def _digest(pairs: Iterable[tuple[str, str]]) -> tuple[int, int]:
    """(count, xor of each (link id, keeper) pair's md5 head): an order-free fingerprint
    ``_TABLE_DIGEST`` computes the same way in SQL."""
    n, acc = 0, 0
    for link_id, keeper in pairs:
        h = int(hashlib.md5(f"{link_id} {keeper}".encode()).hexdigest()[:16], 16)
        acc ^= h
        n += 1
    return n, (acc - (1 << 64) if acc >= 1 << 63 else acc)


_TABLE_DIGEST = ("SELECT count(*), coalesce(bit_xor(('x' || substr(md5(link_id || ' ' || "
                 "keeper_id), 1, 16))::bit(64)::bigint), 0) FROM " + TABLE)


# ------------------------------------------------------------------ tasks

def _task(dsn: str, table: str, lo: str | None, hi: str | None, write: bool
          ) -> dict[str, Any]:
    """One partition range: its markers computed, written (``write``) or fingerprinted."""
    started = time.monotonic()
    stats: dict[str, int] = {}
    with _connect(dsn) as conn:
        sql, params = _range_sql(table, lo, hi)
        with conn.cursor(name=f"mr_{os.getpid()}") as cur:
            cur.itersize = 20_000
            cur.execute(sql, params)
            marks = _marks_of(_grouped(cur), stats)
        conn.rollback()
        written = 0
        if write and marks:
            with conn.transaction(), conn.cursor() as cur:
                written = _insert(cur, marks)
    n, digest = _digest((k, v[0]) for k, v in marks.items())
    return {"table": table, "lo": lo, "hi": hi, "rows": stats.get("rows", 0),
            "marked": len(marks), "written": written, "digest": digest, "n": n,
            "seconds": round(time.monotonic() - started, 1)}


def ranges(conn: Any, rows_per_task: int = ROWS_PER_TASK
           ) -> list[tuple[str, str | None, str | None]]:
    """(partition, lo, hi) tasks over ``ontology.link_rows``' leaf partitions (or the table
    itself when it is not partitioned): from_object ranges from the planner's histogram,
    about ``rows_per_task`` rows each, largest first."""
    leaves = conn.execute(
        "SELECT c.oid::regclass::text, greatest(c.reltuples, 0)::bigint, n.nspname, c.relname "
        "FROM pg_partition_tree('ontology.link_rows') t JOIN pg_class c ON c.oid = t.relid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.isleaf").fetchall()
    if not leaves:
        leaves = [("ontology.link_rows", 0, "ontology", "link_rows")]
    tasks: list[tuple[int, str, str | None, str | None]] = []
    for name, tuples, schema, rel in leaves:
        parts = max(1, round(tuples / rows_per_task))
        bounds: list[str] = []
        if parts > 1:
            row = conn.execute(
                "SELECT histogram_bounds::text::text[] FROM pg_stats WHERE schemaname = %s "
                "AND tablename = %s AND attname = 'from_object'", (schema, rel)).fetchone()
            hist = list(row[0] or ()) if row else []
            if len(hist) > parts:
                step = len(hist) / parts
                bounds = sorted({hist[int(i * step)] for i in range(1, parts)})
        edges: list[str | None] = [None, *bounds, None]
        for i in range(len(edges) - 1):
            tasks.append((tuples // (len(edges) - 1), name, edges[i], edges[i + 1]))
    tasks.sort(key=lambda t: -t[0])
    return [(name, lo, hi) for _, name, lo, hi in tasks]


def _run(dsn: str, write: bool, workers: int, budget_s: float, log: Any,
         rows_per_task: int = ROWS_PER_TASK) -> dict[str, Any]:
    with _connect(dsn) as conn:
        tasks = ranges(conn, rows_per_task)
    started = time.monotonic()
    done: list[dict[str, Any]] = []
    # spawned, not forked: a forked worker would close the caller's inherited session
    import multiprocessing
    pool = ProcessPoolExecutor(max_workers=max(1, min(workers, len(tasks))),
                               mp_context=multiprocessing.get_context("spawn"),
                               **parent_watch.pool_init())
    futures = [pool.submit(_task, dsn, t, lo, hi, write) for t, lo, hi in tasks]
    try:
        for fut in as_completed(futures, timeout=budget_s):
            done.append(fut.result())
            if log is not None:
                print(json.dumps(done[-1], sort_keys=True), file=log, flush=True)
    except TimeoutError:
        for fut in futures:
            fut.cancel()
        for proc in list(getattr(pool, "_processes", {}).values()):
            proc.terminate()   # our own workers; their sessions end with them
        pool.shutdown(wait=False, cancel_futures=True)
        return {"complete": False, "reason": "budget", "tasks": len(tasks),
                "done": len(done), "seconds": round(time.monotonic() - started, 1)}
    wait(futures)
    pool.shutdown()
    acc = 0
    for d in done:
        acc ^= d["digest"] & ((1 << 64) - 1)
    return {"complete": True, "tasks": len(tasks), "rows": sum(d["rows"] for d in done),
            "marked": sum(d["marked"] for d in done),
            "written": sum(d["written"] for d in done),
            "digest": acc - (1 << 64) if acc >= 1 << 63 else acc,
            "seconds": round(time.monotonic() - started, 1)}


# ------------------------------------------------------------------ entry points

def build(dsn: str, *, workers: int = 12, budget_s: float = BUDGET_S, log: Any = None,
          rows_per_task: int = ROWS_PER_TASK) -> dict[str, Any]:
    """Every pair marked again: the table emptied, then each partition range's markers
    written side by side (each its own transaction). Reads ``link_rows``, writes only the
    marker table."""
    with _connect(dsn) as conn:
        got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK,)).fetchone()[0]
        if not got:
            return {"complete": False, "reason": "busy"}
        try:
            conn.execute(f"TRUNCATE {TABLE}")
            conn.commit()
            report = _run(dsn, True, workers, budget_s, log, rows_per_task)
            if report.get("complete"):
                conn.execute(f"ANALYZE {TABLE}")
                conn.commit()
            return report
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK,))
            conn.commit()


def refresh(dsn: str, objects: Sequence[str]) -> dict[str, Any]:
    """The pairs with an end at ``objects`` marked again, in one transaction of its own
    (the delta follower after a layer commits; a train's end with the objects it
    changed). Skipped (``busy``) while a build runs: that build reads the rows as they are."""
    objects = sorted(set(objects))
    if not objects:
        return {"objects": 0, "marked": 0}
    started = time.monotonic()
    with _connect(dsn) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (TABLE,))
        if cur.fetchone()[0] is None:
            return {"objects": len(objects), "skipped": "no_table"}
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (LOCK,))
        if not cur.fetchone()[0]:
            return {"objects": len(objects), "skipped": "busy"}
        removed = marked = rows = 0
        for i in range(0, len(objects), BATCH):
            ids = objects[i:i + BATCH]
            cur.execute(f"DELETE FROM {TABLE} WHERE from_object = ANY(%(ids)s::text[]) "
                        "OR to_object = ANY(%(ids)s::text[])", {"ids": ids})
            removed += cur.rowcount
            cur.execute(_objects_sql(), {"ids": ids})
            got = [dict(zip(_COLUMNS, r)) for r in cur.fetchall()]
            rows += len(got)
            by_end: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
            for row in got:
                by_end[(row["link_type"], row["from_object"], row["to_object"])].append(row)
            marks = _marks_of(by_end.values())
            marked += _insert(cur, marks)
    return {"objects": len(objects), "rows": rows, "removed": removed, "marked": marked,
            "seconds": round(time.monotonic() - started, 2)}


VERIFY_SQL = {
    # each marked row is a live mention row of its stored pair
    "marked_not_live": (
        f"SELECT count(*) FROM {TABLE} r WHERE NOT EXISTS (SELECT 1 FROM ontology.link_rows "
        "l WHERE l.link_id = r.link_id AND l.link_type = r.link_type AND l.binding_kind = "
        "'mention' AND l.superseded_at IS NULL AND l.from_object = r.from_object "
        "AND l.to_object = r.to_object)"),
    # its keeper is unmarked
    "keeper_marked": (f"SELECT count(*) FROM {TABLE} r JOIN {TABLE} k ON "
                      "k.link_id = r.keeper_id"),
    # its keeper is live and stands in its place (same ends, markings, recorded time and
    # evidence, so the same key; a tier at least as strong): links_seen hides it
    "not_hidden": (f"SELECT count(*) FROM {TABLE} r WHERE EXISTS (SELECT 1 FROM "
                   "ontology.links_seen() s WHERE s.link_id = r.link_id AND "
                   "s.link_type = r.link_type)"),
    # each key has exactly one keeper
    "keys_with_two_keepers": (
        "SELECT count(*) FROM (SELECT r.link_type, r.from_object, r.to_object, "
        "CASE WHEN pg_input_is_valid(k.evidence_handles, 'jsonb') THEN "
        "k.evidence_handles::jsonb ->> 0 END AS doc FROM "
        f"(SELECT DISTINCT keeper_id, link_type, from_object, to_object FROM {TABLE}) r "
        "JOIN ontology.links k ON k.link_id = r.keeper_id AND k.link_type = r.link_type "
        "GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) x"),
}
# no unmarked firm row of a marked pair cites a marked lead (its evidence would name a
# hidden row); a firm row cites its own pair's lead (measured 11-14: 18,545 of 18,545)
VERIFY_SQL["firm_cites_marked_lead"] = (
    f"SELECT count(*) FROM (SELECT DISTINCT link_type, from_object, to_object FROM {TABLE}) g "
    "JOIN ontology.links l ON l.link_type = g.link_type AND l.from_object = g.from_object "
    "AND l.to_object = g.to_object CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN "
    "pg_input_is_valid(l.reason, 'jsonb') AND jsonb_typeof(l.reason::jsonb) = 'array' "
    "THEN l.reason::jsonb ELSE '[]'::jsonb END) f "
    f"JOIN {TABLE} m ON m.link_id = f ->> 'link_id' "
    "WHERE l.superseded_at IS NULL AND l.binding_kind = 'mention' AND (l.link_id LIKE "
    "'crb-mid:%%' OR l.link_id LIKE 'crb-mcid:%%') AND f ->> 'feature' = 'corroborates' "
    f"AND NOT EXISTS (SELECT 1 FROM {TABLE} x WHERE x.link_id = l.link_id)")
# planner settings a check runs under (SET LOCAL: its own read-only transaction only).
# not_hidden: the default plan hashes every row of links_seen() (served 11-15: 43.9M rows)
# and hits temp_file_limit (25 GB); without hash/merge joins it walks the marked rows by
# index (served: 20 s, no temp). Same SQL, so the check still reads what a reader reads.
VERIFY_SETTINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "not_hidden": (("enable_hashjoin", "off"), ("enable_mergejoin", "off")),
}


def verify(dsn: str, *, workers: int = 12, recompute: bool = True,
           budget_s: float = BUDGET_S, log: Any = None,
           rows_per_task: int = ROWS_PER_TASK) -> dict[str, Any]:
    """The marker's invariants (each a count that must be 0), and with ``recompute`` that a
    fresh decision over every partition marks exactly the stored rows."""
    started = time.monotonic()
    out: dict[str, Any] = {}
    with _connect(dsn) as conn:
        conn.commit()            # keep _session's settings past each check's rollback
        conn.read_only = True    # verify writes nothing: Postgres refuses it
        for name, sql in VERIFY_SQL.items():
            for setting, value in VERIFY_SETTINGS.get(name, ()):
                conn.execute("SELECT set_config(%s, %s, true)", (setting, value))
            out[name] = conn.execute(sql).fetchone()[0]
            conn.rollback()      # SET LOCAL ends here: no setting reaches the next check
        n, digest = conn.execute(_TABLE_DIGEST).fetchone()
        conn.rollback()
        out["stored"] = {"marked": n, "digest": digest}
    if recompute:
        fresh = _run(dsn, False, workers, budget_s, log, rows_per_task)
        out["recomputed"] = fresh
        out["recompute_differs"] = (not fresh.get("complete")
                                    or fresh.get("marked") != n
                                    or fresh.get("digest") != digest)
    out["ok"] = (all(out[k] == 0 for k in VERIFY_SQL)
                 and not out.get("recompute_differs", False))
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def clear(dsn: str) -> dict[str, Any]:
    """The undo (after the switch is off): the marker table emptied."""
    with _connect(dsn) as conn:
        conn.execute(f"TRUNCATE {TABLE}")
        conn.commit()
    return {"cleared": True}


ROWS_HASH_SQL = (
    "SELECT tableoid::regclass::text, count(*), coalesce(bit_xor(('x' || substr(md5("
    "link_id || '|' || tier || '|' || coalesce(superseded_at, '~')), 1, 16))::bit(64)"
    "::bigint), 0) FROM ontology.link_rows GROUP BY 1 ORDER BY 1")


def rows_hash(dsn: str) -> dict[str, Any]:
    """The stored link rows' per-partition count and hash of (link_id, tier,
    superseded_at): equal before and after a build shows it wrote no link row."""
    with _connect(dsn) as conn:
        conn.execute("SET statement_timeout = 0")
        return {name: [n, h] for name, n, h in conn.execute(ROWS_HASH_SQL).fetchall()}


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.mention_repeats")
    parser.add_argument("action", choices=("build", "refresh", "verify", "clear",
                                           "rows-hash"))
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--budget-s", type=float, default=BUDGET_S)
    parser.add_argument("--objects", default=None,
                        help="refresh: a file of object ids, one per line")
    parser.add_argument("--no-recompute", action="store_true")
    parser.add_argument("--rows-per-task", type=int, default=ROWS_PER_TASK)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.action == "build":
        report = build(dsn, workers=args.workers, budget_s=args.budget_s, log=sys.stderr,
                       rows_per_task=args.rows_per_task)
    elif args.action == "refresh":
        with open(args.objects or "/dev/stdin", encoding="utf-8") as fh:
            objects = [line.strip() for line in fh if line.strip()]
        report = refresh(dsn, objects)
    elif args.action == "verify":
        report = verify(dsn, workers=args.workers, recompute=not args.no_recompute,
                        budget_s=args.budget_s, log=sys.stderr,
                        rows_per_task=args.rows_per_task)
    elif args.action == "clear":
        report = clear(dsn)
    else:
        report = rows_hash(dsn)
    print(json.dumps(report, sort_keys=True), flush=True)
    ok = report.get("complete", True) and report.get("ok", True) and not report.get("skipped")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
