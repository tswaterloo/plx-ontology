"""The autopilot's 30-day replay: for one role, the real events of each past day run
through the autopilot (``autopilot.run_cycle``, the live code path), and each item it would
have prepared is compared with what the person actually did next. Scored per workflow
(``operating.workflows``) of the role; the live engine switches a workflow on for the role's
people only when it passed (``autopilot.gate_from``: 90% or better on 15 or more judged).

* **Role** (``members``): ``owner`` (the approver roster), a derived role (``purchasing``,
  ``customer_service``, ``receivables``, ``payables`` by the records their mail names;
  ``quality``, ``finance``, ``production_planning``, ``rnd`` by what their conversations
  are about: the roles file, else ``autopilot_roster.derive`` over the 90 days before the
  window), any role of the
  roles file, or ``person:<address or id>`` for one person's whole day. Every member
  passes the people exclusion (unavailable: the run is ``blocked``).
* **Prepared**: each day of the window is one cycle (and its drain) with ``now`` at the
  day's end, preparing only for the role's members, into the role's own queue. Resumable:
  a finished day is recorded and skipped on the next run; the queue's events and items are
  insert-or-ignore, so a day cut short re-runs without a duplicate.
* **Actions** (``PROVIDERS``, declared in ``autopilot_replay.json``): mail the person sent,
  approvals they decided, task events they acted on, and every store event naming them as
  its actor (per-person system activity in BC, the Portal, Sage and FileMaker plugs in
  there, or as a ``plugins`` provider).
* **Match**: an item matches when its owner, after the event and within ``horizon_hours``
  (working time: Monday-to-Friday hours on the business calendar, ``answer_end``),
  acted on the same conversation or touched one of the records the item is about (a mail
  to the same contact about something else is no match). An item whose horizon passes the end of the data is
  ``unjudged`` (never counted either way). Precision = matched / judged, per workflow;
  coverage beside it = the person's actions of that workflow an item anticipated / all.

Read-only on the store and the capture; writes only ``--out``. Memory: before each day the
run waits for ``--min-available-gb`` (``MemAvailable``), then gives up resumably (exit 75);
the process's data segment is capped at ``--mem-max-gb``. The result holds ids, counts and
workflow names, never message text.

Run::

    python3 -m operating.autopilot_replay --store STORE --out DIR --role purchasing \\
        --days 30 --until 2026-11-05T07:00:00Z [--capture-state STATE]
"""

from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import autopilot as ap
from . import workflows as wf
from operating import declared_schema as _schemas
from operating.declared_schema import autopilot_replay as _AUTOPILOT_REPLAY

CONFIG_FILE = Path(__file__).with_name("autopilot_replay.json")
SCHEMA = "plx-autopilot-replay/v1"
CHUNK = 2000
EXIT_NO_MEMORY = 75
SEED_DAYS = 30
TEAMS_WORKFLOW = "teams_chat"
MENTIONS = re.compile(r"^Email\.(?:mentions_|sent_by_)")


class ReplayInvalid(ValueError):
    pass


# ----------------------------------------------------------------------------- config

# Extra action providers a config file may name (``"plugins": ["name"]``): registered here in
# code (name -> provider), so a config file never decides which module is imported.
PLUGINS: dict[str, Any] = {}


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    raw = json.loads(Path(path or CONFIG_FILE).read_text(encoding="utf-8"))
    if raw.get("schema") != SCHEMA:
        raise ReplayInvalid("schema")
    unknown = [p for p in raw.get("providers") or () if p not in PROVIDERS]
    if unknown:
        raise ReplayInvalid(f"provider {unknown[0]}")
    plugins = []
    for spec in raw.get("plugins") or ():
        # a name registered in PLUGINS below, never a module imported from the file
        fn = PLUGINS.get(str(spec))
        if not callable(fn):
            raise ReplayInvalid(f"plugin {spec}")
        plugins.append(fn)
    hours = int(raw.get("horizon_hours") or 0)
    if not 1 <= hours <= 24 * 14:
        raise ReplayInvalid("horizon_hours")
    lag = raw.get("feed_lag_minutes") or {}
    if not isinstance(lag, Mapping) or not all(
            isinstance(k, str) and isinstance(v, int) and 0 <= v <= 24 * 60
            for k, v in lag.items()):
        raise ReplayInvalid("feed_lag_minutes")
    return {"horizon": timedelta(hours=hours),
            "feed_lag": {k: timedelta(minutes=v) for k, v in lag.items()},
            "providers": [PROVIDERS[p] for p in raw.get("providers") or ()] + plugins}


def answer_end(at: datetime, horizon: timedelta) -> datetime:
    """When the answer window of a trigger at ``at`` closes: ``horizon`` of Monday-to-Friday
    time on the business's calendar (``time_window``'s zone) later; Saturday and Sunday do not
    count, so a Friday afternoon's message has the same window as a Monday's (measured on DEV
    days 2026-10-06..11-03: the person's Teams items of Monday and Tuesday were all answered
    within 72 h, a Thursday's one in five only after the weekend)."""
    from zoneinfo import ZoneInfo
    from operating.time_window import BUSINESS_TZ
    zone = ZoneInfo(BUSINESS_TZ)
    left, t = horizon, at.astimezone(timezone.utc)
    while left > timedelta(0):
        local = t.astimezone(zone)
        midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(),
                                    tzinfo=zone).astimezone(timezone.utc)
        if local.weekday() < 5:
            if midnight - t >= left:
                return t + left
            left -= midnight - t
        t = midnight
    return t


# ----------------------------------------------------------------------------- members

def members(st: Any, people: Any, role: str, roles: Mapping[str, list[str]], *,
            until: str) -> list[str]:
    """The role's people (module doc), each cleared by the people exclusion."""
    directory, _addresses, _domains = ap._people_maps(people)
    if role.startswith("person:"):
        who = role[len("person:"):].lower()
        found = {directory.get(who, who if re.fullmatch(r"[0-9a-f-]{36}", who) else "")}
    elif not roles.get(role) and role in _roster_rules():
        from . import autopilot_roster as roster
        since = ap._iso(ap._instant(until) - timedelta(days=90))
        found = set(roster.derive(st, people, role, since, until)["members"])
    else:
        found = ap._role_members(role, roles, directory)
    return sorted(o for o in found if o and people.identity_clear(o))


