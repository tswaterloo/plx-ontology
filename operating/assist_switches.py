"""Conditions on a person's Auto switch (Autopilot is a switch on Assist,
per person, per category, per counterparty and per time window, each with its own bar).

A category in ``auto`` (``operating.assist_modes``) performs an item by itself only when
every condition the person set on that category holds for it; otherwise the item stays in
the queue for their click, exactly as an item the rules hold (money, complex, flagged, a
missing fact: ``autopilot_write.routine``). Conditions only ever narrow Auto: none set
means the category's earned workflows as before, and nothing here can widen what the
replay gate, the rules or the kill switches allow.

* ``senders``: only items from these senders (an address, or ``@domain``); empty: any.
* ``days`` / ``start`` / ``end``: only on these weekdays (0 = Monday) and between these
  times, US Eastern (``America/Toronto``); none set: any time.
* ``min_percent``: only workflows whose replay score for the person is at least this
  (never below the gate's own bar, which already applies); none: the gate's bar.

Stored as the person's own rows on one journal beside the write path's audit
(``FILE``); the last row per (person, category) wins. Only the person writes their own.
A row that cannot be read, or a store that cannot be opened, holds Auto back for that
person (fail closed): ``reasons`` names it and the item waits for the click.
Evidence: isolated-fixture (``operating/tests/test_assist_switches.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

FILE = "autopilot-writes/switch-conditions.jsonl"
TZ = ZoneInfo("America/Toronto")
MOST_SENDERS = 50
_ADDRESS = re.compile(r"^(?:[^@\s]+@[^@\s]+\.[^@\s]+|@[^@\s]+\.[^@\s]+)$")
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
UNREADABLE = "switch_conditions_unreadable"


@dataclass(frozen=True)
class Conditions:
    senders: tuple[str, ...] = ()
    days: tuple[int, ...] = ()
    start: Optional[str] = None
    end: Optional[str] = None
    min_percent: Optional[Decimal] = None
    at: Optional[str] = field(default=None, compare=False)

    def empty(self) -> bool:
        return not (self.senders or self.days or self.start or self.end
                    or self.min_percent is not None)

    def as_dict(self) -> dict[str, Any]:
        return {"senders": list(self.senders), "days": list(self.days), "start": self.start,
                "end": self.end,
                "min_percent": float(self.min_percent) if self.min_percent is not None
                else None}


def parse(raw: Mapping[str, Any]) -> Conditions:
    """Checked conditions from a request or a stored row; ``ValueError`` (with a plain
    reason) for anything malformed, never a partly read row."""
    senders: list[str] = []
    for s in raw.get("senders") or ():
        s = str(s).strip().lower()
        if not _ADDRESS.match(s):
            raise ValueError(f"Not an address or @domain: {s[:80]}")
        if s not in senders:
            senders.append(s)
    if len(senders) > MOST_SENDERS:
        raise ValueError(f"At most {MOST_SENDERS} senders.")
    days = sorted({int(d) for d in raw.get("days") or ()})
    if any(d < 0 or d > 6 for d in days):
        raise ValueError("Days are 0 (Monday) to 6 (Sunday).")
    start, end = raw.get("start") or None, raw.get("end") or None
    if (start is None) != (end is None):
        raise ValueError("A time window needs both a start and an end.")
    for t in (start, end):
        if t is not None and not _CLOCK.match(str(t)):
            raise ValueError("Times are HH:MM, 24-hour.")
    if start is not None and str(start) >= str(end):
        raise ValueError("The window must end after it starts, on the same day.")
    pct = raw.get("min_percent")
    if pct is not None:
        try:
            pct = Decimal(str(pct))
        except InvalidOperation:
            raise ValueError("The bar is a percentage.") from None
        if not pct.is_finite() or pct < 0 or pct > 100:
            raise ValueError("The bar is a percentage from 0 to 100.")
    return Conditions(tuple(senders), tuple(days), start and str(start), end and str(end),
                      pct, raw.get("at") if isinstance(raw.get("at"), str) else None)


def _journal(state: Path) -> Any:
    from .autopilot_write import Journal
    return Journal(Path(state) / FILE)


def read(state: Optional[Path], me: Iterable[str]) -> dict[str, Conditions | None]:
    """category -> the person's own conditions (their last row per category); ``None`` for
    a category whose last row cannot be read (Auto held back for it). Raises ``OSError``
    when the store cannot be read: the caller holds Auto back."""
    me = {str(p).lower() for p in me}
    if state is None or not (Path(state) / FILE).exists():
        return {}
    last: dict[str, Conditions | None] = {}
    for row in _journal(state).rows():
        if str(row.get("by") or "").lower() not in me or not isinstance(row.get("choice"), str):
            continue
        try:
            last[row["choice"]] = parse(row)
        except (ValueError, TypeError):
            last[row["choice"]] = None
    return last


def write(state: Path, ids: Sequence[str], choice: str, cond: Conditions,
          now: datetime) -> None:
    """The person's own conditions for one category (one row per id they hold)."""
    (Path(state) / FILE).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _journal(state).locked() as held:
        for oid in ids:
            held.append({"event": "switch_conditions", "choice": choice,
                         "by": str(oid).lower(), "at": now.isoformat(), **cond.as_dict()})


