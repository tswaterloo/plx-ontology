"""The resident plan runner: a record page's question plans (``late_po``, ``people_on_record``)
kept current per record, on a Spark, for ``operating.pg.plan_sections`` to store.

``plan_batch`` runs every open order once and exits (a whole pass takes a quarter of an hour on 14
shards); this process stays up (a systemd user unit on s7d3d, ``deploy/spark/plan_resident.sh``)
with the console built once, as the owner's full reader, reading the Spark's hot standby, and
each few seconds recomputes only the rows that need it:

* a stored row whose dependencies moved (the row fails the guarded view's own freshness check,
  ``plan_sections.deps_ddl``, on the standby's copy of the table and of the ontology);
* an open order with no row of a family, or a row from an earlier business day (the text counts
  days): after midnight (New York) the first cycle starts the full pass, in the order the cycle
  finds them (rows whose dependencies moved first, since readers are being sent to the live
  plan for those now).

Each record is a small file in ``--out`` (written complete, then renamed into place): a header
line with the stamps read just before that record's plans started, then one line per family.
The box fetches the files and puts them in place (``plan_sections pull``). A file is the only
thing this process produces: it writes nothing to a business system or to Postgres, and refuses
to run on anything but a standby.

The standby follows the box some seconds behind and sees the box's put one lag later: a record
it just answered stays out of the next cycles until its dependencies move past the stamps it was
answered at (``memo``), so a put not yet replayed is not redone. A plan that failed
is stored typed, with its attempts and ``next_try_at`` (``plan_sections.backoff_seconds``, doubling
from ten minutes) in the row, and asked again once due, closed orders included. ``--ids FILE`` or files ``*.ids`` in ``--queue`` force records
(one id per line; both families) whatever their state. The process ends itself after
``--max-records`` answers (its unit starts it again with a fresh console: the engine's
long-lived caches never decide an answer for long).

  python3 -m operator_api.plan_resident --out DIR [--queue DIR] [--shard I/N] [--poll SECONDS]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Mapping, Sequence

from operator_api import plan_batch

HOST_CONFIG_ENV, READER_ENV = plan_batch.HOST_CONFIG_ENV, plan_batch.READER_ENV
LEASE = ".lease"             # touched in ``out`` by the box's puller each round
RETRY_AFTER = 600.0          # seconds a line of no stored family is left alone (a failed row's own
                             # back-off is in the row: ``plan_sections.backoff_seconds``)
SESSION_SECONDS = 4 * 3600   # the warm session of the reader is made again after this long
PER_CYCLE = 25               # records answered before the standby is asked again


def shard_of(spec: str) -> tuple[int, int]:
    index, count = (int(x) for x in spec.split("/"))
    if not 0 <= index < count:
        raise ValueError("shard is I/N with 0 <= I < N")
    return index, count


def _hash(record_id: str) -> int:
    """The shard hash of an id (stable across processes, unlike ``hash``)."""
    import zlib
    return zlib.crc32(record_id.encode("utf-8"))


def need_sql(schema: str) -> str:
    """The rows to recompute, as (record id, section, why, failed attempts so far): the stored row is
    stale (its dependencies moved), or missing for an open order, or from another business day, or
    a stored failure whose own ``next_try_at`` has come (the back-off is in the row, so a restart
    of this process retries nothing early). A stored failure of a CLOSED order is asked too, once
    due: the cycle's scope is every open order plus every order with a failed row."""
    from operating.pg import plan_sections as ps
    t = f"{schema}.{ps.TABLE}"
    due = ("(t.payload ->> 'failed' IS NOT NULL AND (t.payload ->> 'next_try_at' IS NULL OR "
           "(t.payload ->> 'next_try_at')::timestamptz <= now()))")
    return (
        f"WITH fam(section) AS (VALUES {', '.join(f'(%({k}_s)s)' for k in ps.FAMILY)}), "
        f"open AS ({plan_batch.OPEN_SQL.format(s=schema).rsplit(' ORDER BY', 1)[0]}), "
        "scope AS (SELECT record_id, true AS is_open FROM open UNION ALL SELECT DISTINCT f.record_id, "
        f"false FROM {t} f WHERE f.payload ->> 'failed' IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
        "open o WHERE o.record_id = f.record_id)) "
        "SELECT o.record_id, f.section, CASE WHEN t.handle IS NULL THEN 'missing' "
        f"WHEN {due} THEN 'failed' WHEN "
        "(t.built_at::timestamptz AT TIME ZONE 'America/New_York')::date <> "
        "(now() AT TIME ZONE 'America/New_York')::date THEN 'day' ELSE 'moved' END, "
        "COALESCE((t.payload ->> 'attempts')::int, 0) "
        f"FROM scope o CROSS JOIN fam f LEFT JOIN {t} t ON t.record_id = "
        f"o.record_id AND t.section = f.section WHERE {due} OR (o.is_open AND (t.handle IS NULL OR "
        "(t.built_at::timestamptz AT TIME ZONE 'America/New_York')::date <> "
        f"(now() AT TIME ZONE 'America/New_York')::date OR NOT ({ps.deps_ddl('t').replace('{s}', schema)})))")


