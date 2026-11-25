"""The comms text sidecar (migration 0074, ASK8; Fable spec 15.3 step 2): ``biz.comm_text``
holds, per mail and Teams message, its date and the union of its text chunks' word vectors, so
``comms_search`` prunes by the window and the view before it reads the words (the chunk word
index has neither a date nor a source: a common party word read its whole match, 45-61 s).

    python3 -m operating.pg.comm_text status
    python3 -m operating.pg.comm_text build [--days 400] [--view teams_messages]
    python3 -m operating.pg.comm_text refresh
    python3 -m operating.pg.comm_text seal --view emails --since 2026-09-29

``build``: each view in day ranges (set-based, each range one statement), a disk and WAL
check between ranges; the view's state row is written only when the whole range is in, so a
search reads the sidecar only for what is complete. ``refresh``: the text fields newer than
the state's ``built_field_id`` (less ``OVERLAP``, for a field committed late), and any message
of the covered window the sidecar lacks (a row the view got after its text was read); the
search itself reads the fields newer than ``built_field_id`` the old way, so a message is never
missed between refreshes. Idempotent: every write is an upsert by (handle, field, piece): a message with several text
fields has a row for each, and a field whose vector would pass tsvector's limit is held in
pieces (migration 0075), never refused. Exit 1 on a failure (never a silent skip)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Mapping, Sequence

# view -> its date column (``comms_search.VIEWS``)
VIEWS = {"teams_messages": "created_at", "emails": "received_at"}
# a view's default cover: Teams whole, mail the last COVER_DAYS (older mail reads the old way)
COVER_DAYS = {"teams_messages": None, "emails": 400}
# the day range of one statement
RANGE_DAYS = {"teams_messages": 60, "emails": 3}
# fields below the state's mark re-read each refresh: a text field committed after a higher id
OVERLAP = 20000
# a field's words are held in pieces whose stored sizes sum to under this, so a piece's vector
# stays under tsvector's limit (1,048,575 bytes) even where the stored form is compressed
PIECE_BYTES = 300_000

# the computation every pass does per range, shared by the fused upsert and the Spark move's
# compute half: a row per (handle, field, piece) with the piece's vector. In split mode the
# compute runs on the Spark's hot standby (the heavy scan and aggregation never on the box),
# the rows ship back and the apply writes them in the very upsert the fused pass used.
_CTE = ("WITH w AS (SELECT b.handle, b.{when} AS at, fd.field_id, w.words, "
        "sum(pg_column_size(w.words)) OVER (PARTITION BY b.handle, fd.field_id "
        "ORDER BY c.chunk_id) AS run "
        "FROM biz.{view} b "
        "LEFT JOIN ontology.text_fields fd ON fd.object_id = b.object_ids[1] "
        "AND fd.version_handle = b.handle "
        "LEFT JOIN ontology.text_chunks c ON c.field_id = fd.field_id "
        "LEFT JOIN ontology.chunk_words w ON w.chunk_id = c.chunk_id "
        "WHERE {where}), "
        "p AS (SELECT handle, at, field_id, floor(coalesce(run, 0) / %(piece)s)::int AS part, "
        "coalesce(string_agg(words::text, ' ')::tsvector, ''::tsvector) AS tsv "
        "FROM w GROUP BY handle, at, field_id, floor(coalesce(run, 0) / %(piece)s)::int) ")

UPSERT_SELECT = ("SELECT handle, %(view)s AS view, at, field_id, part, "
                 "max(part) OVER (PARTITION BY handle, field_id) = 0 AS whole, tsv FROM p ")

UPSERT = (_CTE +
          "INSERT INTO biz.comm_text (handle, view, at, field_id, part, whole, tsv) "
          + UPSERT_SELECT +
          "ON CONFLICT (handle, (coalesce(field_id, 0)), part) DO UPDATE SET at = EXCLUDED.at, "
          "whole = EXCLUDED.whole, tsv = EXCLUDED.tsv")

# the compute half of a split pass (runs read-only, on the Spark's standby): the same rows the
# fused upsert would insert, the vector in its exact text form for the shipping file
# a shipped row needs no view column: it belongs to the header it follows in the file
COMPUTE = (_CTE + UPSERT_SELECT.replace("%(view)s AS view, ", "").replace(
    "tsv FROM p", "tsv::text AS tsv FROM p"))

# a "no text yet" row (no field) is dropped once its message has a row with a field
DROP_PLACEHOLDERS = ("DELETE FROM biz.comm_text t USING biz.{view} b WHERE t.handle = b.handle "
                     "AND t.view = %(view)s AND t.field_id IS NULL AND {where} AND EXISTS "
                     "(SELECT 1 FROM biz.comm_text t2 WHERE t2.handle = t.handle "
                     "AND t2.field_id IS NOT NULL)")


class CommTextError(RuntimeError):
    pass


def state(pg: Any) -> dict[str, dict[str, Any]]:
    """The sidecar's coverage per view (empty before migration 0074 or the first build)."""
    if pg.execute("SELECT to_regclass('biz.comm_text_state')").fetchone()[0] is None:
        return {}
    rows = pg.execute("SELECT view, covered_from, built_field_id, built_at, rows, no_text, "
                      "notes FROM biz.comm_text_state").fetchall()
    return {r[0]: {"covered_from": r[1], "built_field_id": r[2], "built_at": r[3],
                   "rows": r[4], "no_text": r[5], "notes": r[6]} for r in rows}


