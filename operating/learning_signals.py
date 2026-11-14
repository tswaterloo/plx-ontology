"""Where the learning signals come from (``operating.learning`` takes them in).

* **The autopilot queue** (``from_queue``): an item with a drafted reply its owner answered
  in the same mail thread (``owner_replied``) is paired with that reply, read from the
  store (the owner's first message in the conversation after the event): a
  ``draft_sent`` signal of its edit features (``learning.edit_features``), the
  references of the item and the reply and the event's topic digests; a mail item its
  owner answered in the thread without a draft is ``replied`` (the same features of their
  reply alone: a few-shot example, ``learning_fewshot``); another item answered
  is ``item_acted``; an item still open ``LEFT_HOURS`` past its due time
  is ``draft_ignored``. A replay's queue reads the same way (``scripts.learning_replay``).
* **The feedback store** (``from_feedback``): an answer's thumbs (``answer_rated``: the
  systems its claims cited, the markings it was served under), its right answer
  (``answer_corrected``) and an alert's verdict (``alert_verdict``: the item's kind).
* **Asks in a row** (``from_asks``): a reader's questions in time order (the memory's log
  of Asks): a question that is "no, it's X" after an
  answer is ``answer_corrected``; one asking the last again in other words within
  ``REASK_MINUTES`` is ``reasked``.

Read-only everywhere: the queue and the feedback store are opened read-only, the store
through its read API. Each source that cannot be read says so in the run's ``sources``
(never a silent zero).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence  # noqa: F401

from . import learning as L

LEFT_HOURS = 72
REASK_MINUTES = 10
REPLY_DAYS = 7


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _draft_body(work: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    for slot, channel in (("draft", "mail"), ("teams_draft", "teams")):
        d = work.get(slot)
        if isinstance(d, Mapping) and isinstance(d.get("body"), str) and d["body"].strip():
            return d["body"], channel
    return None, None


def reply_in_thread(st: Any, conversation: str, after: str, mine: set[str],
                    *, days: int = REPLY_DAYS) -> Optional[tuple[str, str, str]]:
    """(message id, sent at, body text) of the first mail from one of ``mine`` in the
    conversation after ``after`` (within ``days``); None when there is none."""
    from . import autopilot as ap
    from . import proactive_threads as pt
    start = L.instant(after)
    if not conversation or start is None or not mine:
        return None
    end = start + timedelta(days=days)
    try:
        members = pt._members(st, {conversation}, True).get(conversation, [])
    except Exception:  # noqa: BLE001 - no word index (Postgres): matched by value
        members = pt._members(st, {conversation}, False).get(conversation, [])
    rows = ap._read(st, members, ("sender", "sent_at", "body_preview"))
    found = []
    for oid, r in rows.items():
        sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
        at = L.instant(ap._v(r, "sent_at"))
        if sender in mine and at is not None and start < at <= end:
            found.append((at, oid, r))
    if not found:
        return None
    at, oid, r = min(found, key=lambda x: (x[0], x[1]))
    text = _body(st, oid) or ap._v(r, "body_preview")
    return oid, L._iso(at), text


def replies_in_threads(st: Any, wants: Iterable[tuple[str, str, frozenset]],
                       *, days: int = REPLY_DAYS) -> dict[tuple[str, str, frozenset],
                                                         Optional[tuple[str, str, str]]]:
    """``reply_in_thread`` for many (conversation, after, mine) at once: the conversations'
    members found in one pass per ``MEMBER_CHUNK`` (a store without a word index scans
    once, not once per thread)."""
    from . import autopilot as ap
    from . import proactive_threads as pt
    wants = list(dict.fromkeys(wants))
    convs = {c for c, _a, _m in wants if c}
    try:
        members = pt._members(st, convs, True)
    except Exception:  # noqa: BLE001 - no word index (Postgres): matched by value
        members = pt._members(st, convs, False)
    every = sorted({o for c in convs for o in members.get(c, ())})
    rows = {}
    for start in range(0, len(every), 2000):
        rows.update(ap._read(st, every[start:start + 2000], ("sender", "sent_at",
                                                              "body_preview")))
    out: dict = {}
    for conv, after, mine in wants:
        begin = L.instant(after)
        found = []
        for oid in members.get(conv, ()) if begin is not None else ():
            r = rows.get(oid) or {}
            sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
            at = L.instant(ap._v(r, "sent_at"))
            if sender in mine and at is not None and begin < at <= begin + timedelta(days=days):
                found.append((at, oid, r))
        if not found:
            out[(conv, after, mine)] = None
            continue
        at, oid, r = min(found, key=lambda x: (x[0], x[1]))
        out[(conv, after, mine)] = (oid, L._iso(at), _body(st, oid) or ap._v(r, "body_preview"))
    return out


def _body(st: Any, oid: str) -> Optional[str]:
    from .mail_text import joined_chunks
    try:
        row = st.conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ? AND "
                              "superseded_at IS NULL", (oid,)).fetchone()
        if not row:
            return None
        chunks = st.conn.execute(
            "SELECT k.start, k.body FROM text_fields f JOIN text_chunks k ON k.field_id = "
            "f.field_id WHERE f.citation = ? AND f.field_path = 'row.body_text' ORDER BY "
            "k.start", (row[0],)).fetchall()
        return joined_chunks(chunks) or None
    except Exception:  # noqa: BLE001 - no text: the preview stands in
        return None


def item_signals(items: Iterable[Mapping[str, Any]], events: Mapping[str, Mapping[str, Any]],
                 *, reply_of: Callable[[Mapping[str, Any], Mapping[str, Any]],
                                       Optional[tuple[str, str, str]]],
                 now: datetime, left_hours: float = LEFT_HOURS) -> list[L.Signal]:
    """Signals of prepared items (module doc). ``reply_of(item, event)``: the owner's reply
    (id, at, text) in the item's thread, or None."""
    out: list[L.Signal] = []
    for item in items:
        body = item.get("body") or {}
        work = body.get("prepared") or {}
        happened = body.get("happened") or {}
        event = events.get(item.get("event_id")) or {}
        sender = str(event.get("sender") or happened.get("sender") or "")
        base = {"autopilot": item.get("autopilot"),
                "channel": event.get("channel") or happened.get("channel"),
                "side": event.get("side") or happened.get("side"),
                "sender": L.sender_key(sender), "item": item.get("item_id")}
        conv = str(event.get("conversation_id") or "")
        if conv:
            base["thread"] = L.digest("thread", conv)[:16]
        people = (sender.lower(),) if "@" in sender else ()
        texts = (str(event.get("subject") or happened.get("subject") or ""),)
        draft, channel = _draft_body(work)
        reply = None
        if item.get("state") == "done" or item.get("_judge"):
            mail = (channel or event.get("channel") or happened.get("channel") or "mail") == "mail"
            reply = reply_of(item, event) if mail and (channel == "mail" or draft is None
                                                       and event.get("conversation_id")) \
                else None
        if reply is not None and not draft:
            # their own reply to an item that had no draft: a few-shot example of how they
            # answer this kind of mail (``learning_fewshot``), and an item acted on
            rid, at, text = reply
            feats = L.edit_features("", text)
            out.append(L.Signal(
                kind="replied", person=str(item["owner"]), at=at,
                ref=f"item:{item['item_id']}", people=people, texts=texts,
                data={**base, **{k: feats[k] for k in ("greeting", "signoff", "signoff_text",
                                                      "words", "shape")},
                      "reply": rid, "topic": L.topic_keys(
                          " ".join(str(event.get(k) or "") for k in ("subject", "text")))}))
        elif reply is not None:
            rid, at, text = reply
            feats = L.edit_features(draft or "", text)
            out.append(L.Signal(
                kind="draft_sent", person=str(item["owner"]), at=at,
                ref=f"item:{item['item_id']}", people=people, texts=texts,
                data={**base, **feats, "reply": rid, "topic": L.topic_keys(
                    " ".join(str(event.get(k) or "") for k in ("subject", "text")))}))
        elif item.get("state") == "done" and item.get("done_reason") in (
                "owner_replied", "owner_mailed_attendees", "approval_decided",
                "task_closed", "question_answered", "batch_released"):
            out.append(L.Signal(kind="item_acted", person=str(item["owner"]),
                                at=str(item.get("done_at") or item.get("created_at")),
                                ref=f"item:{item['item_id']}", people=people, texts=texts,
                                data=base))
        elif item.get("state") == "open":
            due = L.instant(item.get("due_at"))
            if due is not None and due + timedelta(hours=left_hours) <= now:
                out.append(L.Signal(kind="draft_ignored", person=str(item["owner"]),
                                    at=L._iso(due + timedelta(hours=left_hours)),
                                    ref=f"item:{item['item_id']}", people=people,
                                    texts=texts, data=base))
    return out


