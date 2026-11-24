"""A person's own mail that got no answer, read by the model once it has waited a working day:
when its recipients still owe the person something, a follow-up item (``chase``).

Fresh days 11-19..20 (customer service, purchasing, receivables; labelled by a small model,
never tuned): about one in eight of the sends no item anticipated followed up the person's
own earlier message ("Can I please get an answer ASAP", "Checking back in, do you have...",
"we are not getting any responses"). Nothing prepared them: no message had arrived to answer.

* **What waits** (``note``): each mail a person sends (``discover``'s ``outbound``, with its
  subject and own words) to one to ``MOST_TO`` people the exclusion clears, none of them a
  system address, that is not one of their recurring series mails. Kept in the queue's meta
  (``META``) per person, at most ``KEEP`` and ``KEEP_DAYS`` old.
* **When it is due** (``due_events``): the person's own chase delay (``chase_delay``) of
  working time after it was sent
  (``autopilot_replay.answer_end``), when by then nothing came back in its conversation from
  any of its recipients and the person wrote nothing more there (both read up to the due
  time only, the queue's own ``inbound`` and ``sent`` rows: replay-honest). It becomes a
  ``message.waiting`` event for its sender, at the due time.
* **Whether anything is owed** is the model's reading, the triage's own call
  (``chat_ask_model``, ``"own": true``): "reply" with what is owed in "work" when the
  recipients still owe the person an answer, a document, a confirmation or a date; else
  nothing. No phrase or kind rule decides it. The registry's ``chase`` takes only that.

* **The person's own delay** (``chase_delay``, 11-22: fresh 11-12..13 at a fixed working
  day, the person followed up in the thread within the horizon after 3 of 29 items: people
  do not chase within a day): learned from their own threads in the queue's ``sent`` and
  ``inbound`` rows before the time (replay-honest; the replay seeds 30 days): each own
  message followed by another own message in the same thread to one of the same people,
  with nothing from those people in between, at least ``MIN_GAP`` and at most ``KEEP_DAYS``
  later, is a chase; the delay is the ``QUANTILE`` of their chases' working-time gaps, so
  the item is there before most of them. Under ``HABIT_MIN`` chases in ``HABIT_DAYS``: no
  delay is known and no item is made (they rarely chase, or we cannot tell when).

The item (``h_chase``) names who has not answered, on what, since when, and what is owed;
it carries the person's message and a follow-up draft to the recipients in the thread
(through the copy gate; none when it does not pass). Nothing is sent: the person sends it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .autopilot_records import shown, _digest, _instant, _internal, _iso, object_href

META = "waits:"
HABIT_DAYS = 60
HABIT_MIN = 5                      # own chases seen before a delay is the person's
MIN_GAP = timedelta(hours=2)       # working time; sooner is the same exchange, not a chase
QUANTILE = 0.25
KEEP = 400                         # waiting mails kept per person
KEEP_DAYS = 10
MOST_TO = 6
TEXT_CHARS = 4000   # of a waiting mail kept in the wait list (the cut is said in the text)
SYSTEM = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|"
                    r"alerts?|bounce)", re.I)


def note(queue: Any, outbound: Iterable[Mapping[str, Any]], people: Any,
         addresses_of: Mapping[str, Any], domains: Sequence[str]) -> int:
    """Each own sent mail that could wait on an answer, kept for its sender (module doc)."""
    added = 0
    touched: set[str] = set()     # only an owner whose list grew is written back
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for n in outbound:
        owner = str(n.get("owner") or "")
        text = " ".join(str(n.get("text") or "").split())
        to = sorted({str(a).lower() for a in n.get("to") or () if "@" in str(a)}
                    - {str(a).lower() for a in addresses_of.get(owner, ())} - {owner.lower()})
        if not owner or not text or n.get("series") or not n.get("at") or not \
                n.get("conversation") or not 1 <= len(to) <= MOST_TO or \
                any(SYSTEM.match(a.split("@")[0]) for a in to):
            continue
        try:
            if not all(people.identity_clear(a) for a in to):
                continue
        except Exception:  # noqa: BLE001 - undecidable: never kept (fail closed)
            continue
        rows = by_owner.setdefault(owner, queue.meta(META + owner) or [])
        oid = str(n.get("object_id") or n.get("message_id") or "")
        if not oid or any(r["id"] == oid for r in rows):
            continue
        rows.append({"id": oid, "at": str(n["at"]), "conversation": str(n["conversation"]),
                     "to": to, "outside": any(not _internal(a, domains) for a in to),
                     "subject": str(n.get("subject") or "")[:200], "text": shown(text, TEXT_CHARS)})
        added += 1
        touched.add(owner)
    for owner, rows in by_owner.items():
        if owner not in touched:
            continue
        rows.sort(key=lambda r: r["at"])
        queue.set_meta(META + owner, rows[-KEEP:])
    return added


def due_events(queue: Any, now: datetime, owners: Iterable[str] | None = None
               ) -> list[dict[str, Any]]:
    """The waiting mails due by ``now`` and still unanswered (module doc), each once: a due
    one leaves the wait list whether or not it became an event."""
    from .autopilot_replay import answer_end
    out = []
    keys = [k for (k,) in queue.conn.execute(
        "SELECT key FROM meta WHERE key LIKE ?", (META + "%",))]
    wanted = None if owners is None else {str(o) for o in owners}
    for key in keys:
        owner = key[len(META):]
        if wanted is not None and owner not in wanted:
            continue
        rows = queue.meta(key) or []
        wait = chase_delay(queue, owner, now) if rows else None
        keep = []
        for r in rows:
            sent = _instant(r.get("at"))
            if sent is None or sent < now - timedelta(days=KEEP_DAYS):
                continue
            if wait is None:      # no own habit known yet: kept, never chased on a guess
                keep.append(r)
                continue
            due = answer_end(sent, wait)
            if due > now:
                keep.append(r)
                continue
            thread = _digest("thread", r["conversation"])
            window = (_iso(sent), _iso(due))
            parties = [_digest("party", a) for a in r["to"]]
            answered = queue.conn.execute(
                "SELECT 1 FROM inbound WHERE owner = ? AND thread = ? AND at > ? AND at <= ? "
                "AND counterpart IN (SELECT value FROM json_each(?)) LIMIT 1",
                (owner, thread, *window, json.dumps(parties))).fetchone()
            again = queue.conn.execute(
                "SELECT 1 FROM sent WHERE owner = ? AND thread = ? AND at > ? AND at <= ? "
                "LIMIT 1", (owner, thread, *window)).fetchone()
            if answered or again:
                continue
            out.append(event_of(owner, r, due))
        if len(keep) != len(rows):
            queue.set_meta(key, keep)
    return out


def working_between(a: datetime, b: datetime) -> timedelta:
    """Monday-to-Friday time from ``a`` to ``b`` on the business's calendar (the inverse of
    ``autopilot_replay.answer_end``)."""
    from zoneinfo import ZoneInfo
    from operating.time_window import BUSINESS_TZ
    zone = ZoneInfo(BUSINESS_TZ)
    total, t = timedelta(0), a
    while t < b:
        local = t.astimezone(zone)
        midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(),
                                    tzinfo=zone).astimezone(t.tzinfo or zone)
        end = min(midnight, b)
        if local.weekday() < 5:
            total += end - t
        t = end
    return total


def chase_delay(queue: Any, owner: str, now: datetime) -> timedelta | None:
    """How long ``owner`` waits before chasing their own unanswered message, from their
    threads known at ``now`` (module doc); None under ``HABIT_MIN`` chases."""
    lo, hi = _iso(now - timedelta(days=HABIT_DAYS)), _iso(now)
    sends: dict[str, dict[str, set[str]]] = {}
    for thread, party, at in queue.conn.execute(
            "SELECT thread, counterpart, at FROM sent WHERE owner = ? AND at >= ? AND at < ?",
            (owner, lo, hi)):
        sends.setdefault(thread, {}).setdefault(at, set()).add(party)
    heard: dict[str, list[tuple[str, str]]] = {}
    for thread, party, at in queue.conn.execute(
            "SELECT thread, counterpart, at FROM inbound WHERE owner = ? AND at >= ? AND at < ?",
            (owner, lo, hi)):
        heard.setdefault(thread, []).append((at, party))
    gaps = []
    for thread, by_at in sends.items():
        times = sorted(by_at)
        for first, nxt in zip(times, times[1:]):
            to = by_at[first]
            if not 1 <= len(to) <= MOST_TO or not to & by_at[nxt]:
                continue
            if any(first < at <= nxt and party in to for at, party in heard.get(thread, ())):
                continue
            a, b = _instant(first), _instant(nxt)
            if a is None or b is None:
                continue
            gap = working_between(a, b)
            if MIN_GAP <= gap <= timedelta(days=KEEP_DAYS):
                gaps.append(gap)
    if len(gaps) < HABIT_MIN:
        return None
    gaps.sort()
    return gaps[int(QUANTILE * (len(gaps) - 1))]


def cycle(queue: Any, registry: Mapping[str, Any], outbound: Sequence[Mapping[str, Any]],
          people: Any,
          addresses_of: Mapping[str, Any], domains: Sequence[str], now: datetime,
          only_owners: Iterable[str] | None) -> list[dict[str, Any]]:
    """One cycle's part (``autopilot.run_cycle``): this cycle's own sent mail kept, then the
    ones due now. Only when a ``chase`` autopilot is on; a failure never stops the cycle."""
    spec = next((a for a in registry["autopilots"]
                 if a["on"] == "message.waiting" and a.get("enabled") is not False), None)
    if spec is None or people is None or getattr(people, "unavailable", None):
        return []
    try:
        with queue.conn:
            note(queue, [n for n in outbound if only_owners is None or n.get("owner")
                         in set(only_owners)], people, addresses_of, domains)
            return due_events(queue, now, only_owners)
    except Exception:  # noqa: BLE001 - one source's failure never stops the cycle
        return []


def event_of(owner: str, r: Mapping[str, Any], due: datetime) -> dict[str, Any]:
    """The ``message.waiting`` event of one unanswered mail: the triage reads it as the
    person's own (``own``); its reply goes to the recipients (``sender``: the first)."""
    to = list(r["to"])
    return {"event_id": "ev_" + _digest("waiting", owner, r["id"])[:24],
            "event_type": "message.waiting", "object_id": r["id"], "object_type": "Email",
            "message_id": r["id"], "channel": "mail", "mailbox": owner, "own": True,
            "sender": to[0], "reply_all": {"to": to[1:], "cc": []}, "to": [owner],
            "to_me": True, "cleared": True, "ask_check": "model",
            "side": "unknown" if r.get("outside") else "internal",
            "subject": r.get("subject") or "", "text": r.get("text") or "",
            "conversation_id": r["conversation"], "sent_at": r["at"], "at": _iso(due)}