def _ranges(lo: dt.datetime, hi: dt.datetime, days: int) -> list[tuple[dt.datetime, dt.datetime]]:
    out, at = [], hi
    while at > lo:       # newest range first
        start = max(lo, at - dt.timedelta(days=days))
        out.append((start, at))
        at = start
    return out


def _guard(pg: Any, view: str) -> None:
    """The disk floor and the standby's WAL budget, before a range (``generic_records.room``)."""
    from operating.pg import generic_records
    generic_records.room(pg, 1)


def build(dsn: str, *, views: Sequence[str] | None = None, days: int | None = None,
          log: Any = print) -> dict[str, Any]:
    import psycopg
    out: dict[str, Any] = {}
    now = dt.datetime.now(dt.timezone.utc)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET statement_timeout = 0")
        for view in views or list(VIEWS):
            when = VIEWS[view]
            span = days if days is not None else COVER_DAYS[view]
            lo = (now - dt.timedelta(days=span)) if span else dt.datetime(2000, 1, 1,
                                                                          tzinfo=dt.timezone.utc)
            top = pg.execute("SELECT coalesce(max(field_id), 0) FROM ontology.text_fields"
                             ).fetchone()[0]
            first = pg.execute(f"SELECT min(b.{when}) FROM biz.{view} b WHERE b.{when} >= %s",
                               (lo,)).fetchone()[0]
            if first is None:
                raise CommTextError(f"{view}: no rows since {lo:%Y-%m-%d}")
            total = 0
            for start, end in _ranges(max(lo, first), now + dt.timedelta(days=1),
                                      RANGE_DAYS[view]):
                _guard(pg, view)
                where = f"b.{when} >= %(lo)s AND b.{when} < %(hi)s"
                params = {"lo": start, "hi": end}
                cur = pg.execute(UPSERT.format(view=view, when=when, where=where),
                                 {**params, "view": view, "piece": PIECE_BYTES})
                total += cur.rowcount
                pg.execute(DROP_PLACEHOLDERS.format(view=view, where=where),
                           {**params, "view": view})
                log(f"{dt.datetime.now():%H:%M:%S} {view} {start:%Y-%m-%d} to {end:%Y-%m-%d}: "
                    f"{cur.rowcount} rows", flush=True)
            pg.execute(f"ANALYZE biz.comm_text")
            rows, notext = pg.execute(
                "SELECT count(*), count(*) FILTER (WHERE field_id IS NULL) FROM biz.comm_text "
                "WHERE view = %s", (view,)).fetchone()
            pg.execute(
                "INSERT INTO biz.comm_text_state (view, covered_from, built_field_id, built_at, "
                "rows, no_text, notes) VALUES (%s, %s, %s, now(), %s, %s, %s) ON CONFLICT (view) "
                "DO UPDATE SET covered_from = EXCLUDED.covered_from, built_field_id = "
                "EXCLUDED.built_field_id, built_at = EXCLUDED.built_at, rows = EXCLUDED.rows, "
                "no_text = EXCLUDED.no_text, notes = EXCLUDED.notes",
                (view, lo if span else dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
                 max(0, top - OVERLAP), rows, notext, json.dumps({"build_days": span})))
            out[view] = {"rows": rows, "no_text": notext, "covered_from": lo.isoformat()}
    return out


