"""Teams items named by their chat and old labels retitled in the queue (split from
``operating.autopilot`` for its size cap; ``autopilot`` re-exports every name)."""

from __future__ import annotations

from collections import Counter
import json
from typing import Any, Iterable, Mapping
from .autopilot_records import _iso, _read, _v
from .autopilot_queue import Queue, SUMMARY_CHARS

def _ap():   # late: autopilot imports this module; its names stay patchable there
    from operating import autopilot
    return autopilot


def _reader_names(*args, **kwargs):
    return _ap()._reader_names(*args, **kwargs)


def _utcnow(*args, **kwargs):
    return _ap()._utcnow(*args, **kwargs)


TEAMS_NAME_BATCH = 500


def name_teams_items(st: Any, queue: Queue, directory: Mapping[str, str],
                     limit: int = TEAMS_NAME_BATCH, skipped: Counter | None = None,
                     ask_rules: Iterable[str] = (), now: str | None = None,
                     judge: Any = None, people: Any = None,
                     addresses_of: Mapping[str, Any] | None = None) -> int:
    """Open Teams message items with no line of their text yet (prepared before the item
    carried who wrote and what, or typed from a store with no text index): each gets the
    sender's name as the stored message states it (``from.display_name``; else their
    address in the directory) and the start of the message's own text (its stored event's,
    else the served store's text index), or the mark that it has none; a reply item's line
    and title are written again from them (``reply_summary``). Once per item (an item with
    a ``snippet`` or a ``snippet_missing`` is done); an item whose message the served store
    does not hold yet waits for a later cycle; at most ``limit`` a cycle. Returns how many
    were named. An item of an autopilot in ``ask_rules`` (its rule needs an ask) prepared
    while the text was not read yet is closed (done, "no_ask", at ``now``) once the read
    text certainly asks nothing (``autopilot_events.chat_ask``) or, with a ``judge``
    (``chat_ask_model.Judge``, its screen read against ``people``), the model reads that it
    asks the item's owner nothing (a failure keeps the item). An unreadable store names none this cycle (the next one tries again); an
    item or event row that does not parse is left as it is and counted in ``skipped``
    (``teams_item_unreadable``), never failing the cycle."""
    from . import autopilot_engine as engine
    from . import proactive_threads as pt
    from .autopilot_events import chat_ask, teams_bodies, teams_sender_name
    from .autopilot_work import reply_summary
    # a body that is not JSON is never read by json_extract (it would fail the query)
    rows = queue.conn.execute(
        "SELECT i.item_id, i.body, e.body, i.autopilot FROM (SELECT item_id, event_id, body, autopilot, "
        "CASE WHEN json_valid(body) THEN json_extract(body, '$.happened') END AS h "
        "FROM items WHERE state = 'open') i JOIN events e USING (event_id) "
        "WHERE json_valid(i.h) AND json_extract(i.h, '$.channel') = 'teams' "
        "AND json_extract(i.h, '$.event_type') = 'message.received' "
        "AND json_extract(i.h, '$.snippet') IS NULL "
        "AND json_extract(i.h, '$.snippet_missing') IS NULL "
        "ORDER BY i.item_id LIMIT ?", (int(limit),)).fetchall()
    if skipped is not None:
        bad = queue.conn.execute("SELECT count(*) FROM items WHERE state = 'open' AND NOT "
                                 "json_valid(body)").fetchone()[0]
        if bad:
            skipped["teams_item_unreadable"] += bad
    if not rows:
        return 0
    items = []
    asking = set(ask_rules)
    rule_of = {item_id: rule for item_id, _b, _e, rule in rows}
    for item_id, body, event, _rule in rows:
        try:
            parsed = (item_id, json.loads(body), json.loads(event or "{}"))
        except (TypeError, ValueError):
            parsed = None
        if parsed is None or not isinstance(parsed[1], dict) or \
                not isinstance(parsed[2], dict) or \
                not isinstance(parsed[1].get("happened"), dict):
            if skipped is not None:
                skipped["teams_item_unreadable"] += 1
            continue
        items.append(parsed)
    oids = {str((b.get("happened") or {}).get("object_id") or "") for _i, b, _e in items}
    # an event typed with no text: the message's body from the served store's text index
    handles = [str((e.get("handles") or [""])[0]) for _i, _b, e in items
               if not str(e.get("text") or "").strip() and e.get("handles")]
    try:
        stored = _read(st, [o for o in oids if o], ("from",))
        bodies = teams_bodies(st, handles) if handles else {}
    except Exception:  # noqa: BLE001 - an unreadable store names nothing this cycle
        return 0
    address_of: dict[str, str] = {}
    for address, person in sorted(directory.items()):
        if "@" in address:
            address_of.setdefault(person, address)
    named = 0
    later: list[tuple[str, dict[str, Any], str]] = []
    with queue.conn:
        for item_id, body, event in items:
            happened = dict(body.get("happened") or {})
            if not str(event.get("text") or "").strip() and \
                    happened.get("object_id") not in stored:
                continue   # the served store does not hold the message yet: a later cycle
            sender = str(happened.get("sender") or "").lower()
            name = teams_sender_name(_v(stored.get(happened.get("object_id")), "from")) or \
                engine.person_name(event.get("sender_name")) or address_of.get(sender, "")
            if name:
                happened["sender_name"] = name
            snippet = engine.message_snippet(event.get("text")) or engine.message_snippet(
                pt._own(bodies.get(str((event.get("handles") or [""])[0])) or ""))
            happened.update({"snippet": snippet} if snippet else {"snippet_missing": "no_text"})
            read = str(event.get("text") or "").strip() or \
                pt._own(bodies.get(str((event.get("handles") or [""])[0])) or "")
            ask_rule = rule_of.get(item_id) in asking
            no_ask = ask_rule and not chat_ask(read, mentioned=bool(event.get("mentioned")))
            if ask_rule and not no_ask and read and judge is not None and \
                    people is not None:
                held = queue.item(item_id)
                if held is not None:
                    later.append((item_id, {**event, "text": read}, str(held["owner"])))
            body["happened"] = happened
            prepared = body.get("prepared")
            title = None
            if isinstance(prepared, dict) and prepared.get("kind") == "reply":
                prepared["summary"] = reply_summary(
                    {**happened, "text": snippet}, len(prepared.get("asked") or ()))
                title = f"{body.get('label') or ''}: " + prepared["summary"][:SUMMARY_CHARS]
            queue.conn.execute(
                "UPDATE items SET body = ?, title = COALESCE(?, title) WHERE item_id = ?",
                (json.dumps(body, sort_keys=True, default=str), title, item_id))
            if no_ask:
                queue.settle_item(item_id, "done", "no_ask", now or _iso(_utcnow()))
                if skipped is not None:
                    skipped["teams_item_no_ask"] += 1
            named += 1
    if later:
        stamp = now or _iso(_utcnow())
        got = judge.ask(queue, [
            (item_id, event, owner, (_reader_names(owner, addresses_of or {}) or [""])[0]
             .capitalize()) for item_id, event, owner in later], people, stamp)
        with queue.conn:
            for item_id, _event, _owner in later:
                if got.get(item_id, True) is False:
                    queue.settle_item(item_id, "done", "no_ask", stamp)
                    if skipped is not None:
                        skipped["teams_item_no_ask"] += 1
    return named


