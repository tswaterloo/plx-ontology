"""Slow-call replay (Ask plan 13.2, model-free): every tool call of stored Ask answers that took
``SLOW_MS`` or more is sent again through the persistent host as the cleared reader (one tool
call per host start, ``ReaderHost.call``: the same gates, the same tool code as the asking
session), and timed.

    python3 evals/slow_call_replay.py API_ARGS MINT LINK OUT.json ROOT [ROOT ...]

ROOT holds ``runs/held/<id>.json`` (the bench's answers: ``run.calls`` with ``tool``, ``args``,
``ms``). OUT.json: one entry per call {id, n, tool, was_ms, seconds, baseline_s, outcome,
detail}. ``baseline_s`` is the host start of a trivial call (``plx.sql`` ``select 1``): the
host's start is in every ``seconds`` and is not the tool's. Pass: ``seconds`` <= LIMIT (11) and
no call ends as an empty timeout (outcome ``empty``); a statement the host refuses before it
runs (``refused``, naming the indexed columns) is not a timeout. No model is called.
"""
from __future__ import annotations

import json
import os
import shlex
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SLOW_MS = 20_000
LIMIT = 11.0


def slow_calls(roots: list[str]) -> list[dict[str, Any]]:
    out = []
    for root in roots:
        for path in sorted(Path(root, "runs", "held").glob("*.json")):
            d = json.loads(path.read_text())
            for n, c in enumerate(((d.get("run") or {}).get("calls")) or []):
                if (c.get("ms") or 0) >= SLOW_MS and isinstance(c.get("args"), dict):
                    out.append({"id": path.stem, "n": n, "tool": c.get("tool"),
                                "args": c["args"], "was_ms": c.get("ms")})
    return out


def outcome(tool: str, result: Any) -> tuple[str, str]:
    """(refused | error | empty | ok, a short detail) of one tool result, from its own
    fields: an error code, hits, rows, a named unfinished leg."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return "ok", "text"
    if not isinstance(result, dict):
        return "error", "no result"
    err = result.get("error")
    if isinstance(err, dict):
        code = str(err.get("code"))
        return ("refused" if code in ("narrow_the_read", "busy") else "error"), \
            f"{code}: {str(err.get('message'))[:160]}"
    gaps = [g for g in result.get("gaps") or () if isinstance(g, dict)]
    unfinished = [g.get("subject") for g in gaps if g.get("reason") == "not_finished"]
    if tool == "plx.search":
        hits = len(result.get("documents") or ())
        if not hits and not gaps:
            return "empty", "no hits and no gap saying why"
        return "ok", f"{hits} hits" + (f"; not finished: {', '.join(map(str, unfinished))}"
                                      if unfinished else "")
    if "rows" in result:
        return "ok", f"{len(result['rows'])} rows"
    return "ok", "result"


def main() -> int:
    from scripts import eval_generative as eg
    from scripts import eval_live_ops as lo
    from operating import agent_session
    from operator_api import simple_ask
    # the host client's wait for one answer is for a hung socket, not a read: a slow read is
    # waited for (the Ask's own session has no such wait)
    agent_session.LIST_SECONDS = 3600
    api_args, mint, link, out = sys.argv[1:5]
    calls = slow_calls(sys.argv[5:])
    email = str(json.loads(os.environ.get("PLX_EVAL_READER") or "{}").get("email") or "")
    state = os.environ.get(lo.real_state_env())
    eg.reader_cleared(email, os.environ, Path(state) if state else None)
    got: list[dict[str, Any]] = []

    def work(backend: Any, auth: Any) -> int:
        session = eg.Session(auth, shlex.split(mint), Path(link).expanduser())
        session.login(0)
        reader = session.reader()
        if str(getattr(reader, "email", "")).lower() != email.lower():
            raise SystemExit("signed-in reader is not the cleared reader")
        asker = backend.agent_ask

        def call(name: str, args: dict[str, Any]) -> tuple[float, Any]:
            host = simple_ask.host_for(asker, reader.session)
            t0 = time.monotonic()
            try:
                result = host.call(name, args)
            except Exception as exc:  # noqa: BLE001 - a failed call is a result
                result = {"error": {"code": type(exc).__name__, "message": str(exc)[:200]}}
            finally:
                close = getattr(host, "close", None)
                if close is not None:
                    close()
            return time.monotonic() - t0, result
        base, _ = call("plx.sql", {"sql": "select 1"})
        for c in calls:
            seconds, result = call(str(c["tool"]), c["args"])
            kind, detail = outcome(str(c["tool"]), result)
            got.append({"id": c["id"], "n": c["n"], "tool": c["tool"], "was_ms": c["was_ms"],
                        "seconds": round(seconds, 1), "baseline_s": round(base, 1),
                        "outcome": kind, "detail": detail})
            Path(out).write_text(json.dumps(got, indent=1))
            print(c["id"], c["n"], c["tool"], round(seconds, 1), kind, detail, flush=True)
        return len(calls)
    print(eg.in_process(shlex.split(api_args), work))
    secs = sorted(r["seconds"] for r in got)
    if secs:
        over = [r for r in got if r["seconds"] > LIMIT]
        empty = [r for r in got if r["outcome"] == "empty"]
        print(json.dumps({"calls": len(got), "p50": statistics.median(secs), "max": secs[-1],
                          "over_limit": len(over), "empty": len(empty),
                          "refused": sum(r["outcome"] == "refused" for r in got),
                          "errors": sum(r["outcome"] == "error" for r in got)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
