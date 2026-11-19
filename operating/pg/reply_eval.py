"""How well "needs your reply" matches what a person really did, measured on the built
emails table (no fixture, no model): for one mailbox and a date range,

- recall: of the mails the person answered (each message they wrote in a thread answers the
  newest message from someone else before it, since they last wrote there), the share whose
  thread the rule flagged at the time (a message since they last wrote passed every test of
  ``alerts.OWED_TESTS`` and was inside the rule's window when they answered);
- flagged-answered: of the threads the rule flagged (its first owed message since they last
  wrote), the share they wrote in within ``WORKING_DAYS`` working days; flagged-never: the
  share they never wrote in after it.

Misses are counted by the first test the answered mail fails, false flags by sender domain
and by subject shape, so a pattern shows as a count, never as a list kept in code. Read-only
(one transaction, statement timeout); ``python3 -m operating.pg.reply_eval --mailbox ...``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Mapping, Sequence

from operating.pg import alerts
from operating.pg import business_views as bv

WORKING_DAYS = 3
TOP = 8
_TS = "coalesce(e.sent_at, e.received_at)"


def _test(sql: str) -> str:
    return sql.format(p={"max_to": str(alerts.MAX_TO)})


def _sql(schema: str) -> str:
    """One statement: per mailbox message the owner's writes around it, which tests it
    passes, then the two measures and the patterns. Parameters: mailbox, start, end (dates,
    business time zone, end exclusive), and the reply window in days."""
    tz = bv.BUSINESS_TZ
    owed = " AND ".join(f"({_test(t)})" for _, t in alerts.OWED_TESTS)
    fails = "CASE " + " ".join(
        f"WHEN NOT coalesce(({_test(t)}), false) THEN '{n}'" for n, t in alerts.OWED_TESTS
    ) + " ELSE NULL END"
    s = schema
    source = alerts.OWED_FROM.replace("{s}", s)
    return f"""