# a sealed build's mark sits this far under the newest field, so the refresh re-reads the
# fields that arrived while the build ran
SEAL_BACK = 200_000


def seal(dsn: str, *, view: str, since: dt.datetime, log: Any = print) -> dict[str, Any]:
    """Write the state row of a build that stopped on its disk floor, for the newest mail it
    finished: every message of the view dated at or after ``since`` must have a sidecar row (a
    count of those without one, which must be 0, else nothing is written and it says how many),
    and the mark goes ``SEAL_BACK`` fields under the newest, so the next refresh reads again
    what arrived while the build ran. Mail older than ``since`` keeps reading the old way."""
    import psycopg
    when = VIEWS[view]
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET statement_timeout = 0")
        missing = pg.execute(
            f"SELECT count(*) FROM biz.{view} b WHERE b.{when} >= %s AND NOT EXISTS (SELECT 1 "
            "FROM biz.comm_text t WHERE t.handle = b.handle)", (since,)).fetchone()[0]
        if missing:
            raise CommTextError(f"{view}: {missing} messages since {since:%Y-%m-%d} have no "
                                "sidecar row; nothing sealed")
        top = pg.execute("SELECT coalesce(max(field_id), 0) FROM ontology.text_fields"
                         ).fetchone()[0]
        rows, notext = pg.execute(
            "SELECT count(*), count(*) FILTER (WHERE field_id IS NULL) FROM biz.comm_text "
            "WHERE view = %s", (view,)).fetchone()
        pg.execute(
            "INSERT INTO biz.comm_text_state (view, covered_from, built_field_id, built_at, "
            "rows, no_text, notes) VALUES (%s, %s, %s, now(), %s, %s, %s) ON CONFLICT (view) "
            "DO UPDATE SET covered_from = EXCLUDED.covered_from, built_field_id = "
            "EXCLUDED.built_field_id, built_at = EXCLUDED.built_at, rows = EXCLUDED.rows, "
            "no_text = EXCLUDED.no_text, notes = EXCLUDED.notes",
            (view, since, max(0, top - SEAL_BACK), rows, notext,
             json.dumps({"sealed_at_disk_floor": True})))
        log(f"{view}: sealed from {since:%Y-%m-%d}, {rows} rows", flush=True)
        return {view: {"rows": rows, "no_text": notext, "covered_from": since.isoformat()}}


def refresh(dsn: str, *, log: Any = print) -> dict[str, Any]:
    """The tail of every view the sidecar covers: fields past the mark, and rows it lacks."""
    import psycopg
    out: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET statement_timeout = 0")
        for view, st in state(pg).items():
            when = VIEWS[view]
            top = pg.execute("SELECT coalesce(max(field_id), 0) FROM ontology.text_fields"
                             ).fetchone()[0]
            where_tail = (f"b.{when} >= %(cov)s AND b.handle IN (SELECT fd2.version_handle FROM "
                          "ontology.text_fields fd2 WHERE fd2.field_id > %(mark)s)")
            where_miss = (f"b.{when} >= %(cov)s AND NOT EXISTS (SELECT 1 FROM biz.comm_text t "
                          "WHERE t.handle = b.handle)")
            n = 0
            for where in (where_tail, where_miss):
                params = {"cov": st["covered_from"], "mark": st["built_field_id"]}
                n += pg.execute(UPSERT.format(view=view, when=when, where=where),
                                {**params, "view": view, "piece": PIECE_BYTES}).rowcount
                pg.execute(DROP_PLACEHOLDERS.format(view=view, where=where),
                           {**params, "view": view})
            pg.execute("UPDATE biz.comm_text_state SET built_field_id = %s, built_at = now(), "
                       "rows = (SELECT count(*) FROM biz.comm_text WHERE view = %s) "
                       "WHERE view = %s AND built_field_id < %s",
                       (max(0, top - OVERLAP), view, view, max(0, top - OVERLAP)))
            out[view] = {"upserted": n}
            log(f"{view}: {n} rows", flush=True)
    return out


