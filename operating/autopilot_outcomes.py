"""What became of each prepared autopilot item: accepted, edited, rejected, ignored or
still waiting (row 22's measure, 11-15).

One class per item, from the queue's item (``operating.autopilot``) and the owner's last
decision on it (the app's decision journal, ``operator_api.autopilot_area``):

- ``accepted``: the owner accepted it with the prepared text unchanged (or it had no text).
- ``edited``: the owner changed the draft's text (an edit, or an accept with new text).
- ``rejected``: the owner rejected it.
- ``ignored``: no decision, and either the owner acted on the matter without it (the queue
  settled it: they replied, decided the approval, closed the task) or it is past due by
  ``IGNORED_AFTER`` and still open.
- ``waiting``: open, not past due by ``IGNORED_AFTER`` (or snoozed to a time ahead).

An unknown decision word or item state fails closed into ``ignored`` with the reason
``unknown`` (counted, never read as an accept). Every item lands in exactly one class, so
the classes sum to the items; the shares are over the items decided (all but waiting).

``python3 -m operating.autopilot_outcomes --state-dir /data/plx-state``: the counts (no
item content), overall and per autopilot, as one JSON line (the log line).

Stdlib only.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Optional

CLASSES = ("accepted", "edited", "rejected", "ignored", "waiting")
DECIDED = ("accepted", "edited", "rejected", "ignored")
IGNORED_AFTER = timedelta(hours=24)
QUEUE_FILE = Path("autopilot") / "autopilot.sqlite"
DECISIONS_FILE = Path("autopilot-write") / "autopilot-writes" / "decisions.jsonl"


def _instant(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _draft_text(item: Mapping[str, Any]) -> Optional[str]:
    body = item.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    prepared = (body or {}).get("prepared") if isinstance(body, Mapping) else None
    draft = (prepared or {}).get("draft") if isinstance(prepared, Mapping) else None
    text = draft.get("body") if isinstance(draft, Mapping) else None
    return text if isinstance(text, str) else None


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def classify(item: Mapping[str, Any], decision: Optional[Mapping[str, Any]],
             now: datetime) -> tuple[str, str]:
    """(class, reason) for one prepared item (module doc)."""
    if decision:
        word = decision.get("decision")
        text = decision.get("text")
        if word == "accept":
            if text and not _same(text, _draft_text(item)):
                return "edited", "accepted_with_changes"
            return "accepted", "accepted_as_prepared"
        if word == "edit":
            return "edited", "edited_not_accepted"
        if word == "reject":
            return "rejected", "rejected"
        if word == "snooze":
            until = _instant(decision.get("until"))
            if until is not None and until > now:
                return "waiting", "snoozed"
        else:
            return "ignored", "unknown"
    state = item.get("state")
    if state == "done":
        return "ignored", "acted_without_it:" + str(item.get("done_reason") or "settled")
    if state != "open":
        return "ignored", "unknown"
    due = _instant(item.get("due_at")) or _instant(item.get("created_at"))
    if due is None:
        return "ignored", "unknown"
    if due + IGNORED_AFTER <= now:
        return "ignored", "no_response"
    return "waiting", "open"


def _share(n: int, of: int) -> Optional[str]:
    if not of:
        return None
    return str((Decimal(n) * 100 / Decimal(of)).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _counts(classes: Counter) -> dict[str, Any]:
    total = sum(classes[c] for c in CLASSES)
    decided = sum(classes[c] for c in DECIDED)
    return {"items": total, "decided": decided, **{c: classes[c] for c in CLASSES},
            "shares": {c: _share(classes[c], decided) for c in DECIDED}}


def measure(items: Iterable[Mapping[str, Any]],
            decisions: Mapping[str, Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Counts only: overall and per autopilot, each class once per item; the reasons of
    ``ignored`` beside them."""
    overall: Counter = Counter()
    per: dict[str, Counter] = {}
    reasons: Counter = Counter()
    for item in items:
        cls, why = classify(item, decisions.get(str(item.get("item_id"))), now)
        overall[cls] += 1
        per.setdefault(str(item.get("autopilot") or ""), Counter())[cls] += 1
        if cls == "ignored":
            reasons[why] += 1
    return {**_counts(overall), "ignored_reasons": dict(sorted(reasons.items())),
            "by_autopilot": {k: _counts(v) for k, v in sorted(per.items())}}


def read_decisions(path: Path) -> dict[str, dict[str, Any]]:
    """The last decision per item from the journal (JSON lines); unreadable lines are
    skipped (a torn last line), a missing file is no decisions."""
    last: dict[str, dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("item_id"):
                    last[str(row["item_id"])] = row
    except FileNotFoundError:
        return {}
    return last


def read_items(queue: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{queue}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT item_id, autopilot, owner, body, due_at, created_at, state, done_reason "
            "FROM items")]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--decisions", type=Path, help="default <state>/" + str(DECISIONS_FILE))
    a = ap.parse_args(argv)
    queue = a.state_dir / QUEUE_FILE
    if not queue.exists():
        print(json.dumps({"status": "no_queue"}))
        return 1
    now = datetime.now(timezone.utc)
    out = measure(read_items(queue), read_decisions(a.decisions or a.state_dir / DECISIONS_FILE),
                  now)
    print(json.dumps({"schema": "plx-autopilot-outcomes/v1",
                      "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **out}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
