"""Label and artwork change control, as alerts and an item page section, run by
``operating.proactive_engine`` beside its other rules (``label_changes``).

A change request: a mail or Teams message whose own text (quoted history removed; the
subject only on a message that is no reply or forward) asks for a change to a label, an
artwork, a dieline or a proof (``change_request``: a sentence naming the artefact, a change
word and a request form, not negated, not a question about whether to change). Its life is
read from the later messages of its conversation (mail: the conversation id; Teams: the
chat) and the SharePoint files of its item:

* ``approved``: a later message approves it (the requester too: a customer approves the proof)
  (``approval``: "approved", "looks good, go ahead", "OK to print"; negated or asked, no);
* ``done``: a later message says the change is made (``done``: "updated artwork
  attached", "revised label uploaded", "sent to print"), or a label or artwork file of the
  named item (a SharePoint file whose name holds the artefact's word and that mentions the
  item) changed after the request.

Each request is linked to its item (the message's settled ``mentions_product`` link) or, with
none, to its customer (``Email.sent_by_customer``, a ``mentions_customer`` link); with
neither it stays on the message. Kinds (one per request):

* ``label_change_unapproved`` (high): done with no approval recorded in its thread;
* ``label_change_stale`` (medium): requested ``STALE_BUSINESS_DAYS`` business days ago or
  more and not done;
* ``label_change_tracked`` (low; the item page section only, never the briefing): any
  other request of the window, with its state.

The item page section (``item_section``) lists the reader's own trimmed alerts of these
kinds on that item: requested, approved, done, stale, no approval.

Found through the store's word index (``value_text``): the messages whose subject or text
hold the artefact words (``WORDS``), then their sent time (``WINDOW_DAYS``), then every
message of their conversations. A store without the word index is read whole.

People first, fail closed (``people``): with the guard unavailable or no tenant domain known
the rule is ``blocked``; a copy in a mailbox or chat the exclusion does not clear is dropped
unread; a thread with a person of ours not cleared is not judged. An alert rests on what the
thread does not hold (no approval, nothing done), so it requires a grant on every audience
of its thread (``requires``); it stores message ids, enum words and a subject cut to
``SUBJECT_CHARS`` characters, never a body. Rule-based, no model call; fixture runs are
``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import proactive_threads as pt
from .proactive_business import _tick
from .proactive_chasers import _FORWARD, _REPLY, _addr, _instant

RULE = "label_changes"
UNAPPROVED = "label_change_unapproved"
STALE = "label_change_stale"
TRACKED = "label_change_tracked"
KINDS = (UNAPPROVED, STALE, TRACKED)
WINDOW_DAYS = 90
STALE_BUSINESS_DAYS = 10
SUBJECT_CHARS = 60
MAX_TO = 8
WORD_GROUPS = ("label OR labels", "artwork OR artworks OR dieline OR dielines",
               "proof OR proofs")
WORDS = " OR ".join(WORD_GROUPS)
MAIL_PROPS = pt.PROPS
TEAMS_PROPS = ("created_at", "chat_id", "from", "deleted_at")
FILE = "SharePointFile"
ITEM_LINK = "mentions_product"
CUSTOMER_LINKS = ("Email.sent_by_customer", "Email.mentions_customer",
                  "TeamsChatMessage.mentions_customer")
SETTLED = ("verified", "reviewed", "corroborated")

_ART = r"(?:label|labels|labelling|labeling|artwork|artworks|art\s*work|dieline|dielines|" \
       r"proofs?|packaging\s+(?:copy|design|file)|carton\s+(?:copy|design|file))"
_CHANGE = r"(?:change|changes|changed|update|updates|updated|revise|revised|revision|" \
          r"revisions|amend|amended|amendment|corrected|correction|corrections|" \
          r"modify|modified|replace|replaced|redo|new\s+version|edit|edits|edited|fix|fixed)"
_REQUEST = (r"(?:\?|\bplease\b|\b(?:can|could|would|will)\s+you\b|\bkindly\b|\b(?:we|i)\s+"
            r"(?:need|require|would\s+like|want)\b|\bneeds?\s+to\s+be\b|\brequest(?:ing|ed)?\b|"
            r"\bmust\s+be\b|\bshould\s+be\b)")
ART = re.compile(r"\b" + _ART + r"\b", re.I)
CHANGE = re.compile(r"\b" + _CHANGE + r"\b", re.I)
REQUEST = re.compile(_REQUEST, re.I)
# A request turned down, withdrawn or asked about, not made.
NOT_REQUEST = re.compile(
    r"\b(?:no|not|never|without)\s+(?:\w+\s+){0,2}" + _CHANGE + r"|\bno\s+(?:need|changes?)\b|"
    r"\b(?:don'?t|do\s+not|doesn'?t)\s+(?:need|want|have)\s+to\b|\bnever\s+mind\b|\bdisregard\b|\balready\b|"
    r"^\s*(?:do|does|did|is|are|was|were|should|shall|has|have)\s+(?:we|you|it|they|this|"
    r"there)\b[^?]*\b(?:need|have)\b", re.I)
APPROVAL = re.compile(
    r"\b(?:approved|i\s+approve|we\s+approve|approval\s+(?:given|granted)|(?:ok|okay)\s+to\s+"
    r"(?:print|proceed|go|run)|go\s+ahead|good\s+to\s+go|signed\s+off|sign[- ]off\s+(?:given|"
    r"granted)|looks\s+(?:good|great|fine)|all\s+good|confirmed?\s+(?:the\s+)?(?:artwork|"
    r"labels?|proofs?))\b", re.I)
NOT_APPROVAL = re.compile(r"\b(?:not|never|no|un)\s*-?\s*approved\b|\bnot\s+(?:yet\s+)?(?:ok|"
                          r"good|approved)\b|\b(?:once|when|if|after|until)\b[^.?!\n]{0,30}"
                          r"\bapprov|\?\s*$|\bpending\s+approval\b|\bfor\s+(?:your\s+)?"
                          r"approval\b", re.I)
DONE = re.compile(
    r"\b(?:i|we)(?:'ve|\s+have)\s+(?:\w+\s+){0,2}(?:updated|revised|changed|corrected|fixed|"
    r"amended|uploaded|sent)\b|\b(?:updated|revised|corrected|new|amended|final)\s+(?:\w+\s+)?"
    + _ART + r"\s+(?:is\s+|are\s+)?(?:attached|uploaded|saved|sent|done|ready)\b|\battached\s+"
    r"(?:is|are)\s+the\s+(?:updated|revised|corrected|new|amended|final)\b|\bsent\s+to\s+"
    r"(?:print|the\s+printer|press)\b|\b(?:change|update|revision|correction)s?\s+(?:is|are|"
    r"has\s+been|have\s+been)\s+(?:done|made|completed|uploaded)\b|\b(?:has|have)\s+been\s+"
    r"(?:updated|revised|corrected|changed)\b", re.I)


def _match(sentence: str, rx: re.Pattern) -> bool:
    return bool(rx.search(sentence))


# Labels that are no artwork: a brand model ("private label"), shipping and pallet labels,
# a proof of payment or delivery; a status update ("any update on"); a pointer ("please see").
NOT_ART = re.compile(r"\b(?:private|white|own)[\s-]+label(?:s|ed|ing)?\b|\b(?:pallet|shipping|"
                     r"address|courier|return|ups|fedex|purolator|carrier|bol|tracking)\s+"
                     r"labels?\b|\bproofs?\s+of\s+\w+|\b(?:an|any|the|for\s+an?|no)\s+"
                     r"updates?\b|\bupdates?\s+(?:on|from|about)\b|\bplease\s+(?:see|find|"
                     r"note)\b", re.I)
NEAR = re.compile(r"\b" + _ART + r"\b\W+(?:\w+\W+){0,5}?" + r"\b" + _CHANGE + r"\b|\b"
                  + _CHANGE + r"\b\W+(?:\w+\W+){0,5}?\b" + _ART + r"\b", re.I)


def change_request(text: Any) -> bool:
    """Whether the text asks for a label or artwork change (module doc): the artefact and a
    change word within a few words of each other, in a sentence with a request form."""
    for sentence in pt._sentences(text if isinstance(text, str) else ""):
        sentence = NOT_ART.sub(" ", sentence)
        if not (NEAR.search(sentence) and REQUEST.search(sentence)):
            continue
        if NOT_REQUEST.search(sentence) or DONE.search(sentence):
            continue
        return True
    return False


def approval(text: Any) -> bool:
    for sentence in pt._sentences(text if isinstance(text, str) else ""):
        if APPROVAL.search(sentence) and not NOT_APPROVAL.search(sentence):
            return True
    return False


def done(text: Any) -> bool:
    return any(DONE.search(s) and not re.search(r"\b(?:not|never|once|when|if|will)\b", s,
                                               re.I)
               for s in pt._sentences(text if isinstance(text, str) else ""))


# --------------------------------------------------------------------------- the read

def _teams_rows(st: Any, since: date, people: Any) -> tuple[dict, dict, set[str], int]:
    """Teams messages holding the words, then their chats' messages of the window."""
    from .proactive_ops import declared_sources
    text_props = tuple(dict.fromkeys(
        p for ps in declared_sources()["ask_text"].values() for p in ps))
    seeds = pt._word_ids(st, WORDS, text_props, prefix="TeamsChatMessage:%")
    if seeds is None:
        return {}, {}, set(), 0
    audience = {}
    ids = sorted(seeds)
    for start in range(0, len(ids), pt.CHUNK):
        for oid, key in st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN (SELECT value "
                "FROM json_each(?))", (json.dumps(ids[start:start + pt.CHUNK]),)):
            audience[oid] = key
    clear = {k: bool(people.unit_clear(k)) for k in set(audience.values())}
    kept = [o for o, k in audience.items() if clear[k]]
    rows = pt._read(st, kept, TEAMS_PROPS + text_props)
    out = {}
    for oid, row in rows.items():
        at = _instant((row.get("created_at") or {}).get("value"))
        if at is None or at.date() < since or (row.get("deleted_at") or {}).get("value"):
            continue
        text = next((row[p]["value"] for p in text_props if p in row), "")
        out[oid] = {**row, "_text": {"value": text}}
    return out, audience, set(out), len(audience) - len(kept)