OLD_LABELS = {"teams_reply": ("Someone is waiting on your reply in Teams",),
              "reply_prep": ("Someone is waiting on your reply",)}


RETITLE_BATCH = 500


def retitle_old_labels(queue: Queue, registry: Mapping[str, Any],
                       limit: int = RETITLE_BATCH) -> int:
    """Open items whose title starts with an earlier label of their workflow get the
    registry's label in its place (title and the body's ``label``); the rest of the title
    is kept. Idempotent (a re-titled item no longer matches), only open items, at most
    ``limit`` a cycle, in its own transaction. Returns how many were re-titled."""
    labels = {a["id"]: a.get("label") for a in registry.get("autopilots") or ()}
    done = 0
    with queue.conn:
        for ap_id, olds in OLD_LABELS.items():
            new = labels.get(ap_id)
            if not new or done >= limit:
                continue
            for old in olds:
                rows = queue.conn.execute(
                    "SELECT item_id, title, body FROM items WHERE state = 'open' AND "
                    "autopilot = ? AND substr(title, 1, ?) = ? AND substr(title, ?, 2) = ': ' "
                    "ORDER BY item_id LIMIT ?",
                    (ap_id, len(old), old, len(old) + 1, limit - done)).fetchall()
                for item_id, title, body in rows:
                    try:
                        parsed = json.loads(body)
                    except (TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict):
                        parsed["label"] = new
                        body = json.dumps(parsed, sort_keys=True, default=str)
                    queue.conn.execute("UPDATE items SET title = ?, body = ? WHERE "
                                       "item_id = ?", (new + title[len(old):], body, item_id))
                    done += 1
    return done
