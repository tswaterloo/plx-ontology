"""The resident History builder (speed specs 8.3 ``history`` "Incremental", 9.3 V2): every purchase
order page's History tab kept current per record, on a Spark, for ``operating.pg.record_history``
to store on the box (``record_history pull``).

``history_batch`` reads every record once and exits (the fill); this process stays up (a systemd
unit on the box holds its ssh: ``deploy/spark/history_resident.sh``) with the console built once,
as the owner's full reader (the same ``history_batch.history_of`` the fill runs), reading the
Spark's hot standby, and each few seconds reads again only the records that need it, in this order:

* ``requested``: a History tab asked for and not stored (the route's ``page_requests`` row,
  kind ``history``: a purchase order created after the fill is built at its first visit);
* ``moved``: a stored history whose dependencies moved (``record_history.moved_sql``: a newer
  version of any object its items' lineage names, a newer link row from or to the record),
  found from the versions and link rows above this process's last watermarks (re-read
  ``plan_sections.MARKS_OVERLAP`` rows below them: a row committed late), each candidate then
  decided by the exact condition; at start, every stored head is decided once;
* ``missing``: a member record of a purchase order page (``record_pages.MEMBERS_SQL``) with no
  stored history (every ``MISSING_EVERY`` seconds).

Each record is a small file in ``--out`` (written complete, then renamed: ``plan_resident.
write_atomic``): a stamps line, then the record's line (``history_batch`` format, with its
watermarks), or ``{"record_id", "error"}`` when it could not be read (the box counts and logs it;
the record is tried again after ``plan_resident.RETRY_AFTER``). It writes nothing to a business
system or to Postgres and refuses to run on anything but a standby; it ends itself when the
people exclusion changes, when nobody fetches its files, or when a newer process takes its shard
(``plan_resident``'s rules, the same functions).

  python3 -m operator_api.history_resident --out DIR [--queue DIR] [--shard I/N] [--poll SECONDS]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Mapping, Sequence

from operator_api import history_batch, plan_resident as pr

MISSING_EVERY = 60.0       # seconds between two looks for member records with no history
SWEEP_EVERY = 120.0       # seconds between two exact decisions of EVERY stored head: an audience
                          # is changed in place with no new version, so only the sweep finds it
WHY_ORDER = {"requested": 0, "moved": 1, "missing": 2}


def requested_sql(schema: str) -> str:
    return (f"SELECT r.page_key FROM {schema}.page_requests r WHERE r.page_kind = 'history' "
            "ORDER BY r.requested_at")


def missing_sql(schema: str) -> str:
    from operating.pg.record_pages import MEMBERS_SQL
    return (f"SELECT m.record_id FROM ({MEMBERS_SQL.format(s=schema)}) m WHERE NOT EXISTS "
            f"(SELECT 1 FROM {schema}.page_items h WHERE h.page_kind = 'history' AND h.page_key = "
            "m.record_id AND h.section = 'history_head') GROUP BY m.record_id ORDER BY "
            "bool_or(m.status IN ('open', 'draft')) DESC, m.record_id")


def moved_sql(schema: str, keys: bool) -> str:
    """The stored histories whose dependencies moved (``keys``: among ``%(keys)s``)."""
    from operating.pg import record_history as rh
    among = "AND h.page_key = ANY(%(keys)s) " if keys else ""
    return (f"SELECT h.page_key FROM {schema}.page_items h WHERE h.page_kind = 'history' AND "
            f"h.section = 'history_head' {among}AND {rh.moved_sql(schema)} ORDER BY h.page_key")


def sweep_sql(schema: str) -> str:
    """Every stored head decided exactly: (key, audience moved). The audience stamp is the part
    no watermark can find; the box takes a page whose audience moved OUT until it is read again."""
    from operating.pg import record_history as rh
    content, aud = rh.moved_parts(schema)
    return (f"SELECT h.page_key, {aud} FROM {schema}.page_items h WHERE h.page_kind = 'history' "
            f"AND h.section = 'history_head' AND ({content} OR {aud}) ORDER BY h.page_key")


def candidates_sql(schema: str) -> str:
    """Stored histories that MAY have moved since watermarks ``%(v)s`` / ``%(l)s`` (lowered by the
    overlap): their items' lineage names an object with a version above it, or a link row above it
    starts or ends at the record. The exact condition decides each (``moved_sql``)."""
    return (
        "WITH obj AS (SELECT DISTINCT object_id FROM ontology.object_versions WHERE ver > "
        "%(v)s), lnk AS (SELECT from_object AS o FROM ontology.link_rows WHERE row_no > %(l)s "
        "UNION SELECT to_object FROM ontology.link_rows WHERE row_no > %(l)s) "
        f"SELECT DISTINCT p.page_key FROM {schema}.lineage g JOIN {schema}.page_items p ON "
        "p.handle = g.handle WHERE g.view = 'page_items' AND p.page_kind = 'history' AND "
        "(g.object_ids && ARRAY(SELECT object_id FROM obj) OR g.read_ids && ARRAY(SELECT "
        f"object_id FROM obj)) UNION SELECT h.page_key FROM {schema}.page_items h JOIN lnk ON "
        "lnk.o = h.page_key WHERE h.page_kind = 'history' AND h.section = 'history_head'")


def answered_since(pg: Any, schema: str, key: str, deps: Mapping[str, int]) -> bool:
    """Whether this process's own answer at ``deps`` still holds (nothing moved past it): the
    standby may not show the box's put of it yet."""
    from operating.pg import record_history as rh
    got = pg.execute(
        f"SELECT NOT {rh.moved_sql(schema, given=True)} FROM (SELECT %(k)s::text AS page_key) h",
        {"k": key, "v": deps["ver"], "l": deps["link"], "x": deps["xmin"]}).fetchone()
    return bool(got and got[0])