def population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    key = f"label_changes:{as_of.isoformat()}"
    if callable(getattr(st, "memo", None)):
        return st.memo(key, lambda: _population(st, as_of, people))
    return _population(st, as_of, people)


def _population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    since = as_of - timedelta(days=WINDOW_DAYS)
    # the word groups and the Teams read side by side (each thread on its own connection)
    with ThreadPoolExecutor(len(WORD_GROUPS) + 1) as pool:
        teams_job = pool.submit(_teams_rows, st, since, people)
        found = list(pool.map(lambda words: pt._word_ids(st, words, pt.TEXT_PROPS),
                              WORD_GROUPS))
        teams, teams_audience, teams_inside, teams_dropped = teams_job.result()
    indexed = all(f is not None for f in found)
    seeds = set().union(*found) if indexed else None
    if indexed:
        sent = pt._read(st, sorted(seeds), ("sent_at",))
        inside = {o for o, r in sent.items()
                  if (_instant(r["sent_at"]["value"]) or datetime.min).date() >= since}
        rows = pt._read(st, sorted(inside), MAIL_PROPS)
    else:
        rows = {o: r for o, r in pt._scan(st, MAIL_PROPS).items()
                if (_instant((r.get("sent_at") or {}).get("value")) or datetime.min).date()
                >= since}
        inside = set(rows)
    conversations = {r["conversation_id"]["value"] for o, r in rows.items()
                     if "conversation_id" in r and o in inside}
    members = pt._members(st, conversations, indexed)
    others = sorted({o for ms in members.values() for o in ms[:pt.MAX_THREAD]} - set(rows))
    rows.update(pt._read(st, others, MAIL_PROPS) if indexed else
                {o: r for o, r in pt._scan(st, MAIL_PROPS).items() if o in set(others)})
    audience: dict[str, str] = {}
    wanted = sorted(rows)
    for start in range(0, len(wanted), pt.CHUNK):
        for oid, key_ in st.conn.execute(
                "SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_id IN (SELECT "
                "value FROM json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id"),
                (json.dumps(wanted[start:start + pt.CHUNK]),)):
            audience[oid] = key_
    clear = {k: bool(people.unit_clear(k)) for k in set(audience.values())}
    kept = {o: r for o, r in rows.items() if o in audience and clear[audience[o]]}
    return {"rows": kept, "audience": audience, "members": members, "inside": inside,
            "teams": teams, "teams_audience": teams_audience, "teams_inside": teams_inside,
            "indexed": indexed,
            "dropped_unread": len(rows) - len(kept) + teams_dropped}