def _roster_rules() -> frozenset[str]:
    from . import autopilot_roster as roster
    return roster.ROLE_NAMES


# ----------------------------------------------------------------------------- actions

def _window(st: Any, kinds: Sequence[str], since: str, until: str) -> list[tuple[str, str]]:
    # events first (``autopilot.window_ids``: the store has no time index)
    return [(r[0], r[1]) for r in st.conn.execute(
        "SELECT eo.object_id, MIN(e.time) FROM events e CROSS JOIN event_objects eo ON "
        "eo.event_id = e.event_id WHERE e.event_type IN (SELECT value FROM json_each(?)) AND e.time >= ? "
        "AND e.time < ? GROUP BY eo.object_id", (json.dumps(list(kinds)), since, until))]


def _words(text: str) -> str:
    """A text's words without digits, punctuation or case (every copy of a template)."""
    return re.sub(r"[\W\d_]+", " ", (text or "").casefold()).strip()


def sent_shape(sender: str, subject: str, body: str) -> dict[str, Any]:
    """What tells a machine's send from a person's, as digests (never text): an automated
    sender, footer or reply (``proactive_threads.automated``), the body's words (a template
    sent again and again; a body of under 30 letters is no template) and the subject's."""
    from . import proactive_threads as pt
    words = _words(body)
    return {"automated": pt.automated(sender, body, subject),
            "body_key": ap._digest("body", words) if len(words) >= 30 else None,
            "subject_key": ap._digest("subject", _words(subject)) if _words(subject) else None}


def mail_sent(st: Any, people: Any, who: set[str], since: str, until: str
              ) -> list[dict[str, Any]]:
    """Mail a member sent (the copy in their own mailbox)."""
    directory, addresses_of, domains = ap._people_maps(people)
    boxes = {f"mail:{m}" for m in who}
    ids = [o for o, _t in _window(st, ("Email.received",), since, until)
           if o.startswith("Email:")]
    out = []
    for start in range(0, len(ids), CHUNK):
        typed = ap._types(st, ids[start:start + CHUNK])
        mine = sorted(o for o, (_t, aud) in typed.items() if aud in boxes)
        rows = ap._read(st, mine, ("sender", "to_addresses", "cc_addresses", "subject",
                                   "body_preview", "conversation_id", "sent_at",
                                   "received_at", "mailbox"))
        links: dict[str, set[str]] = defaultdict(set)
        for row in (st.touching(mine, None) if mine else ()):
            if MENTIONS.match(row[2]) and row[3] in rows:
                links[row[3]].add(row[4])
        for oid, r in rows.items():
            box = ap._v(r, "mailbox").lower()
            sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
            if box not in who or sender not in addresses_of.get(box, set()):
                continue
            to = ap._addresses(ap._v(r, "to_addresses")) + ap._addresses(ap._v(r, "cc_addresses"))
            outside = [a for a in to if not ap._internal(a, domains)]
            if outside:
                sides = {s for a in outside for s, _o in ap._parties(st, a)}
                side = next(iter(sides)) if len(sides) == 1 else "internal_out"
            else:
                side = "internal"
            subject, body = ap._v(r, "subject"), ap._v(r, "body_preview")
            out.append({"person": box, "at": ap._v(r, "sent_at") or ap._v(r, "received_at"),
                        "kind": "mail_sent", **sent_shape(sender, subject, body),
                        **ap.routine_note(ap._v(r, "subject"), ap._v(r, "body_preview"),
                                          sender, to, addresses_of.get(box, set()), domains,
                                          oid),
                        "workflow": wf.classify(ap._v(r, "subject"), ap._v(r, "body_preview"),
                                                side),
                        "conversation": ap._v(r, "conversation_id") or None,
                        "to": sorted(set(to)), "records": sorted(links.get(oid, ()))})
    return out


def mail_received(st: Any, people: Any, who: set[str], since: str, until: str
                  ) -> list[dict[str, Any]]:
    """Mail that reached a member from someone else (their own mailbox's copy), the way the
    feed sees it (automated and bulk mail left out): the replay's history before its window."""
    from . import inbox_triage as it
    from . import proactive_threads as pt
    _directory, addresses_of, _domains = ap._people_maps(people)
    boxes = {f"mail:{m}" for m in who}
    ids = [o for o, _t in _window(st, ("Email.received",), since, until)
           if o.startswith("Email:")]
    out = []
    machine = it.automated_mail()   # the live feed's test (``autopilot._mail_events``)
    for start in range(0, len(ids), CHUNK):
        typed = ap._types(st, ids[start:start + CHUNK])
        mine = sorted(o for o, (_t, aud) in typed.items() if aud in boxes)
        rows = ap._read(st, mine, ("sender", "subject", "body_preview", "conversation_id",
                                   "received_at", "sent_at", "mailbox", "internet_message_id"))
        for oid, r in rows.items():
            box = ap._v(r, "mailbox").lower()
            sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
            if box not in who or not sender or sender in addresses_of.get(box, set()):
                continue
            subject, text = ap._v(r, "subject"), pt._own(ap._v(r, "body_preview"))
            if machine(sender, text, subject):
                continue
            out.append({"event_type": "message.received", "mailbox": box, "sender": sender,
                        "conversation_id": ap._v(r, "conversation_id") or None,
                        "message_id": ap._v(r, "internet_message_id") or oid,
                        "at": ap._v(r, "received_at") or ap._v(r, "sent_at")})
    return out


def approval_decided(st: Any, people: Any, who: set[str], since: str, until: str
                     ) -> list[dict[str, Any]]:
    directory, _a, _d = ap._people_maps(people)
    ids = [o for o, _t in _window(st, ("MCApproval.decided",), since, until)]
    rows = ap._read(st, ids, ("decided_by", "decided_at"))
    out = []
    for oid, r in rows.items():
        by = ap._v(r, "decided_by").lower()
        person = directory.get(by, by)
        if person in who:
            out.append({"person": person, "at": ap._v(r, "decided_at"), "kind": "approval",
                        "workflow": "internal_approval", "conversation": None, "to": [],
                        "records": [oid]})
    return out