class Finder:
    """What to read next, per cycle (``discover``); its watermarks advance as it looks."""

    def __init__(self, schema: str = "biz") -> None:
        self.schema = schema
        self.marks: tuple[int, int] | None = None     # (ver, link) looked through
        self.missing_at = -MISSING_EVERY
        self.swept_at: float | None = None
        self.moved_marks: list[tuple[str, bool]] = []     # the last sweep's (key, audience moved)
        self.swept = False                # a sweep was made by the last ``discover``
        self.memo: dict[str, tuple[float, dict[str, int] | None]] = {}

    def discover(self, pg: Any, shard: tuple[int, int], now: float | None = None
                 ) -> list[tuple[str, str]]:
        from operating.pg import plan_sections as ps
        at = time.monotonic() if now is None else now
        s = self.schema
        found: dict[str, str] = {}

        def add(keys: Sequence[str], why: str) -> None:
            for k in keys:
                if pr._hash(k) % shard[1] == shard[0] and k not in found:
                    found[k] = why
        add([r[0] for r in pg.execute(requested_sql(s))], "requested")
        ver = int(pg.execute(ps.VER_SQL).fetchone()[0])
        link = int(pg.execute(ps.LINK_SQL).fetchone()[0])
        sweep = self.marks is None or self.swept_at is None or at - self.swept_at >= SWEEP_EVERY
        self.swept = sweep
        if sweep:                     # every stored head, decided exactly (the first look, then
            got = pg.execute(sweep_sql(s)).fetchall()        # every SWEEP_EVERY: audiences)
            self.moved_marks = [(r[0], bool(r[1])) for r in got]
            self.swept_at = at
            add([k for k, _ in self.moved_marks], "moved")
        if not sweep:
            cands = [r[0] for r in pg.execute(candidates_sql(s), {
                "v": self.marks[0] - ps.MARKS_OVERLAP, "l": self.marks[1] - ps.MARKS_OVERLAP})]
            mine = [k for k in cands if pr._hash(k) % shard[1] == shard[0]]
            if mine:
                add([r[0] for r in pg.execute(moved_sql(s, True), {"keys": mine})], "moved")
        self.marks = (ver, link)
        if at - self.missing_at >= MISSING_EVERY:
            add([r[0] for r in pg.execute(missing_sql(s))], "missing")
            self.missing_at = at
        out = []
        for key, why in found.items():
            held = self.memo.get(key)
            if held is not None:
                when, deps = held
                if deps is None:                     # it failed: left alone a while
                    if at - when < pr.RETRY_AFTER:
                        continue
                elif why in ("missing", "requested") and at - when >= pr.RETRY_AFTER:
                    pass                             # answered long ago, still not stored: again
                elif answered_since(pg, s, key, deps):
                    continue                         # answered; the standby lags the box's put
            out.append((key, why))
        out.sort(key=lambda kw: WHY_ORDER[kw[1]])
        return out