def from_queue(queue_path: Path, st: Any, people: Any, *, now: datetime
               ) -> list[L.Signal]:
    from . import autopilot as ap
    _directory, addresses_of, _domains = ap._people_maps(people)
    conn = _ro(queue_path)
    try:
        items = []
        for row in conn.execute("SELECT * FROM items WHERE state IN ('open', 'done')"):
            item = dict(row)
            item["body"] = json.loads(item["body"])
            items.append(item)
        wanted = sorted({i["event_id"] for i in items})
        events = {}
        for start in range(0, len(wanted), 500):
            for eid, body in conn.execute(
                    "SELECT event_id, body FROM events WHERE event_id IN (SELECT value FROM "
                    "json_each(?))", (json.dumps(wanted[start:start + 500]),)):
                events[eid] = json.loads(body)
    finally:
        conn.close()

    def want(item: Mapping[str, Any], event: Mapping[str, Any]) -> tuple:
        return (str(event.get("conversation_id") or ""), str(event.get("at") or ""),
                frozenset(addresses_of.get(item["owner"], set())))

    # the replies of every drafted item answered, in one pass over their conversations
    found = replies_in_threads(st, [
        want(i, events.get(i["event_id"]) or {}) for i in items
        if i.get("state") == "done" and (events.get(i["event_id"]) or {}).get(
            "conversation_id") and _draft_body((i.get("body") or {}).get(
                "prepared") or {})[1] in ("mail", None)])

    def reply_of(item: Mapping[str, Any], event: Mapping[str, Any]
                 ) -> Optional[tuple[str, str, str]]:
        return found.get(want(item, event))

    return item_signals(items, events, reply_of=reply_of, now=now)


