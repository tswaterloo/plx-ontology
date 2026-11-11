"""
USAGE
    python3 -m operating.usage_review --state-dir DIR [--since 24h|7d|ISO]
        [--latency-log FILE] [--slow-ms 3000] [--json]

Reads three stores and changes none (each opened ``mode=ro``):

- the work log (``operator_api.work_log``; ``PLX_WORK_LOG_DIR`` or ``<state>/work-log``):
  who did what, and every Ask answer as its reader saw it (question, answer, sources,
  the steps it took, its time, its outcome and gaps) or the error that ended it;
- the feedback store (``operator_api.feedback``; ``PLX_FEEDBACK_DIR`` or
  ``<state>/feedback``): thumbs on an answer (linked by ``answer_id``) and "Give feedback"
  notes (linked to the screen, ``page``, or the record they were given on);
- the API's latency log (``serve --latency-log``), when given: slow pages and server
  errors per route.

The text digest is compact: who used what, answers that are not plainly answered or cite
nothing, errors, slow pages and new feedback. ``--json`` prints every answer in full, for
grading.

The people exclusion is fail closed: unloaded, nothing is printed (exit 3); a row whose
person (or a person it names) is excluded is left out; text is cleaned as the feedback
store cleans it. Pay, personal financial and HR text (``chunking.sensitive``
``personal_financial``, the text index's rule) is withheld from every printed text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

WITHHELD_PAY_HR = "[pay/HR text withheld]"
LOW_OUTCOMES = frozenset({"partial", "not_found", "abstained", "refused"})


def since_of(text: str, now: datetime) -> datetime:
    """``24h``, ``7d``, ``90m`` or an ISO instant (ValueError otherwise)."""
    found = re.fullmatch(r"\s*(\d{1,5})\s*([mhd])\s*", text or "")
    if found:
        n, unit = int(found.group(1)), found.group(2)
        return now - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class Text:
    """Every printed text: cleaned of excluded people and secrets, pay/HR withheld."""

    def __init__(self, guard: Any) -> None:
        from operator_api import feedback as fb
        self.guard, self._clean = guard, fb.clean

    def __call__(self, value: Any, limit: int = 600) -> Optional[str]:
        out = self._clean(value, self.guard, 100_000) if isinstance(value, str) else None
        if out is None:
            return None
        from .chunking.sensitive import personal_financial
        try:
            if personal_financial(out) is not None:
                return WITHHELD_PAY_HR
        except Exception:  # noqa: BLE001 - undecidable: withheld
            return WITHHELD_PAY_HR
        return out if len(out) <= limit else out[:limit - 1] + "…"


def _answers(conn: sqlite3.Connection, since: str, guard: Any, text: Text,
             ratings: Mapping[str, list[dict[str, Any]]]) -> tuple[list[dict], Counter]:
    from operator_api import work_log as wl
    used: Counter = Counter()
    answers: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM event WHERE at >= ? ORDER BY seq", (since,)):
        if wl._row_excluded(guard, row):
            continue
        who = text(row["reader_name"], 120) or "someone"
        used[(who, row["kind"])] += 1
        if row["kind"] != "ask":
            continue
        try:
            data = json.loads(row["detail"] or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        review = data.get("_review") or {}
        sources = [{"system": s.get("system"), "label": text(s.get("label"), 200),
                    "handle": s.get("handle")}
                   for f in data.get("facts") or () for s in f.get("sources") or ()]
        sources = [s for s in sources
                   if s["handle"] is None or _clean_handle(s["handle"], guard)]
        answer_id = row["source_id"]
        answers.append({
            "at": row["at"], "who": who, "question": text(data.get("question")
                                                           or row["title"], 400),
            "outcome": data.get("outcome") or ("error" if row["verb"] == "failed" else None),
            "error": review.get("error"), "answer": text(data.get("direct_answer"), 4000),
            "sources": sources, "not_found": [text(n.get("what"), 200)
                                              for n in data.get("not_found") or ()],
            "hidden": (data.get("hidden") or {}).get("count", 0),
            "steps": [s.get("step") for s in review.get("steps") or ()],
            "latency_ms": data.get("latency_ms"), "stream_ms": review.get("stream_ms"),
            "answer_id": answer_id, "conversation": row["thread"].partition(":")[2],
            "feedback": ratings.get(answer_id or "", [])})
    return answers, used


def _clean_handle(handle: str, guard: Any) -> bool:
    from operator_api import feedback as fb
    return fb.clean_handle(handle, guard) is not None


def _feedback(conn: Optional[sqlite3.Connection], since: str, guard: Any, text: Text
              ) -> list[dict[str, Any]]:
    if conn is None:
        return []
    from operator_api import feedback as fb
    out = []
    for row in conn.execute("SELECT * FROM feedback WHERE at >= ? ORDER BY at", (since,)):
        if any(fb._excluded_id(guard, o.lower()) for o in fb._UUID.findall(row["reader"])):
            continue
        out.append({"at": row["at"], "kind": row["kind"],
                    "who": text(row["reader_name"], 120) or "someone",
                    "rating": row["rating"], "verdict": row["verdict"],
                    "note": text(row["note"], 600), "expected": text(row["expected"], 600),
                    "page": text(row["page"], 300), "object_type": row["object_type"],
                    "object_id": text(row["object_id"], 200), "label": text(row["label"], 300),
                    "answer_id": row["answer_id"], "item_id": row["item_id"],
                    "question": text(row["question"], 400), "status": row["status"]})
    return out


def _latency(path: Optional[Path], since: datetime, slow_ms: float) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"read": False, "slow": [], "errors": []}
    slow: dict[str, list[float]] = defaultdict(list)
    errors: Counter = Counter()
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                continue                   # an older line without its time: not windowed
            if at < since:
                continue
            route = str(row.get("route") or "unmatched")
            if int(row.get("status") or 0) >= 500:
                errors[route] += 1
            if row.get("method") == "GET" and float(row.get("ms") or 0) >= slow_ms:
                slow[route].append(float(row["ms"]))
    return {"read": True,
            "slow": sorted(({"route": r, "count": len(v), "max_ms": max(v)}
                            for r, v in slow.items()), key=lambda s: -s["count"]),
            "errors": [{"route": r, "count": n} for r, n in errors.most_common()]}


def review(state_dir: Optional[Path], *, since: datetime, latency_log: Optional[Path] = None,
           slow_ms: float = 3000, guard: Any = None) -> dict[str, Any]:
    """Everything the digest prints; ``{"withheld": reason}`` while the people exclusion
    cannot decide."""
    if guard is None:
        from operator_api import feedback as fb
        guard = fb.exclusion()
    if guard is None or getattr(guard, "unavailable", "absent"):
        return {"withheld": "people exclusion not loaded"}
    text = Text(guard)
    wl_dir = os.environ.get("PLX_WORK_LOG_DIR") or (state_dir / "work-log" if state_dir
                                                    else None)
    fb_dir = os.environ.get("PLX_FEEDBACK_DIR") or (state_dir / "feedback" if state_dir
                                                    else None)
    work = _ro(Path(wl_dir) / "work-log.sqlite") if wl_dir else None
    notes = _ro(Path(fb_dir) / "feedback.sqlite") if fb_dir else None
    start = _iso(since)
    try:
        feedback = _feedback(notes, start, guard, text)
        ratings: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in feedback:
            if f["answer_id"]:
                ratings[f["answer_id"]].append({k: f[k] for k in (
                    "rating", "verdict", "note", "expected")})
        answers, used = _answers(work, start, guard, text, ratings) if work else ([], Counter())
    finally:
        for conn in (work, notes):
            if conn is not None:
                conn.close()
    people: dict[str, dict[str, int]] = defaultdict(dict)
    for (who, kind), n in sorted(used.items()):
        people[who][kind] = n
    flagged = [a for a in answers if a["outcome"] in LOW_OUTCOMES or a["outcome"] == "error"
               or (a["outcome"] == "answered" and not a["sources"])
               or (a["latency_ms"] or 0) >= slow_ms
               or any((r.get("rating") or 0) < 0 or r.get("verdict") in ("wrong", "not_useful")
                      for r in a["feedback"])]
    return {"since": start, "work_log": work is not None, "people": dict(people),
            "answers": answers, "flagged": flagged,
            "feedback": [f for f in feedback if f["kind"] != "answer" or f["note"]],
            "latency": _latency(latency_log, since, slow_ms)}


def _why(a: Mapping[str, Any], slow_ms: float) -> str:
    why = []
    if a["outcome"] == "error":
        why.append(f"error {a['error']}")
    elif a["outcome"] in LOW_OUTCOMES:
        why.append(a["outcome"].replace("_", " "))
    if a["outcome"] == "answered" and not a["sources"]:
        why.append("no citations")
    if (a["latency_ms"] or 0) >= slow_ms:
        why.append(f"{a['latency_ms'] / 1000:.1f}s")
    if any((r.get("rating") or 0) < 0 for r in a["feedback"]):
        why.append("thumbs down")
    return ", ".join(why) or "flagged"


def digest(found: Mapping[str, Any], slow_ms: float = 3000) -> Iterator[str]:
    if "withheld" in found:
        yield f"Usage review withheld: {found['withheld']}."
        return
    yield f"Workbench use since {found['since']}"
    if not found["work_log"]:
        yield "  (no work log on this server yet)"
    yield f"People ({len(found['people'])}):"
    for who, kinds in found["people"].items():
        yield "  " + who + ": " + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
    answers = found["answers"]
    up = sum(1 for a in answers for r in a["feedback"] if (r.get("rating") or 0) > 0)
    down = sum(1 for a in answers for r in a["feedback"] if (r.get("rating") or 0) < 0)
    yield (f"Ask: {len(answers)} questions, {len(found['flagged'])} flagged, "
           f"{up} thumbs up, {down} thumbs down")
    for a in found["flagged"][:25]:
        yield f"  {a['at'][:16]} {a['who']}: {a['question']}  [{_why(a, slow_ms)}]"
    if len(found["flagged"]) > 25:
        yield f"  ... {len(found['flagged']) - 25} more (--json)"
    lat = found["latency"]
    if lat["read"]:
        yield f"Slow pages (>= {slow_ms / 1000:.1f}s): " + (", ".join(
            f"{s['route']} x{s['count']} (max {s['max_ms'] / 1000:.1f}s)"
            for s in lat["slow"][:10]) or "none")
        yield "Server errors: " + (", ".join(f"{e['route']} x{e['count']}"
                                            for e in lat["errors"][:10]) or "none")
    else:
        yield "Slow pages and server errors: no latency log given (--latency-log)"
    yield f"Feedback ({len(found['feedback'])}):"
    for f in found["feedback"][:25]:
        about = f["question"] or f["label"] or f["page"] or f["object_id"] or f["kind"]
        mark = f["verdict"] or ({1: "up", -1: "down"}.get(f["rating"]) if f["rating"] else "")
        yield f"  {f['at'][:16]} {f['who']} on {about}: {mark + ' ' if mark else ''}" \
              f"{f['note'] or ''}".rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.usage_review")
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--since", default="24h")
    p.add_argument("--latency-log", type=Path, default=None)
    p.add_argument("--slow-ms", type=float, default=3000)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.state_dir is None and not (os.environ.get("PLX_WORK_LOG_DIR")
                                    or os.environ.get("PLX_FEEDBACK_DIR")):
        p.error("--state-dir is required")
    try:
        since = since_of(a.since, datetime.now(timezone.utc))
    except ValueError:
        p.error("--since: 24h, 7d, 90m or an ISO time")
    found = review(a.state_dir, since=since, latency_log=a.latency_log, slow_ms=a.slow_ms)
    if a.json:
        print(json.dumps(found, ensure_ascii=False, indent=1, default=str))
    else:
        print("\n".join(digest(found, a.slow_ms)))
    return 3 if "withheld" in found else 0


if __name__ == "__main__":
    sys.exit(main())
