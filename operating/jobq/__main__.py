"""``python3 -m operating.jobq <command>``:

    init                                   create the queue's schema (idempotent)
    enqueue --class C [--key K] [--once]   queue a unit job (timers and path units)
    run --class C [--key K] -- CMD...      run CMD as an inline job; its exit status
                                           (69: the queue is unreachable, CMD did not run)
    worker [--state-dir D]                 the worker daemon
    status [--json]                        depth, running, waiting, refusals, dead letters
    retry ID | cancel ID                   requeue a dead job | cancel a queued one

The queue's database is ``PLX_JOBQ_DSN`` (default ``dbname=plx_jobq``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import inline
from . import queue as jq
from . import status as js

UNREACHABLE = 69   # EX_UNAVAILABLE: `run` could not reach the queue; the command did not run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.jobq",
                                     description="The host's job queue.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    enq = sub.add_parser("enqueue")
    enq.add_argument("--class", dest="cls", required=True)
    enq.add_argument("--key")
    enq.add_argument("--label")
    enq.add_argument("--once", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--class", dest="cls", required=True)
    run.add_argument("--key")
    run.add_argument("--label")
    run.add_argument("--wait-timeout", type=float)
    run.add_argument("command", nargs=argparse.REMAINDER)
    work = sub.add_parser("worker")
    work.add_argument("--state-dir", type=Path)
    work.add_argument("--poll", type=float, default=5.0)
    stat = sub.add_parser("status")
    stat.add_argument("--json", action="store_true")
    for name in ("retry", "cancel"):
        sub.add_parser(name).add_argument("id", type=int)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run needs a command after --")
        try:
            return inline.run(command, args.cls, key=args.key, label=args.label,
                              wait_timeout=args.wait_timeout)
        except inline.Duplicate as exc:
            print(f"jobq: {exc}", file=sys.stderr)
            return 75
        except (inline.Cancelled, inline.WaitTimeout) as exc:
            print(f"jobq: {exc}", file=sys.stderr)
            return 124
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001 - a queue that cannot be reached
            import psycopg
            if not isinstance(exc, psycopg.OperationalError):
                raise
            print(f"jobq: queue unreachable ({type(exc).__name__})", file=sys.stderr)
            return UNREACHABLE

    with jq.Queue() as queue:
        if args.cmd == "init":
            queue.init()
            print("jobq schema ready")
        elif args.cmd == "enqueue":
            job_id, what = queue.enqueue(args.cls, key=args.key, label=args.label, once=args.once)
            print(f"{what} {job_id if job_id is not None else ''}".strip())
        elif args.cmd == "worker":
            from .worker import SystemdExecutor, Worker
            Worker(queue, SystemdExecutor(), state_dir=args.state_dir).serve(args.poll)
        elif args.cmd == "status":
            status = queue.status()
            print(json.dumps(status, default=str, indent=1) if args.json else js.render(status))
        elif args.cmd == "retry":
            ok = queue.retry(args.id)
            print("queued again" if ok else "not a dead job, or its key is already queued")
            return 0 if ok else 1
        elif args.cmd == "cancel":
            ok = queue.cancel(args.id, "cancelled by hand")
            print("cancelled" if ok else "not a queued job")
            return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
