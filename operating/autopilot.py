"""Autopilot: every new message, task, approval, QC result, meeting and record change becomes
a typed event, and each event an autopilot declares prepares the work, ready for one click.

* **Event feed** (``discover``): after each row-delta cycle (``ontology.delta``) the records
  the newest delta layer owns that this feed has not taken in (``layer_ids``), or for a
  replay the store's event index over a time window (``window_ids``), are read and typed by
  the registry's ``sources`` (``operating/autopilot_registry.json``): an inbound mail or
  one-to-one Teams message (``message.received``, with its classified ``intents`` and the
  sender's ``side``), a task assigned to someone (``task.assigned``), an order, PO,
  receipt, shipment or invoice new or changed (``record.changed``), a QC result
  (``qc.result``), an approval request (``approval.requested``) and a meeting that ended
  (``meeting.ended``). An event's id is the digest of its type, record and version
  (a message: its internet message id and mailbox), so a record taken in twice is one
  event; each event is processed once (``Queue``: insert-or-ignore, ``processed`` mark).
* **Registry** (``load_registry``): an autopilot is ``on`` (event type) + ``when``
  (conditions on the event's general fields) -> ``handler`` (one of ``HANDLERS``: general
  preparation, never per record) -> ``owner`` (``OWNER_RULES``) -> ``due_minutes``. An
  unknown event type, handler, business rule or condition refuses the whole registry
  (``RegistryInvalid``): nothing runs on a registry this code does not understand.
* **Prepared work** (``Queue`` items): what happened, the prepared work (answer, drafted
  reply, dry-run write intent spec, impacted records, recommendation), the evidence (cited
  handles), the audiences of every record read and the markings. The queue is per person:
  ``visible`` shows an item only to its owner and to a manager the owner granted the
  owner's team (``inbox_triage.WorkloadGrants``), and only when that reader may read every
  audience, holds every marking and may open every cited handle (fail closed). ``settle``
  marks items done from evidence (the owner replied in the conversation, the approval was
  decided, the task closed, the lot released). ``digest`` gives a person's daily digest and
  nudges for items past due.
* **Approve** never sends or writes here: an item's draft is a card (``card_of``) that
  ``operator_api`` routes through ``card_actions.ActionDesk`` (preview, the owner's confirm,
  the people exclusion, the live-send gate); a write spec becomes a
  ``write_intents.WriteDesk`` intent prepared by the autopilot (dry run until the owner's
  flags and Vince's write scopes exist, and approved by a person who did not prepare it).

Safety: the people exclusion is read first and fails closed (unavailable: the cycle is
``blocked``; a mailbox, owner or participant it does not clear: no item; an MC approval
any person of its gate or task is not cleared for is never an event, by the fast feed's
rule: ``approvals_cleared``); mail work is
prepared only for the mailbox's owner (and To, not Cc) or a role owner; per-owner hourly
rate limit (``limits.per_owner_per_hour``: the rest wait for the next cycle); every handler
runs under a deadline (``limits.handler_seconds``, SQLite progress handler); kill switch
(``KILL_ENV`` or the ``KILL_FILE`` beside the queue): nothing is read or prepared, nothing is
approved. Reads only the store and delta layers; writes only its own queue file.

Run::

    python3 -m operating.autopilot follow --store STORE --queue QUEUE
    python3 -m operating.autopilot fast --state-dir STATE    # operating.autopilot_fast
    python3 -m operating.autopilot replay --store STORE --queue QUEUE --days 7 [--layer L]
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .citation_schemes import HANDLE_PREFIX
from operating import declared_schema as _schemas
from operating.declared_schema import autopilot_queue as _AUTOPILOT_QUEUE

from .autopilot_records import (
    OWN_POSTS_DAYS, _digest, _instant, _iso, _linked, _read, _v)
from .autopilot_queue import (
    CONDITIONS, DONE_RULES, EVENT_TYPES, HANDLER_TRIES, OWNER_RULES, QUEUE_FILE, Queue, ROLE_OWNER,
    RegistryInvalid, SCHEMA, SUMMARY_CHARS, killed, matches)
from .autopilot_events import (
    FEED_CONSUMER, FOLLOW_HORIZON, HORIZON_EVENTS, _people_maps, _value_window, handle_person)
from .autopilot_handlers import (
    HANDLERS, _meeting_extra, _roles, deciders, mc_project)
from .autopilot_engine import (
    DRAFT_CONTEXT_SECONDS, OrderedFeed, _happened, _learned_call, _prepare, _recipient_lessons,
    _recipients, _with_context, discover, draft_owners, event_queue_on, gate_from, item_roles,
    load_draft, pay_or_hr, settle, with_draft, SwitchGate)
from .autopilot_reading import (
    _plain, _store, digest)
from . import autopilot_engine, autopilot_handlers

REGISTRY_FILE = Path(__file__).with_name("autopilot_registry.json")
REPLAY_CONFIG_FILE = Path(__file__).with_name("autopilot_replay.json")


# ----------------------------------------------------------------------------- registry

def load_registry(path: Path | str | None = None) -> dict[str, Any]:
    """The registry, validated whole (module doc); anything unknown refuses it."""
    raw = json.loads(Path(path or REGISTRY_FILE).read_text(encoding="utf-8"))
    return validate_registry(raw)


def validate_registry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema") != "plx-autopilot-registry/v1":
        raise RegistryInvalid("registry_schema")
    sources = raw.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise RegistryInvalid("registry_sources")
    for otype, spec in sources.items():
        if not isinstance(spec, Mapping) or spec.get("event") not in EVENT_TYPES:
            raise RegistryInvalid(f"registry_source_event:{otype}")
    seen: set[str] = set()
    for ap in raw.get("autopilots") or ():
        if not isinstance(ap, Mapping) or not re.fullmatch(r"[a-z_]{2,40}", str(ap.get("id"))):
            raise RegistryInvalid("registry_autopilot_id")
        if ap["id"] in seen:
            raise RegistryInvalid(f"registry_autopilot_duplicate:{ap['id']}")
        seen.add(ap["id"])
        if ap.get("on") not in EVENT_TYPES:
            raise RegistryInvalid(f"registry_event:{ap['id']}")
        if ap.get("handler") not in HANDLERS:
            raise RegistryInvalid(f"registry_handler:{ap['id']}")
        owner = ap.get("owner")
        if owner not in OWNER_RULES and not ROLE_OWNER.match(str(owner)):
            raise RegistryInvalid(f"registry_owner:{ap['id']}")
        when = ap.get("when") or {}
        if not isinstance(when, Mapping) or set(when) - set(CONDITIONS):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        quiet = when.get("quiet_sender")
        if quiet is not None and not (
                isinstance(quiet, list) and len(quiet) == 3 and isinstance(quiet[0], int)
                and quiet[0] >= 1 and isinstance(quiet[1], (int, float))
                and 0 < quiet[1] <= 1 and isinstance(quiet[2], list)
                and set(quiet[2]) <= {"internal", "customer", "supplier", "unknown"}):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        share = when.get("answers_sender")
        if share is not None and not (
                isinstance(share, list) and len(share) == 2 and isinstance(share[0], int)
                and share[0] >= 1 and isinstance(share[1], (int, float))
                and 0 < share[1] <= 1):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        turn = when.get("after_own_post")
        if turn is not None and not (
                isinstance(turn, list) and len(turn) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) and x >= 1
                        for x in turn)):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        cc_min = when.get("cc_min")
        if cc_min is not None and not (isinstance(cc_min, int) and not isinstance(cc_min, bool)
                                       and cc_min >= 1):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        if ap.get("done_when") not in DONE_RULES:
            raise RegistryInvalid(f"registry_done:{ap['id']}")
        if not isinstance(ap.get("due_minutes"), int) or ap["due_minutes"] <= 0:
            raise RegistryInvalid(f"registry_due:{ap['id']}")
        for flag in ("enabled", "fallback"):
            if flag in ap and not isinstance(ap[flag], bool):
                raise RegistryInvalid(f"registry_{flag}:{ap['id']}")
        if "draft" in ap and (ap["draft"] != "model" or ap["on"] != "message.received"):
            raise RegistryInvalid(f"registry_draft:{ap['id']}")
        if ap["on"] == "record.due" and not isinstance(ap.get("params") or {}, Mapping):
            raise RegistryInvalid(f"registry_params:{ap['id']}")
        marks = ap.get("markings") or []
        if not isinstance(marks, list) or not all(isinstance(m, str) for m in marks):
            raise RegistryInvalid(f"registry_markings:{ap['id']}")
    limits = raw.get("limits") or {}
    for key in ("per_owner_per_hour", "handler_seconds", "cycle_seconds",
                "max_events_per_cycle"):
        if not isinstance(limits.get(key), (int, float)) or limits[key] <= 0:
            raise RegistryInvalid(f"registry_limit:{key}")
    return dict(raw)


def answer_window_hours() -> int | None:
    """The answer window the replay judges an item in (``autopilot_replay.json``
    ``horizon_hours``, working hours): the window ``answers_sender`` counts a person's
    past answers in, so a rule asks of the past what the gate asks of the item. Unreadable
    or out of range: None (``answers_sender`` then never passes: fail closed)."""
    try:
        raw = json.loads(REPLAY_CONFIG_FILE.read_text(encoding="utf-8"))
        hours = raw.get("horizon_hours")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 24 * 14:
        return None
    return hours


def window_ids(st: Any, registry: Mapping[str, Any], since: str, until: str) -> list[str]:
    """The records the store's event index names in [since, until) (a replay's feed)."""
    kinds = list(registry.get("store_events") or ())
    # the events first (CROSS JOIN keeps that order): the store has no time index, and the
    # planner's choice, every event-object pair by object, read about 7 times as much
    # (measured on the 2026-11 store: 165 s against 24 s for one day, the same records)
    rows = st.conn.execute(
        "SELECT DISTINCT eo.object_id FROM events e CROSS JOIN event_objects eo ON "
        "eo.event_id = e.event_id WHERE e.event_type IN (SELECT value FROM json_each(?)) AND "
        "e.time >= ? AND e.time < ?", (json.dumps(kinds), since, until)).fetchall()
    ids = {r[0] for r in rows}
    # a task event names its task: the task is the record the feed types
    tasks = [o for o in ids if o.startswith("MCTaskEvent:")]
    if tasks:
        for oid in tasks:
            ids.update(other for _t, other in _linked(st, oid) if other.startswith("MCTask:"))
    ids.update(_value_window(st, registry, since, until))
    return sorted(ids)


def layer_ids(store: Path, queue: Queue, stamp: str | None,
              cap: int | None = None) -> tuple[set[str], list[str], int]:
    """(the records the served delta layers own that this feed has not taken in yet, those
    layers, how many records of them were taken in unread). Only from a store that holds
    them: Postgres (``stamp`` its ``<digest>@<data version>``, the generation is the part
    before the ``@``), and there only the layers it has applied (a layer it has not is
    taken in by a later cycle). The SQLite generation file alone holds none of the layers'
    records: no layer is taken in, since one taken in unread would lose its events. A layer
    that would bring the records read past ``cap`` (by the size the pointer states) is
    taken in without being opened (the window feed covers it, ``follow_ids``); one whose
    size it does not state is read only as the cycle's first layer."""
    from . import delta_overlay
    from .proactive_incremental import _applied, generation_digest
    applied = _applied(store)
    if applied is None:
        return set(), [], 0
    digest = generation_digest(stamp)
    try:
        raw = json.loads(delta_overlay.pointer_path(store).read_text())
    except (OSError, ValueError):
        return set(), [], 0
    if not isinstance(raw, dict) or not digest or raw.get("base_digest") != digest:
        return set(), [], 0
    layers = [(str(x["store"]), x.get("owned")) for x in raw.get("layers") or ()
              if isinstance(x, dict) and x.get("store")]
    if not layers and raw.get("store"):
        layers = [(str(raw["store"]), None)]
    seen = {row[0] for row in queue.conn.execute(
        "SELECT layer FROM layers WHERE base_digest = ?", (digest,))}
    owned: set[str] = set()
    taken: list[str] = []
    unread = read = 0
    for layer, size in layers:
        if layer in seen or Path(layer).name not in applied:
            continue
        known = isinstance(size, int)
        if cap is not None and (read + size > cap if known else bool(taken)):
            taken.append(layer)
            unread += size if known else 0
            continue
        try:
            with contextlib.closing(sqlite3.connect(f"file:{layer}?mode=ro", uri=True)) as c:
                owned.update(r[0] for r in c.execute("SELECT object_id FROM delta_owned"))
        except sqlite3.Error:
            continue     # pruned or unreadable now: a later layer holds its records
        taken.append(layer)
        read += size if known else len(owned)
    return owned, taken, unread