WITH m AS (
  SELECT e.handle, e.thread, e.sender, e.sender_domain, e.subject, e.received_at,
         e.from_owner, CASE WHEN e.from_owner THEN {_TS} ELSE e.received_at END AS at,
         ({owed}) AS owed, {fails} AS fails
  FROM {source} WHERE e.mailbox = %(box)s AND e.thread IS NOT NULL
    AND e.from_owner IS NOT NULL
), o AS (   -- per message, in thread order: the owner's writes and owed mail around it
  SELECT m.*,
    max(at) FILTER (WHERE from_owner) OVER (PARTITION BY thread ORDER BY at, handle
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_own,
    min(at) FILTER (WHERE from_owner) OVER (PARTITION BY thread ORDER BY at, handle
        ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS next_own,
    min(at) FILTER (WHERE NOT from_owner) OVER (PARTITION BY thread ORDER BY at, handle
        ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS next_in,
    max(at) FILTER (WHERE owed) OVER (PARTITION BY thread ORDER BY at, handle
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_owed,
    max(at) FILTER (WHERE owed) OVER (PARTITION BY thread ORDER BY at, handle
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_owed
  FROM m
), w AS (
  SELECT o.* FROM o WHERE o.from_owner IS FALSE
    AND (o.received_at AT TIME ZONE '{tz}')::date >= %(start)s::date
    AND (o.received_at AT TIME ZONE '{tz}')::date < %(end)s::date
), caught AS (   -- the newest message from someone else before each owner write
  SELECT w.*, (w.last_owed IS NOT NULL AND (w.prev_own IS NULL OR w.last_owed > w.prev_own)
      AND w.last_owed >= w.next_own - make_interval(days => %(days)s)) AS hit
  FROM w WHERE w.next_own IS NOT NULL AND (w.next_in IS NULL OR w.next_in >= w.next_own)
), flagged AS (   -- the first owed message since the owner last wrote: one alert per thread
  SELECT w.*, (SELECT count(*) FROM generate_series(
        (w.received_at AT TIME ZONE '{tz}')::date + 1, (w.next_own AT TIME ZONE '{tz}')::date,
        interval '1 day') d WHERE extract(isodow FROM d) < 6) AS wait_days
  FROM w WHERE w.owed AND (w.prev_owed IS NULL OR (w.prev_own IS NOT NULL
                                                   AND w.prev_owed <= w.prev_own))
)
SELECT json_build_object(
  'answered', (SELECT count(*) FROM caught),
  'caught', (SELECT count(*) FROM caught WHERE hit),
  'flagged', (SELECT count(*) FROM flagged),
  'flagged_answered', (SELECT count(*) FROM flagged WHERE next_own IS NOT NULL
      AND wait_days <= {WORKING_DAYS}),
  'flagged_later', (SELECT count(*) FROM flagged WHERE wait_days > {WORKING_DAYS}),
  'flagged_never', (SELECT count(*) FROM flagged WHERE next_own IS NULL),
  'miss_by_test', (SELECT coalesce(json_object_agg(f, n), '{{}}') FROM (SELECT coalesce(fails,
      'flagged, but answered after the window') AS f, count(*) n FROM caught WHERE NOT hit
      GROUP BY 1) x),
  'miss_domains', (SELECT coalesce(json_agg(x), '[]') FROM (SELECT sender_domain d, fails f,
      count(*) n FROM caught WHERE NOT hit GROUP BY 1, 2 ORDER BY 3 DESC, 1 LIMIT {TOP}) x),
  'false_domains', (SELECT coalesce(json_agg(x), '[]') FROM (SELECT sender_domain d, count(*) n,
      min(left(subject, 70)) s FROM flagged WHERE next_own IS NULL GROUP BY 1 ORDER BY 2 DESC, 1
      LIMIT {TOP}) x),
  'false_samples', (SELECT coalesce(json_agg(x), '[]') FROM (SELECT sender, left(subject, 70) s
      FROM flagged WHERE next_own IS NULL ORDER BY received_at DESC LIMIT {TOP}) x),
  'miss_samples', (SELECT coalesce(json_agg(x), '[]') FROM (SELECT sender, fails f,
      left(subject, 70) s FROM caught WHERE NOT hit ORDER BY received_at DESC LIMIT {TOP}) x))
"""


def measure(conn: Any, mailbox: str, start: dt.date, end: dt.date, *,
            owners: Mapping[str, Sequence[str]], schema: str | None = None,
            timeout_s: int = 60) -> dict[str, Any]:
    """The measures for ``mailbox`` (the emails table's mailbox id) over [start, end);
    ``owners``: per mailbox id its owner's addresses (``alerts.owner_table``)."""
    s = alerts._schema(schema)
    if end <= start:
        raise ValueError("end must be after start")
    with conn.transaction():
        alerts.owner_table(conn, owners)   # a temp table: this session's only
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
        got = conn.execute(_sql(s), {"box": mailbox.strip().lower(), "start": start,
                                     "end": end, "days": alerts.REPLY_DAYS}).fetchone()[0]
    out = dict(got if isinstance(got, dict) else json.loads(got))
    a, f = out["answered"], out["flagged"]
    out["recall"] = round(100.0 * out["caught"] / a, 1) if a else None
    out["flagged_answered_pct"] = round(100.0 * out["flagged_answered"] / f, 1) if f else None
    out["flagged_never_pct"] = round(100.0 * out["flagged_never"] / f, 1) if f else None
    out.update(mailbox=mailbox, start=start.isoformat(), end=end.isoformat())
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.reply_eval",
                                 description="needs-reply recall and precision vs real replies")
    ap.add_argument("--mailbox", action="append", required=True,
                    help="the emails table's mailbox id (repeatable)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", help="exclusive end day, ISO (default: today - working days)")
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)
    import psycopg
    from operating.pg import config
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(
        days=WORKING_DAYS + 2)   # flagged mail needs its working days to be judged
    start = end - dt.timedelta(days=args.days)
    from operating.pg import business_comms
    owners = business_comms.mailbox_addresses(business_comms.capture_db())
    if not owners:
        print("the mailbox owners' addresses could not be read", file=sys.stderr)
        return 1
    with psycopg.connect(args.dsn or config.dsn()) as conn:
        for box in args.mailbox:
            print(json.dumps(measure(conn, box, start, end, owners=owners,
                                     schema=args.schema, timeout_s=args.timeout),
                             default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