def moved_past(pg: Any, schema: str, record_id: str, section: str, ver: int, link: int,
               xmin: int = 0) -> bool:
    """Whether anything the stored row depends on moved past the given watermarks (the memo of
    this process's own answer, which the standby's copy of the row may not show yet)."""
    from operating.pg import plan_sections as ps
    gate = (ps.deps_ddl("t").replace("{s}", schema).replace("t.dep_ver", "%(v)s::bigint")
            .replace("t.dep_link", "%(l)s::bigint").replace("t.dep_xmin", "%(x)s::bigint"))
    got = pg.execute(f"SELECT NOT ({gate}) FROM {schema}.{ps.TABLE} t WHERE t.record_id = "
                     "%(r)s AND t.section = %(s)s", {"v": ver, "l": link, "x": xmin,
                                                      "r": record_id, "s": section}).fetchone()
    return True if got is None else bool(got[0])


def discover(pg: Any, shard: tuple[int, int], memo: Mapping[tuple[str, str], tuple[float, int, int]],
             schema: str = "biz", now: float | None = None,
             why_out: dict[str, dict[str, str]] | None = None,
             attempts_out: dict[tuple[str, str], int] | None = None
             ) -> list[tuple[str, list[str]]]:
    """The records to answer now, each with the families to run, rows with moved dependencies
    first, then rows missing, then rows of an earlier day; a record left out by this process's
    memo (a put the standby has not replayed yet) is not asked again yet. A stored failure comes back
    only when its own ``next_try_at`` has come; ``attempts_out`` gets its attempts so far."""
    from operating.pg import plan_sections as ps
    at = time.monotonic() if now is None else now
    index, count = shard
    rows = pg.execute(need_sql(schema), {f"{k}_s": k for k in ps.FAMILY}).fetchall()
    order = {"moved": 0, "missing": 1, "day": 2, "failed": 3}
    chosen: dict[str, list[str]] = {}
    rank: dict[str, int] = {}
    for rid, section, why, tried in rows:
        if _hash(rid) % count != index:
            continue
        held = memo.get((rid, section))
        if held is not None:
            when, ver, link, *rest = held
            if when < 0:                       # a line of no stored family: leave it alone a while
                if at + when < RETRY_AFTER:
                    continue
            elif why == "moved":
                if not moved_past(pg, schema, rid, section, ver, link, rest[0] if rest else 0):
                    continue                   # answered since; the standby lags the put
            elif at - when < 120:
                continue                       # answered a moment ago, the put not replayed yet
        chosen.setdefault(rid, []).append(section)
        if why_out is not None:
            why_out.setdefault(rid, {})[section] = why
        if attempts_out is not None:
            attempts_out[(rid, section)] = int(tried or 0)
        rank[rid] = min(rank.get(rid, 9), order.get(why, 9))
    return [(rid, sorted(chosen[rid])) for rid in sorted(chosen, key=lambda r: (rank[r], r))]