# Live follow's feed (``follow_ids``). The served delta layers each own every record changed
# since the generation (2026-11-13: 0.66-0.8 M records a layer), so typing a layer's records
# every cycle is not a feed: above ``FOLLOW_LAYER_CAP`` a layer is taken in unopened and the
# feed is the store's event index over the last ``FOLLOW_HORIZON``, the very feed the replay
# gate scores (``window_ids``). A cycle reads the whole horizon whenever the store changed
# since the last one (its content stamp: Postgres's moves with every applied layer), so a
# record that lands up to the horizon after its own time is still seen (an event read twice
# is one event); a store unchanged holds nothing new and is not read again (a re-read of the
# horizon took 141 s of the cycle's 240 s handling budget on 2026-11-13, every 5 minutes).
# A message or meeting older than the horizon is not prepared (``run_cycle``
# ``horizon``): the replay judges an answer within ``REPLY_HOURS``, and a first cycle would
# otherwise prepare a week of answered mail.
FOLLOW_LAYER_CAP = 50_000


def feed_ids(store: Path, queue: Queue, stamp: str | None) -> tuple[Any, set[str],
                                                                    list[str], int] | None:
    """(the feed batch, its records, the applied layers it covers, how many records were
    left to the window) when the store read is Postgres and the feed reads ``ok``; None:
    the layers are read (``layer_ids``)."""
    from . import delta_overlay
    from .proactive_incremental import _applied, generation_digest
    from operating.pg import change_feed, ontology_reader
    applied = _applied(store)
    digest = generation_digest(stamp)
    if applied is None or not digest:
        return None
    batch = change_feed.read(ontology_reader.dsn(), FEED_CONSUMER, generation=digest,
                             limit=FOLLOW_LAYER_CAP)
    if batch is None:
        return None
    if not batch.ready:
        return batch, set(), [], 0
    try:
        raw = json.loads(delta_overlay.pointer_path(store).read_text())
    except (OSError, ValueError):
        raw = {}
    layers = [str(x["store"]) for x in (raw.get("layers") or ()) if isinstance(x, dict)
              and x.get("store")] if isinstance(raw, dict) \
        and raw.get("base_digest") == digest else []
    seen = {row[0] for row in queue.conn.execute(
        "SELECT layer FROM layers WHERE base_digest = ?", (digest,))}
    taken = [x for x in layers if x not in seen and Path(x).name in applied]
    if batch.overflow or batch.migrated:
        skipped = batch.skipped()
        return skipped, set(), taken, len(batch.changes)
    return batch, batch.ids(), taken, 0


def follow_ids(st: Any, store: Path, queue: Queue, registry: Mapping[str, Any],
               now: datetime) -> tuple[set[str], list[str], dict[str, Any], Any]:
    """(the records one live cycle types, the layers it takes in, what it read, the
    change feed batch to commit once they are taken in, or None)."""
    found = feed_ids(store, queue, st.digest)
    batch = found[0] if found is not None else None
    if batch is not None and batch.ready:
        _batch, ids, taken, unread = found
    else:
        ids, taken, unread = layer_ids(store, queue, st.digest, FOLLOW_LAYER_CAP)
    read: dict[str, Any] = {"layers": len(taken), "layer_records": len(ids),
                            "layer_records_unread": unread}
    if batch is not None:
        read["feed"] = batch.summary()
    if st.digest and queue.meta("follow_stamp") == st.digest:
        read["window"], read["window_records"] = None, 0     # the store is unchanged
        return set(ids), taken, read, batch
    since = now - FOLLOW_HORIZON
    window = window_ids(st, registry, _iso(since), _iso(now))
    read["window"], read["window_records"] = [_iso(since), _iso(now)], len(window)
    return set(ids) | set(window), taken, read, batch


def _role_members(role: str, roles: Mapping[str, list[str]],
                  directory: Mapping[str, str]) -> set[str]:
    """A role's people (the roles file); the owner role falls back to the approver roster."""
    return set(roles.get(role) or (_approvers(directory) if role == "owner" else ()))


def _approvers(directory: Mapping[str, str]) -> list[str]:
    path = Path(__file__).with_name("approver_roster.json")
    try:
        roster = json.loads(path.read_text(encoding="utf-8")).get("approvers") or {}
    except (OSError, ValueError):
        return []
    out = []
    for address, who in roster.items():
        oid = (who or {}).get("oid") or directory.get(str(address).lower())
        if oid:
            out.append(str(oid).lower())
    return sorted(set(out))