def h_chase(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """The follow-up the model's reading said is owed (module doc)."""
    from . import autopilot_engine
    from .autopilot_work import draft
    owed = " ".join(str(event.get("triage_work") or "").split())[:120]
    if not owed:
        return {"skip": "nothing_owed"}
    sent = _instant(event.get("sent_at"))
    day = sent.strftime("%A") if sent else "earlier"
    who = autopilot_engine.sender_label(event.get("sender"), None)
    subject = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", str(event.get("subject") or ""),
                     flags=re.I).strip()
    about = f"“{subject[:80]}”" if subject else "your message"
    msg = draft(str(event.get("sender") or ""), str(event.get("subject") or ""),
                [f"Just following up on my note from {day} about {subject or 'this'}.",
                 "Could you let me know where this stands?"], {},
                in_reply_to=event.get("object_id"), today=ctx.as_of,
                others=event.get("reply_all"))
    out: dict[str, Any] = {
        "kind": "reply", "asked": [owed],
        "summary": f"No answer from {who} on {about} since {day}: {owed}",
        "message": {"text": event.get("text") or ""},
        "links": [{"label": "Open your message",
                   "href": object_href(event["object_id"])}],
        "gaps": [] if msg else ["no_draft"]}
    if msg is not None:
        out["draft"] = msg
    return out