def compute(dsn: str, out_path: str, *, log: Any = print) -> dict[str, Any]:
    """The compute half of a split refresh (the Spark move, T2 speed): read-only, so the
    Spark's hot standby carries the scan and aggregation. Writes one gzip JSON-lines file:
    a header per view (the covered window and the field mark the pass saw, so the apply's
    state write matches what the compute read, never the box's newer tail) and one line per
    row the fused upsert would have inserted. Nothing here writes."""
    import gzip
    import psycopg
    out: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as pg, gzip.open(out_path, "wt",
                                                                encoding="utf-8") as fh:
        pg.execute("SET statement_timeout = 0")
        pg.execute("SET default_transaction_read_only = on")
        for view, st in state(pg).items():
            when = VIEWS[view]
            top = pg.execute("SELECT coalesce(max(field_id), 0) FROM ontology.text_fields"
                             ).fetchone()[0]
            where_tail = (f"b.{when} >= %(cov)s AND b.handle IN (SELECT fd2.version_handle FROM "
                          "ontology.text_fields fd2 WHERE fd2.field_id > %(mark)s)")
            where_miss = (f"b.{when} >= %(cov)s AND NOT EXISTS (SELECT 1 FROM biz.comm_text t "
                          "WHERE t.handle = b.handle)")
            fh.write(json.dumps({"header": True, "view": view,
                                 "covered_from": str(st["covered_from"]),
                                 "built_field_id": st["built_field_id"],
                                 "top": top}) + "\n")
            n = 0
            for where in (where_tail, where_miss):
                params = {"cov": st["covered_from"], "mark": st["built_field_id"]}
                for handle, at, field_id, part, whole, tsv in pg.execute(
                        COMPUTE.format(view=view, when=when, where=where),
                        {**params, "piece": PIECE_BYTES}):
                    fh.write(json.dumps([handle, at.isoformat(), field_id, part, whole, tsv])
                             + "\n")
                    n += 1
            out[view] = {"computed": n}
            log(f"{view}: {n} rows computed", flush=True)
    return out


def _sections(fh: Any):
    """Stream ``(header, rows)`` pairs from a ``compute`` JSON-lines file without buffering
    rows in RAM. Raises ``CommTextError`` if any row precedes a header or is malformed."""
    pending: list[Any] = []

    def next_rec() -> Any:
        if pending:
            return pending.pop()
        line = fh.readline()
        if not line:
            return None
        return json.loads(line)

    rec = next_rec()
    while rec is not None:
        if not (isinstance(rec, dict) and rec.get("header")):
            raise CommTextError("comm_text apply: row before header")
        header = rec

        def rows() -> Any:
            while True:
                item = next_rec()
                if item is None:
                    return
                if isinstance(item, dict) and item.get("header"):
                    pending.append(item)
                    return
                if not isinstance(item, list) or len(item) != 6:
                    raise CommTextError(f"comm_text apply: malformed row: {item!r}")
                yield item

        yield header, rows()
        rec = next_rec()


