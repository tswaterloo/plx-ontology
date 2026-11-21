"""Every message triaged by the model before a reply item is made for it (split from
``operating.autopilot``; ``autopilot`` re-exports ``_ask_wanted`` and ``_judge_asks``).

* ``_ask_wanted`` / ``_judge_asks``: in a cycle, each message the events mark readable
  (``ask_check``) whose reply item depends on the reading waits for a batched small-model
  call (``chat_ask_model.Judge``): reply, fyi, automated or notice, plus the work it implies.
  Only "reply" leaves a reply item possible (``triage_none`` in the registry); a reply
  reading also stands for the phrase and history guesses (``autopilot_queue.READ_BY_TRIAGE``).
  The other kinds make no reply item, yet the message stays an event every other autopilot
  (system work, findings) reads, and the verdict with its work is kept in the queue
  (``chat_asks``).
* ``recheck_open_replies``: an open reply item made before its message had a reading (an
  older cycle, a model then off or failing) is read once; one read as not owed a reply by the
  person (a closer, an address, an automated notice) is closed, ``triaged_<kind>``. A failed
  or screened reading keeps the item. Measured on the live queue 11-21: 6 of the 10 open
  Teams reply items were read fyi ("No worries", a bare address, "I can imagine, will leave
  you to it").

Model screens (people exclusion, pay or HR, Freightcom) are ``chat_ask_model.screened``:
a screened message is never sent and keeps the rules' answer.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from .autopilot_queue import Queue, matches
from .autopilot_records import _digest

RECHECK_BATCH = 100       # open reply items read a cycle


def _reader_names(owner: str, addresses_of: Mapping[str, Any]) -> list[str]:
    from operating import autopilot      # late: autopilot imports this module
    return autopilot._reader_names(owner, addresses_of)


def _ask_wanted(queue: Queue, event: Mapping[str, Any], asking: Sequence[Mapping[str, Any]],
                *, now: datetime, live: bool, addresses_of: Mapping[str, Any]
                ) -> tuple[str, str, str] | None:
    """``(key, reader, reader's first name)`` when the model must triage ``event`` for its
    reader before it is handled (``chat_ask_model``: reply, fyi, automated or notice): every
    message the events mark readable (``ask_check``: a chat message with words that is not
    certainly nothing, any mail with words, whatever the phrase rules found), cleared, to its
    reader (a mail in To; a chat to them or a group message they may answer),
    that a rule reading the ask or the triage (``asking``) would take or not by the verdict,
    with no item for it yet and (live) not answered already. A verdict already cached is put
    on the event (``asks``, ``triage``, ``triage_work``) and None returned: no call."""
    if event.get("ask_check") != "model" or not event.get("cleared") or \
            not (event.get("to_me") or event.get("unaddressed")):
        return None
    reader = str(event.get("mailbox") or "")
    message = str(event.get("message_id") or event.get("object_id") or "")
    # only a rule the verdict decides: it takes the message read as owed a reply and not
    # one read as owed nothing
    rules = [a for a in asking if a.get("enabled") is not False and
             a.get("on") == event.get("event_type")
             and matches(a.get("when") or {}, {**event, "asks": True, "triage": "reply"})
             and not matches(a.get("when") or {}, {**event, "asks": False, "triage": "fyi"})]
    if not reader or not message or not rules:
        return None
    cached = queue.chat_triage(message, reader)
    if cached is not None:
        _put_triage(event, *cached)
        return None
    if all(queue.item("ap_" + _digest(a["id"], event["event_id"], reader)[:24]) is not None
           for a in rules):
        return None
    names = _reader_names(reader, addresses_of)
    if live and queue.answered_in_chat(reader, event, now, names):
        return None                     # skipped as answered when handled: no call
    return f"{message}|{reader}", reader, (names[0].capitalize() if names else "")


def _put_triage(event: Mapping[str, Any], asks: bool, kind: str = "", work: str = "") -> None:
    """The model's reading on the event: ``asks``, and with a triage reading its ``triage``
    kind (``chat_ask_model.KINDS``; ``triage_none`` reads it) and ``triage_work``."""
    event["asks"] = asks                # type: ignore[index]
    if kind:
        event["triage"] = kind          # type: ignore[index]
        if work:
            event["triage_work"] = work  # type: ignore[index]


def _judge_asks(judge: Any, queue: Queue, waiting: Sequence[tuple[dict[str, Any],
                tuple[str, str, str]]], people: Any, stamp: str,
                summary: dict[str, Any]) -> None:
    """The model's verdicts on the waiting events, put on each (``asks``; any failure:
    ``chat_ask_model.kept``: a chat message stays asking, a mail keeps the phrase rules'
    answer); counts in ``summary["chat_ask"]``."""
    from .chat_ask_model import kept as kept_answer
    before = dict(judge.stats)
    try:
        got = judge.ask(queue, [(key, event, reader, name)
                                for event, (key, reader, name) in waiting], people, stamp)
    except Exception:  # noqa: BLE001 - any failure keeps every message (fail open)
        got = {}
        summary["skipped"]["chat_ask_judge_failed"] += len(waiting)
    triaged = getattr(judge, "triaged", None) or {}
    for event, (key, _reader, _name) in waiting:
        _put_triage(event, got.get(key, kept_answer(event)), *triaged.get(key, ("", "")))
        if not event["asks"]:
            summary["skipped"]["chat_ask_no"] += 1
    counts = summary.setdefault("chat_ask", {})
    for k, v in judge.stats.items():
        counts[k] = round(counts.get(k, 0) + v - before.get(k, 0), 2)


def recheck_open_replies(queue: Queue, registry: Mapping[str, Any], judge: Any, people: Any,
                         addresses_of: Mapping[str, Any], now: str,
                         limit: int = RECHECK_BATCH) -> dict[str, int]:
    """Open items of a reply autopilot (its ``when`` has ``triage_none``) whose message has
    text and no triage reading yet: read in one batch; an item read as a kind its autopilot
    skips is closed (done, ``triaged_<kind>``). Returns counts (read, closed)."""
    skip = {a["id"]: set((a.get("when") or {}).get("triage_none") or ())
            for a in registry.get("autopilots") or ()
            if (a.get("when") or {}).get("triage_none")}
    if judge is None or not skip:
        return {"read": 0, "closed": 0}
    marks = ",".join("?" * len(skip))
    rows = queue.conn.execute(
        f"SELECT i.item_id, i.autopilot, i.owner, e.body, i.body FROM items i JOIN events e "
        f"USING (event_id) WHERE i.state = 'open' AND i.autopilot IN ({marks}) "
        f"ORDER BY i.created_at, i.item_id", sorted(skip)).fetchall()
    entries: list[tuple[str, dict[str, Any], str, str]] = []
    for item_id, _apid, owner, body, item_body in rows:
        if len(entries) >= limit:
            break
        try:
            event = json.loads(body)
            happened = (json.loads(item_body) or {}).get("happened") or {}
        except (ValueError, AttributeError):
            continue
        if not str(event.get("text") or "").strip() and isinstance(happened, Mapping):
            # a chat message whose text the store's index held only later: the line the
            # item quotes (``name_teams_items``)
            event["text"] = str(happened.get("snippet") or "")
        reader = str(event.get("mailbox") or owner or "")
        message = str(event.get("message_id") or event.get("object_id") or "")
        if not message or not reader or not str(event.get("text") or "").strip() or \
                not event.get("cleared"):
            continue
        cached = queue.chat_triage(message, reader)
        if cached is not None and cached[1]:
            continue              # read already: its cycle decided the item
        names = _reader_names(reader, addresses_of)
        entries.append((item_id, event, reader, names[0].capitalize() if names else ""))
    if not entries:
        return {"read": 0, "closed": 0}
    judge.ask(queue, entries, people, now)
    closed = 0
    kinds = {r[0]: r[1] for r in rows}
    with queue.conn:
        for item_id, _event, _reader, _name in entries:
            kind = (judge.triaged.get(item_id) or ("", ""))[0]
            if kind and kind in skip.get(kinds.get(item_id), ()):
                queue.settle_item(item_id, "done", f"triaged_{kind}", now)
                closed += 1
    return {"read": len(entries), "closed": closed}


def readings(queue_path: Any, readers: Sequence[str], records: Sequence[str]
             ) -> dict[str, str]:
    """``{record id: kind}``: the triage reading kept for each message record (its
    ``object_id``) for one of ``readers`` (the person's own mailbox ids), read-only from the
    autopilot queue at ``queue_path``; a record with no reading, or no readable queue, is
    absent (the caller keeps it)."""
    import sqlite3
    from pathlib import Path
    from .chat_ask_model import triage_of
    path = Path(queue_path) if queue_path else None
    wanted = sorted({str(r) for r in records if r})
    who = sorted({str(r) for r in readers if r})
    if path is None or not path.is_file() or not wanted or not who:
        return {}
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            rows = conn.execute(
                f"SELECT message_id, reason FROM chat_asks WHERE message_id IN "
                f"({','.join('?' * len(chunk))}) AND reader IN ({','.join('?' * len(who))})",
                [*chunk, *who]).fetchall()
            for record, reason in rows:
                kind = triage_of(reason)[0]
                if kind:
                    out[str(record)] = kind
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


__all__ = ["RECHECK_BATCH", "readings", "recheck_open_replies"]