def _links(st: Any, oids: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    """message -> {"items": [...], "customers": [...]} from settled links (index reads)."""
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"items": [], "customers": []})
    for start in range(0, len(oids), pt.CHUNK):
        for frm, link_type, to, tier in st.conn.execute(
                "SELECT from_object, link_type, to_object, tier FROM link_rows WHERE from_object "
                "IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                (json.dumps(list(oids[start:start + pt.CHUNK])),)):
            if tier not in SETTLED:
                continue
            if link_type.endswith("." + ITEM_LINK):
                out[frm]["items"].append(to)
            elif link_type in CUSTOMER_LINKS:
                out[frm]["customers"].append(to)
    return out


def _file_changes(st: Any, items: Iterable[str]) -> dict[str, list[tuple[datetime, str]]]:
    """item -> (last modified, file) of its label or artwork SharePoint files (the files
    linked to it, by the link table's ``to`` index)."""
    out: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    files: dict[str, set[str]] = defaultdict(set)
    for item in sorted(set(items)):
        for (frm,) in st.conn.execute(
                "SELECT from_object FROM link_rows WHERE to_object = ? AND from_object LIKE ? "
                "AND link_type LIKE ? AND superseded_at IS NULL",
                (item, FILE + ":%", "%." + ITEM_LINK)):
            files[frm].add(item)
    if not files:
        return out
    rows = pt._read(st, sorted(files), ("name", "last_modified"))
    for oid, row in rows.items():
        name = (row.get("name") or {}).get("value") or ""
        at = _instant((row.get("last_modified") or {}).get("value"))
        if at is None or not ART.search(re.sub(r"[_\-.]+", " ", name)):
            continue
        for item in files[oid]:
            out[item].append((at, oid))
    return out


# --------------------------------------------------------------------------- the rule

def label_changes(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                  skips: Callable, people: Any, **_: Any) -> dict[str, Any]:
    blocked = pt._blocked(people, result)
    if blocked is not None:
        return blocked
    domains = pt._domains(people)
    pop = population(st, as_of, people)
    skipped = skips()
    if pop["dropped_unread"]:
        skipped["not_cleared_unread"] += pop["dropped_unread"]
    counts: Counter = Counter()
    # one thread per conversation (mail) or chat (Teams): [(at, oid, sender, text, channel)]
    threads: dict[str, list[tuple]] = defaultdict(list)
    thread_ok: dict[str, bool] = {}
    thread_aud: dict[str, set[str]] = defaultdict(set)
    seen: set[str] = set()
    for conversation in pop["members"]:
        t = pt._Thread(pop, conversation, people, domains)
        key = "mail:" + conversation
        thread_ok[key] = t.ok
        thread_aud[key].update(t.audiences)
        for oid in t.messages:
            ident = t.ident(oid)
            if ident in seen:
                continue
            seen.add(ident)
            threads[key].append((t.sent(oid), oid, t.sender(oid), t.text(oid), "mail",
                                 t.subject(oid)))
    for oid, row in pop["teams"].items():
        chat = pop["teams_audience"][oid]
        sender = str((row.get("from") or {}).get("value") or "").lower()
        at = _instant(row["created_at"]["value"])
        key = chat
        thread_ok.setdefault(key, True)
        if not people.identity_clear(sender):
            thread_ok[key] = False
        thread_aud[key].add(chat)
        threads[key].append((at, oid, sender, pt._own(row["_text"]["value"]), "teams", ""))
    asks = []
    for key, msgs in threads.items():
        _tick(st)
        msgs.sort(key=lambda m: (m[0], m[1]))
        for i, (at, oid, sender, text, channel, subject) in enumerate(msgs):
            inside = oid in pop["inside"] or oid in pop["teams_inside"]
            if not inside or at.date() < as_of - timedelta(days=WINDOW_DAYS):
                continue
            if channel == "mail":
                if pt.automated(sender, text, subject) or _FORWARD.match(subject):
                    counts["automated_or_forward"] += 1
                    continue
                row = pop["rows"][oid]
                to = pt._to(row)
                if isinstance(to, list) and len(to) > MAX_TO:
                    counts["mailing"] += 1
                    continue
            if not change_request(text):
                continue
            counts["requests"] += 1
            if not thread_ok.get(key, False):
                skipped["participants_not_cleared"] += 1
                continue
            later = msgs[i + 1:]
            if any(change_request(m[3]) and m[2] == sender for m in later):
                counts["superseded_by_later_request"] += 1
                continue   # the requester asked again later: that one is judged
            approved = next((m for m in later if approval(m[3])), None)
            finished = next((m for m in later if done(m[3])), None)
            asks.append({"key": key, "oid": oid, "at": at, "sender": sender,
                             "channel": channel, "subject": subject, "approved": approved,
                             "done": finished})
    links = _links(st, sorted({r["oid"] for r in asks}))
    files = _file_changes(st, [i for r in asks for i in links[r["oid"]]["items"]])
    gaps = []
    for r in sorted(asks, key=lambda x: (x["at"], x["oid"])):
        _tick(st)
        linked = links[r["oid"]]
        items = sorted(set(linked["items"]))
        customers = sorted(set(linked["customers"]))
        file_done = None
        for item in items:
            later = [f for f in files.get(item, ()) if f[0] > r["at"]]
            if later:
                file_done = min(later)
        state = ("done" if r["done"] or file_done else "approved" if r["approved"]
                 else "requested")
        waited = pt._business_days(r["at"].date(), as_of)
        if state == "done" and not r["approved"]:
            kind, priority = UNAPPROVED, "high"
        elif state != "done" and waited >= STALE_BUSINESS_DAYS:
            kind, priority = STALE, "medium"
        else:
            kind, priority = TRACKED, "low"
        counts[kind] += 1
        counts[state] += 1
        subject_id, subject_type = (items[0], items[0].split(":", 1)[0]) if items else (
            (customers[0], customers[0].split(":", 1)[0]) if customers else (
                r["oid"], r["oid"].split(":", 1)[0]))
        rows = pop["rows"] if r["channel"] == "mail" else pop["teams"]
        props = ("sent_at", "subject", "sender") if r["channel"] == "mail" else (
            "created_at", "from")
        groups = [pt._cites_of(cites, rows, [r["oid"]], props)]
        for step in (r["approved"], r["done"]):
            if step is not None and step[1] in rows:
                groups.append(pt._cites_of(cites, rows, [step[1]], props))
        what = {UNAPPROVED: "was changed with no approval recorded",
                STALE: f"is not done after {waited} business day(s)",
                TRACKED: f"is {state}"}[kind]
        summary = (f"A label or artwork change requested {r['at'].date().isoformat()} "
                   f"({'mail' if r['channel'] == 'mail' else 'Teams'}) {what}"
                   + ("" if items else "; no item named"))
        made = gap(
            kind, RULE, subject_id, subject_type, f"label|{r['oid']}",
            summary=summary,
            missing={UNAPPROVED: "Record who approved the change before it is used",
                     STALE: "Make the change or tell the requester when it will be done",
                     TRACKED: "Nothing yet: tracked until it is approved and done"}[kind],
            why=("A message asks for a label or artwork change; its thread's later messages"
                 + (" and the item's label files" if items else "")
                 + " were read for an approval and a finished change (the messages cited)."),
            priority=priority, about=(subject_type, "label_change"), groups=groups,
            event={"days": waited, "state": state, "channel": r["channel"],
                   "requested_on": r["at"].date().isoformat(),
                   "approved_on": r["approved"][0].date().isoformat() if r["approved"] else None,
                   "done_on": (r["done"][0] if r["done"] else file_done[0]).date().isoformat()
                   if (r["done"] or file_done) else None,
                   "done_by": "message" if r["done"] else "file" if file_done else None,
                   "items": items[:5], "customers": customers[:5],
                   "messages": [{"id": r["oid"], "channel": r["channel"],
                                 "sent": r["at"].date().isoformat(),
                                 "subject": pt._clip(r["subject"], SUBJECT_CHARS)}]})
        if made is None:
            skipped["message_uncited"] += 1
            continue
        made["requires"] = sorted(thread_aud[r["key"]])
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"threads": len(threads), **{k: counts[k] for k in (
        "requests", "requested", "approved", "done", UNAPPROVED, STALE, TRACKED,
        "automated_or_forward", "mailing", "superseded_by_later_request")}}
    return out


# --------------------------------------------------------------------------- the page section

def item_section(gaps: Iterable[Mapping[str, Any]], object_id: str) -> dict[str, Any]:
    """The item's label and artwork changes from the reader's own trimmed alert list."""
    rows = []
    for item in gaps:
        if item.get("kind") not in KINDS or item.get("object_id") != object_id:
            continue
        event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
        rows.append({"id": item.get("id"), "kind": item.get("kind"),
                     "state": event.get("state"), "requested_on": event.get("requested_on"),
                     "approved_on": event.get("approved_on"), "done_on": event.get("done_on"),
                     "done_by": event.get("done_by"), "channel": event.get("channel"),
                     "days": event.get("days"),
                     "stale": item.get("kind") == STALE,
                     "no_approval": item.get("kind") == UNAPPROVED,
                     "summary": item.get("summary"),
                     "citations": list(item.get("citations") or ())})
    rows.sort(key=lambda r: (r["requested_on"] or "", str(r["id"])), reverse=True)
    return {"object_id": object_id, "total": len(rows),
            "by_state": dict(Counter(r["state"] for r in rows)),
            "stale": sum(r["stale"] for r in rows),
            "no_approval": sum(r["no_approval"] for r in rows), "changes": rows}