# an MC approval's decision is also a task event: ``approval_decided`` counts it once
TASK_EVENTS_COUNTED_ELSEWHERE = frozenset({"approval.decided"})


def task_event(st: Any, people: Any, who: set[str], since: str, until: str
               ) -> list[dict[str, Any]]:
    """MC task events a member took themselves (the actor an address or a bare name of
    exactly one person, ``autopilot.handle_person``, and the MC capture's event names no
    agent runtime: agents work under their operator's identity, and their approval
    requests, questions and updates are no person's work). No capture, or the event not in
    it: not attributed (fail closed). An answer to a question asked of them is the
    ``mc_question`` workflow, naming the question."""
    directory, _a, _d = ap._people_maps(people)
    ids = [o for o, _t in _window(st, ("MCTaskEvent.occurred",), since, until)]
    rows = ap._read(st, ids, ("actor", "at", "kind"))
    reader = getattr(st, "autopilot_mc", None)
    named: dict[str, str | None] = {}
    out = []
    for oid, r in rows.items():
        kind = ap._v(r, "kind")
        if kind in TASK_EVENTS_COUNTED_ELSEWHERE:
            continue
        actor = ap._v(r, "actor").lower()
        if actor not in named:
            named[actor] = ap.handle_person(
                actor, directory, reader.home_domains() if reader is not None else None)
        person = named[actor]
        if person not in who:
            continue
        got = st.conn.execute("SELECT native_key FROM objects WHERE object_id = ?",
                              (oid,)).fetchone()
        key = reader.key_of(got[0]) if reader is not None and got else None
        ev = reader.event(*key) if key else None
        if ev is None or ev["by_agent"]:
            continue
        tasks = [o for _t, o in ap._linked(st, oid) if o.startswith("MCTask:")]
        action = {"person": person, "at": ap._v(r, "at"), "kind": "task", "workflow": None,
                  "conversation": None, "to": [], "records": tasks}
        if kind == "question.answered":
            action.update(kind="question", workflow="mc_question")
            if ev["question_id"]:
                action["records"] = tasks + [f"mcq:{key[0]}:{ev['question_id']}"]
        out.append(action)
    return out


def record_actor(st: Any, people: Any, who: set[str], since: str, until: str
                 ) -> list[dict[str, Any]]:
    """Every store event naming a member as its actor (``event_objects.qualifier``)."""
    directory, _a, _d = ap._people_maps(people)
    rule = wf.load()["record_workflow"]
    out = []
    for eid, at, actor in st.conn.execute(
            "SELECT e.event_id, e.time, a.object_id FROM events e CROSS JOIN event_objects a "
            "ON a.event_id = e.event_id AND a.qualifier = 'actor' WHERE e.time >= ? AND "
            "e.time < ?", (since, until)):
        key = str(actor).rsplit(":", 1)[-1].lower()
        person = directory.get(key, key)
        if person not in who:
            continue
        recs = [r[0] for r in st.conn.execute(
            "SELECT object_id FROM event_objects WHERE event_id = ? AND qualifier <> 'actor'",
            (eid,))]
        flow = next((rule[r.split(":", 1)[0]] for r in recs if r.split(":", 1)[0] in rule),
                    None)
        out.append({"person": person, "at": at, "kind": "record", "workflow": flow,
                    "conversation": None, "to": [], "records": recs})
    return out


def teams_sent(st: Any, people: Any, who: set[str], since: str, until: str
               ) -> list[dict[str, Any]]:
    """Teams chat and channel messages a member wrote (their side of a chat or channel
    thread the exclusion clears). Scored as the ``teams_chat`` workflow (the channel's own:
    the autopilot's Teams items are scored there); ``topic`` is what the words are about."""
    import bisect
    from .proactive_ops import declared_sources
    directory, _a, _d = ap._people_maps(people)
    text_props = tuple(dict.fromkeys(
        p for ps in declared_sources()["ask_text"].values() for p in ps))
    lo, hi = ap._instant(since), ap._instant(until)
    out = []
    for otype in ap.TEAMS_TYPES:
        dated = ap._dated(st, otype, "created_at")
        ids = [o for _t, o in dated[bisect.bisect_left(dated, (lo,)):
                                    bisect.bisect_left(dated, (hi,))]]
        # conversation -> sender -> when they last wrote there (messages in time order):
        # an action's ``other_at`` is the last message someone else wrote before it
        last_by: dict[str, dict[str, datetime]] = defaultdict(dict)
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            typed = ap._types(st, chunk)
            rows = ap._read(st, chunk, ("created_at", "from", "deleted_at", "reply_to_id")
                            + text_props)
            mine = {o: r for o, r in rows.items()
                    if ap.teams_sender(ap._v(r, "from"), directory) in who}
            bodies = ap.teams_bodies(st, [h for r in mine.values()
                                          for h in ap._handles_of(r)[:1]])
            for oid in chunk:
                row = rows.get(oid) or {}
                aud = typed.get(oid, ("", ""))[1]
                channel = aud.startswith("teams:channel:")
                sender = ap.teams_sender(ap._v(row, "from"), directory)
                when = ap._instant(ap._v(row, "created_at"))
                if not (channel or aud.startswith("teams:chat:")) or not sender or \
                        when is None:
                    continue
                conversation = ap.teams_thread(aud, oid, ap._v(row, "reply_to_id")) \
                    if channel else aud
                others = [t for s, t in last_by[conversation].items() if s != sender]
                last_by[conversation][sender] = when
                if oid not in mine or not people.unit_clear(aud) or \
                        ap._v(row, "deleted_at"):
                    continue
                text = bodies.get((ap._handles_of(row) or [""])[0]) or next(
                    (ap._v(row, p) for p in text_props if ap._v(row, p)), "")
                out.append({"person": sender,
                            "at": ap._v(row, "created_at"), "kind": "teams_sent",
                            "workflow": TEAMS_WORKFLOW,
                            "topic": wf.classify("", text, "internal"),
                            "conversation": conversation,
                            "other_at": ap._iso(max(others)) if others else None,
                            "to": [], "records": []})
    return out


