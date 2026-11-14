"""Measure learning on held-out days: replay queues' prepared items, with and without the
lessons learned from the days before.

For each held-out window, the items of every queue given (deduplicated by event and owner)
are split: **train** = items whose trigger is before the window's start and outside every
held-out window (``--any-train-day``: any day outside them, which is not time-forward and
is labelled so); **test** = items in the window. Train items become signals
(``learning_signals.item_signals``: a drafted mail answered in its thread is paired with
the owner's reply read from the store; an item answered is ``item_acted``; one left is
``draft_ignored``) and lessons are distilled at the window's start. Then, on the test items:

* **precision** (thread-reply precision): an item is judged when its trigger plus
  ``--horizon-hours`` is before the queue's last noted send, and matched when its owner
  wrote in the item's thread within the horizon (the queue's ``sent`` table, the record
  ``settle`` reads); without lessons vs with the learned skips (``Lessons.skip_reason``),
  and the matched items the skips removed (coverage lost);
* **edit distance**: drafted mail items the owner answered in the thread, the draft and the
  draft after ``Lessons.apply_draft`` against the reply (quoted thread and signature cut),
  word-level normalized Levenshtein (``learning.normalized_edit_distance``).

Prints counts and rates only (no text, no address, no id). Read-only on the store and the
queues; the people exclusion is read first and fails closed (``learning.Blocked``).

    python3 scripts/learning_replay.py --store STORE --source postgres \\
        --queues DIR [DIR ...] --held 2026-11-06:2026-11-11 [--held ...] [--out FILE]
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operating import learning as L            # noqa: E402
from operating import learning_signals as ls   # noqa: E402


def _day(raw: str) -> datetime:
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def load_queues(paths: Iterable[Path]) -> tuple[dict, dict, dict]:
    """(items by (event, owner), events by id, owner -> sorted [(thread digest, at)])."""
    items: dict[tuple[str, str], dict] = {}
    events: dict[str, dict] = {}
    sent: dict[str, set] = defaultdict(set)
    for path in paths:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute("SELECT * FROM items"):
                key = (row["event_id"], row["owner"])
                if key not in items:
                    item = dict(row)
                    item["body"] = json.loads(item["body"])
                    items[key] = item
            wanted = sorted({e for e, _o in items} - set(events))
            for start in range(0, len(wanted), 500):
                for eid, body in conn.execute(
                        "SELECT event_id, body FROM events WHERE event_id IN (SELECT value "
                        "FROM json_each(?))", (json.dumps(wanted[start:start + 500]),)):
                    events[eid] = json.loads(body)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sent'").fetchone():
                for owner, thread, at in conn.execute("SELECT owner, thread, at FROM sent"):
                    sent[owner].add((thread, at))
        except sqlite3.Error as exc:
            print(json.dumps({"queue_unreadable": type(exc).__name__}), file=sys.stderr)
        finally:
            conn.close()
    return items, events, {o: sorted(v) for o, v in sent.items()}


def trigger(item: Mapping[str, Any], event: Mapping[str, Any]) -> Optional[datetime]:
    return L.instant(event.get("at") or (item.get("body") or {}).get("happened", {}).get("at")
                     or item.get("created_at"))


def judge(item: Mapping[str, Any], event: Mapping[str, Any], sent: Mapping[str, list],
          horizon: timedelta, data_end: datetime) -> Optional[bool]:
    """True matched, False not, None unjudged (horizon past the data)."""
    from operating import autopilot as ap
    at = trigger(item, event)
    conv = event.get("conversation_id") or event.get("message_id")
    if at is None or at + horizon > data_end or not conv:
        return None
    thread = ap._digest("thread", conv)
    lo, hi = L._iso(at), L._iso(at + horizon)
    return any(t == thread and lo < when <= hi for t, when in sent.get(item["owner"], ()))


def reply_text(text: str) -> str:
    """A reply up to its sign-off line (no quoted thread, no signature)."""
    core = L.reply_core(text)
    lines = core.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if L._CLOSE.match(lines[i].strip()):
            return "\n".join(lines[:i + 1]).strip()
    return core


def _rate(a: int, b: int) -> Optional[str]:
    return str((Decimal(a) / b).quantize(Decimal("0.001"))) if b else None


def measure(items: Mapping, events: Mapping, sent: Mapping, *, window: tuple[datetime, datetime],
            held: Sequence[tuple[datetime, datetime]], horizon: timedelta,
            reply_of: Any, roles: Mapping[str, Sequence[str]], any_train_day: bool,
            guard: L.Guard) -> dict[str, Any]:
    start, end = window
    data_end = max((L.instant(at) for v in sent.values() for _t, at in v), default=start)

    def in_held(at: datetime) -> bool:
        return any(a <= at < b for a, b in held)

    train, test = [], []
    for key, item in items.items():
        event = events.get(item["event_id"]) or {}
        at = trigger(item, event)
        if at is None:
            continue
        if start <= at < end:
            test.append(item)
        elif not in_held(at) and not in_held(at + horizon) and (
                any_train_day or at + horizon <= start):
            # judged over [trigger, trigger + horizon]: that whole span is before the
            # window (and outside every held-out window), else its outcome leaks in
            train.append(item)
    # train signals: judged by the same thread record; drafted mail answered: paired
    signals = []
    for item in train:
        event = events.get(item["event_id"]) or {}
        verdict = judge(item, event, sent, horizon, data_end)
        if verdict is None:
            continue
        copy = dict(item, state="done" if verdict else "open", done_reason="owner_replied"
                    if verdict else None, due_at=L._iso(trigger(item, event)),
                    done_at=L._iso(trigger(item, event)))
        signals += ls.item_signals([copy], events, reply_of=reply_of,
                                   now=trigger(item, event) + horizon + timedelta(hours=1),
                                   left_hours=horizon.total_seconds() / 3600)
    signals = [s for s in signals if (L.instant(s.at) or end) < start or any_train_day]
    kept, dropped = L.admit(signals, guard)
    # time-forward: the lessons as known at the window's start; any train day: as known
    # at the last training signal (lessons are never distilled from the future of "now")
    as_of = start if not any_train_day else max(
        (L.instant(s.at) for s in kept), default=start) + timedelta(seconds=1)
    lessons = L.Lessons(L.distil(kept, now=as_of, roles=roles), kept)
    owner_roles = {o: [r for r, ms in roles.items() if o in ms] for o in
                   {i["owner"] for i in test}}

    flows: dict[str, Counter] = defaultdict(Counter)
    dist_before, dist_after, changed = [], [], 0
    frames: dict[str, Counter] = defaultdict(Counter)
    for item in test:
        event = events.get(item["event_id"]) or {}
        verdict = judge(item, event, sent, horizon, data_end)
        flow = item["autopilot"]
        c = flows[flow]
        c["prepared"] += 1
        skipped = lessons.skip_reason(item["owner"], owner_roles[item["owner"]],
                                      item["autopilot"], event)
        c["skipped"] += bool(skipped)
        if verdict is None:
            c["unjudged"] += 1
            continue
        c["judged"] += 1
        c["matched"] += verdict
        if not skipped:
            c["judged_with"] += 1
            c["matched_with"] += verdict
        else:
            c["matched_lost"] += verdict
        work = (item.get("body") or {}).get("prepared") or {}
        draft = (work.get("draft") or {}).get("body")
        if verdict and draft and (event.get("channel") or "mail") == "mail":
            got = reply_of(item, event)
            if got is None:
                continue
            reply = reply_text(got[2])
            if not reply:
                continue
            after = lessons.apply_draft(work, item["owner"], owner_roles[item["owner"]],
                                        event=event, autopilot=item.get("autopilot"),
                                        before=L._iso(trigger(item, event)))
            new = (after.get("draft") or {}).get("body") or draft
            changed += new != draft
            dist_before.append(L.normalized_edit_distance(draft, reply))
            dist_after.append(L.normalized_edit_distance(new, reply))
            # the frame the person wrote vs the frame drafted (greeting, sign-off forms)
            for part, form in (("greeting", L.greeting_of), ("signoff", L.signoff_of)):
                said = form(reply)[0]
                frames[part]["without"] += form(draft)[0] == said
                frames[part]["with"] += form(new)[0] == said
                frames[part]["pairs"] += 1
    total = Counter()
    for c in flows.values():
        total.update(c)
    per_flow = {f: {**dict(c), "precision": _rate(c["matched"], c["judged"]),
                    "precision_with": _rate(c["matched_with"], c["judged_with"])}
                for f, c in sorted(flows.items())}

    def mean(xs: list) -> Optional[str]:
        return str(Decimal(sum(xs) / len(xs)).quantize(Decimal("0.001"))) if xs else None

    applied = [l for scope_who, d in lessons.by.items() for l in d.values()
               if l.confidence >= L.APPLY_AT]
    return {"window": [L._iso(start), L._iso(end)], "time_forward": not any_train_day,
            "lessons_as_of": L._iso(as_of),
            "train_items": len(train), "train_signals": len(kept),
            "train_dropped": dict(dropped),
            "lessons_applied": dict(Counter(f"{l.scope}:{l.kind}" for l in applied)),
            "test_items": len(test),
            "precision": {"judged": total["judged"], "matched": total["matched"],
                          "without": _rate(total["matched"], total["judged"]),
                          "judged_with": total["judged_with"],
                          "matched_with": total["matched_with"],
                          "with": _rate(total["matched_with"], total["judged_with"]),
                          "skipped": total["skipped"], "matched_lost": total["matched_lost"]},
            "edit_distance": {"pairs": len(dist_before), "drafts_changed": changed,
                              "without": mean(dist_before), "with": mean(dist_after),
                              "better": sum(a < b for a, b in zip(dist_after, dist_before)),
                              "worse": sum(a > b for a, b in zip(dist_after, dist_before))},
            "frame_match": {part: {**dict(c), "without_rate": _rate(c["without"], c["pairs"]),
                                   "with_rate": _rate(c["with"], c["pairs"])}
                            for part, c in sorted(frames.items())},
            "by_autopilot": per_flow}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--source", default=None)
    p.add_argument("--queues", nargs="+", required=True, type=Path)
    p.add_argument("--held", action="append", required=True,
                   help="START:END days (END inclusive), e.g. 2026-11-06:2026-11-11")
    p.add_argument("--roles-file", type=Path, default=None)
    p.add_argument("--horizon-hours", type=float, default=72)
    p.add_argument("--any-train-day", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args(argv)
    held = []
    for spec in a.held:
        lo, hi = spec.split(":")
        held.append((_day(lo), _day(hi) + timedelta(days=1)))
    queues = []
    for q in a.queues:
        queues += [q] if q.suffix == ".sqlite" else sorted(q.rglob("autopilot.sqlite"))
    items, events, sent = load_queues(queues)
    roles = {}
    if a.roles_file and a.roles_file.is_file():
        raw = json.loads(a.roles_file.read_text("utf-8"))
        roles = {r: list(ms) for r, ms in (raw.get("roles", raw)).items() if isinstance(ms, list)}

    from operating import autopilot as ap
    from operating import proactive_engine as pe
    people = pe._people(Path(a.store))
    guard = L.Guard.from_env(people)
    if not guard.available:
        print(json.dumps({"status": "blocked", "reason": "people_exclusion_unavailable"}))
        return 3
    st = ap._store(a.store, a.source)
    _d, addresses_of, _dom = ap._people_maps(people)
    days = int(a.horizon_hours // 24) or 1

    def want(item: Mapping[str, Any], event: Mapping[str, Any]) -> tuple:
        return (str(event.get("conversation_id") or ""), str(event.get("at") or ""),
                frozenset(addresses_of.get(item["owner"], set())))

    # every drafted mail item's reply, found in one pass over the conversations
    drafted = [(i, events.get(i["event_id"]) or {}) for i in items.values()
               if ((i.get("body") or {}).get("prepared") or {}).get("draft")]
    found = ls.replies_in_threads(st, [want(i, e) for i, e in drafted], days=days)

    def reply_of(item: Mapping[str, Any], event: Mapping[str, Any]):
        key = want(item, event)
        return found[key] if key in found else ls.reply_in_thread(
            st, key[0], key[1], set(key[2]), days=days)

    out = {"queues": len(queues), "items": len(items), "results": []}
    try:
        for window in held:
            out["results"].append(measure(
                items, events, sent, window=window, held=held,
                horizon=timedelta(hours=a.horizon_hours), reply_of=reply_of, roles=roles,
                any_train_day=a.any_train_day, guard=guard))
    finally:
        st.close()
    text = json.dumps(out, indent=1, sort_keys=True)
    if a.out:
        a.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