def exclusion_stamp(pg: Any) -> str | None:
    """The people exclusion's stamp as the database holds it (the set this process was started
    with, on ssh's stdin, is the one of that stamp)."""
    try:
        row = pg.execute("SELECT exclusion FROM ontology.people_excluded_state").fetchone()
    except Exception:  # noqa: BLE001 - no such table (a fresh database): no stamp
        return None
    return None if row is None else str(row[0])


def still_wanted(out: str, queue: str | None, worker: str, nonce: str, began: float,
                 lease_seconds: float, now: float | None = None) -> str | None:
    """Why this process should end now, None when it should go on. The unit that holds its ssh
    cannot stop it from the box (the process outlives a closed ssh), so two things end it: a
    newer process of the same shard (the unit started it again; each takes the shard's owner
    file in the queue folder), and the box's puller not having touched the lease file in
    ``out`` for ``lease_seconds`` (nobody is fetching its answers: the box or the puller is
    gone)."""
    at = time.time() if now is None else now
    if queue:
        try:
            with open(os.path.join(queue, f"owner-{worker}"), encoding="utf-8") as fh:
                if fh.read().strip() != nonce:
                    return "a newer process took the shard"
        except OSError:
            pass
    try:
        seen = os.path.getmtime(os.path.join(out, LEASE))
    except OSError:
        seen = began
    if at - max(seen, began) > lease_seconds:
        return "nobody has fetched the answers for %d s" % lease_seconds
    return None


def take_shard(queue: str | None, worker: str) -> str:
    """Become the shard's owner (a fresh nonce in its owner file, written whole)."""
    import secrets
    nonce = secrets.token_hex(8)
    if queue:
        os.makedirs(queue, exist_ok=True)
        tmp = os.path.join(queue, f".owner-{worker}.{nonce}")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(nonce)
        os.replace(tmp, os.path.join(queue, f"owner-{worker}"))
    return nonce