def owners_of(rule: str, event: Mapping[str, Any], *, directory: Mapping[str, str],
              addresses_of: Mapping[str, set[str]], roles: Mapping[str, list[str]],
              decided: set[str] | None = None) -> tuple[list[str], str | None]:
    """(owner oids, None) or ([], the reason no owner is known: no item). ``decided``: who
    decided approvals in the event's MC project in the 30 days before it (``deciders``):
    they get an approval; none: every approver."""
    if rule == "recipient":
        if event.get("event_type") != "message.received":
            return [], "owner_rule_mismatch"
        owner = str(event.get("mailbox") or "")
        if not owner or not (addresses_of.get(owner) or event.get("channel") == "teams"):
            return [], "owner_unknown"
        if not event.get("to_me") and not event.get("unaddressed") and not (
                event.get("named_me") and event.get("asks")):
            # Cc or Bcc: the To owner prepares it, unless the message asks this person
            # by name ("Vince, can you ...")
            return [], "owner_not_addressed"
        return [owner], None
    if rule == "assignee":
        found = []
        for a in event.get("assignees") or ():
            oid = a if re.fullmatch(r"[0-9a-f-]{36}", a) else directory.get(a)
            if oid:
                found.append(oid)
        return (sorted(set(found)), None) if found else ([], "assignee_unknown")
    if rule == "approvers":
        found = sorted(decided) if decided else _approvers(directory)
        # a request goes to someone other than the person who asked for it whenever anyone
        # else can decide: the project's other recent deciders, else the other approvers
        # (3 of them in projects only he had decided in); the asker keeps it only
        # when no one else can decide. An asker the directory does not know changes nothing.
        asker = str(directory.get(str((event.get("fields") or {}).get("requested_by") or "")
                                  .strip().lower()) or "").lower()
        if asker and asker in {o.lower() for o in found}:
            others = [o for o in found if o.lower() != asker] or [
                o for o in _approvers(directory) if o.lower() != asker]
            found = others or found
        return (found, None) if found else ([], "approvers_unknown")
    if rule == "addressee":
        if event.get("event_type") != "question.asked":
            return [], "owner_rule_mismatch"
        owner = handle_person(str(event.get("addressee") or ""), directory,
                              event.get("addressee_domains") or ())
        return ([owner], None) if owner else ([], "addressee_unknown")
    if rule == "organizer":
        owner = str(event.get("mailbox") or "")
        if not owner or directory.get(str(event.get("organizer") or "")) != owner:
            return [], "not_organizer_copy"
        return [owner], None
    if rule == "buyer":
        hint = str(event.get("owner_hint") or "")
        if hint and hint in addresses_of:
            return [hint], None                   # who sent the PO to the supplier
        found = roles.get("purchasing") or []
        return (found, None) if found else ([], "buyer_unknown")
    if rule == "hinted":
        # the person the records show doing this work for this party (``owner_hint``),
        # else nobody: never a whole role for one record
        hint = str(event.get("owner_hint") or "")
        return ([hint], None) if hint and hint in addresses_of else ([], "owner_unknown")
    if rule.startswith("role:"):
        found = roles.get(rule[len("role:"):]) or _approvers(directory)
        return (found, None) if found else ([], "role_unassigned")
    return [], "owner_rule_unknown"


def run_cycle(st: Any, queue: Queue, ids: Iterable[str], *, people: Any,
              registry: Mapping[str, Any] | None = None, as_of: date | None = None,
              environ: Mapping[str, str] | None = None, now: datetime | None = None,
              landed_at: str | None = None, settle_items: bool = True,
              only_owners: Iterable[str] | None = None,
              gate: Callable[..., bool] | None = None,
              rate_by_event_hour: bool = False,
              feed_lag: Mapping[str, timedelta] | None = None,
              read_work_log: bool = False, event_queue: bool = False,
              horizon: datetime | None = None, discover_store: Any = None,
              due: bool = True, ask_judge: Any = None) -> dict[str, Any]:
    """One cycle: type the changed records, store each event once, prepare the work of each
    new event every matching autopilot declares, settle items their evidence closed.
    ``only_owners``: prepare only for these people (a replay of one role); ``gate(owner,
    workflow, roles)``: prepare only what passes it (the live cycle and a replay into a
    queue: the workflows a role's replay passed, in that role, ``gate_from``).
    ``rate_by_event_hour``: the hourly cap counts each event in the hour it happened (a
    replay runs one cycle per past day; live, a cycle's hour is the one it runs in).
    ``read_work_log``: skip a message the owner already answered from the Workbench
    (``Queue.answered_in_workbench``). Live only: the ``follow`` command alone passes it;
    every other caller (a replay, its catch-up ``_drain``, tests) never opens the log, so a
    replay never learns what the Workbench did after the day it judges.
    ``event_queue``: hand the events out through the ordered event queue when
    ``PLX_AUTOPILOT_EVQ`` is on (``OrderedFeed``). Live only: the ``follow`` command alone
    passes it, so a replay or its catch-up never touches the live queue's order.
    ``horizon``: a message or meeting before it (or with no time) is not stored as an event
    (live follow, ``FOLLOW_HORIZON``); a sender's history still counts it.
    ``discover_store``: the records ``ids`` are typed in this store instead (the fast feed's
    scratch store of just-captured messages, ``discover`` ``base``); handled over ``st``.
    ``due`` False: no due-date events this cycle (the fast feed; follow reads them).
    ``ask_judge``: a ``chat_ask_model.Judge`` that reads whether a chat message the cheap
    rules leave (``ask_check``) asks its reader something, for a rule that needs an ask
    (``when.asks``): such events wait in a batch, one model call per batch, then are
    processed (``_judge_asks``); None (a replay unless it passes one, ``chat_ask_model``
    off): every such message is kept."""
    environ = os.environ if environ is None else environ
    registry = registry or load_registry()
    now = now or datetime.now(timezone.utc)
    as_of = as_of or now.date()
    started = time.monotonic()
    summary: dict[str, Any] = {"schema": SCHEMA, "status": "complete", "events_new": 0,
                               "events_seen": 0, "prepared": Counter(), "skipped": Counter(),
                               "sent": 0, "writes": 0}
    if killed(queue, environ):
        summary["status"] = "off"
        return _plain(summary)
    if people is None or getattr(people, "unavailable", None):
        summary["status"] = "blocked"          # the people exclusion fails closed
        summary["reason"] = "people_exclusion_unavailable"
        return _plain(summary)
    limits = registry["limits"]
    outbound: list[dict[str, Any]] = []
    events, skipped = discover(discover_store or st, ids, registry, people,
                               landed_at=landed_at, outbound=outbound, now=now,
                               feed_lag=feed_lag,
                               base=st if discover_store is not None else None)
    with queue.conn:
        summary["sent_noted"] = queue.note_sent(outbound)
        queue.note_routine(outbound)
        # a group message no one addressed to the owner is not a message to them: a
        # sender's answered share (``reply_history``) never counts it
        queue.note_inbound(e for e in events if e.get("event_type") == "message.received"
                           and not e.get("unaddressed"))
    if any(e.get("unaddressed") for e in events):
        # only a member who wrote in the conversation in the turn rule's days can have a
        # turn there (``own_posts`` decides, from the messages before the event): the
        # others' copies are never stored
        oldest = min((_instant(e.get("at")) for e in events if e.get("unaddressed")
                      and _instant(e.get("at"))), default=now)
        wrote = queue.writers(_iso(oldest - timedelta(days=OWN_POSTS_DAYS)))
        events = [e for e in events if not e.get("unaddressed") or (
            str(e.get("mailbox") or ""), _digest("thread", e.get("conversation_id"))) in wrote]
    if horizon is not None:
        kept = []
        for e in events:
            at = _instant(e.get("at")) if e.get("event_type") in HORIZON_EVENTS else horizon
            if at is None or at < horizon:
                skipped["before_horizon"] += 1
            else:
                kept.append(e)
        events = kept
    discovered = time.monotonic()
    summary["skipped"].update(skipped)
    directory, addresses_of, domains = _people_maps(people)
    roles = _roles(environ, queue.path.parent)
    stamp = _iso(now)
    rd_day = collections_day = None
    if due:
        try:
            from . import autopilot_work as aw
            events += aw.po_due_events(st, registry, as_of, domains, directory, addresses_of)
            events += aw.invoice_due_events(st, registry, as_of, domains, directory)
            events += aw.routine_due_events(st, queue, registry, as_of, people)
            # a project's state is read as it stood at the day's start: once a day is enough
            # (its 90-day mail read is the slowest of these)
            if queue.meta("rd_due_day") != as_of.isoformat():
                events += aw.rd_due_events(st, registry, as_of, people)
                rd_day = as_of.isoformat()
            from . import autopilot_receivables as ar
            events += ar.due_events(st, registry, as_of, people)
            from . import autopilot_payables as pay
            events += pay.due_events(st, registry, as_of, people)
            # the customers' books and payment mail are read once a day (the slowest read
            # here after the R&D one); a customer's reminder is due at most once a week
            from . import autopilot_collections as ac
            if ac._spec(registry) is not None and \
                    queue.meta("collections_due_day") != as_of.isoformat():
                # off, it reads nothing and marks no day (switched on later that day, it runs)
                events += ac.collection_events(st, registry, as_of, people)
                collections_day = as_of.isoformat()
        except sqlite3.Error:
            summary["skipped"]["due_events_unreadable"] += 1
    with queue.conn:
        for e in events:
            summary["events_seen"] += 1
            summary["events_new"] += queue.add_event(e, stamp)
        if rd_day:
            queue.set_meta("rd_due_day", rd_day)
        if collections_day:
            queue.set_meta("collections_due_day", collections_day)
    named = name_teams_items(st, queue, directory, skipped=summary["skipped"],
                             ask_rules={a["id"] for a in registry["autopilots"]
                                        if (a.get("when") or {}).get("asks") is True},
                             now=stamp, judge=ask_judge, people=people,
                             addresses_of=addresses_of)
    if named:
        summary["teams_items_named"] = named
    retitled = retitle_old_labels(queue, registry)
    if retitled:
        summary["items_retitled"] = retitled
    ordered = None
    if event_queue and event_queue_on(environ):
        try:
            ordered = OrderedFeed(queue, environ)
        except Exception as error:  # noqa: BLE001 - fail closed: the events wait, unordered never
            summary["status"] = "blocked"
            summary["reason"] = f"event_queue_unavailable:{type(error).__name__}"
            return _plain(summary)
    feed = ordered.events(int(limits["max_events_per_cycle"])) if ordered else \
        iter(queue.pending(int(limits["max_events_per_cycle"])))
    answered_rule = any("answers_sender" in (a.get("when") or {})
                        for a in registry.get("autopilots") or ())
    window = answer_window_hours() if answered_rule else None
    asking = [a for a in registry["autopilots"] if (a.get("when") or {}).get("asks") is True]
    waiting: list[tuple[dict[str, Any], tuple[str, str, str]]] = []

    def handle(event: dict[str, Any]) -> None:
        state, reason = _process(st, queue, event, registry, people=people,
                                 directory=directory, addresses_of=addresses_of, roles=roles,
                                 domains=domains, as_of=as_of, now=now, summary=summary,
                                 only_owners=None if only_owners is None else set(only_owners),
                                 gate=gate, rate_by_event_hour=rate_by_event_hour,
                                 read_work_log=read_work_log, over=discover_store)
        with queue.conn:
            queue.mark(event["event_id"], state, reason, stamp)
        if ordered:
            ordered.settle(event["event_id"], state, reason)   # after the mark: a crash between redelivers

    def judged() -> None:
        if waiting:
            _judge_asks(ask_judge, queue, waiting, people, stamp, summary)
            for held, _who in waiting:
                handle(held)
            waiting.clear()

    while True:
        if time.monotonic() - started > limits["cycle_seconds"]:
            summary["status"] = "partial"      # the rest stay new: the next cycle goes on
            break
        event = next(feed, None)
        if event is None:
            break
        _meeting_extra(st, event, domains)
        if event.get("event_type") == "message.received" and event.get("mailbox") and \
                event.get("at"):
            event.update(queue.engagement(str(event["mailbox"]), event.get("conversation_id"),
                                          str(event.get("sender") or ""), str(event["at"])))
            event["sender_history"] = list(queue.reply_history(
                str(event["mailbox"]), str(event.get("sender") or ""), str(event["at"])))
            # read only when a rule asks for it (``answers_sender``): one more history
            # read per message otherwise spent on every mail
            event["sender_answered"] = list(queue.reply_history(
                str(event["mailbox"]), str(event.get("sender") or ""), str(event["at"]),
                horizon_hours=window, working=True)) if window and answered_rule else [0, 0]
            event["own_posts"] = list(queue.own_posts(
                str(event["mailbox"]), event.get("conversation_id"), str(event["at"])))
        who = _ask_wanted(queue, event, asking, now=now, live=read_work_log,
                          addresses_of=addresses_of) if ask_judge is not None else None
        if who is not None:
            waiting.append((event, who))       # read by the model with its batch, then handled
            if len(waiting) >= ask_judge.batch:
                judged()
            continue
        handle(event)
    judged()        # also after a partial cycle: an event taken from the feed is handled
    if ordered:
        if ordered.unknown:
            summary["skipped"]["unknown_event_type"] += ordered.unknown
        ordered.close()
    summary["discover_seconds"] = round(discovered - started, 2)
    if settle_items:
        summary["settled"] = settle(st, queue, registry, people, now=now)
    summary["seconds"] = round(time.monotonic() - started, 2)
    return _plain(summary)