def from_feedback(directory: Path) -> list[L.Signal]:
    path = Path(directory) / "feedback.sqlite"
    if not path.is_file():
        return []
    conn = _ro(path)
    out: list[L.Signal] = []
    try:
        for r in conn.execute("SELECT * FROM feedback WHERE kind IN ('answer', 'alert') "
                              "ORDER BY at, id"):
            ctx = json.loads(r["context"] or "{}")
            if ctx.get("reader_hidden"):
                continue   # an excluded person's own feedback: never learned from
            person = L.person_id(r["reader"])
            if r["kind"] == "answer" and r["rating"] in (1, -1):
                systems = sorted({str(c.get("system") or "") for claim in ctx.get("claims") or ()
                                  for c in claim.get("citations") or () if c.get("system")})
                common = {"markings": ctx.get("markings"), "question": r["question"]}
                out.append(L.Signal(kind="answer_rated", person=person, at=r["at"],
                                    ref=f"feedback:{r['id']}",
                                    texts=(r["question"] or "",),
                                    data={"markings": ctx.get("markings"),
                                          "rating": r["rating"], "systems": systems}))
                if r["rating"] == -1 and (r["expected"] or "").strip():
                    out.append(L.Signal(kind="answer_corrected", person=person, at=r["at"],
                                        ref=f"feedback:{r['id']}:expected",
                                        texts=(r["question"] or "", r["expected"]),
                                        data={**common, "value": r["expected"].strip()}))
            elif r["kind"] == "alert" and r["verdict"] in ("useful", "not_useful", "wrong"):
                out.append(L.Signal(kind="alert_verdict", person=person, at=r["at"],
                                    ref=f"feedback:{r['id']}",
                                    data={"verdict": r["verdict"],
                                          "item_kind": ctx.get("event_type") or r["object_type"]
                                          or "unknown"}))
    finally:
        conn.close()
    return out