def teams_received(st: Any, people: Any, who: set[str], since: str, until: str
                   ) -> list[dict[str, Any]]:
    """Teams messages that reached a member as the feed types them (a one-to-one chat's
    other member; a group's or channel's mentioned): the replay's history before its
    window, beside ``mail_received`` (a sender's answered share counts both channels)."""
    import bisect
    directory, _a, _d = ap._people_maps(people)
    lo, hi = ap._instant(since), ap._instant(until)
    out: list[dict[str, Any]] = []
    skipped: Counter = Counter()
    for otype in ap.TEAMS_TYPES:
        dated = ap._dated(st, otype, "created_at")
        ids = [o for _t, o in dated[bisect.bisect_left(dated, (lo,)):
                                    bisect.bisect_left(dated, (hi,))]]
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            typed = ap._types(st, chunk)
            out += [e for e in ap._teams_events(st, list(typed), typed, people, directory,
                                                skipped, words=False)
                    if e["mailbox"] in who]
    return out


PROVIDERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "mail_sent": mail_sent, "approval_decided": approval_decided, "task_event": task_event,
    "record_actor": record_actor, "teams_sent": teams_sent}


# ----------------------------------------------------------------------------- scoring

def item_keys(item: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, set[str]]:
    work = (item.get("body") or {}).get("prepared") or {}
    records = set(event.get("records") or ())
    if event.get("object_type") not in ("Email", "TeamsChatMessage") and event.get("object_id"):
        records.add(event["object_id"])
    for key in ("purchase_order", "order", "object"):
        if isinstance(work.get(key), Mapping) and work[key].get("object_id"):
            records.add(work[key]["object_id"])
    to = {str(t.get("address")).lower() for t in (work.get("draft") or {}).get("to") or ()
          if isinstance(t, Mapping) and t.get("address")}
    if event.get("question_key"):
        records.add(str(event["question_key"]))   # the question, not the task's other work
    if event.get("sender"):
        to.add(str(event["sender"]).lower())
    conv = {str(event["conversation_id"])} if event.get("conversation_id") else set()
    series = {str(event["series"])} if event.get("series") else set()
    return {"records": records, "to": to, "conversation": conv, "series": series}


def trigger_at(item: Mapping[str, Any], event: Mapping[str, Any]) -> datetime | None:
    """When the item's trigger happened (a due record: the day the cycle found it)."""
    if event.get("event_type") == "record.due":
        made = ap._instant(item.get("created_at"))
        return made - timedelta(days=1) if made else None
    # a record the feed held only some time after it happened (``feed_lag_minutes``)
    return ap._instant(event.get("seen_at") or event.get("at"))


# one Teams reply sent as several messages: each next message of the person in the same
# chat within this long of the one before it is the same reply (measured on Vince's DEV
# days 10-26..11-05: 22 of 110 unanticipated messages followed a matched one within it)
BURST = timedelta(minutes=10)

# Scoring rules that change how a result compares with one scored before them: every
# result names them, so a jump read beside an older run is labelled a rule change, not
# new work prepared.
SCORING_RULES = (
    {"id": "teams_burst_credit", "workflow": TEAMS_WORKFLOW, "measure": "coverage",
     "since": "2026-11-11",
     "note": "the rest of a Teams reply typed as several messages counts as anticipated "
             "with its matched first message (BURST); precision unchanged"},
    {"id": "answer_window_working_days", "workflow": "*", "measure": "precision",
     "since": "2026-11-13",
     "note": "the answer window is horizon_hours of Monday-to-Friday time (answer_end): an "
             "item answered after a weekend within the window matches; a result "
             "names horizon_hours, counted as working hours"},
)


def _credit_burst(hit: Mapping[str, Any], actions: Sequence[tuple[datetime, Mapping[str, Any]]],
                  anticipated: set[int]) -> None:
    """The rest of a matched Teams reply (``BURST``) is anticipated with it: the prepared
    reply replaces all of it. Coverage only; an item still matches once. Someone else
    writing in the conversation in between (``other_at``, after the person's message
    before) ends the reply: what follows answers a new message (measured on Vince's DEV
    days 10-06..10-26: 58 of 214 own messages within ten minutes of the one before had
    another member's message in between)."""
    if hit.get("kind") != "teams_sent" or not hit.get("conversation"):
        return
    last = ap._instant(hit.get("at"))
    for when, a in actions:
        if last is None or when <= last:
            continue
        if when - last > BURST:
            break
        if a.get("kind") == "teams_sent" and a.get("conversation") == hit["conversation"]:
            # an action read without it (an older cache): no credit, never a guess
            other = ap._instant(a.get("other_at"))
            if "other_at" not in a or (other is None and a["other_at"]) or \
                    (other is not None and other > last):
                break
            anticipated.add(id(a))
            last = when