def _reader_names(owner: str, addresses_of: Mapping[str, Any]) -> list[str]:
    """A person's first names as their mail addresses start (``jane.doe@`` -> ``jane``;
    an object id names no one)."""
    from .autopilot_events import _first_names
    return sorted(_first_names(a for a in (owner, *(addresses_of.get(owner) or ()))
                               if "@" in str(a)))


def _ask_wanted(queue: Queue, event: Mapping[str, Any], asking: Sequence[Mapping[str, Any]],
                *, now: datetime, live: bool, addresses_of: Mapping[str, Any]
                ) -> tuple[str, str, str] | None:
    """``(key, reader, reader's first name)`` when the model must read whether ``event``
    asks its reader something before it is handled: a chat message the cheap rules left
    (``ask_check``), to its reader, that a rule needing an ask (``asking``) would otherwise
    take, with no item for it yet and (live) not answered already. A verdict already cached
    is put on the event (``asks``) and None returned: no call."""
    if event.get("ask_check") != "model" or not event.get("asks") or \
            event.get("unaddressed") or not event.get("to_me") or not event.get("cleared"):
        return None
    reader = str(event.get("mailbox") or "")
    message = str(event.get("message_id") or event.get("object_id") or "")
    rules = [a for a in asking if a.get("enabled") is not False and
             a.get("on") == event.get("event_type") and matches(a.get("when") or {}, event)]
    if not reader or not message or not rules:
        return None
    cached = queue.chat_ask(message, reader)
    if cached is not None:
        event["asks"] = cached          # type: ignore[index]
        return None
    if all(queue.item("ap_" + _digest(a["id"], event["event_id"], reader)[:24]) is not None
           for a in rules):
        return None
    names = _reader_names(reader, addresses_of)
    if live and queue.answered_in_chat(reader, event, now, names):
        return None                     # skipped as answered when handled: no call
    return f"{message}|{reader}", reader, (names[0].capitalize() if names else "")


def _judge_asks(judge: Any, queue: Queue, waiting: Sequence[tuple[dict[str, Any],
                tuple[str, str, str]]], people: Any, stamp: str,
                summary: dict[str, Any]) -> None:
    """The model's verdicts on the waiting events, put on each (``asks``; any failure:
    True, kept); counts in ``summary["chat_ask"]``."""
    before = dict(judge.stats)
    try:
        got = judge.ask(queue, [(key, event, reader, name)
                                for event, (key, reader, name) in waiting], people, stamp)
    except Exception:  # noqa: BLE001 - any failure keeps every message (fail open)
        got = {}
        summary["skipped"]["chat_ask_judge_failed"] += len(waiting)
    for event, (key, _reader, _name) in waiting:
        event["asks"] = got.get(key, True)
        if not event["asks"]:
            summary["skipped"]["chat_ask_no"] += 1
    counts = summary.setdefault("chat_ask", {})
    for k, v in judge.stats.items():
        counts[k] = round(counts.get(k, 0) + v - before.get(k, 0), 2)


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


# workflow labels an item's title may still start with (the registry's earlier wording),
# by autopilot id: the open items are re-titled to the registry's label now
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