def apply_rows(dsn: str, in_path: str, *, log: Any = print) -> dict[str, Any]:
    """The write half of a split refresh, on the box: streams each view section from the gzip
    JSON-lines file straight into a temp table COPY, upserts into ``biz.comm_text`` through
    the very upsert the fused pass used, drops placeholders only for handles in the shipped
    batch that gained a field, and moves the state mark to what the compute saw — never past
    it, so a field the standby had not replayed yet is read again on the next pass. Idempotent:
    applying the same file twice writes nothing new."""
    import gzip
    import psycopg
    out: dict[str, Any] = {}
    with gzip.open(in_path, "rt", encoding="utf-8") as fh, \
            psycopg.connect(dsn, autocommit=False) as pg:
        pg.execute("SET statement_timeout = 0")
        pg.execute("CREATE TEMP TABLE comm_text_ship (handle text, at timestamptz, "
                   "field_id bigint, part int, whole boolean, tsv text) ON COMMIT DROP")
        for header, rows in _sections(fh):
            view = header.get("view")
            if view not in VIEWS:
                raise CommTextError(f"comm_text apply: unknown view {view!r}")
            pg.execute("TRUNCATE comm_text_ship")
            with pg.cursor().copy("COPY comm_text_ship (handle, at, field_id, part, whole, tsv) "
                                  "FROM STDIN") as copy:
                for handle, at, field_id, part, whole, tsv in rows:
                    copy.write_row((handle, at, field_id, part, whole, tsv))
            # one INSERT for what the fused pass upserted in two: a message whose rows the
            # tail and the missing-window phases both computed is shipped twice; ctid DESC
            # keeps the later phase's row deterministically, matching the fused pass's second
            # upsert overwriting the first's row
            cur = pg.execute(
                "INSERT INTO biz.comm_text (handle, view, at, field_id, part, whole, tsv) "
                "SELECT DISTINCT ON (handle, coalesce(field_id, 0), part) handle, %s, at, "
                "field_id, part, whole, tsv::tsvector FROM comm_text_ship "
                "ORDER BY handle, coalesce(field_id, 0), part, ctid DESC "
                "ON CONFLICT (handle, (coalesce(field_id, 0)), part) DO UPDATE SET at = "
                "EXCLUDED.at, whole = EXCLUDED.whole, tsv = EXCLUDED.tsv", (view,))
            n = cur.rowcount
            # scope placeholder deletion to the shipped batch: never scan biz.{view} on the box
            pg.execute(
                "DELETE FROM biz.comm_text t WHERE t.view = %s AND t.field_id IS NULL "
                "AND EXISTS (SELECT 1 FROM comm_text_ship s "
                "WHERE s.handle = t.handle AND s.field_id IS NOT NULL)", (view,))
            new_mark = max(0, header["top"] - OVERLAP)
            pg.execute("UPDATE biz.comm_text_state SET built_field_id = %s, built_at = now(), "
                       "rows = (SELECT count(*) FROM biz.comm_text WHERE view = %s) "
                       "WHERE view = %s AND built_field_id < %s",
                       (new_mark, view, view, new_mark))
            out[view] = {"applied": out.get(view, {}).get("applied", 0) + n}
            log(f"{view}: {n} rows applied", flush=True)
        pg.commit()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.comm_text")
    ap.add_argument("command", choices=("status", "build", "refresh", "seal", "compute", "apply"))
    ap.add_argument("--file", help="compute: write the rows here; apply: read them from here")
    ap.add_argument("--dsn")
    ap.add_argument("--days", type=int, help="build: cover this many days of mail")
    ap.add_argument("--view", action="append", choices=sorted(VIEWS))
    ap.add_argument("--since", help="seal: the date (YYYY-MM-DD) the finished ranges reach back to")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    try:
        if args.command == "status":
            import psycopg
            with psycopg.connect(dsn, autocommit=True) as pg:
                out: Any = {v: {k: str(x) for k, x in s.items()} for v, s in state(pg).items()}
        elif args.command == "seal":
            if not args.since or not args.view or len(args.view) != 1:
                raise CommTextError("seal needs one --view and --since YYYY-MM-DD")
            out = seal(dsn, view=args.view[0], since=dt.datetime.fromisoformat(
                args.since).replace(tzinfo=dt.timezone.utc))
        elif args.command == "build":
            out = build(dsn, views=args.view, days=args.days)
        elif args.command == "compute":
            if not args.file:
                raise CommTextError("compute needs --file")
            out = compute(dsn, args.file)
        elif args.command == "apply":
            if not args.file:
                raise CommTextError("apply needs --file")
            out = apply_rows(dsn, args.file)
        else:
            out = refresh(dsn)
    except Exception as exc:  # noqa: BLE001 - said, exit 1: never a silent skip
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