def score(items: Sequence[Mapping[str, Any]], events: Mapping[str, Mapping[str, Any]],
          actions: Sequence[Mapping[str, Any]], *, horizon: timedelta, data_end: datetime,
          since: datetime, until: datetime, window_items: bool = False,
          anticipated: set[int] | None = None,
          source_end: Mapping[str, datetime] | None = None) -> dict[str, Any]:
    """Per workflow: items prepared, judged, matched; the person's actions in [since, until)
    and how many an item anticipated. ``window_items``: only the items whose trigger is in
    [since, until) (a split of the window: dev or held-out days). ``source_end``
    (``source_ends``): an item on a record of such a source is judged only when its horizon
    ends by that source's last event too (else unjudged: its answer may not be captured).
    The answer window is ``horizon`` of working time (``answer_end``)."""
    by_person: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
    for a in actions:
        at = ap._instant(a.get("at"))
        if at is not None:
            by_person[a["person"]].append((at, a))
    for v in by_person.values():
        v.sort(key=lambda x: x[0])
    flows: dict[str, Counter] = defaultdict(Counter)
    anticipated = set() if anticipated is None else anticipated
    by_records = set(wf.load().get("scored_by_records") or ())
    for item in items:
        event = events.get(item["event_id"]) or {}
        at = trigger_at(item, event)
        if window_items and (at is None or not since <= at < until):
            continue
        flow = wf.of_item(item["autopilot"], event)
        c = flows[flow]
        c["prepared"] += 1
        # an entry keyed in a system: judged against what people entered there
        # (``receivables_score``), never against their mail
        end = data_end
        source = SOURCE_OF.get(str(event.get("object_type") or ""))
        if source_end and source in source_end:
            end = min(end, source_end[source])
        close = answer_end(at, horizon) if at is not None else None
        if close is None or close > end or flow in by_records:
            c["unjudged"] += 1
            continue
        c["judged"] += 1
        keys = item_keys(item, event)
        # a due-day item (a recurring update): only that day's mail answers it
        until_at = at + timedelta(hours=float(event["match_hours"])) \
            if event.get("match_hours") else close
        hit = None
        for when, a in by_person.get(item["owner"], ()):
            if when <= at or when > until_at:
                continue
            if a.get("series") and a["series"] in keys["series"]:
                hit = a
                break
            # the same conversation or a record the item is about: a mail to the same
            # contact about something else is no match
            if (a.get("conversation") and a["conversation"] in keys["conversation"]) or \
                    keys["records"] & set(a.get("records") or ()):
                hit = a
                break
        if hit is not None:
            c["matched"] += 1
            anticipated.add(id(hit))
            _credit_burst(hit, by_person.get(item["owner"], ()), anticipated)
    for person_actions in by_person.values():
        for when, a in person_actions:
            if since <= when < until:
                flow = a.get("workflow") or "unclassified"
                flows[flow]["actions"] += 1
                flows[flow]["anticipated"] += id(a) in anticipated
    out = {}
    for flow, c in sorted(flows.items()):
        judged = c["judged"]
        out[flow] = {k: c[k] for k in ("prepared", "judged", "matched", "unjudged", "actions",
                                       "anticipated")}
        out[flow]["precision"] = str((Decimal(c["matched"]) / judged).quantize(
            Decimal("0.001"))) if judged else None
        out[flow]["coverage"] = str((Decimal(c["anticipated"]) / c["actions"]).quantize(
            Decimal("0.001"))) if c["actions"] else None
        out[flow]["passes_gate"] = bool(judged >= ap.GATE_MIN_JUDGED and
                                        Decimal(c["matched"]) / judged >= ap.GATE_PRECISION)
    return out


# ----------------------------------------------------------------------------- misses

MINUTES = {"mail_sent": 6, "teams_sent": 2, "approval": 3, "task": 10, "record": 5,
           "question": 3}
MISS_CAUSES = ("trigger_not_seen", "self_initiated", "participant_not_cleared",
               "wrong_timing", "wrong_item", "workflow_unknown", "matcher_too_strict")


def miss_cause(action: Mapping[str, Any], *, by_conversation: Mapping[str, list],
               by_record: Mapping[str, list], items_of: Mapping[str, list],
               sent_to: Mapping[str, list], horizon: timedelta) -> str:
    """Why no item anticipated a real action (``MISS_CAUSES``), read from the events the
    replay's queue holds (each with its state and reason) and the items it prepared:

    * ``self_initiated``: no message came in on the action's conversation before it, and
      it names no record an event was seen for (the person started it);
    * ``trigger_not_seen``: a conversation or record of another system the feed never typed;
    * ``participant_not_cleared``: the trigger involved someone the people exclusion did not
      clear (fail closed: never prepared, by rule);
    * ``wrong_timing``: the trigger is older than the horizon, or an item came after;
    * ``wrong_item``: an autopilot took the trigger but declined (no single record) or its
      item is about something else;
    * ``matcher_too_strict``: an item on a message from someone the action went to, inside
      the horizon, on another conversation;
    * ``workflow_unknown``: the trigger was seen and no autopilot covers it."""
    at = ap._instant(action.get("at"))
    person = action["person"]
    triggers = []
    if action.get("conversation"):
        triggers = [e for e in by_conversation.get(action["conversation"], ())
                    if e["mailbox"] in ("", person) and e["at"] and e["at"] < at]
    for rec in action.get("records") or ():
        triggers += [e for e in by_record.get(rec, ()) if e["at"] and e["at"] < at]
    mine = sent_to.get(person, ())
    lo = bisect.bisect_left(mine, (at - horizon,))
    hi = bisect.bisect_left(mine, (at,))
    to = set(action.get("to") or ())
    near = any(e["sender"] in to and items_of.get(e["event_id"]) for _t, _i, e in mine[lo:hi])
    if near and not triggers:
        return "matcher_too_strict"
    if not triggers:
        if action.get("conversation") or not action.get("records"):
            return "self_initiated" if action.get("kind") == "mail_sent" else \
                "trigger_not_seen"
        return "trigger_not_seen"
    recent = [e for e in triggers if e["at"] >= at - horizon]
    if not recent:
        return "wrong_timing"
    if any(items_of.get(e["event_id"]) for e in recent):
        return "wrong_item"
    if any(items_of.get(e["event_id"]) for e in triggers):
        return "wrong_timing"
    if all(e["reason"] == "participants_not_cleared" for e in recent):
        return "participant_not_cleared"
    reasons = {r for e in recent for r in (e["reason"] or "").split(",") if r}
    if reasons - {"participants_not_cleared", "owner_not_addressed"}:
        return "wrong_item"
    return "workflow_unknown"


