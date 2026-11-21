"""One switch per category of Assist work, with three modes (Autopilot is a
switch on Assist, and switches are earned).

* ``off``: nothing of the category is prepared for the person.
* ``confirm`` (the mode a switch takes when turned on): every item of it is prepared in
  full (a draft, or the exact system change) and waits in the queue; one click of the
  person's confirm performs it (``autopilot_write.Executor`` with their ``Approval``).
* ``auto``: performed without the click, only for the category's workflows whose replay
  score earned it for the person (``autopilot_write.eligible``: the write gate's bar), and
  still only while the master switch is on and the person has not paused; anything the
  rules hold (money, complex, flagged, a missing fact) stays in the queue for the click.

No new store: a mode is the person's own rows on the write path's audit journal, the ones
the live feed and the executor already read. ``confirm`` = their drafts switch on for each
workflow of the category (``set_drafts``) and their opt-in off; ``auto`` = drafts on and
their opt-in on for each earned workflow (``set_opt_in``, which refuses an unearned one);
``off`` = both off. A category is shown in the strongest mode any of its workflows is in.
Only the person switches their own (``PermissionError`` from the journal helpers
otherwise, the owner included).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import autopilot_write as aw

OFF, CONFIRM, AUTO = "off", "confirm", "auto"
MODES = (OFF, CONFIRM, AUTO)
DEFAULT_ON = CONFIRM


def category(choice: str) -> tuple[str, tuple[str, ...]]:
    """(label, workflows) of one category (``autopilot_write.DRAFT_CHOICES``);
    ``ValueError`` for anything else."""
    for cid, label, flows in aw.DRAFT_CHOICES:
        if cid == choice:
            return label, flows
    raise ValueError("unknown category")


def mode_of(flows: Iterable[str], prepared: Mapping[str, Any],
            opted: Mapping[tuple[str, str], Mapping[str, Any]], me: Iterable[str]) -> str:
    """The mode a category stands in for a person: ``auto`` when they switched any of its
    workflows on to perform by itself, else ``confirm`` when any is prepared for them
    (``prepared``: their ``autopilot_write.prepared_for`` entry), else ``off``."""
    flows, me = tuple(flows), tuple(str(p).lower() for p in me)
    if any((opted.get((p, f)) or {}).get("on") for p in me for f in flows):
        return AUTO
    if any(f in prepared for f in flows):
        return CONFIRM
    return OFF


def earned(gate: Optional[aw.WriteGate], me: Iterable[str], flows: Iterable[str]
           ) -> list[str]:
    """The category's workflows whose replay score earned ``auto`` for the person."""
    if gate is None:
        return []
    me = tuple(str(p).lower() for p in me)
    return [f for f in flows if any(aw.eligible(gate, p, f) for p in me)]


def score(gate: Optional[aw.WriteGate], me: Iterable[str], flows: Iterable[str]
          ) -> Optional[tuple[int, int]]:
    """(judged, matched) of the category on replay for the person: per workflow the best
    scored role of theirs, summed over the workflows; None when nothing is scored."""
    if gate is None:
        return None
    me = {str(p).lower() for p in me}
    judged = matched = 0
    seen = False
    for flow in flows:
        best: Optional[tuple[int, int]] = None
        for role, (members, _ok, _seen) in gate.roles.items():
            counts = gate.counts.get((role, flow))
            if not counts or not (members & me) or counts[0] <= 0:
                continue
            if best is None or counts[1] * best[0] > best[1] * counts[0]:
                best = (int(counts[0]), int(counts[1]))
        if best is not None:
            seen = True
            judged, matched = judged + best[0], matched + best[1]
    return (judged, matched) if seen else None


def apply(journal: Any, gate: aw.WriteGate, ids: Sequence[str], flows: Sequence[str],
          mode: str, now: datetime, *,
          opted: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[str]:
    """Sets the person's own category to ``mode`` (each of ``ids`` is the person; the
    journal helpers refuse anyone else). ``auto`` turns the drafts on for every workflow
    and performs-by-itself only for the earned ones (``ValueError("not_eligible")`` when
    none is); ``confirm`` turns the drafts on and every opt-in of the category off;
    ``off`` turns both off. Returns the workflows now performed by themselves."""
    if mode not in MODES:
        raise ValueError("unknown mode")
    ids = [str(i).lower() for i in ids]
    auto = earned(gate, ids, flows) if mode == AUTO else []
    if mode == AUTO and not auto:
        raise ValueError("not_eligible")
    for oid in ids:
        for flow in flows:
            aw.set_drafts(journal, oid, flow, mode != OFF, oid, now)
            on_now = bool((opted.get((oid, flow)) or {}).get("on"))
            want = flow in auto and bool(aw.eligible(gate, oid, flow))
            if want and not on_now:
                aw.set_opt_in(journal, gate, oid, flow, True, oid, now)
            elif on_now and not want:
                aw.set_opt_in(journal, gate, oid, flow, False, oid, now)
    return auto
