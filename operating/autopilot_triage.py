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

* ``earlier_of``: what the model reads beside the message, the conversation just before it
  (a mail's quoted message, a chat's last messages, the reader's own marked): on fresh days
  11-19..20 the triage read without it took answers to the reader's own asks ("99 gallons,
  in WH 11", "Those two are correct!") as owing nothing, while the reader acted on them in
  the thread next (customer service 81 -> 52 sends covered vs the rules alone). It is read as context only: it never adds work the message does not ask (held-out 11-18 with that ask: to-dos right 9 of 20).

Model screens (people exclusion, pay or HR, Freightcom) are ``chat_ask_model.screened``:
a screened message is never sent and keeps the rules' answer; the earlier part passes the
drafts' ``thread_screen`` and the Freightcom check first, or none of it is shown.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .autopilot_queue import Queue, matches
from .autopilot_records import _digest

RECHECK_BATCH = 100       # open reply items read a cycle
# what the model reads beside a message (every
# remaining cut is said in the text, ``autopilot_records.shown``)
EARLIER = 40              # messages before this one the model reads (a whole thread in practice)
EARLIER_CHARS = 12000     # of each
BODY_CHARS = 12000        # of a mail's own words (the preview is 255)
FILES = 30                # attachments read, each by name and its text
FILE_CHARS = 20000
RECORDS = 40              # the states of the orders a message names
# what the model reads beside a message (``earlier_for``), put on it for the call only
READ_WITH = ("earlier", "full_text", "attachments", "files_unread", "files_not_shown",
             "records")
_HEADER = re.compile(r"^\s*(sent|to|cc|bcc|date|subject|importance)\s*:", re.I)


def _reader_names(owner: str, addresses_of: Mapping[str, Any]) -> list[str]:
    from operating import autopilot      # late: autopilot imports this module
    return autopilot._reader_names(owner, addresses_of)


KEEPS_UP = (10, 0.9)      # one-to-one chat messages of a sender known, share answered


def keeps_up(event: Mapping[str, Any]) -> bool:
    """A one-to-one chat whose reader answered at least ``KEEPS_UP`` of the sender's
    messages before (``sender_history``, known at the message's time): the reader keeps up
    the exchange, so the rules' reading stands and no model call can read a turn away
    (held-out 11-18, one such reader: 90 of 108 sends anticipated by the rules, 85-86 with
    every turn read by the model, even told the share)."""
    seen = event.get("sender_history")
    if event.get("channel") != "teams" or event.get("group") or not (
            isinstance(seen, (list, tuple)) and len(seen) == 2 and
            all(isinstance(x, int) and not isinstance(x, bool) for x in seen)):
        return False
    n, answered = seen
    return n >= KEEPS_UP[0] and 0 <= answered <= n and answered >= KEEPS_UP[1] * n


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
            not (event.get("to_me") or event.get("unaddressed")) or keeps_up(event):
        return None
    reader = str(event.get("mailbox") or "")
    message = str(event.get("message_id") or event.get("object_id") or "")
    # only a rule the verdict decides: it takes the message read as owed a reply and not
    # one read as owed nothing with work for the reader, or the other way round (the work
    # a message leaves its reader, ``autopilot_message_work``)
    rules = [a for a in asking if a.get("enabled") is not False and
             a.get("on") == event.get("event_type")
             and matches(a.get("when") or {}, {**event, "asks": True, "triage": "reply",
                                              "triage_work": "work"})
             != matches(a.get("when") or {}, {**event, "asks": False, "triage": "fyi",
                                              "triage_work": "work"})]
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


def earlier_of(st: Any, state: Optional[Path], event: Mapping[str, Any], reader: str,
               reader_names: Sequence[str], addresses_of: Mapping[str, Any], people: Any,
               ctx: Callable[[], Any]) -> list[str]:
    """The conversation just before ``event`` as its reader saw it (module doc), oldest
    first, at most ``EARLIER`` lines of ``EARLIER_CHARS``; [] when there is none, it cannot
    be read, or any of it fails the screens (an excluded person's words, pay or HR,
    Freightcom): the message is then read alone, as before."""
    from .autopilot_drafter import thread_screen
    try:
        if event.get("channel") == "teams":
            if state is None:
                return []
            from .autopilot_chat import chat_thread
            name = reader_names[0].capitalize() if reader_names else ""
            lines = chat_thread(state, event, reader, name, most=EARLIER)
            raw = "\n".join(lines)
        elif event.get("channel") == "mail":
            from .autopilot_work import body_text
            text = str(body_text(ctx(), event) or "")
            if "\nFrom:" not in text:
                return []
            quoted = text.split("\nFrom:", 1)[1].split("\nFrom:", 1)[0]
            raw = "From:" + quoted      # screened whole: its header names who wrote it
            head, _, rest = quoted.partition("\n")
            body = " ".join(ln.strip() for ln in rest.splitlines()
                            if ln.strip() and not _HEADER.match(ln))
            mine = {str(a).lower() for a in addresses_of.get(reader, ())} | {reader.lower()}
            own = any(a and a in head.lower() for a in mine) or any(
                n and len(n) > 2 and n.lower() in head.lower() for n in reader_names)
            who = "the reader (their own)" if own else head.split("<")[0].strip() or "someone"
            lines = [f"{who}: {body}"] if body else []
        else:
            return []
        from .autopilot_records import shown
        lines = [shown(" ".join(str(x).split()), EARLIER_CHARS) for x in lines if str(x).strip()]
        joined = "\n".join(lines)
        if not lines or any(thread_screen(people, t)
                            for t in (raw, joined)):
            return []
        return lines
    except Exception:  # noqa: BLE001 - unreadable: the message is read alone
        return []


def _unmarked(cell: Any) -> bool:
    """A cell that carries no marking (finance, pii, payroll, ...): an absent, empty or
    ``[]`` markings value; anything else, unreadable included, counts as marked."""
    raw = cell.get("markings") if isinstance(cell, Mapping) else None
    if raw in (None, "", [], ()):
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return False
    return isinstance(raw, (list, tuple)) and not raw


class Unmarked:
    """A handler context whose ``read`` keeps only the cells that carry no marking: the
    triage model reads an order's state for any reader of the message, before any item or
    reader check exists, so a finance (or any other) marked cell is never put in its
    prompt (fail closed; the item the reading leads to carries the records' markings and
    passes the reader's own check, ``autopilot_engine``)."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    def read(self, ids: Any, props: Any) -> dict[str, dict[str, Any]]:
        return {oid: {p: cell for p, cell in (row or {}).items() if _unmarked(cell)}
                for oid, row in self._ctx.read(ids, props).items()}


def content_of(event: Mapping[str, Any], people: Any, ctx: Callable[[], Any]
               ) -> dict[str, Any]:
    """What the model reads of a mail beyond its 255-character preview (content audit
    11-22): its own words in full (to ``BODY_CHARS``, the quoted thread cut), its
    attachments by name and the start of their text, and the state of the orders it names
    (an outside sender's own customer's orders only, ``autopilot_handlers.senders_own``),
    each piece screened like the earlier part (excluded people, pay or HR, Freightcom) and
    left out when it fails. ``files_unread``: attachments with no text to read, said so."""
    from .autopilot_drafter import thread_screen
    if event.get("channel") != "mail":
        return {}
    out: dict[str, Any] = {}

    def ok(text: str) -> bool:
        return bool(text.strip()) and not thread_screen(people, text)
    try:
        from . import autopilot_handlers as ah
        from .autopilot_work import _current, _status_lines, attachments, body_text
        c = ctx()
        own = str(body_text(c, event) or "").split("\nFrom:")[0]
        from .autopilot_records import shown
        own = shown(" ".join(own.split()), BODY_CHARS)
        if len(own) > len(str(event.get("text") or "")) and ok(own):
            out["full_text"] = own
        unread: list[dict[str, str]] = []
        found = attachments(c, event, unread=unread)
        files = [f for f in found[:FILES] if ok(f"{f['name']}\n{f['text'][:FILE_CHARS]}")]
        if len(found) > FILES:
            out["files_not_shown"] = len(found) - FILES
        if files:
            out["attachments"] = [f"{f['name']}: {shown(' '.join(f['text'].split()), FILE_CHARS)}"
                                  for f in files]
        if unread:
            out["files_unread"] = len(unread)
        named = ah._records_named(c, {"subject": event.get("subject") or "", "text": own,
                                      "records": event.get("records") or []})
        orders = _current(c, [o for o in named if o.startswith("SalesOrder:")])[:RECORDS]
        mine = ah.senders_own(c, {**event, "text": own}, orders) if orders else []
        # the order state from unmarked cells only (``Unmarked``): no reader door here
        lines = _status_lines(Unmarked(c), mine)[0] if mine else []
        if lines and ok("\n".join(lines)):
            out["records"] = lines
    except Exception:  # noqa: BLE001 - unreadable: the preview alone, as before
        return out
    return out


def earlier_for(st: Any, queue: Queue, people: Any, addresses_of: Mapping[str, Any],
                as_of: Any, domains: Sequence[str]
                ) -> Callable[[Mapping[str, Any], str], dict[str, Any]]:
    """``earlier_of`` for a cycle: the Teams capture of the store's state (a replay's
    ``--capture-state``: ``st.autopilot_state``; live: the queue's state directory)."""
    import time
    from .autopilot_handlers import Ctx
    state = getattr(st, "autopilot_state", None) or queue.path.parent.parent
    state = Path(state) if state else None

    def read(event: Mapping[str, Any], reader: str) -> dict[str, Any]:
        ctx = lambda: Ctx(st, as_of, time.monotonic() + 10, domains)  # noqa: E731
        lines = earlier_of(st, state, event, reader, _reader_names(reader, addresses_of),
                           addresses_of, people, ctx)
        return {**({"earlier": lines} if lines else {}), **content_of(event, people, ctx)}
    return read


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
                summary: dict[str, Any],
                earlier: Optional[Callable[[Mapping[str, Any], str], list[str]]] = None
                ) -> None:
    """The model's verdicts on the waiting events, put on each (``asks``; any failure:
    ``chat_ask_model.kept``: a chat message stays asking, a mail keeps the phrase rules'
    answer); counts in ``summary["chat_ask"]``."""
    from .chat_ask_model import kept as kept_answer
    before = dict(judge.stats)
    shown = 0
    for event, (_key, reader, _name) in waiting:
        got = earlier(event, reader) if earlier is not None and not \
            queue.chat_triage(str(event.get("message_id") or event.get("object_id") or ""),
                              reader) else {}
        if isinstance(got, list):            # an earlier part alone
            got = {"earlier": got} if got else {}
        for k in READ_WITH:
            if got.get(k):
                event[k] = got[k]            # read with the message, never kept on it
        shown += bool(got.get("earlier"))
    try:
        got = judge.ask(queue, [(key, event, reader, name)
                                for event, (key, reader, name) in waiting], people, stamp)
    except Exception:  # noqa: BLE001 - any failure keeps every message (fail open)
        got = {}
        summary["skipped"]["chat_ask_judge_failed"] += len(waiting)
    finally:
        for event, _who in waiting:
            for k in READ_WITH:
                event.pop(k, None)
    if shown:
        summary.setdefault("chat_ask", {})
        summary["chat_ask"]["earlier_shown"] = summary["chat_ask"].get("earlier_shown", 0) \
            + shown
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
                not event.get("cleared") or keeps_up(event):
            continue              # (``keeps_up``: the rules' reading stands, never re-read)
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