def _process(st: Any, queue: Queue, event: dict[str, Any], registry: Mapping[str, Any], *,
             people: Any, directory: Mapping[str, str], addresses_of: Mapping[str, set[str]],
             roles: Mapping[str, list[str]], domains: Sequence[str], as_of: date,
             now: datetime, summary: dict[str, Any], only_owners: set[str] | None = None,
             gate: Callable[..., bool] | None = None, rate_by_event_hour: bool = False,
             read_work_log: bool = False, over: Any = None) -> tuple[str, str | None]:
    """``over``: the fast feed's scratch store, read first for an MC approval's closure."""
    from . import workflows as wf
    limits = registry["limits"]
    drafting = queue.__dict__.setdefault("_draft_context", {})
    if event.get("event_type") == "message.received" and not event.get("cleared"):
        summary["skipped"]["participants_not_cleared"] += 1
        return "skipped", "participants_not_cleared"
    if pay_or_hr(event):
        summary["skipped"]["pay_or_hr"] += 1       # never prepared, never shown
        return "skipped", "pay_or_hr"
    deferred = False
    reasons = []
    failed: list[str] = []     # handler failures and timeouts: the event is tried again
    for ap in registry["autopilots"]:
        if ap["on"] != event["event_type"] or not matches(ap.get("when") or {}, event):
            continue
        if ap.get("enabled") is False:
            continue                       # switched off in the registry (``off_reason``)
        when = ap.get("when") or {}
        if event.get("unaddressed") and "after_own_post" not in when:
            continue    # a group message not addressed to the owner: only a turn rule answers it
        box = str(event.get("mailbox") or "")
        role = when.get("mailbox_role")
        if role and box not in _role_members(role, roles, directory):
            continue
        role = when.get("mailbox_role_none")
        if role and box in _role_members(role, roles, directory):
            continue
        decided = None
        if ap["owner"] == "approvers":
            try:
                project = mc_project(str(event.get("audience") or ""))
                decided = deciders(st, directory, min(_instant(event.get("at")) or now, now),
                                   project=project) if project else None
            except sqlite3.Error:
                decided = None
        owners, why = owners_of(ap["owner"], event, directory=directory,
                                addresses_of=addresses_of, roles=roles, decided=decided)
        if why:
            summary["skipped"][f"{ap['id']}:{why}"] += 1
            reasons.append(why)
            continue
        owners = [o for o in owners if people.identity_clear(o)]
        if not owners:
            summary["skipped"][f"{ap['id']}:owner_not_cleared"] += 1
            continue
        if only_owners is not None:
            owners = [o for o in owners if o in only_owners]
            if not owners:
                continue                       # another role's work (a role's replay)
        if gate is not None:
            workflow = wf.of_item(ap["id"], event)
            owners = [o for o in owners if gate(o, workflow, item_roles(ap))]
            if not owners:
                summary["skipped"][f"{ap['id']}:workflow_not_passed"] += 1
                continue
        if ap.get("fallback"):
            # only for an owner no other autopilot prepared this event for
            owners = [o for o in owners if not queue.conn.execute(
                "SELECT 1 FROM items WHERE event_id = ? AND owner = ? LIMIT 1",
                (event["event_id"], o)).fetchone()]
            if not owners:
                continue
        happened = _instant(event.get("at")) if rate_by_event_hour else None
        hour = (happened if happened and happened <= now else now).strftime("%Y-%m-%dT%H")
        prepared = None
        for owner in owners:
            item_id = "ap_" + _digest(ap["id"], event["event_id"], owner)[:24]
            if queue.item(item_id) is not None:
                continue
            if queue.thread_item_open(owner, ap["id"], event):
                summary["skipped"][f"{ap['id']}:thread_item_open"] += 1
                continue
            # live only, like the Workbench check below: a replay's cycles are coarser than
            # live ones, so its clock would see the answer it scores the item against
            answered = queue.answered_in_chat(owner, event, now,
                                              _reader_names(owner, addresses_of)) \
                if read_work_log and ap.get("done_when") == "owner_replied" else None
            if answered:
                summary["skipped"][f"{ap['id']}:{answered}"] += 1
                continue
            if read_work_log and queue.answered_in_workbench(
                    {owner, *addresses_of.get(owner, ())}, event, now):
                # live only (an explicit opt-in): a replay never reads the Workbench
                summary["skipped"][f"{ap['id']}:answered_in_workbench"] += 1
                continue
            learned = _lessons(queue)
            owner_roles = [r for r in roles if owner in _role_members(r, roles, directory)] \
                if learned else []
            left = _learned_call(learned.skip_reason, owner, owner_roles, ap["id"], event,
                                 default=None) if learned else None
            if left:
                # the owner leaves items of this kind (or from this sender): a lesson with
                # its evidence (``operating.learning``), never a guess
                summary["skipped"][f"{ap['id']}:learned_left"] += 1
                reasons.append("learned_left")
                continue
            if not queue.rate_take(owner, hour, int(limits["per_owner_per_hour"])):
                summary["skipped"][f"{ap['id']}:rate_limited"] += 1
                deferred = True
                continue
            if prepared is None:
                # the fast feed's approval reads its closure from the scratch store first
                prepared = _prepare(st, ap, event, as_of, domains,
                                    float(limits["handler_seconds"]),
                                    over=over if event.get("object_type") == "MCApproval"
                                    else None)
            if prepared.get("skip"):
                summary["skipped"][f"{ap['id']}:{prepared['skip']}"] += 1
                reasons.append(prepared["skip"])
                break
            if prepared.get("error"):
                summary["skipped"][f"{ap['id']}:{prepared['error']}"] += 1
                reasons.append(prepared["error"])
                failed.append(prepared["error"])
                break
            work = _learned_call(learned.apply_draft, prepared["work"], owner, owner_roles,
                                 event=event, autopilot=ap["id"],
                                 default=prepared["work"]) \
                if learned else prepared["work"]
            if ap.get("draft") == "model" and queue.path.parent.name == "autopilot":
                # the live queue's reply drafts are the model's (``autopilot_drafter``):
                # none worth sending, no item (a replay scores the handler's work)
                work, why = _model_draft(queue, drafting, summary, event, owner, work,
                                         people, addresses_of)
                if why == "draft_busy":
                    summary["skipped"][f"{ap['id']}:draft_busy"] += 1
                    deferred = True
                    continue
                if why == "draft_failed":
                    summary["skipped"][f"{ap['id']}:draft_failed"] += 1
                    failed.append(why)
                    continue
                if why:
                    summary["skipped"][f"{ap['id']}:{why}"] += 1
                    reasons.append(why)
                    continue
            else:
                work = {k: v for k, v in work.items() if k != "material"}
            # who each email draft goes to, decided for this owner on the one recipient
            # path (``assist.resolve_reply``), never as the handler wrote it
            work = _recipients(work, event, owner, people,
                               _recipient_lessons(learned, owner))
            work, used = _with_context(_draft_context(queue, drafting), owner, ap["id"],
                                       work, event)
            due = now + timedelta(minutes=int(ap["due_minutes"]))
            landed = _instant(event.get("seen_at") or event.get("landed_at") or event.get("at"))
            with queue.conn:
                queue.add_item({
                    "item_id": item_id, "event_id": event["event_id"], "autopilot": ap["id"],
                    "owner": owner, "title": f"{ap['label']}: "
                    + str(work.get("summary") or "")[:SUMMARY_CHARS],
                    "body": {"happened": _happened(event), "prepared": work,
                             "done_when": ap["done_when"], "label": ap["label"],
                             **({"context_used": used} if used else {})},
                    "audiences": prepared["audiences"],
                    "markings": sorted(set(ap.get("markings") or ()) | prepared["markings"]),
                    "citations": prepared["citations"], "due_at": _iso(due),
                    "created_at": _iso(now), "prepare_ms": prepared["ms"],
                    "latency_s": round((now - landed).total_seconds(), 1) if landed else None})
            summary["prepared"][ap["id"]] += 1
    if failed:
        # what was prepared stands (an item is made once per event and owner); the event
        # is tried again, up to HANDLER_TRIES, then left failed
        why = ",".join(sorted(set(failed)))
        with queue.conn:
            tries = queue.tried(event["event_id"])
        if tries >= HANDLER_TRIES:
            summary["skipped"]["handler_failed_for_good"] += 1
            return "failed", why
        return "deferred", why
    if deferred:
        return "deferred", "rate_limited"
    return "processed", ",".join(sorted(set(reasons))) or None


# What a "your call" card keeps of its event: what the writer's screens read again when
# the person answers it (``autopilot_drafter.screened``: clearance, the people exclusion,
# pay or HR, Freightcom) and the reply's place.
CARD_EVENT = ("object_id", "channel", "sender", "sender_name", "subject", "cleared",
              "audience", "records", "side", "to", "cc", "conversation_id", "intents")


