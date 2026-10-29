"""``python3 -m operating.pg migrate|status|check|set-embedding --model M``.

Exit 0 when the command succeeded (check: no problem), 1 on a problem, 2 on bad usage.
Output is JSON on stdout; the DSN is never printed (it may carry a password).
"""
from __future__ import annotations

import argparse
import json
import sys

import psycopg

from operating.pg import config, migrate, schema_check
from operating.pg.pool import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg")
    parser.add_argument("--dsn", default=None, help="default: PLX_PG_DSN or postgresql:///plx")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations")
    sub.add_parser("status", help="applied / pending migrations and row counts")
    sub.add_parser("check", help="schema against the lane contract")
    switch = sub.add_parser("set-embedding", help="retype the vector columns for a model "
                            "(clears embeddings of the old model)")
    switch.add_argument("--model", required=True)
    switch.add_argument("--dim", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        with connect(args.dsn or config.dsn()) as conn:
            if args.command == "migrate":
                out = {"applied_now": migrate.migrate(conn), **migrate.status(conn)}
                ok = not out["drift"]
            elif args.command == "status":
                out = {**migrate.status(conn), **schema_check.summary(conn)}
                ok = not out["drift"] and not out["pending"]
            elif args.command == "check":
                problems = schema_check.check(conn)
                state = migrate.status(conn)
                problems += [f"migration_pending:{n}" for n in state["pending"]]
                problems += [f"migration_drift:{n}" for n in state["drift"]]
                out, ok = {"problems": problems}, not problems
            else:
                out = migrate.set_embedding(conn, args.model, args.dim)
                ok = True
    except (psycopg.Error, migrate.MigrationError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc).splitlines()[0]
                          if str(exc) else ""}))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