def write_atomic(out: str, name: str, lines: Sequence[Mapping[str, Any]]) -> str:
    """The file complete under its own name or not at all (the box lists ``*.jsonl`` only)."""
    final = os.path.join(out, name + ".jsonl")
    tmp = os.path.join(out, "." + name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final


def forced(queue: str | None) -> list[str]:
    """The ids named in ``*.ids`` files of the queue folder (each file is taken once)."""
    ids: list[str] = []
    if not queue or not os.path.isdir(queue):
        return ids
    for name in sorted(os.listdir(queue)):
        if not name.endswith(".ids"):
            continue
        path = os.path.join(queue, name)
        try:
            with open(path, encoding="utf-8") as fh:
                ids += [x.strip() for x in fh if x.strip()]
            os.replace(path, path + ".taken")
        except OSError:
            continue
    return ids


def loop(view: Any, args: argparse.Namespace, pg_dsn: str, shard: tuple[int, int],
         worker: str) -> int:
    import psycopg
    from operating.pg import plan_sections as ps
    memo: dict[tuple[str, str], tuple[float, int, int]] = {}
    answered = 0
    counter = 0
    began = time.monotonic()
    started_with: list[str | None] = []
    nonce = take_shard(args.queue, worker)
    wall = time.time()
    while answered < args.max_records:
        stop = still_wanted(args.out, args.queue, worker, nonce, wall,
                            getattr(args, "lease_seconds", 300.0))
        if stop:
            print(f"plan_resident: ending: {stop}", file=sys.stderr)
            return 0
        try:
            with psycopg.connect(pg_dsn, autocommit=True) as pg:
                if (getattr(args, "require_standby", True)
                        and not pg.execute("SELECT pg_is_in_recovery()").fetchone()[0]):
                    print("plan_resident: the database is not a standby: refusing to run",
                          file=sys.stderr)
                    return 3
                stamp = exclusion_stamp(pg)
                if not started_with:
                    started_with.append(stamp)
                elif stamp != started_with[0]:
                    print("plan_resident: the people exclusion changed: ending to start with the "
                          "new list", file=sys.stderr)
                    return 0
                why: dict[str, dict[str, str]] = {}
                tried: dict[tuple[str, str], int] = {}
                todo = discover(pg, shard, memo, why_out=why, attempts_out=tried)
                for rid in forced(args.queue):
                    if _hash(rid) % shard[1] == shard[0] and all(r != rid for r, _ in todo):
                        todo.insert(0, (rid, sorted(ps.FAMILY)))
                seen = time.time()
                lag = pg.execute("SELECT COALESCE(extract(epoch FROM now() - "
                                 "pg_last_xact_replay_timestamp()), 0)").fetchone()[0]
                for rid, sections in todo[:PER_CYCLE]:
                    if still_wanted(args.out, args.queue, worker, nonce, wall,
                                    getattr(args, "lease_seconds", 300.0)):
                        break          # the cycle's top says why and ends the process
                    stamps = plan_batch.stamps_of(pg, quick=True)
                    lines = []
                    for section in sections:
                        line = plan_batch.answer(view, rid, ps.FAMILY[section])
                        # every line is written, as the batch path writes it: an answer, a typed
                        # "no answer" (the plan completed and found none) or a typed "failed"
                        # (stored with its reason, attempts and next_try_at, run again once due)
                        key = (rid, section)
                        kind, why_not = ps.outcome(line)
                        if kind == "skip":
                            memo[key] = (-time.monotonic(), 0, 0)
                            print(f"plan_resident: {rid} {section}: line of no stored family; "
                                  "left alone", file=sys.stderr)
                            continue
                        if kind == "failed":
                            # the retry pace is in the row (attempts, next_try_at: stage writes
                            # them), so a restart of this process does not retry it early
                            line["attempts"] = tried.get(key, 0) + 1
                            print(f"plan_resident: {rid} {section} failed (attempt "
                                  f"{line['attempts']}): {why_not}", file=sys.stderr)
                        memo[key] = (time.monotonic(), int(stamps["ver"]), int(stamps["link"]),
                                     int(stamps["xmin"]))
                        lines.append(line)
                    if lines:
                        counter += 1
                        # when the rows were found to need an answer (and the standby's replay lag
                        # then), for the box to say how long a change took to reach a fresh row
                        write_atomic(args.out, f"{worker}-{int(time.time())}-{counter}",
                                     [{"stamps": stamps, "worker": worker, "seen_at": seen,
                                       "replay_lag_s": round(float(lag), 1), "why": why.get(rid, {})}, *lines])
                        answered += 1
                if not todo:
                    time.sleep(args.poll)
        except psycopg.Error as exc:
            # a table the box is swapping (its lock reaches the standby with the WAL), a
            # recovery conflict, the standby restarting: nothing is wrong with the process
            print(f"plan_resident: the database said {type(exc).__name__}: {str(exc)[:120]}; "
                  "trying again", file=sys.stderr)
            time.sleep(args.poll * 2)
        if time.monotonic() - began > args.max_seconds:
            break
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operator_api.plan_resident")
    ap.add_argument("--out", required=True)
    ap.add_argument("--queue")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--max-records", type=int, default=400)
    ap.add_argument("--max-seconds", type=float, default=6 * 3600)
    ap.add_argument("--lease-seconds", type=float, default=300.0)
    ap.add_argument("--host-config")
    args = ap.parse_args(argv)
    host = args.host_config or os.environ.get(HOST_CONFIG_ENV)
    reader, dsn = os.environ.get(READER_ENV), os.environ.get("PLX_PG_DSN")
    if not host or not reader or not dsn:
        print(f"{HOST_CONFIG_ENV}, {READER_ENV} and PLX_PG_DSN are required", file=sys.stderr)
        return 2
    try:
        shard = shard_of(args.shard)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    from operator_api.workspace import case_console

    def serve(ctx: dict[str, Any]) -> int:
        from operating.session_identity import warm_session
        session = warm_session(ctx["resolver"], reader,
                               expires_at=int(time.time()) + SESSION_SECONDS + 3600)
        view = ctx["objects_for"](session)
        return loop(view, args, dsn, shard, f"w{shard[0]}")
    return case_console.main(plan_batch.console_args(host, dsn), serve_with=serve)


if __name__ == "__main__":
    sys.exit(main())