def _model_draft(queue: Queue, held: dict[str, Any], summary: Mapping[str, Any],
                 event: Mapping[str, Any], owner: str, work: Mapping[str, Any], people: Any,
                 addresses_of: Mapping[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """(``work`` with the model's reply as its draft, None) or (work, why there is no
    item): ``draft_model_off`` (no model named), ``draft_busy`` (this cycle's calls are
    spent: the event waits), ``draft_failed`` (tried again), or the writer's skip
    (``nothing_useful``, ``figure_not_in_material``, a screen). Work that stands without
    a reply (a write to confirm, documents to attach) keeps its item without one."""
    from . import autopilot_drafter as ad
    from .autopilot_work import first_name
    material = work.get("material") if isinstance(work.get("material"), Mapping) else {}
    out = {k: v for k, v in work.items() if k != "material"}
    stands = bool(out.get("write") or out.get("attachments"))
    if held.get("cycle") is not summary:
        held["cycle"] = summary
        if "writer" not in held:
            held["writer"] = ad.from_environ(os.environ)
        if held["writer"] is not None:
            held["writer"].new_cycle()
    writer = held.get("writer")
    if writer is None:
        return out, None if stands else "draft_model_off"
    if writer.busy:
        return out, "draft_busy"
    mine = sorted(addresses_of.get(owner) or ())
    text = str(material.get("text") or event.get("text") or "")
    found = [str(work[k]["body"]) for k in ("draft", "teams_draft")
             if isinstance(work.get(k), Mapping) and work[k].get("body")]
    sent_to = {str(a).lower() for a in [*(event.get("to") or ()), *(event.get("cc") or ())]}
    own = next((a for a in mine if a in sent_to), mine[0] if mine else "")
    m = ad.Material(
        reader=(ad.name_in(text, own) or first_name(own) or "") if own else "",
        address=own, relation=ad.relation_of(event),
        channel="teams" if event.get("channel") == "teams" else "mail",
        sender=autopilot_engine.sender_label(event.get("sender"), event.get("sender_name")),
        subject=str(event.get("subject") or ""), text=text,
        lines=[f"What our records state: {b}" for b in found],
        account=[str(a) for a in material.get("account") or ()],
        style=_writing_style(queue, held, owner, mine, event, out))
    got, why = writer.write(m, event, people, owner)
    if got is None:
        return out, None if stands and why not in ("draft_failed",) else why
    out["message"] = {"text": text[:ad.MESSAGE_CHARS]}
    if got.kind == "your_call":
        # the answer is the person's to give: no hedging draft, a card asking for their
        # line (``autopilot_drafter.from_line`` writes the reply from it); the material is
        # kept on their own item to write it from
        # the reply's envelope stays with an empty body, so the reply written from the
        # line is edited and confirmed on the usual path; the card keeps what the screens
        # read again then (``CARD_EVENT``)
        if m.channel == "teams":
            out.pop("draft", None)
            out["teams_draft"] = {"channel": "teams",
                                  "conversation": event.get("conversation_id"),
                                  "reply_to": event.get("object_id"), "body": "",
                                  "post": False}
        else:
            out.pop("teams_draft", None)
            base = out.get("draft") if isinstance(out.get("draft"), Mapping) else \
                autopilot_handlers._reply(event, [])
            out["draft"] = {**{k: v for k, v in base.items() if k != "copy_check"},
                            "body": ""}
        out["your_call"] = {"ask": got.ask, "facts": got.facts, "material": m.kept(),
                            "event": {k: event[k] for k in CARD_EVENT if k in event}}
        out["drafted_by"] = "model"
        return out, None
    reply = got.text
    if m.channel == "teams":
        out["teams_draft"] = {"channel": "teams", "conversation": event.get("conversation_id"),
                              "reply_to": event.get("object_id"), "body": reply, "post": False}
    else:
        base = out.get("draft") if isinstance(out.get("draft"), Mapping) else \
            autopilot_handlers._reply(event, [])
        out["draft"] = {**{k: v for k, v in base.items() if k != "copy_check"}, "body": reply}
    out["drafted_by"] = "model"
    return out, None


class _DryQueue:
    """The live queue's place, never opened (``dry_drafts``): the writer reads the person's
    memory and learned style beside it, as a live cycle does, and nothing is written."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)


def dry_drafts(st: Any, registry: Mapping[str, Any], people: Any, ids: Sequence[str],
               queue_path: Path, *, owners: set[str] | None = None, force: str = "",
               environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The model's reply drafts for the records ``ids`` as the live cycle would write them
    (same events, owners, screens, handler, material, style and writer), for a person to
    grade: nothing is written to any queue, nothing is sent, no gate is consulted (a grade
    asks what the draft would be, not who switched it on). ``owners``: only these people
    (mailbox ids or addresses); ``force``: the autopilot that drafts an event no live rule
    matched (``matched: false``). One row per (event, owner): the draft or why not."""
    from . import autopilot_drafter as ad
    environ = os.environ if environ is None else environ
    events, skipped = discover(st, ids, registry, people)
    directory, addresses_of, domains = _people_maps(people)
    roles = _roles(environ, Path(queue_path).parent)
    queue: Any = _DryQueue(Path(queue_path))
    held: dict[str, Any] = {}
    now = _utcnow()
    seen = {str(e.get("object_id")) for e in events}
    out: list[dict[str, Any]] = [
        {"object_id": oid, "why": "no_event", "skipped": dict(skipped)}
        for oid in ids if oid not in seen]
    for event in events:
        base = {"object_id": event.get("object_id"), "sender": event.get("sender"),
                "subject": event.get("subject"), "at": event.get("at")}
        if event.get("event_type") == "message.received" and not event.get("cleared"):
            out.append({**base, "why": "participants_not_cleared"})
            continue
        if pay_or_hr(event):
            out.append({**base, "why": "pay_or_hr"})
            continue
        done: set[str] = set()
        live = [ap for ap in registry["autopilots"]
                if ap.get("draft") == "model" and ap["on"] == event["event_type"]
                and ap.get("enabled") is not False and matches(ap.get("when") or {}, event)]
        if not live and force:
            # the live cycle would prepare nothing (its rules saw no ask): the grade still
            # asks what the draft would have been, marked ``matched: false``
            live = [ap for ap in registry["autopilots"] if ap["id"] == force]
            base["matched"] = False
        elif live:
            base["matched"] = True
        else:
            out.append({**base, "why": "no_workflow_matched"})
            continue
        before = len(out)
        for ap in live:
            got, why = owners_of(ap["owner"], event, directory=directory,
                                 addresses_of=addresses_of, roles=roles, decided=None)
            got = [o for o in (got or ()) if people.identity_clear(o) and o not in done
                   and (owners is None or o in owners
                        or bool(set(addresses_of.get(o, ())) & owners))]
            if why or not got:
                continue
            prepared = _prepare(st, ap, event, now.date(), domains,
                                float(registry["limits"]["handler_seconds"]))
            for owner in got:
                done.add(owner)
                row = {**base, "owner": owner, "autopilot": ap["id"]}
                if prepared.get("skip") or prepared.get("error"):
                    out.append({**row, "why": prepared.get("skip") or prepared.get("error")})
                    continue
                summary = {"skipped": Counter()}      # a new cycle per draft: never busy
                work, why = _model_draft(queue, held, summary, event, owner,
                                         prepared["work"], people, addresses_of)
                body = (work.get("draft") or work.get("teams_draft") or {}).get("body") \
                    if not why else None
                card = work.get("your_call") if not why else None
                material = prepared["work"].get("material") or {}
                out.append({**row, "why": why or "", "draft": body,
                            "kind": "your_call" if card else "reply" if body else "",
                            **({"ask": card["ask"], "facts": card["facts"],
                                "card": {"material": card["material"],
                                         "event": card["event"]}} if card else {}),
                            "message": str(material.get("text") or event.get("text") or ""),
                            "account": list(material.get("account") or ()),
                            "style": _writing_style(queue, held, owner,
                                                    sorted(addresses_of.get(owner) or ()),
                                                    event, work)})
        if len(out) == before:
            out.append({**base, "why": "no_owner"})
    return out


def _writing_style(queue: Queue, held: dict[str, Any], owner: str, mine: Sequence[str],
                   event: Mapping[str, Any], work: Mapping[str, Any]) -> list[str]:
    """How ``owner`` writes: their writing memory (the draft-context provider's
    ``writing``) and their learned style (``person_style``); [] when neither is known."""
    out: list[str] = []
    provider = _draft_context(queue, held)
    writing = getattr(provider, "writing", None)
    if writing is not None:
        try:
            out += [str(x) for x in writing(owner, event, work) or ()][:8]
        except Exception:  # noqa: BLE001 - memory never stops a draft
            pass
    if mine:
        from . import person_style
        if "style" not in held:
            held["style"] = person_style.StyleSource(queue.path.parent.parent)
        try:
            got = held["style"](mine[0]) or {}
        except Exception:  # noqa: BLE001 - no learned style: plain
            got = {}
        out += [str(x) for x in got.get("rules") or ()][:8]
    return out


# The person's memory and matching template for each draft (``operator_api.
# autopilot_context`` registers it: ``operating`` never imports ``operator_api``): a callable
# making a provider with ``for_draft(owner, autopilot, work, event) -> (work, used)``; None:
# drafts take nothing from memory or templates and record nothing.
DRAFT_CONTEXT_FACTORY: Optional[Callable[[], Any]] = None


def _draft_context(queue: Queue, held: dict[str, Any]) -> Any:
    """The queue's draft-context provider, made once an hour (None: off, a replay's own queue, or
    it could not be made). A replay never reads anyone's memory (gate scores)."""
    if "provider" not in held or time.monotonic() - held["made"] > DRAFT_CONTEXT_SECONDS:
        held["provider"], held["made"] = None, time.monotonic()
        if DRAFT_CONTEXT_FACTORY is not None and queue.path.parent.name == "autopilot":
            try:
                held["provider"] = DRAFT_CONTEXT_FACTORY()
            except Exception as error:  # noqa: BLE001 - no context: drafts as before
                print(json.dumps({"autopilot_draft_context": "off",
                                  "reason": type(error).__name__}), file=sys.stderr)
    return held["provider"]


def _lessons(queue: Queue) -> Any:
    """The learned lessons for the live queue's owners (``operating.learning``; the live
    queue is ``<state>/autopilot/autopilot.sqlite``, a replay's queue never takes any): the
    ledger in ``PLX_LEARNING_DIR``, else ``<state>/learning``; none when off, absent,
    unreadable or the people exclusion is unavailable."""
    from . import learning
    env = os.environ
    if queue.path.parent.name != "autopilot":
        return learning.Lessons(())     # a replay's own queue: never lessons (gate scores)
    if env.get(learning.DIR_ENV):
        return learning.Lessons.load(Path(env[learning.DIR_ENV]))
    try:
        return learning.Lessons.load(learning.ledger_dir(queue.path.parent.parent, env))
    except Exception:  # noqa: BLE001 - an unreadable ledger: no lessons, the cycle runs
        return learning.Lessons(())


def _drain(st: Any, queue: Queue, people: Any, registry: Mapping[str, Any],
           now: datetime, **scope: Any) -> dict[str, int]:
    """A replay's further cycles until no event waits (each cycle bounded in time); the
    rate limit's hour moves on with each cycle, as it would live."""
    total: Counter = Counter()
    for n in range(1, 500):
        left = queue.conn.execute("SELECT COUNT(*) FROM events WHERE state IN ('new', "
                                  "'deferred')").fetchone()[0]
        if not left:
            break
        out = run_cycle(st, queue, [], people=people, registry=registry,
                        now=now + timedelta(hours=n), settle_items=False, **scope)
        total.update(out.get("prepared") or {})
        total.update({f"skipped:{k}": v for k, v in (out.get("skipped") or {}).items()})
        if out.get("status") not in ("complete", "partial"):
            break
    return dict(total)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot")
    p.add_argument("command", choices=("follow", "fast", "replay", "drafts-dry", "supersede", "unsupersede"))
    p.add_argument("--state-dir", default=None,
                   help="the served generation's store and <state>/autopilot/ queue")
    p.add_argument("--store", default=None)
    p.add_argument("--queue", default=None)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--until", default=None, help="replay window end (ISO), default now")
    p.add_argument("--layer", default=None, help="replay: also the records this layer owns")
    p.add_argument("--registry", default=None)
    p.add_argument("--ids-file", default=None, help="drafts-dry: record ids, one per line")
    p.add_argument("--owner", action="append", default=None,
                   help="drafts-dry: only this person (mailbox id or address; repeatable)")
    p.add_argument("--force", default=None,
                   help="drafts-dry: the autopilot that drafts what no live rule matched")
    p.add_argument("--capture-state", default=None,
                   help="state dir whose mail capture holds attachment text (read-only)")
    p.add_argument("--apply", action="store_true",
                   help="supersede, unsupersede: change the queue (default: count only)")
    p.add_argument("--serve", action="store_true",
                   help="fast: run continuously (operating.autopilot_fast.serve)")
    a = p.parse_args(argv)
    if a.serve and a.command != "fast":
        p.error("--serve is for fast")
    store_given = a.store
    if a.state_dir:
        state = Path(a.state_dir)
        current = json.loads((state / "projection" / "CURRENT").read_text())
        a.store = a.store or str(state / "projection" / "generations"
                                 / str(current["generation"]) / "store.sqlite")
        a.queue = a.queue or str(state / "autopilot" / QUEUE_FILE)
    if not a.queue or (not a.store and a.command not in ("supersede", "unsupersede")):
        p.error("--state-dir, or --store and --queue, are required")
    if a.command == "fast" and not a.state_dir:
        p.error("fast reads the capture stores of --state-dir")
    if a.command in ("supersede", "unsupersede"):
        queue = Queue(a.queue)
        try:
            out = queue.supersede_draftless(_iso(_utcnow()), a.apply) \
                if a.command == "supersede" else {"applied": a.apply,
                                                  "items": queue.unsupersede(a.apply)}
        finally:
            queue.close()
        print(json.dumps(out, sort_keys=True))
        return 0
    registry = load_registry(a.registry)
    if a.command == "drafts-dry":
        return _drafts_dry(a, registry)
    if a.command == "fast":
        from . import autopilot_fast
        if a.serve:
            # a consumer that failed waits longer each time before it takes the stores (the
            # path and timer starts cover the wait); SIGTERM still ends it at once
            time.sleep(autopilot_fast.backoff_seconds(Path(a.queue).parent, _utcnow()))
        with autopilot_fast.consumer_lock(Path(a.queue).parent, wait=a.serve) as held:
            if not held:
                # the continuous consumer holds the stores (``fast --serve``): this path or
                # timer start is its backstop and has nothing to do
                print(json.dumps({"schema": SCHEMA, "feed": "fast", "status": "served"}))
                return 0
            if a.serve:
                return _serve_fast(a, registry, store_given)
            return _run(a, registry, Queue(a.queue))
    return _run(a, registry, Queue(a.queue))


def _with_captures(st: Any, a: argparse.Namespace) -> Any:
    """``st`` with the captures' attachment and message text and Mission Control questions
    (the capture stores of ``--capture-state``, else ``--state-dir``)."""
    capture_dir = a.capture_state or a.state_dir
    if capture_dir and (Path(capture_dir) / "mail-capture" / "capture.db").exists():
        from . import autopilot_work as aw
        st.autopilot_capture = aw.CaptureTexts(capture_dir)
    if capture_dir and (Path(capture_dir) / "mc-capture" / "capture.sqlite").exists():
        from . import autopilot_work as aw
        st.autopilot_mc = aw.MCQuestions(capture_dir)
    return st


def _drafts_dry(a: argparse.Namespace, registry: Mapping[str, Any]) -> int:
    """``drafts-dry``: one JSON line per draft (``dry_drafts``) for the Email/Teams record
    ids in ``--ids-file`` (one per line); the queue is never opened."""
    from . import proactive_engine as pe
    if not a.ids_file:
        print(json.dumps({"error": "drafts-dry needs --ids-file"}), file=sys.stderr)
        return 2
    ids = [x.strip() for x in Path(a.ids_file).read_text().splitlines() if x.strip()]
    people = pe._people(Path(a.store))
    st = _with_captures(_store(a.store), a)
    try:
        rows = dry_drafts(st, registry, people, ids, Path(a.queue),
                          owners=set(a.owner) if a.owner else None, force=a.force or "")
    finally:
        st.close()
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, default=str))
    return 0


