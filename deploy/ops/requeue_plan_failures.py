#!/usr/bin/env python3
"""Ask the resident runner again for every stored plan-section row that is a failure or a
wrongly stored "no answer": rows made before a failed run was told from a true "no answer"
(payload ``failed``, or ``no_answer`` with the old fixed reason), closed orders included (the
runner's own cycle asks open orders only). Lists the ids; with ``--apply`` each id goes into the
request spool (``operating.plan_requests``), which the puller hands to the runner's queue.

  PGUSER=plx_api python3 deploy/ops/requeue_plan_failures.py [--apply] [--dsn postgresql:///plx]
"""
from __future__ import annotations

import argparse
import os
import sys

OLD_REASON = "the plan gave no complete answer when it ran"
SQL = ("SELECT DISTINCT record_id FROM {s}.plan_section_items WHERE payload ->> 'failed' IS NOT NULL "
       "OR payload ->> 'no_answer' = %s ORDER BY 1")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"))
    ap.add_argument("--schema", default="biz")
    args = ap.parse_args(argv)
    import psycopg
    from operating import plan_requests
    with psycopg.connect(args.dsn, autocommit=True) as pg:
        ids = [r[0] for r in pg.execute(SQL.format(s=args.schema), (OLD_REASON,))]
    print(f"{len(ids)} records have a stored failure or an old-reason no answer")
    if args.apply:
        bad = [i for i in ids if not plan_requests.request(i)]
        print(f"requested {len(ids) - len(bad)}; could not request {len(bad)}")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
