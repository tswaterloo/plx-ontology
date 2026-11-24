"""Check replay (Ask plan A4 rev 2): STAGE 1 ONLY, ``ask_check.check`` on stored Ask answers.
The check needs no database and no reader (one call, no tools, over the stored answer and its
whole stored results), so a stored answer is valid at any time; anything that re-reads is
measured inside a live run, never here.

    python3 evals/ask_check_replay.py OUT.json ROOT [ROOT ...]

ROOT holds ``runs/held/<id>.json`` (the bench's answers, ``run.calls`` = the session's tool
calls, results whole since M2). OUT.json: one entry per answer {root, id, question, status,
seconds, why, claims}. Scoring against the truth set is a reading step (each flag adjudicated),
not done here. CONC (default 4) answers at once. Runs on a Spark, never the box.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

_RID = re.compile(r"sql://[0-9a-f]{20}")
TZ = ZoneInfo("America/New_York")


def _result(raw: Any) -> Any:
    """A stored call's result as the session got it: JSON, or the bench's Python repr of
    it; a repr cut short keeps only its result id (all the checker's task reads of it)."""
    if not isinstance(raw, str):
        return raw
    for read in (json.loads, ast.literal_eval):
        try:
            return json.dumps(read(raw), default=str)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
    rid = _RID.search(raw)
    return json.dumps({"result": rid.group(0)} if rid else {"text": "unreadable"})


def answers(roots: list[str]) -> list[dict[str, Any]]:
    out = []
    for root in roots:
        for path in sorted(Path(root, "runs", "held").glob("*.json")):
            d = json.loads(path.read_text())
            calls = ((d.get("run") or {}).get("calls")) or []
            events = [{"type": "tool_call", "name": c.get("tool"), "arguments": c.get("args"),
                       "is_error": bool(c.get("err")), "result": _result(c.get("result"))}
                      for c in calls]
            # when the answer was written (its reads ran then): the bench's own stamp,
            # else the file's time
            at = d.get("answered_at")
            when = (dt.datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ) if at
                    else dt.datetime.fromtimestamp(path.stat().st_mtime, TZ))
            out.append({"root": root, "id": path.stem, "question": d.get("question") or "",
                        "text": d.get("text") or "", "raw": d.get("agent_text") or d.get("text") or "",
                        "events": events, "written": when})
    return out


def main() -> int:
    from types import SimpleNamespace
    from operator_api import ask_check
    out, roots = sys.argv[1], sys.argv[2:]
    items = answers(roots)
    conc = int(os.environ.get("CONC", "4"))
    asker = SimpleNamespace(claude_bin=os.environ.get("CLAUDE_BIN", "claude"))
    got: list[dict[str, Any]] = []
    lock = threading.Lock()

    def one(item: dict[str, Any]) -> None:
        v = ask_check.check(asker, item["question"], item["raw"], [], item["events"],
                            printed=item["text"], written=item["written"])
        row = {k: item[k] for k in ("root", "id", "question")}
        row.update(status=v.status, seconds=round(v.seconds, 1), why=v.why, claims=v.claims)
        with lock:
            got.append(row)
            Path(out).write_text(json.dumps(got, indent=1))
        print(item["id"], v.status, round(v.seconds, 1), len(v.claims), flush=True)
    with ThreadPoolExecutor(conc) as ex:
        list(ex.map(one, items))
    secs = sorted(r["seconds"] for r in got)
    if secs:
        print(json.dumps({"answers": len(got), "statuses": {s: sum(r["status"] == s for r in got)
                                                             for s in ask_check.CHECK_STATUSES},
                          "p50": statistics.median(secs),
                          "p95": secs[min(len(secs) - 1, int(0.95 * len(secs)))]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