def from_asks(turns: Iterable[Mapping[str, Any]], *,
              minutes: float = REASK_MINUTES) -> list[L.Signal]:
    """``turns``: a reader's Asks ({reader, at, question, ref, markings, systems}) in any
    order; signals of corrections and re-asks (module doc)."""
    by: dict[str, list[Mapping[str, Any]]] = {}
    for t in turns:
        if L.instant(t.get("at")) is not None and t.get("reader"):
            by.setdefault(L.person_id(t["reader"]), []).append(t)
    out: list[L.Signal] = []
    for person, rows in by.items():
        rows = sorted(rows, key=lambda t: (L.instant(t["at"]), str(t.get("ref"))))
        for prev, cur in zip(rows, rows[1:]):
            gap = (L.instant(cur["at"]) - L.instant(prev["at"])).total_seconds() / 60
            if gap > minutes:
                continue
            value = L.correction_of(cur.get("question"))
            common = {"markings": prev.get("markings"), "question": prev.get("question")}
            if value:
                out.append(L.Signal(kind="answer_corrected", person=person, at=cur["at"],
                                    ref=f"ask:{cur.get('ref')}:correction",
                                    texts=(str(prev.get("question") or ""), value),
                                    data={**common, "value": value}))
            elif L.rephrased(prev.get("question"), cur.get("question")):
                out.append(L.Signal(kind="reasked", person=person, at=cur["at"],
                                    ref=f"ask:{cur.get('ref')}:reask",
                                    texts=(str(prev.get("question") or ""),),
                                    data={"markings": prev.get("markings"),
                                          "systems": list(prev.get("systems") or ())}))
    return out


def collect(state_dir: Path, *, store: Optional[str] = None, source: Optional[str] = None,
            now: datetime) -> tuple[list[L.Signal], dict[str, Any]]:
    """Every source's new signals and what each source gave (or why it gave nothing)."""
    from . import feedback_signals
    signals: list[L.Signal] = []
    sources: dict[str, Any] = {}
    feedback_dir = feedback_signals.signals_path(Path(state_dir))
    try:
        got = from_feedback(feedback_dir.parent) if feedback_dir is not None else []
        sources["feedback"] = len(got)
        signals += got
    except (sqlite3.Error, OSError, ValueError) as exc:
        sources["feedback"] = f"unreadable:{type(exc).__name__}"
    queue = Path(state_dir) / "autopilot" / "autopilot.sqlite"
    if not queue.is_file():
        sources["autopilot"] = "no_queue"
    elif not store:
        sources["autopilot"] = "no_store_given"
    else:
        try:
            from . import autopilot as ap
            from . import proactive_engine as pe
            people = pe._people(Path(store))
            st = ap._store(store, source)
            try:
                got = from_queue(queue, st, people, now=now)
            finally:
                st.close()
            sources["autopilot"] = len(got)
            signals += got
        except Exception as exc:  # noqa: BLE001 - one source's failure is reported
            sources["autopilot"] = f"unreadable:{type(exc).__name__}"
    sources["asks"] = "not_served"   # Read once it serves one
    return signals, sources