def loop(view: Any, args: argparse.Namespace, pg_dsn: str, shard: tuple[int, int],
         worker: str) -> int:
    import psycopg
    finder = Finder()
    answered = counter = 0
    began = time.monotonic()
    started_with: list[str | None] = []
    nonce = pr.take_shard(args.queue, worker)
    wall = time.time()
    while answered < args.max_records:
        stop = pr.still_wanted(args.out, args.queue, worker, nonce, wall, args.lease_seconds)
        if stop:
            print(f"history_resident: ending: {stop}", file=sys.stderr)
            return 0
        try:
            with psycopg.connect(pg_dsn, autocommit=True) as pg:
                if args.require_standby and not pg.execute(
                        "SELECT pg_is_in_recovery()").fetchone()[0]:
                    print("history_resident: the database is not a standby: refusing to run",
                          file=sys.stderr)
                    return 3
                stamp = pr.exclusion_stamp(pg)
                if not started_with:
                    started_with.append(stamp)
                elif stamp != started_with[0]:
                    print("history_resident: the people exclusion changed: ending to start "
                          "with the new list", file=sys.stderr)
                    return 0
                todo = finder.discover(pg, shard)
                if finder.swept:     # said to the box on EVERY sweep, moved pages or none: shown
                    mine = [(k, a) for k, a in finder.moved_marks   # with their age / taken out;
                            if pr._hash(k) % shard[1] == shard[0]]  # and the sweep itself, so the
                    finder.moved_marks = []                         # box can tell a silent shard
                    counter += 1
                    pr.write_atomic(args.out, f"{worker}-{int(time.time())}-{counter}", [
                        {"stamps": {"why": "sweep"}, "worker": worker},
                        {"sweep": {"shard": f"{shard[0]}/{shard[1]}", "moved": len(mine)},
                         "stale": [k for k, a in mine if not a],
                         "reset": [k for k, a in mine if a]}])
                for key in pr.forced(args.queue):
                    if pr._hash(key) % shard[1] == shard[0] and all(k != key for k, _ in todo):
                        todo.insert(0, (key, "forced"))
                for key, why in todo[:pr.PER_CYCLE]:
                    if pr.still_wanted(args.out, args.queue, worker, nonce, wall,
                                       args.lease_seconds):
                        break
                    deps = history_batch.deps_of(pg)
                    t0 = time.monotonic()
                    try:
                        line = {**history_batch.history_of(view, key), "deps": deps}
                        finder.memo[key] = (time.monotonic(), deps)
                    except Exception as exc:  # noqa: BLE001 - one typed line, counted by the box
                        line = {"record_id": key, "error": f"{type(exc).__name__}: {exc}"}
                        finder.memo[key] = (time.monotonic(), None)
                    line["took"] = round(time.monotonic() - t0, 2)
                    counter += 1
                    pr.write_atomic(args.out, f"{worker}-{int(time.time())}-{counter}",
                                    [{"stamps": {"why": why}, "worker": worker}, line])
                    answered += 1
                if not todo:
                    time.sleep(args.poll)
        except psycopg.Error as exc:
            # a table the box is swapping, a recovery conflict, the standby restarting
            print(f"history_resident: the database said {type(exc).__name__}: {exc}; trying "
                  "again", file=sys.stderr)
            time.sleep(args.poll * 2)
        if time.monotonic() - began > args.max_seconds:
            break
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operator_api.history_resident")
    ap.add_argument("--out", required=True)
    ap.add_argument("--queue")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--max-records", type=int, default=400)
    ap.add_argument("--max-seconds", type=float, default=pr.SESSION_SECONDS)
    ap.add_argument("--lease-seconds", type=float, default=300.0)
    ap.add_argument("--host-config")
    ap.add_argument("--no-require-standby", dest="require_standby", action="store_false",
                    help="a build copy (tests and rehearsals): not a standby")
    args = ap.parse_args(argv)
    from operator_api.plan_batch import console_args
    from operator_api.workspace import case_console
    host = args.host_config or os.environ.get(history_batch.HOST_CONFIG_ENV)
    reader = os.environ.get(history_batch.READER_ENV)
    dsn = os.environ.get("PLX_PG_DSN")
    if not host or not reader or not dsn:
        print(f"{history_batch.HOST_CONFIG_ENV}, {history_batch.READER_ENV} and PLX_PG_DSN are "
              "required", file=sys.stderr)
        return 2
    shard = pr.shard_of(args.shard)
    os.makedirs(args.out, exist_ok=True)
    worker = f"h{shard[0]}of{shard[1]}"

    def serve(ctx: dict[str, Any]) -> int:
        from operating.session_identity import warm_session
        # the process ends (``--max-seconds``) before its session would
        session = warm_session(ctx["resolver"], reader,
                               expires_at=int(time.time() + args.max_seconds) + 3600)
        return loop(ctx["objects_for"](session), args, dsn, shard, worker)
    return case_console.main(console_args(host, dsn), serve_with=serve)


if __name__ == "__main__":
    sys.exit(main())