def misses(queue: Any, items: Sequence[Mapping[str, Any]], actions: Sequence[Mapping[str, Any]],
           anticipated: set[int], *, since: datetime, until: datetime, horizon: timedelta
           ) -> dict[str, Any]:
    """The actions no item anticipated, by workflow and cause, with hours a week
    (``MINUTES`` per action kind: an estimate of the time each takes the person)."""
    by_conversation: dict[str, list] = defaultdict(list)
    by_record: dict[str, list] = defaultdict(list)
    sent_to: dict[str, list] = defaultdict(list)
    for eid, body, state, reason in queue.conn.execute(
            "SELECT event_id, body, state, reason FROM events"):
        e = json.loads(body)
        row = {"event_id": eid, "at": ap._instant(e.get("at")), "state": state,
               "reason": reason, "mailbox": str(e.get("mailbox") or ""),
               "sender": str(e.get("sender") or "").lower()}
        if e.get("conversation_id"):
            by_conversation[str(e["conversation_id"])].append(row)
        for rec in [e.get("object_id"), *(e.get("records") or ()), e.get("question_key")]:
            if rec and not str(rec).startswith(("Email:", "TeamsChatMessage:")):
                by_record[str(rec)].append(row)
        if row["mailbox"] and row["at"]:
            sent_to[row["mailbox"]].append((row["at"], eid, row))
    for rows in sent_to.values():
        rows.sort(key=lambda r: (r[0], r[1]))
    items_of: dict[str, list] = defaultdict(list)
    for item in items:
        items_of[item["event_id"]].append(item)
    weeks = Decimal(max((until - since).total_seconds(), 1)) / Decimal(7 * 86400)
    table: dict[str, Counter] = defaultdict(Counter)
    minutes: dict[str, Counter] = defaultdict(Counter)
    for a in actions:
        at = ap._instant(a.get("at"))
        if at is None or not since <= at < until or id(a) in anticipated:
            continue
        cause = miss_cause(a, by_conversation=by_conversation, by_record=by_record,
                           items_of=items_of, sent_to=sent_to, horizon=horizon)
        flow = a.get("workflow") or "unclassified"
        table[flow][cause] += 1
        minutes[flow][cause] += MINUTES.get(a.get("kind") or "", 5)
    ranked = sorted(((Decimal(m) / 60 / weeks, flow, cause, table[flow][cause])
                     for flow, row in minutes.items() for cause, m in row.items()),
                    reverse=True)
    return {"minutes_per_action": MINUTES,
            "by_workflow": {f: dict(sorted(c.items())) for f, c in sorted(table.items())},
            "top": [{"workflow": f, "cause": c, "actions": n,
                     "hours_week": str(h.quantize(Decimal("0.1")))}
                    for h, f, c, n in ranked[:25]]}


# ----------------------------------------------------------------------------- memory

def available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def wait_for_memory(floor_gb: float, wait_s: float, *, probe: Callable[[], float] = available_gb,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + wait_s
    while probe() < floor_gb:
        if time.monotonic() >= deadline:
            return False
        sleep(min(30.0, max(1.0, wait_s / 20)))
    return True


def cap_memory(max_gb: float | None) -> None:
    if not max_gb:
        return
    import resource
    limit = int(max_gb * 1024 ** 3)
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))


# ----------------------------------------------------------------------------- run

class Progress:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        _schemas.apply(self.conn, _AUTOPILOT_REPLAY)

    def done(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT day FROM days")}

    def finish(self, day: str, summary: Mapping[str, Any]) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)",
                              (day, ap._iso(datetime.now(timezone.utc)),
                               json.dumps(summary, sort_keys=True, default=str)))

    def summaries(self) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT summary FROM days ORDER BY day")]

    def close(self) -> None:
        self.conn.close()


def _role_ids(st: Any, ids: Sequence[str], who: set[str]) -> list[str]:
    """The day's records less the mail of mailboxes outside the role (never read)."""
    typed = ap._types(st, ids)
    boxes = {f"mail:{m}" for m in who}
    return [o for o in ids if typed.get(o, ("", ""))[0] != "Email"
            or typed[o][1] in boxes]


def data_end(st: Any, until: str) -> datetime:
    row = st.conn.execute("SELECT MAX(time) FROM events WHERE event_type = 'Email.received' "
                          "AND time <= ?", (until,)).fetchone()
    return ap._instant(row[0]) if row and row[0] else ap._instant(until)


# record types whose own capture can stop while mail goes on (the MC capture paused for a
# migration, 2026-11-10 17:42Z): their items are judged only to that capture's last write
SOURCE_OF = {"MCApproval": "mc", "MCTaskEvent": "mc"}


def source_ends(st: Any) -> dict[str, datetime]:
    """Each such source's capture's last write (``MCQuestions.known_to``); no key when no
    capture reader is attached or its end is unknown (the mail's end alone, as before)."""
    reader = getattr(st, "autopilot_mc", None)
    known = getattr(reader, "known_to", None)
    end = known() if callable(known) else None
    return {"mc": end} if end is not None else {}


TEXT_SAMPLE = 300          # Teams messages of the window probed for their body text
TEXT_COMPLETE = 0.9        # share of probed body fields that must hold their text


