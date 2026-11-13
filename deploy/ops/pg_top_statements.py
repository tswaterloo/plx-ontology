#!/usr/bin/env python3
"""The statements that cost the PostgreSQL server most, read-only (CORRECTNESS-COMPUTE audit
P-05/P-07): pg_stat_statements ranked by total execution time, with each statement's calls,
mean time, rows, blocks read from disk, blocks hit and temp blocks written. The statement
text is the server's normalised form (constants are ``$n``), cut to ``--text`` characters, so
no stored value is printed.

The library loads only at server start (deploy/ops/host/postgres/zz-plx-stats.conf, installed by
deploy/ops/host/install.py); until a restart has loaded it and the extension exists in the
database, the result is ``{"state": "not_loaded"}`` and the exit status 2 (never an empty
ranking that reads as "nothing costs anything").

    python3 deploy/ops/pg_top_statements.py [--dsn DSN] [--limit 25] [--text 160] [--by total]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ORDER = {"total": "total_exec_time", "read": "shared_blks_read", "temp": "temp_blks_written",
         "calls": "calls"}
NOT_LOADED = {"state": "not_loaded",
              "next": "restart PostgreSQL with deploy/ops/host/postgres/zz-plx-stats.conf "
                      "installed, then CREATE EXTENSION IF NOT EXISTS pg_stat_statements"}


def ranking(conn: Any, *, limit: int = 25, text: int = 160, by: str = "total") -> dict[str, Any]:
    """The top ``limit`` statements by ``by`` (``ORDER``) on ``conn`` (read-only), or
    ``NOT_LOADED`` when the extension is missing or its library was not preloaded."""
    import psycopg

    if by not in ORDER:
        raise ValueError(f"order_unknown:{by}")
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
        if cur.fetchone() is None:
            conn.rollback()
            return dict(NOT_LOADED)
        try:
            cur.execute(
                "SELECT left(regexp_replace(query, '\\s+', ' ', 'g'), %s), calls,"
                " round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 2), rows,"
                " shared_blks_read, shared_blks_hit, temp_blks_written"
                " FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid"
                " WHERE d.datname = current_database()"
                f" ORDER BY {ORDER[by]} DESC NULLS LAST LIMIT %s", (int(text), int(limit)))
        except psycopg.errors.ObjectNotInPrerequisiteState:
            conn.rollback()   # "must be loaded via shared_preload_libraries"
            return dict(NOT_LOADED)
        rows = cur.fetchall()
    conn.rollback()
    keys = ("statement", "calls", "total_ms", "mean_ms", "rows", "blocks_read", "blocks_hit",
            "temp_blocks_written")
    return {"state": "ok", "by": by,
            "statements": [{k: (float(v) if k.endswith("_ms") else v)
                            for k, v in zip(keys, row)} for row in rows]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--text", type=int, default=160)
    parser.add_argument("--by", choices=sorted(ORDER), default="total")
    args = parser.parse_args(argv)
    from operating.pg import pool

    with pool.connect(args.dsn, options="-c default_transaction_read_only=on "
                                        "-c statement_timeout=60000") as conn:
        found = ranking(conn, limit=args.limit, text=args.text, by=args.by)
    print(json.dumps(found, indent=1))
    return 0 if found["state"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
