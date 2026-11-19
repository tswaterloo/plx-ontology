"""What a Teams chat draft reads of the chat besides its one message (2026-11-18 ceiling
test: a Claude session given the chat's earlier messages and the person's own recent
replies wrote 4/27 sendable replies on the fresh set where the drafter, given the one
message alone, wrote 0/27; the drafter said so itself: "the earlier part of the chat,
which I couldn't read").

* ``chat_thread``: the chat's messages before this one (oldest first, at most ``THREAD``),
  the person's own marked as theirs. The chat is the event's own audience
  (``conversation_id`` is ``teams:chat:<id>``, the capture store's unit): nothing a reader
  of the message may not read. Kept on the item as the message is.
* ``reply_examples``: the person's own replies of the ``EXAMPLE_DAYS`` before the message,
  each with the message it answered, from chats the people exclusion clears and the
  thread screen passes (a pair it refuses is left out, never the whole draft). Style only:
  the writer screens them with the prompt but never takes a figure from them and never
  keeps them on an item (``autopilot_drafter.Material.examples``).

Read-only over ``<state>/teams-capture/capture.db`` (the capture's own store, indexed by
unit); anything unreadable gives nothing (the draft is written from the message alone).
Evidence: isolated-fixture (``operating/tests/test_autopilot_chat.py``); live dry run
2026-11-18 on a copy of the live queue."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

THREAD = 12            # earlier messages of the chat
THREAD_CHARS = 600     # of each
EXAMPLES = 30          # the person's own replies
EXAMPLE_DAYS = 30
EXAMPLE_CHARS = (250, 300)   # the message answered, the reply
CACHE_SECONDS = 3600   # the person's replies, read once an hour per person

_SQL_UNIT = ("SELECT payload FROM versions WHERE kind = 'teams_chat' AND unit = ? "
             "AND superseded_at IS NULL")
_SQL_SINCE = ("SELECT unit, payload FROM versions WHERE kind = 'teams_chat' "
              "AND superseded_at IS NULL AND json_extract(payload, '$.row.created_at') >= ? "
              "AND instr(payload, ?) > 0")


def _open(state: Path) -> Optional[sqlite3.Connection]:
    path = Path(state) / "teams-capture" / "capture.db"
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None


def _message(payload: str) -> Optional[dict[str, str]]:
    """A chat message's time, sender id, name and text; None for anything else (a system
    event, a deleted message, an empty one)."""
    try:
        row = (json.loads(payload) or {}).get("row") or {}
    except (TypeError, ValueError):
        return None
    if row.get("kind") != "chat_message" or row.get("deleted_at") or \
            (row.get("message_type") or "message") != "message":
        return None
    text = str(row.get("body_text") or "").strip()
    at = str(row.get("created_at") or "")
    if not text or not at:
        return None
    who = row.get("from")
    if isinstance(who, str) and who.startswith("{"):
        try:
            who = json.loads(who)
        except ValueError:
            who = {}
    who = who.get("user", who) if isinstance(who, Mapping) else {}
    who = who if isinstance(who, Mapping) else {}
    return {"at": at, "sender": str(who.get("id") or "").lower(),
            "name": str(who.get("display_name") or ""), "text": text, "id": str(row.get("id") or "")}


def _chat(conn: sqlite3.Connection, unit: str) -> list[dict[str, str]]:
    out = [m for (p,) in conn.execute(_SQL_UNIT, (unit,)) if (m := _message(p))]
    out.sort(key=lambda m: (m["at"], m["id"]))
    return out


def chat_thread(state: Path, event: Mapping[str, Any], owner: str, reader: str = "") -> list[str]:
    """The chat's messages before the event's (module doc); [] for a channel post, mail,
    or anything unreadable."""
    unit = str(event.get("conversation_id") or "")
    at = str(event.get("at") or "")
    if event.get("channel") != "teams" or not unit.startswith("teams:chat:") or not at:
        return []
    conn = _open(state)
    if conn is None:
        return []
    try:
        msgs = _chat(conn, unit)
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    me = str(owner).lower()
    before = [m for m in msgs if m["at"] < at][-THREAD:]
    def who(m: Mapping[str, str]) -> str:
        if m["sender"] == me:
            return f"{reader or m['name'] or 'them'} (their own)"
        return m["name"] or "someone"
    return [f"[{m['at'][:16].replace('T', ' ')}] {who(m)}: {m['text'][:THREAD_CHARS]}"
            for m in before]


def _instant(text: str) -> Optional[datetime]:
    try:
        got = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def reply_examples(state: Path, event: Mapping[str, Any], owner: str, people: Any,
                   held: Optional[dict[str, Any]] = None) -> list[str]:
    """The person's own recent chat replies with what each answered (module doc), newest
    last; [] when unreadable. ``held``: a dict kept across calls (the cache)."""
    from .autopilot_drafter import thread_screen
    from integrations.image_ocr_gpu import touches_freightcom
    at = _instant(str(event.get("at") or ""))
    me = str(owner).lower()
    if at is None or not me:
        return []
    since = (at - timedelta(days=EXAMPLE_DAYS)).isoformat()
    cache = held.setdefault("reply_examples", {}) if held is not None else {}
    key = (me, since[:13])
    got = cache.get(key)
    if got is None or time.monotonic() - got[0] > CACHE_SECONDS:
        conn = _open(state)
        if conn is None:
            return []
        try:
            units = sorted({u for u, _p in conn.execute(_SQL_SINCE, (since[:10], me))})
            chats = {u: _chat(conn, u) for u in units}
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        pairs: list[tuple[str, str, str, str]] = []
        for unit, msgs in chats.items():
            if not unit.startswith("teams:chat:") or people is None or \
                    not people.unit_clear(unit):
                continue
            for i, m in enumerate(msgs):
                if m["sender"] != me or i == 0 or msgs[i - 1]["sender"] == me:
                    continue
                pairs.append((m["at"], msgs[i - 1]["name"] or "someone",
                              msgs[i - 1]["text"][:EXAMPLE_CHARS[0]], m["text"][:EXAMPLE_CHARS[1]]))
        pairs.sort()
        got = (time.monotonic(), pairs)
        cache[key] = got
    out = []
    cut = at.isoformat()
    for when, name, asked, said in got[1]:
        if not since <= when < cut:
            continue          # never the reply itself or anything after the message
        line = f"{name} wrote: {asked}\n  -> {said}"
        if thread_screen(people, line) or touches_freightcom(line, ""):
            continue
        out.append(line)
    return out[-EXAMPLES:]


def message_text(state: Path, event: Mapping[str, Any]) -> str:
    """A chat message's own text from the capture store, for an event typed before the
    store's text index held it (the fast feed takes a message seconds after its capture:
    11-18 live, 6 of 6 of Vince's chat events reached the writer with no text and were
    answered nothing_useful). Same chat (the event's audience), same message id; "" when
    it is not a chat message or cannot be read."""
    unit = str(event.get("conversation_id") or "")
    if event.get("channel") != "teams" or not unit.startswith("teams:chat:"):
        return ""
    try:
        key = json.loads(str(event.get("object_id") or "").split(":", 2)[2])
        mid = str(key.get("id") or "")
    except (IndexError, ValueError, AttributeError):
        return ""
    if not mid:
        return ""
    conn = _open(state)
    if conn is None:
        return ""
    try:
        return next((m["text"] for m in _chat(conn, unit) if m["id"] == mid), "")
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