def text_gap(st: Any, since: datetime, until: datetime) -> dict[str, Any] | None:
    """None when the store's text index holds the words of the window's Teams messages;
    else what is missing. A store whose index lists a message's body field but holds none
    of its text (Postgres before its text chunks are loaded) reads every message as empty,
    so asks and closing notes go unseen and the Teams items would be scored on nothing:
    the replay refuses (fail closed), as it does when the index cannot be read."""
    handles: list[str] = []
    try:
        for otype in ap.TEAMS_TYPES:
            dated = ap._dated(st, otype, "created_at")
            ids = [o for _t, o in dated[bisect.bisect_left(dated, (since,)):
                                        bisect.bisect_left(dated, (until,))]]
            step = max(1, len(ids) // TEXT_SAMPLE)
            rows = ap._read(st, ids[::step][:TEXT_SAMPLE], ("created_at",))
            handles += [h for r in rows.values() for h in ap._handles_of(r)[:1]]
        if not handles:
            return None
        where = ("f.citation IN (SELECT value FROM json_each(?)) AND f.field_path = ?")
        args = (json.dumps(sorted(set(handles))), ap.TEAMS_BODY)
        fields = st.conn.execute(f"SELECT COUNT(*) FROM text_fields f WHERE {where}",
                                 args).fetchone()[0]
        held = st.conn.execute(
            f"SELECT COUNT(DISTINCT f.field_id) FROM text_fields f JOIN text_chunks k "
            f"ON k.field_id = f.field_id WHERE {where}", args).fetchone()[0]
    except sqlite3.Error as error:
        return {"reason": "text_index_unreadable", "detail": str(error)[:200]}
    if fields and held < TEXT_COMPLETE * fields:
        return {"reason": "text_index_incomplete", "messages": len(handles),
                "body_fields": int(fields), "with_text": int(held)}
    return None


def replay(st: Any, people: Any, *, role: str, out: Path, since: datetime, until: datetime,
           roles: Mapping[str, list[str]], config: Mapping[str, Any],
           registry: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None,
           floor_gb: float = 0.0, wait_s: float = 0.0,
           probe: Callable[[], float] = available_gb, end: datetime | None = None,
           held_out_days: float = 0.0) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    registry = registry or ap.load_registry()
    if people is None or getattr(people, "unavailable", None):
        return {"schema": SCHEMA, "role": role, "status": "blocked",
                "reason": "people_exclusion_unavailable"}
    # a derived role reads the 90 days before the window (no evidence from the days judged)
    who = set(members(st, people, role, roles, until=ap._iso(since)))
    if not who:
        return {"schema": SCHEMA, "role": role, "status": "no_members"}
    gap = text_gap(st, since, until)
    if gap:
        return {"schema": SCHEMA, "role": role, "status": "blocked", **gap,
                "source": ap.store_source(st)}
    slug = re.sub(r"[^a-z0-9_]+", "_", role.lower())[:60]
    root = out / slug
    root.mkdir(parents=True, exist_ok=True)
    (root / ap.ROLES_FILE).write_text(json.dumps(
        {"roles": {**roles, **({role: sorted(who)} if not role.startswith("person:") else {})}}))
    queue = ap.Queue(root / ap.QUEUE_FILE)
    progress = Progress(root / "progress.sqlite")
    env = {k: v for k, v in environ.items() if k != ap.ROLES_ENV}
    status = "complete"
    source = ap.store_source(st)
    try:
        # a queue holds the days one store prepared: a resumed run on the other store
        # (SQLite generation vs Postgres) would mix two data sets in one score
        began = queue.meta("store")
        if began is not None and began != source["store"]:
            return {"schema": SCHEMA, "role": role, "status": "blocked",
                    "reason": f"queue_started_on_{began}"}
        if began is None:
            with queue.conn:
                queue.set_meta("store", source["store"])
        if not queue.meta("seeded"):
            # what the person wrote before the window: a live feed would know it already
            # (``Queue.engagement``); their own sent mail and chat messages only
            lo, hi = ap._iso(since - timedelta(days=SEED_DAYS)), ap._iso(since)
            sent = [{**a, "owner": a["person"]} for a in mail_sent(st, people, who, lo, hi)]
            notes = [{"owner": a["person"], "conversation": a.get("conversation"),
                      "message_id": f"{a['person']}:{a['at']}", "to": a.get("to") or (),
                      "at": a["at"]}
                     for a in sent + teams_sent(st, people, who, lo, hi) if a.get("at")]
            heard = mail_received(st, people, who, lo, hi) + teams_received(
                st, people, who, lo, hi)
            with queue.conn:
                queue.note_sent(notes)
                queue.note_routine(sent)
                queue.note_inbound(heard)
                queue.set_meta("seeded", {"days": SEED_DAYS, "notes": len(notes),
                                          "inbound": len(heard)})
        day = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        done = progress.done()
        while day < until:
            nxt = min(day + timedelta(days=1), until)
            key = day.date().isoformat()
            if key not in done:
                if not wait_for_memory(floor_gb, wait_s, probe=probe):
                    status = "waiting_for_memory"
                    break
                ids = ap.window_ids(st, registry, ap._iso(max(day, since)), ap._iso(nxt))
                ids = _role_ids(st, ids, who)
                summary = ap.run_cycle(st, queue, ids, people=people, registry=registry,
                                       environ=env, now=nxt, as_of=day.date(),
                                       settle_items=False, only_owners=who,
                                       rate_by_event_hour=True,
                                       feed_lag=config.get("feed_lag") or {})
                if summary.get("status") in ("off", "blocked"):
                    status = summary["status"]
                    break
                summary["drained"] = ap._drain(st, queue, people, registry, nxt,
                                               only_owners=who)
                progress.finish(key, summary)
            day = nxt
        closes = answer_end(until, config["horizon"])
        last = end or data_end(st, ap._iso(closes))
        items = queue.items(sorted(who), ("open", "done", "settled", "dismissed"))
        ev_ids = sorted({i["event_id"] for i in items})
        events = {}
        for start in range(0, len(ev_ids), 500):
            for eid, body in queue.conn.execute(
                    "SELECT event_id, body FROM events WHERE event_id IN (SELECT value FROM "
                    "json_each(?))", (json.dumps(ev_ids[start:start + 500]),)):
                events[eid] = json.loads(body)
        actions: list[dict[str, Any]] = []
        a_until = ap._iso(min(closes, last))
        for provider in config["providers"]:
            actions += provider(st, people, who, ap._iso(since), a_until)
        anticipated: set[int] = set()
        ends = source_ends(st)
        flows = score(items, events, actions, horizon=config["horizon"], data_end=last,
                      since=since, until=until, anticipated=anticipated, source_end=ends)
        splits: dict[str, Any] = {}
        split_at = until - timedelta(days=held_out_days) if held_out_days else None
        if split_at is not None and since < split_at < until:
            for name, lo, hi in (("dev", since, split_at), ("held_out", split_at, until)):
                seen: set[int] = set()
                splits[name] = {"window": [ap._iso(lo), ap._iso(hi)],
                                "workflows": score(items, events, actions,
                                                   horizon=config["horizon"], data_end=last,
                                                   since=lo, until=hi, window_items=True,
                                                   anticipated=seen, source_end=ends)}
                splits[name]["misses"] = misses(queue, items, actions, seen, since=lo,
                                                until=hi, horizon=config["horizon"])
        missed = misses(queue, items, actions, anticipated, since=since, until=until,
                        horizon=config["horizon"])
        skipped: Counter = Counter()
        prepared: Counter = Counter()
        for s in progress.summaries():
            skipped.update(s.get("skipped") or {})
            prepared.update(s.get("prepared") or {})
            for k, v in (s.get("drained") or {}).items():
                (skipped if k.startswith("skipped:") else prepared)[
                    k.split(":", 1)[1] if k.startswith("skipped:") else k] += v
        days_done = sorted(progress.done())
    finally:
        queue.close()
        progress.close()
    result = {"schema": SCHEMA, "role": role, "status": status, "members": len(who),
              "window": [ap._iso(since), ap._iso(until)], "data_end": ap._iso(last),
              "horizon_hours": int(config["horizon"].total_seconds() // 3600),
              "days_done": len(days_done), "workflows": flows,
              "prepared_by_autopilot": dict(sorted(prepared.items())),
              "skipped": dict(sorted(skipped.items())), "actions": len(actions),
              "misses": missed, "splits": splits, "source": source,
              "scoring_rules": [dict(r) for r in SCORING_RULES],
              "source_ends": {k: ap._iso(v) for k, v in sorted(ends.items())},
              # when the feed held each prepared MC item: the capture's own first read of
              # it (live days) or the configured feed lag (days the capture backfilled)
              "feed_seen": dict(sorted(Counter(
                  e["seen_from"] for e in events.values() if e.get("seen_from")).items()))}
    # the gate is decided on the held-out days when the window has them (never tuned on)
    gated = splits["held_out"]["workflows"] if splits else flows
    result["gate"] = {"precision": str(ap.GATE_PRECISION), "min_judged": ap.GATE_MIN_JUDGED,
                      "on": "held_out" if splits else "window",
                      "passed": sorted(f for f, v in gated.items() if v["passes_gate"])}
    (root / "result.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    write_gate(out / ap.GATE_FILE, role, sorted(who), gated)
    return result


def write_gate(path: Path, role: str, who: Sequence[str],
               flows: Mapping[str, Mapping[str, Any]]) -> None:
    """The role's counts into the gate file (other roles kept); ``gate_from`` recomputes the
    verdict from them. Copy it beside the live queue to switch passed workflows on."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != ap.GATE_SCHEMA:
            raw = {}
    except (OSError, ValueError):
        raw = {}
    roles = dict(raw.get("roles") or {})
    roles[role] = {"members": list(who),
                   "workflows": {f: {"judged": v["judged"], "matched": v["matched"]}
                                 for f, v in flows.items() if v["judged"]}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": roles}, indent=1,
                              sort_keys=True))
    os.replace(tmp, path)


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot_replay")
    p.add_argument("--store", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--role", default=None,
                   help="owner, purchasing, a roles-file role, or person:<address or id>")
    p.add_argument("--days", type=float, default=30.0)
    p.add_argument("--until", default=None, help="window end (ISO), default the data's end")
    p.add_argument("--roles-file", default=None)
    p.add_argument("--capture-state", default=None)
    p.add_argument("--min-available-gb", type=float, default=9.0)
    p.add_argument("--wait-minutes", type=float, default=60.0)
    p.add_argument("--mem-max-gb", type=float, default=8.0)
    p.add_argument("--held-out-days", type=float, default=10.0,
                   help="the window's last days scored apart (the gate's test days); 0: none")
    p.add_argument("--source", choices=ap.STORE_SOURCES, default=None,
                   help="read the SQLite generation at --store, or the Postgres ontology "
                        "the API reads in mode 1 (refused when it does not hold that "
                        "generation); default: as PLX_ONTOLOGY_PG says")
    p.add_argument("--print-data-end", action="store_true",
                   help="print the store's last received mail (ISO) and its source; no run")
    a = p.parse_args(argv)
    if not a.print_data_end and not (a.role and a.out):
        p.error("--role and --out are required")
    cap_memory(a.mem_max_gb)
    config = load_config()
    store = Path(a.store)
    try:
        st = ap._store(a.store, a.source)
    except ostore.StoreError as error:
        print(json.dumps({"schema": SCHEMA, "role": a.role, "status": "blocked",
                          "reason": error.code, "detail": str(error)}))
        return 2
    if a.print_data_end:
        # with --until: also the data end a replay to then is judged to (its result's
        # ``data_end``), so a scorer reruns a finished role once more data came in
        try:
            ends = {"data_end": ap._iso(data_end(st, ap._iso(datetime.now(timezone.utc))))}
            if a.until:
                ends["judged_to"] = ap._iso(data_end(st, ap._iso(answer_end(
                    ap._instant(a.until), config["horizon"]))))
            print(json.dumps({**ends, **ap.store_source(st)}))
        finally:
            st.close()
        return 0
    people = pe._people(store)
    if a.capture_state and (Path(a.capture_state) / "mail-capture" / "capture.db").exists():
        from . import autopilot_work as aw
        st.autopilot_capture = aw.CaptureTexts(a.capture_state)
    if a.capture_state and (Path(a.capture_state) / "mc-capture" / "capture.sqlite").exists():
        from . import autopilot_work as aw
        st.autopilot_mc = aw.MCQuestions(a.capture_state)
    try:
        until = ap._instant(a.until) if a.until else data_end(
            st, ap._iso(datetime.now(timezone.utc)))
        since = until - timedelta(days=a.days)
        roles = ap._roles({ap.ROLES_ENV: a.roles_file} if a.roles_file else {},
                          Path(a.out))
        result = replay(st, people, role=a.role, out=Path(a.out), since=since, until=until,
                        roles=roles, config=config, floor_gb=a.min_available_gb,
                        wait_s=a.wait_minutes * 60, held_out_days=a.held_out_days)
    finally:
        st.close()
    print(json.dumps(result, indent=1, sort_keys=True))
    if result.get("status") == "waiting_for_memory":
        return EXIT_NO_MEMORY
    return 0 if result.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