def _open_run(a: argparse.Namespace, queue: "Queue") -> tuple[Any, Any, Any, Any, Any]:
    """What a start reads before it handles anything: the delta pointer, the people
    exclusion, the served store (with the captures' attachment text and Mission Control
    questions) and the replay gate with Draft; the caller closes the store."""
    from . import proactive_engine as pe
    store = Path(a.store)
    # the delta pointer sits beside the served projection (``host.projection_path``, as the
    # proactive pass reads it), not beside a generation's file
    if a.state_dir:
        from .host import projection_path
        layers_at = projection_path(Path(a.state_dir))
    else:
        layers_at = store
    people = pe._people(store)
    st = _with_captures(_store(a.store), a)
    try:
        if a.command in ("follow", "fast"):
            # live: prepared only for a person who switched the workflow's drafts on
            # themselves, or in Draft for them (the owner's own items): ``SwitchGate``.
            # The replay gate decides only what may be performed (``write_state``).
            drafted = draft_owners(load_draft(), _people_maps(people)[0], people)
            from .autopilot_write import AUDIT_FILE   # (imports this module)
            journal = (Path(a.state_dir) / "autopilot-write" / AUDIT_FILE) \
                if a.state_dir else None
            gate = SwitchGate(journal, drafted)
            passed = gate.passed
        else:
            # a replay scores the workflows a role's replay passed, in that role
            # (``gate_from``); a replay never drafts
            gate, passed = gate_from(os.environ, queue.path.parent)
    except BaseException:
        st.close()
        raise
    return layers_at, people, st, gate, passed