def choice_of(workflow: str) -> Optional[str]:
    from .autopilot_write import DRAFT_CHOICES
    return next((cid for cid, _label, flows in DRAFT_CHOICES if workflow in flows), None)


def sender_of(item: Mapping[str, Any]) -> str:
    happened = (item.get("body") or {}).get("happened") or {}
    return str(happened.get("sender") or "").strip().lower()


def sender_matches(sender: str, senders: Sequence[str]) -> bool:
    sender = sender.strip().lower()
    if not sender:
        return False
    domain = "@" + sender.rsplit("@", 1)[1] if "@" in sender else None
    return any(s == sender or (s.startswith("@") and s == domain) for s in senders)


def holds(cond: Conditions | None, item: Mapping[str, Any], now: datetime,
          percent: Optional[Decimal]) -> list[str]:
    """Why Auto waits for the click on this item under these conditions ([]: it may go).
    ``percent``: the workflow's replay score for the person (None: not scored)."""
    if cond is None:
        return [UNREADABLE]
    why: list[str] = []
    if cond.senders and not sender_matches(sender_of(item), cond.senders):
        why.append("switch:sender_not_chosen")
    local = now.astimezone(TZ)
    if cond.days and local.weekday() not in cond.days:
        why.append("switch:outside_days")
    if cond.start and cond.end:
        h1, m1 = map(int, cond.start.split(":"))
        h2, m2 = map(int, cond.end.split(":"))
        if not time(h1, m1) <= local.time() < time(h2, m2):
            why.append("switch:outside_hours")
    if cond.min_percent is not None and (percent is None or percent < cond.min_percent):
        why.append("switch:below_your_bar")
    return why


def workflow_percent(gate: Any, me: Iterable[str], workflow: str) -> Optional[Decimal]:
    """The judged score of the workflow's category for the person in percent (the number
    beside their switch, ``assist_modes.score``); None when nothing is judged."""
    from .assist_modes import score
    found = score(gate, me, (workflow,))
    if found is None or found.judged <= 0:
        return None
    return Decimal(found.matched) * 100 / Decimal(found.judged)


def reasons(state: Optional[Path], me: Iterable[str], item: Mapping[str, Any],
            workflow: str, now: datetime, gate: Any) -> list[str]:
    """Why the person's switch conditions hold this item back from Auto ([]: none do).
    Fail closed: an unreadable store or row holds it back."""
    me = [str(p).lower() for p in me]
    choice = choice_of(workflow)
    if choice is None:
        return []
    from .card_actions import JournalBroken
    try:
        found = read(state, me)
    except (OSError, ValueError, JournalBroken):
        return [UNREADABLE]
    if choice not in found:
        return []
    cond = found[choice]
    if cond is not None and cond.empty():
        return []
    pct = workflow_percent(gate, me, workflow) if cond is not None and \
        cond.min_percent is not None else None
    return holds(cond, item, now, pct)