def _fast_off(passed: Mapping[str, Any]) -> dict[str, Any] | None:
    return None if passed else {"schema": SCHEMA, "status": "off",
                                "reason": "nobody_switched_drafts_on"}


class _FastStart:
    """What one ``fast`` start loads (``_open_run``), kept by ``fast --serve`` for many
    cycles until ``stale`` (a new generation is served) or its reload time."""

    def __init__(self, a: argparse.Namespace, registry: Mapping[str, Any], queue: "Queue",
                 generation: Any) -> None:
        self.a, self.registry, self.queue, self.generation = a, registry, queue, generation
        _layers, self.people, self.st, self.gate, self.passed = _open_run(a, queue)

    def stale(self) -> bool:
        if self.generation is None:
            return False
        try:
            return _served_generation(self.a) != self.generation
        except (OSError, ValueError, KeyError, TypeError):
            return False      # CURRENT caught mid-write: read again next cycle

    def run(self, clock: Callable[[], datetime]) -> dict[str, Any]:
        from . import autopilot_fast
        if isinstance(self.gate, SwitchGate):
            self.passed = self.gate.passed     # a switch takes effect without a reload
        off = _fast_off(self.passed)
        if off:
            return off
        summary = autopilot_fast.run(Path(self.a.state_dir), self.st, self.queue, self.people,
                                     self.registry, gate=self.gate, environ=os.environ,
                                     clock=clock)
        summary["workflows_on"] = {"owners": len(self.passed)}
        return summary

    def close(self) -> None:
        self.st.close()


def _served_generation(a: argparse.Namespace) -> Any:
    current = Path(a.state_dir) / "projection" / "CURRENT"
    return json.loads(current.read_text())["generation"]


def _serve_fast(a: argparse.Namespace, registry: Mapping[str, Any],
                store_given: str | None) -> int:
    """``fast --serve``: ``autopilot_fast.serve`` until SIGTERM, reading the served
    generation afresh on each load (unless ``--store`` named one)."""
    import signal
    from . import autopilot_fast
    stopping: list[int] = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    queue = Queue(a.queue)

    def load() -> _FastStart:
        b = argparse.Namespace(**vars(a))
        generation = None
        if not store_given:
            generation = _served_generation(a)
            b.store = str(Path(a.state_dir) / "projection" / "generations" / str(generation)
                          / "store.sqlite")
        return _FastStart(b, registry, queue, generation)

    def out(summary: Mapping[str, Any]) -> None:
        print(json.dumps(summary, sort_keys=True, default=str), flush=True)

    # its unit's proactive.env (people exclusion, PLX_AUTOPILOT_OFF) reaches this process only
    # at start: a change ends it before the next cycle and Restart=always reads it afresh
    from integrations.people_exclusion import ENV_FILE as EXCLUSION_FILE
    checks = [("release_moved", autopilot_fast.release_moved(os.environ)),
              ("env_changed", autopilot_fast.env_moved(autopilot_fast.env_file(os.environ)))]
    if os.environ.get(EXCLUSION_FILE):
        checks.append(("exclusion_changed",
                       autopilot_fast.env_moved(Path(os.environ[EXCLUSION_FILE]))))
    qdir = Path(a.queue).parent

    def stop() -> bool:
        if stopping:
            return True
        for reason, moved in checks:
            if moved():
                print(json.dumps({"schema": SCHEMA, "feed": "fast", "status": "restart",
                                  "reason": reason}), flush=True)
                return True
        return False

    def ran(summary: Mapping[str, Any]) -> None:
        autopilot_fast.serve_ok(qdir)
        out(summary)

    try:
        autopilot_fast.serve(Path(a.state_dir), queue, load, environ=os.environ,
                             clock=_utcnow, out=ran, stop=stop)
    except Exception as e:
        autopilot_fast.serve_failed(qdir, e, _utcnow())
        raise
    finally:
        queue.close()
    return 0


def _outcomes_line(a: argparse.Namespace, queue: "Queue", now: datetime) -> dict[str, Any]:
    """Each follow cycle logs what became of the prepared items (counts only,
    ``operating.autopilot_outcomes``); a failure to read them never fails the cycle."""
    from . import autopilot_outcomes as ao
    try:
        decisions = ao.read_decisions(Path(a.state_dir) / ao.DECISIONS_FILE) \
            if a.state_dir else {}
        got = ao.measure(ao.read_items(queue.path), decisions, now)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {"status": "unreadable", "error": type(exc).__name__}
    got.pop("by_autopilot", None)
    return got


def _run(a: argparse.Namespace, registry: Mapping[str, Any], queue: "Queue") -> int:
    if a.command == "fast":
        from . import autopilot_fast
        state = autopilot_fast.idle(Path(a.state_dir), queue, os.environ)
        if state:
            # nothing stored since the last cycle (its timer's usual start): no store opened
            queue.close()
            print(json.dumps({"schema": SCHEMA, "feed": "fast", "status": state}))
            return 0
    try:
        layers_at, people, st, gate, passed = _open_run(a, queue)
    except BaseException:
        queue.close()
        raise
    try:
        if not passed:                # off until someone switched drafts on (``SwitchGate``)
            print(json.dumps({"schema": SCHEMA, "status": "off",
                              "reason": "nobody_switched_drafts_on" if a.command in
                              ("follow", "fast") else "no_workflow_passed_replay"}))
            return 0
        if a.command == "fast":
            from . import autopilot_fast
            summary = autopilot_fast.run(Path(a.state_dir), st, queue, people, registry,
                                         gate=gate, environ=os.environ, clock=_utcnow)
            summary["workflows_on"] = {"owners": len(passed)}
        elif a.command == "follow":
            now = _utcnow()
            ids, taken, read, batch = follow_ids(st, layers_at, queue, registry, now)
            from .chat_ask_model import from_environ as chat_ask_judge
            summary = run_cycle(st, queue, ids, people=people, registry=registry, gate=gate,
                                now=now, read_work_log=True, event_queue=True,
                                horizon=now - FOLLOW_HORIZON,
                                ask_judge=chat_ask_judge(os.environ))
            summary["read"] = read
            summary["workflows_on"] = {"owners": len(passed),
                                       "workflows": sorted({w for v in passed.values()
                                                            for w in v})}
            if summary["status"] in ("complete", "partial"):
                from .proactive_incremental import generation_digest
                with queue.conn:
                    for layer in taken:   # kept by generation, as ``layer_ids`` reads them
                        queue.conn.execute("INSERT OR IGNORE INTO layers VALUES (?, ?)",
                                           (layer, generation_digest(st.digest)))
                    if read.get("window") is not None:
                        queue.set_meta("follow_stamp", st.digest)
                if batch is not None:   # after the layers are marked: the feed's cursor
                    from operating.pg import change_feed, ontology_reader
                    read["feed"]["committed"] = change_feed.commit_to(
                        ontology_reader.dsn(), batch)
            summary["layers_taken"] = len(taken)
            summary["outcomes"] = _outcomes_line(a, queue, now)
        else:
            until = a.until or _iso(datetime.now(timezone.utc))
            since = _iso(_instant(until) - timedelta(days=a.days))
            ids = window_ids(st, registry, since, until)
            summary = run_cycle(st, queue, ids, people=people, registry=registry,
                                now=_instant(until), settle_items=False, gate=gate)
            summary["drained"] = _drain(st, queue, people, registry, _instant(until),
                                        gate=gate)
            if a.layer:
                with contextlib.closing(sqlite3.connect(f"file:{a.layer}?mode=ro", uri=True)
                                        ) as conn:
                    owned = [r[0] for r in conn.execute("SELECT object_id FROM delta_owned")]
                lst = _store(a.layer)
                try:
                    summary["layer"] = run_cycle(lst, queue, owned, people=people,
                                                 registry=registry, now=_instant(until),
                                                 settle_items=False, gate=gate)
                    summary["layer"]["drained"] = _drain(lst, queue, people, registry,
                                                         _instant(until), gate=gate)
                finally:
                    lst.close()
            summary["window"] = [since, until]
        print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    finally:
        st.close()
        queue.close()
    return 0 if summary.get("status") in ("complete", "partial", "off") else 1


if __name__ == "__main__":
    sys.exit(main())
