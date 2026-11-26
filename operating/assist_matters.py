"""Assist matters (ASSIST specs 2.3, contract C5): one open piece of a person's work, kept
true every cycle.

A matter is opened only from data that says someone owes an act:

* ``conversation``: a message reading (``assist.readings``, C4) that needs an act and names
  one of our staff as the actor (``actor_person``); one matter per (actor, thread), its basis
  the newest such reading of the thread. A reading whose actor is unresolved (two staff
  match) opens nothing and is counted ``actor_unresolved``.
* ``approval``: a pending Mission Control approval (the ``MCApproval`` ontology objects: no
  business view holds them yet), one matter per approver the one approver rule
  (``autopilot.owners_of('approvers')``) names.
* ``task``: an open Mission Control or Planner task assigned to a person, and an open
  Mission Control question asked of one (the MC capture holds the questions).
* ``record``: an open alert (``biz.alerts``) of a kind named in ``PLX_ASSIST_ALERT_KINDS``
  (default none) that names its person; and the open items of the six queue triggers that
  stay until their replacements land (``REMAINING``), each by its ``legacy_item_id``.

Every cycle re-checks every open matter against its source (``reverify``): a later message
of the owner in the thread, a colleague answering the asker in the thread, an approval
decided or superseded, a task closed or reassigned, a question answered, an alert closed, a
``still_true`` condition no longer true. A source that cannot be read makes the matter
``unverified`` with the reason (never closed, never counted true). No matter is closed by
age. Lists (``as_items``) show the last ``LIST_DAYS`` days by basis and say how many older
ones there are; ``all_ages`` lists them all.

Who sees a matter: ``shown_to`` (the person opted in, or the owner's draft file names them);
the API applies the reader's door to each item (its audiences, markings and every cited
handle), as it does to queue items. Nothing here sends or writes anywhere but the
``assist`` tables.

    python3 -m operating.assist_matters cycle [--dsn DSN] [--queue autopilot.sqlite]
    python3 -m operating.assist_matters adopt --queue /data/plx-state/autopilot/autopilot.sqlite
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

LOG = logging.getLogger(__name__)

SCHEMA = "assist"

# Mission Control approvals (owner 11-24: Vince decides them in Mission Control itself): the
# matter kind made from the MCApproval objects and the queue workflows that prepare the
# registry's ``approval.requested`` event. One definition: the list, the counts and the
# runners (plan and prepared tiers) all leave these out; Mission Control questions stay.
MC_APPROVAL_KIND = "approval"
MC_APPROVAL_EVENT = "approval.requested"


def mc_approval_workflows() -> frozenset[str]:
    """The queue workflows made from a Mission Control approval request (the registry's
    entries on ``approval.requested``): never listed in Prepared."""
    from operating import autopilot as ap
    return frozenset(a["id"] for a in ap.load_registry()["autopilots"]
                     if a.get("on") == MC_APPROVAL_EVENT)
KINDS = ("conversation", "approval", "task", "record", "routine", "meeting", "promise", "wait")
LIST_DAYS = 14            # a list's default window by basis; the rest is counted and reachable
LOCK = 41049              # one matters cycle at a time
ALERT_KINDS_ENV = "PLX_ASSIST_ALERT_KINDS"
QUEUE_ENV = "PLX_AUTOPILOT_QUEUE"
# The registry entries that keep running until their replacements land (specs section 5):
# their open queue items are listed through matters (``open_from_queue``).
REMAINING = ("qc_hold", "invoice_entry", "qc_entry", "shipment_posting", "cash_application",
             "job_recurring")
# the kind a queue item becomes (``adopt`` moves every open item once; specs 2.3)
QUEUE_KIND = {"qc_hold": "record", "invoice_entry": "routine", "qc_entry": "routine",
              "shipment_posting": "routine", "cash_application": "routine",
              "job_recurring": "routine", "approval_request": "approval",
              "job_approval": "approval", "mc_question": "task", "task_assigned": "task"}
# the reading's act class -> the matter's next act (spec 2.3; ``do`` depends on records)
NEXT_ACT = {"reply": "reply", "forward": "forward", "send_document": "attach",
            "system_entry": "system_write", "decide": "ask_person",
            "follow_up": "new_message"}
ACT_WORDS = {  # guard-ok: the person's words for a typed next_act (display), decides nothing
    "reply": "Reply", "forward": "Pass it on", "attach": "Send the document",
             "system_write": "Enter it in the system", "ask_person": "Decide",
             "new_message": "Write to them", "nothing": "Nothing to do"}
UNVERIFIED_WORDS = "could not be re-checked"  # guard-ok: the line shown for state unverified, decides nothing
OPT_IN_UNREADABLE = "opt_in_unreadable"


def matter_id(owner: str, kind: str, subject: str) -> str:
    """The matter's key: as the SQL ``_ID_SQL`` computes it."""
    return hashlib.sha256(f"{owner}|{kind}|{subject}".encode()).hexdigest()[:24]


_ID_SQL = ("left(encode(sha256(convert_to({o} || '|' || {k} || '|' || {s}, 'UTF8')), "
           "'hex'), 24)")


def record_group(handles: Sequence[str]) -> list[str]:
    """The identity a record is compared by (spec 1.2). C7 (``biz.party_members`` and its
    handle-group relation) is not built yet, so a record is its own handle; this is the one
    function that compares record identities, and the C7 group replaces the handle here."""
    return [str(h) for h in handles if h]


def _tz() -> str:
    from operating.pg import business_views as bv
    return bv.BUSINESS_TZ


# --------------------------------------------------------------------------- open

_NOISE_READINGS_SQL = f"""
SELECT r.source, r.message_key, r.summary, r.act, r.quote,
       e_info.sender, e_info.subject, t_info.sender, t_info.subject
FROM {SCHEMA}.readings r
LEFT JOIN LATERAL (
    SELECT e.sender, e.subject FROM biz.emails e
    WHERE r.source = 'emails' AND e.handle = ANY(r.handles) LIMIT 1
) e_info ON true
LEFT JOIN LATERAL (
    SELECT t.sender, t.subject FROM biz.teams_messages t
    WHERE r.source = 'teams_messages' AND t.handle = ANY(r.handles) LIMIT 1
) t_info ON true
WHERE r.status = 'ok' AND r.needs_act AND r.actor_person IS NOT NULL"""

_OPEN_FOR_NOISE_SQL = f"""
SELECT m.matter_id, m.title, m.detail, m.counterpart,
       b.source, b.summary, b.act, b.quote,
       e_info.sender, e_info.subject, t_info.sender, t_info.subject
FROM {SCHEMA}.matters m
LEFT JOIN {SCHEMA}.readings b
  ON jsonb_typeof(m.sources->'readings') = 'array'
 AND b.source = m.sources->'readings'->-1->>0
 AND b.message_key = m.sources->'readings'->-1->>1
LEFT JOIN LATERAL (
    SELECT e.sender, e.subject FROM biz.emails e
    WHERE b.source = 'emails' AND e.handle = ANY(b.handles) LIMIT 1
) e_info ON true
LEFT JOIN LATERAL (
    SELECT t.sender, t.subject FROM biz.teams_messages t
    WHERE b.source = 'teams_messages' AND t.handle = ANY(b.handles) LIMIT 1
) t_info ON true
WHERE m.state IN ('open', 'unverified')"""


def _system_noise_reading_keys(pg: Any) -> list[str]:
    """Identify ``source|message_key`` pairs among candidate readings that are automated IT
    or system monitoring alerts, so ``open_from_readings`` never opens employee matters for
    them."""
    from operating.assist_readings import is_automated_system_alert
    rows = pg.execute(_NOISE_READINGS_SQL).fetchall()
    noise: list[str] = []
    for source, message_key, summary, act, quote, e_sender, e_subject, t_sender, t_subject in rows:
        sender = e_sender if source == "emails" else t_sender
        subject = e_subject if source == "emails" else t_subject
        if is_automated_system_alert(
            sender=sender,
            subject=subject,
            summary=summary,
            note=act,
            message=quote,
        ):
            noise.append(f"{source}|{message_key}")
    return noise


def retire_system_noise_matters(pg: Any) -> dict[str, int]:
    """Safely and idempotently auto-close existing open or unverified matters that came from
    automated IT monitoring alerts, staging/UAT sync bot notifications, quarantine digests,
    or system health checks (``done_reason = 'system_noise'``)."""
    from operating.assist_readings import is_automated_system_alert
    rows = pg.execute(_OPEN_FOR_NOISE_SQL).fetchall()
    verdicts: list[dict[str, str]] = []
    for (matter_id, title, detail, counterpart, source, summary, act, quote,
         e_sender, e_subject, t_sender, t_subject) in rows:
        sender = e_sender if source == "emails" else (t_sender or counterpart)
        subject = e_subject if source == "emails" else (t_subject or "")
        if is_automated_system_alert(
            sender=sender,
            subject=subject,
            summary=title or summary,
            note=detail or act,
            message=quote,
        ):
            verdicts.append({"matter": matter_id, "state": "done", "reason": "system_noise"})
    if not verdicts:
        return {"retired": 0}
    got = set_states(pg, verdicts)
    return {"retired": int(got.get("done", 0))}


def _readings_sql() -> str:
    acts = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in NEXT_ACT.items())
    mid = _ID_SQL.format(o="b.owner", k="'conversation'", s="b.tk")
    return f"""
WITH r AS MATERIALIZED (
  SELECT r.source, r.message_key, r.handles, r.said_at, r.actor_person AS owner,
         coalesce(r.thread_key, r.message_key) AS tk, r.summary, r.act, r.act_class, r.due,
         r.counterpart, r.counterpart_party, r.records
  FROM {SCHEMA}.readings r
  WHERE r.status = 'ok' AND r.needs_act AND r.actor_person IS NOT NULL
    AND NOT ((r.source || '|' || r.message_key) = ANY(%(noise_keys)s::text[]))
    ),
b AS (SELECT DISTINCT ON (owner, tk) * FROM r ORDER BY owner, tk, said_at DESC, message_key DESC),
s AS (SELECT owner, tk, jsonb_agg(jsonb_build_array(source, message_key)
        ORDER BY said_at, message_key) AS readings FROM r GROUP BY 1, 2),
rec AS (SELECT r.owner, r.tk, jsonb_agg(DISTINCT x) AS records
        FROM r, unnest(r.records) x GROUP BY 1, 2),
hr AS MATERIALIZED (
  SELECT r.owner, r.tk, coalesce(e.audience_keys, t.audience_keys) AS aud,
         coalesce(e.rec_marks, t.rec_marks) AS marks
  FROM r CROSS JOIN LATERAL unnest(r.handles) h
  LEFT JOIN biz.emails e ON r.source = 'emails' AND e.handle = h
  LEFT JOIN biz.teams_messages t ON r.source = 'teams_messages' AND t.handle = h),
d1 AS (SELECT owner, tk, jsonb_agg(DISTINCT k) AS aud FROM hr, unnest(hr.aud) k GROUP BY 1, 2),
d2 AS (SELECT owner, tk, jsonb_agg(DISTINCT m) AS marks
       FROM hr, unnest(hr.marks) rm, jsonb_array_elements_text(rm::jsonb) m GROUP BY 1, 2),
old AS (SELECT matter_id, basis_at FROM {SCHEMA}.matters WHERE kind = 'conversation'),
up AS (
  INSERT INTO {SCHEMA}.matters AS m (matter_id, owner_person, kind, subject_key, sources,
      basis_at, title, detail, next_act, due_at, counterpart, counterpart_party, audiences,
      markings)
  SELECT {mid}, b.owner, 'conversation', b.tk,
      jsonb_build_object('readings', s.readings, 'records', coalesce(rec.records, '[]')),
      b.said_at, b.summary, b.act,
      CASE WHEN b.act_class = 'do' THEN CASE WHEN cardinality(b.records) > 0
           THEN 'system_write' ELSE 'ask_person' END ELSE CASE b.act_class {acts} END END,
      -- a stated due date is due by the end of that business day
      coalesce(((b.due + 1)::timestamp AT TIME ZONE %(tz)s), b.said_at + interval '1 day'),
      b.counterpart, b.counterpart_party, coalesce(d1.aud, '[]'), coalesce(d2.marks, '[]')
  FROM b JOIN s USING (owner, tk) LEFT JOIN rec USING (owner, tk)
  LEFT JOIN d1 USING (owner, tk) LEFT JOIN d2 USING (owner, tk)
  ON CONFLICT (owner_person, kind, subject_key) DO UPDATE SET
      sources = EXCLUDED.sources, audiences = EXCLUDED.audiences,
      markings = EXCLUDED.markings,
      basis_at = greatest(m.basis_at, EXCLUDED.basis_at),
      title = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN EXCLUDED.title ELSE m.title END,
      detail = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN EXCLUDED.detail ELSE m.detail END,
      next_act = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN EXCLUDED.next_act
                 ELSE m.next_act END,
      due_at = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN EXCLUDED.due_at ELSE m.due_at END,
      counterpart = EXCLUDED.counterpart, counterpart_party = EXCLUDED.counterpart_party,
      state = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN 'open' ELSE m.state END,
      done_reason = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN NULL ELSE m.done_reason END,
      done_at = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN NULL ELSE m.done_at END,
      plan = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN NULL ELSE m.plan END,
      plan_status = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN 'none' ELSE m.plan_status END,
      plan_reason = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN '' ELSE m.plan_reason END,
      plan_ver = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN NULL ELSE m.plan_ver END,
      plan_at = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN NULL ELSE m.plan_at END,
      ready_at = CASE WHEN EXCLUDED.basis_at > m.basis_at THEN NULL ELSE m.ready_at END
  WHERE m.sources IS DISTINCT FROM EXCLUDED.sources OR m.basis_at < EXCLUDED.basis_at
     OR m.audiences IS DISTINCT FROM EXCLUDED.audiences
     OR m.markings IS DISTINCT FROM EXCLUDED.markings
  RETURNING m.matter_id, m.basis_at, (xmax = 0) AS fresh),
logged AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT u.matter_id, CASE WHEN u.fresh THEN 'opened' ELSE 'basis_moved' END,
         jsonb_build_object('basis_at', u.basis_at)
  FROM up u LEFT JOIN old o USING (matter_id) WHERE u.fresh OR u.basis_at > o.basis_at
  RETURNING what)
SELECT (SELECT count(*) FROM up), (SELECT count(*) FROM logged WHERE what = 'opened'),
       (SELECT count(*) FROM logged WHERE what = 'basis_moved'),
       (SELECT count(*) FROM {SCHEMA}.readings x WHERE x.status = 'ok' AND x.needs_act
          AND x.actor_basis LIKE 'unresolved%%')"""


_BACKFILL_TEAMS_SQL = f"""
SELECT r.message_key, r.thread_key, r.handles, r.sender_person, r.actor_person, r.act_class,
       t.chat, t.sender, t.chat_type, t.member_ids, t.addresses,
       r.summary, r.act, r.quote, t.said
FROM {SCHEMA}.readings r
LEFT JOIN LATERAL (
    SELECT tm.chat, tm.sender,
           to_jsonb(tm)->>'chat_type' AS chat_type,
           coalesce(to_jsonb(tm)->'member_ids', '[]'::jsonb) AS member_ids,
           coalesce(to_jsonb(tm)->'addresses', '[]'::jsonb) AS addresses,
           to_jsonb(tm)->>'said' AS said
    FROM biz.teams_messages tm
    WHERE tm.handle = ANY(r.handles)
    LIMIT 1
) t ON true
WHERE r.source = 'teams_messages' AND r.status = 'ok' AND r.needs_act
  AND (
    (r.actor_person IS NULL AND r.actor_basis NOT LIKE 'unresolved%%')
    OR (r.actor_person IS NOT NULL AND r.actor_person = r.sender_person)
  )"""


def _backfill_one_on_one_teams_actors(pg: Any) -> int:
    """Resolve missing or self-assigned ``actor_person`` on existing 1:1 Teams readings
    whose recipient was not included in the reading's participant list (for example,
    1:1 chats identified by ``chat_type = 'oneOnOne'``, ``member_ids``, or a
    ``19:<uuid1>_<uuid2>@unq.gbl.spaces`` chat handle). Never assigns agent-authored
    self-notifications as action items for the other chat participant."""
    from operating import assist_readings as ar
    from operating.assist_waits import is_agent_notification
    rows = pg.execute(_BACKFILL_TEAMS_SQL).fetchall()
    if not rows:
        return 0
    staff = ar.staff_of(pg, "biz")
    if not staff.by_id:
        return 0
    updates: list[dict[str, Any]] = []
    for (mkey, thread_key, handles, sender_person, actor_person, _act_class,
         t_chat, t_sender, chat_type, member_ids, addresses,
         r_summary, r_act, r_quote, t_said) in rows:
        sender_pid = staff.lookup_id(sender_person) if sender_person else None
        if sender_pid is None and t_sender:
            tid, _shown, _kind = ar.teams_sender(t_sender)
            if tid:
                if "@" in tid:
                    pids = staff.by_address.get(tid, [])
                    sender_pid = pids[0] if len(pids) == 1 else None
                else:
                    sender_pid = staff.lookup_id(tid)
        if not sender_pid:
            continue
        actor_pid = staff.lookup_id(actor_person) if actor_person else None
        self_assigned = bool(actor_pid and actor_pid == sender_pid)
        if is_agent_notification(r_summary, r_act, r_quote, t_said, self_assigned=self_assigned):
            continue
        raw_members = list(member_ids or ())
        mids = ar.teams_chat_member_ids(t_chat or thread_key, raw_members)
        if thread_key and thread_key != t_chat:
            mids = ar.teams_chat_member_ids(thread_key, mids)
        for h in handles or ():
            mids = ar.teams_chat_member_ids(h, mids)
        one_on_one = (
            ar.is_one_on_one_teams_chat(t_chat or thread_key, chat_type or "", mids)
            or (bool(thread_key) and ar.is_one_on_one_teams_chat(thread_key, chat_type or "", mids))
            or any(ar.is_one_on_one_teams_chat(h, chat_type or "", mids) for h in (handles or ()))
        )
        if not one_on_one:
            continue
        staff_members: list[str] = []
        seen: set[str] = set()
        for mid in mids:
            canon = staff.lookup_id(mid)
            if canon and canon not in seen:
                seen.add(canon)
                staff_members.append(canon)
        for addr in addresses or ():
            a = str(addr or "").strip().lower()
            pids = staff.by_address.get(a, [])
            if len(pids) == 1 and pids[0] not in seen:
                seen.add(pids[0])
                staff_members.append(pids[0])
        others = [p for p in staff_members if p != sender_pid]
        if len(others) == 1:
            updates.append({
                "message_key": mkey,
                "actor_person": others[0],
                "sender_person": sender_pid,
            })
    if not updates:
        return 0
    pg.execute(
        f"UPDATE {SCHEMA}.readings r SET "
        "actor_person = u.actor_person, "
        "actor = CASE WHEN coalesce(r.actor, '') IN ('', 'outside') "
        "OR r.actor_person = r.sender_person THEN u.actor_person ELSE r.actor END, "
        "actor_basis = 'teams_one_on_one', "
        "sender_person = coalesce(r.sender_person, u.sender_person) "
        "FROM jsonb_to_recordset(%(u)s::jsonb) AS u("
        "message_key text, actor_person text, sender_person text) "
        "WHERE r.source = 'teams_messages' AND r.message_key = u.message_key",
        {"u": json.dumps(updates)},
    )
    return len(updates)


def open_from_readings(pg: Any, *, now: dt.datetime) -> dict[str, int]:
    """One conversation matter per (actor, thread) of the readings that need an act, skipping
    automated IT/system monitoring alerts; returns ``changed``, ``opened``, ``basis_moved``,
    ``actor_unresolved``, ``teams_1on1_resolved``, and ``system_noise_skipped``."""
    resolved_teams = _backfill_one_on_one_teams_actors(pg)
    noise_keys = _system_noise_reading_keys(pg)
    row = pg.execute(_readings_sql(), {"tz": _tz(), "noise_keys": noise_keys}).fetchone()
    out = dict(zip(("changed", "opened", "basis_moved", "actor_unresolved"),
                   (int(v or 0) for v in row)))
    out["teams_1on1_resolved"] = resolved_teams
    out["system_noise_skipped"] = len(noise_keys)
    return out


_UPSERT = f"""
WITH x AS (SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(owner text, kind text,
    subject text, sources jsonb, basis_at timestamptz, title text, detail text,
    next_act text, due_at timestamptz, audiences jsonb, markings jsonb, legacy text)),
up AS (
  INSERT INTO {SCHEMA}.matters AS m (matter_id, owner_person, kind, subject_key, sources,
      basis_at, title, detail, next_act, due_at, audiences, markings, legacy_item_id)
  SELECT {_ID_SQL.format(o="x.owner", k="x.kind", s="x.subject")}, x.owner, x.kind,
      x.subject, x.sources, x.basis_at, x.title, coalesce(x.detail, ''), x.next_act,
      coalesce(x.due_at, x.basis_at + interval '1 day'), coalesce(x.audiences, '[]'),
      coalesce(x.markings, '[]'), x.legacy
  FROM x
  ON CONFLICT (owner_person, kind, subject_key) DO UPDATE SET
      sources = EXCLUDED.sources, title = EXCLUDED.title, detail = EXCLUDED.detail,
      audiences = EXCLUDED.audiences, markings = EXCLUDED.markings,
      legacy_item_id = coalesce(m.legacy_item_id, EXCLUDED.legacy_item_id)
  WHERE m.sources IS DISTINCT FROM EXCLUDED.sources OR m.title IS DISTINCT FROM EXCLUDED.title
     OR m.audiences IS DISTINCT FROM EXCLUDED.audiences
     OR m.markings IS DISTINCT FROM EXCLUDED.markings
  RETURNING m.matter_id, (xmax = 0) AS fresh),
logged AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT matter_id, 'opened', '{{}}'::jsonb FROM up WHERE fresh RETURNING 1)
SELECT (SELECT count(*) FROM up), (SELECT count(*) FROM logged)"""


def upsert(pg: Any, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Matters of the typed sources (approvals, tasks, alerts, queue items), all in one
    statement: ``changed`` and ``opened``."""
    if not rows:
        return {"changed": 0, "opened": 0}
    got = pg.execute(_UPSERT, {"rows": json.dumps(list(rows), default=str)}).fetchone()
    return {"changed": int(got[0]), "opened": int(got[1])}


# The pivot of one ontology object type's current values (with its audience and every
# marking its values carry) for the typed MC and Planner rows: no business view holds them.
_OBJECTS = """
SELECT o.object_id, o.audience_key, o.first_recorded_at,
       jsonb_object_agg(v.property, v.value) FILTER (WHERE v.property IS NOT NULL) AS props,
       coalesce((SELECT jsonb_agg(DISTINCT mk) FROM ontology.object_values w,
           jsonb_array_elements_text(CASE WHEN w.markings LIKE '[%%' THEN w.markings::jsonb
               ELSE '[]'::jsonb END) mk
           WHERE w.object_id = o.object_id AND w.superseded_at IS NULL), '[]') AS marks
FROM ontology.objects o
LEFT JOIN ontology.object_values v ON v.object_id = o.object_id AND v.superseded_at IS NULL
WHERE o.object_type = %(type)s AND o.missing_from_source_since IS NULL {where}
GROUP BY o.object_id, o.audience_key, o.first_recorded_at"""


def _objects(pg: Any, otype: str, where: str = "", params: Optional[Mapping] = None
             ) -> list[dict[str, Any]]:
    rows = pg.execute(_OBJECTS.format(where=where), {"type": otype, **(params or {})}).fetchall()
    return [{"object_id": r[0], "audience": r[1], "first": r[2], "props": r[3] or {},
             "marks": list(r[4] or [])} for r in rows]


class People:
    """Our staff as Assist resolves them: ``directory`` lower-case address -> staff id for
    every address that names exactly one staff member (``biz.staff`` and the mailbox
    owners' addresses), ``ids`` every staff id, ``addresses`` staff id -> its addresses."""

    def __init__(self, rows: Iterable[tuple[str, Sequence[str]]] = ()) -> None:
        seen: dict[str, set[str]] = {}
        self.addresses: dict[str, set[str]] = {}
        for pid, addrs in rows:
            if not pid:
                continue
            for a in addrs or ():
                a = str(a or "").strip().lower()
                if "@" in a:
                    seen.setdefault(a, set()).add(pid)
                    self.addresses.setdefault(pid, set()).add(a)
            self.addresses.setdefault(pid, set())
        self.directory = {a: next(iter(p)) for a, p in seen.items() if len(p) == 1}
        self.ids = frozenset(self.addresses)

    def person(self, handle: str, domains: Optional[Iterable[str]] = None) -> Optional[str]:
        """A staff id for an id, an address or a bare MC name (``autopilot_events.
        handle_person``: one person or none, never a guess)."""
        from operating.autopilot_events import handle_person
        h = str(handle or "").strip()
        if h in self.ids:
            return h
        return handle_person(h, self.directory, domains)


def people_of(pg: Any) -> People:
    rows: list[tuple[str, list[str]]] = []
    if pg.execute("SELECT to_regclass('biz.staff')").fetchone()[0] is not None:
        rows += [(p, [e]) for p, e in pg.execute(
            "SELECT person_id, email FROM biz.staff WHERE person_id IS NOT NULL").fetchall()]
    if pg.execute("SELECT to_regclass('biz.mailbox_owners')").fetchone()[0] is not None:
        rows += [(b, list(a or ())) for b, a in pg.execute(
            "SELECT mailbox, addrs FROM biz.mailbox_owners").fetchall()]
    return People(rows)


def _instant(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        got = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return got if got.tzinfo else got.replace(tzinfo=dt.timezone.utc)


def approvals(pg: Any, people: People) -> tuple[list[dict], list[dict]]:
    """(matters to open, verdicts) of the Mission Control approvals: a pending approval with
    no newer approval on the same task is open for each approver (``autopilot.owners_of``);
    a newer one supersedes it; any other status is decided."""
    from operating import autopilot as ap
    rows = _objects(pg, "MCApproval")
    newest: dict[str, dt.datetime] = {}
    for r in rows:
        at = _instant(r["props"].get("requested_at"))
        task = str(r["props"].get("task_id") or "")
        if task and at is not None and (task not in newest or at > newest[task]):
            newest[task] = at
    opened, verdicts = [], []
    for r in rows:
        p, oid = r["props"], r["object_id"]
        at = _instant(p.get("requested_at"))
        task = str(p.get("task_id") or "")
        if str(p.get("status") or "") != "pending":
            verdicts.append({"subject": oid, "state": "done", "reason": "approval_decided"})
            continue
        if at is not None and task and newest.get(task, at) > at:
            verdicts.append({"subject": oid, "state": "done", "reason": "superseded"})
            continue
        owners, why = ap.owners_of("approvers", {"fields": p}, directory=people.directory,
                                   addresses_of={}, roles={})
        if not owners:
            verdicts.append({"subject": oid, "state": "unverified",
                             "reason": f"no approver known ({why})"})
            continue
        verdicts.append({"subject": oid, "state": "open", "reason": ""})
        for owner in owners:
            opened.append({
                "owner": owner, "kind": "approval", "subject": oid,
                "sources": {"records": [oid], "approval": oid, "task": task},
                "basis_at": (at or _instant(r["first"])),
                "title": str(p.get("reason") or "Approval requested"),
                "detail": f"Mission Control approval on {task}, asked by "
                          f"{p.get('requested_by') or 'unknown'}",
                "next_act": "system_write", "audiences": [r["audience"]],
                "markings": r["marks"]})
    return opened, verdicts


def _assignees(otype: str, props: Mapping[str, Any]) -> list[str]:
    if otype == "MCTask":
        return [str(props.get("assignee") or "")]
    try:
        got = json.loads(props.get("assignees") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in got if a] if isinstance(got, list) else []


LEGACY_ZOMBIE_TASK_DAYS = 180      # undated Planner tasks older than 180 days
LEGACY_ZOMBIE_MAX_TASK_DAYS = 365  # any unupdated task older than 365 days without a future due_at
LEGACY_ZOMBIE_WAIT_DAYS = 120      # ancient undated waits/promises older than 120 days


def is_zombie_task(
    otype: str,
    props: Mapping[str, Any],
    basis_at: Optional[dt.datetime],
    *,
    now: Optional[dt.datetime] = None,
) -> bool:
    """True for stale legacy tasks (2021-2025 Planner zombies and >365d unupdated tasks
    with no upcoming due date) that should be closed rather than cluttering active work."""
    ref = now or dt.datetime.now(dt.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt.timezone.utc)
    due_at = _instant(props.get("due_at") or props.get("due_date"))
    if due_at is not None and due_at >= ref:
        return False
    updated_at = (
        _instant(props.get("updated_at") or props.get("modified_at") or props.get("last_modified_at"))
        or basis_at
    )
    if updated_at is None:
        return False
    if otype == "PlannerTask" and due_at is None:
        if updated_at < ref - dt.timedelta(days=LEGACY_ZOMBIE_TASK_DAYS):
            return True
    max_cutoff = ref - dt.timedelta(days=LEGACY_ZOMBIE_MAX_TASK_DAYS)
    if updated_at < max_cutoff and (due_at is None or due_at < max_cutoff):
        return True
    return False


def tasks(pg: Any, people: People, questions: Optional[Callable[[], list[dict]]] = None, *,
          now: Optional[dt.datetime] = None
          ) -> tuple[list[dict], list[dict], dict[str, int]]:
    """(matters to open, verdicts, counts) of the open MC and Planner tasks assigned to a
    person and the open MC questions asked of one (``questions()``: rows of the MC
    capture, see ``mc_questions``). An assignee no staff id resolves is counted
    ``assignee_unresolved``, never guessed. Stale legacy Planner/unupdated zombie tasks
    are closed as ``stale_legacy_task``."""
    from operating.autopilot_queue import TASK_DONE
    ref = now or dt.datetime.now(dt.timezone.utc)
    opened, verdicts, counts = [], [], {"assignee_unresolved": 0, "questions_unreadable": 0,
                                        "zombie_tasks_closed": 0}
    for otype in ("MCTask", "PlannerTask"):
        for r in _objects(pg, otype):
            p, oid = r["props"], r["object_id"]
            closed = bool(p.get("completed_at")) or bool(TASK_DONE.match(str(p.get("stage") or "")))
            if closed:
                verdicts.append({"subject": oid, "state": "done", "reason": "task_closed"})
                continue
            basis_at = _instant(p.get("created_at")) or _instant(r["first"])
            if is_zombie_task(otype, p, basis_at, now=ref):
                counts["zombie_tasks_closed"] += 1
                verdicts.append({"subject": oid, "state": "done", "reason": "stale_legacy_task"})
                continue
            names = [a for a in _assignees(otype, p) if a]
            owners = [people.person(a) for a in names]
            counts["assignee_unresolved"] += sum(1 for o in owners if o is None)
            owners = sorted({o for o in owners if o})
            # an open task: open for its assignees, reassigned for anyone else
            verdicts.append({"subject": oid, "state": "done", "reason": "reassigned"})
            for owner in owners:
                verdicts.append({"subject": oid, "owner": owner, "state": "open", "reason": ""})
                opened.append({
                    "owner": owner, "kind": "task", "subject": oid,
                    "sources": {"records": [oid]},
                    "basis_at": basis_at,
                    "title": str(p.get("title") or "Task"),
                    "detail": ("Mission Control task " + str(p.get("task_id") or "")
                               if otype == "MCTask" else
                               f"Planner task in {p.get('plan_title') or 'a plan'}"),
                    "next_act": "ask_person",
                    "due_at": _instant(p.get("due_at") or p.get("due_date")),
                    "audiences": [r["audience"]], "markings": r["marks"]})
    if questions is not None:
        try:
            asked = questions()
        except (sqlite3.Error, OSError) as exc:
            # unreadable: every open question matter becomes unverified (no verdict below)
            LOG.warning("MC questions unreadable: %s", exc)
            counts["questions_unreadable"] = 1
            asked = None
        if asked is not None:
            audience = _task_audiences(pg, [q["task_id"] for q in asked])
            for q in asked:
                subject = f"mc-question:{q['id']}"
                if q["answered"] or q["withdrawn"]:
                    verdicts.append({"subject": subject, "state": "done",
                                     "reason": "question_answered" if q["answered"]
                                     else "question_withdrawn"})
                    continue
                owner = people.person(q["addressee"], q.get("domains"))
                if owner is None:
                    counts["assignee_unresolved"] += 1
                    verdicts.append({"subject": subject, "state": "unverified",
                                     "reason": "addressee not resolved"})
                    continue
                aud = audience.get(q["task_id"])
                verdicts.append({"subject": subject, "owner": owner, "state": "open",
                                 "reason": ""})
                opened.append({
                    "owner": owner, "kind": "task", "subject": subject,
                    "sources": {"records": [q["task_id"]], "question": q["id"]},
                    "basis_at": _instant(q["asked_at"]) or dt.datetime.now(dt.timezone.utc),
                    "title": str(q["text"] or "A question in Mission Control"),
                    "detail": (f"Mission Control question on {q['task_id']}"
                               if str(q.get("task_id") or "").strip()
                               else "Mission Control question"),
                    "next_act": "ask_person",
                    # no task object to take the audience from: the question is shown to no
                    # reader (an audience nobody holds) rather than to everyone
                    "audiences": [aud[0]] if aud else ["mc:unknown-task"],
                    "markings": aud[1] if aud else []})
    return opened, verdicts, counts


def _task_audiences(pg: Any, task_ids: Sequence[str]) -> dict[str, tuple[str, list[str]]]:
    if not task_ids:
        return {}
    rows = pg.execute(
        "SELECT v.value, o.audience_key, coalesce((SELECT jsonb_agg(DISTINCT mk) FROM "
        "ontology.object_values w, jsonb_array_elements_text(CASE WHEN w.markings LIKE '[%%' "
        "THEN w.markings::jsonb ELSE '[]'::jsonb END) mk WHERE w.object_id = o.object_id AND "
        "w.superseded_at IS NULL), '[]') FROM ontology.objects o JOIN ontology.object_values v "
        "ON v.object_id = o.object_id AND v.property = 'task_id' AND v.superseded_at IS NULL "
        "WHERE o.object_type = 'MCTask' AND v.value = ANY(%s)", (list(set(task_ids)),)).fetchall()
    return {t: (a, list(m or [])) for t, a, m in rows}


def mc_questions(state: Path) -> Callable[[], list[dict]]:
    """The MC capture's questions (each task's ``questions``: id, addressee, asked, answered
    or withdrawn), read in one statement; the home domains a bare MC name resolves in
    (``autopilot_work.MCQuestions.home_domains``)."""
    def read() -> list[dict]:
        from operating.autopilot_work import MCQuestions
        mc = MCQuestions(state)
        try:
            domains = mc.home_domains()
            rows = mc.conn.execute(
                "SELECT json_extract(v.payload, '$.row.task_id'), q.value FROM versions v, "
                "json_each(json_extract(v.payload, '$.row.questions')) q WHERE v.kind = ? AND "
                "v.superseded_at IS NULL AND v.path LIKE '%/tasks/%' AND v.path NOT LIKE "
                "'%/task_events/%'", (mc.KIND,)).fetchall()
        finally:
            mc.conn.close()
        out = []
        for task, raw in rows:
            q = json.loads(raw) if isinstance(raw, str) else {}
            if not isinstance(q, Mapping) or not q.get("id"):
                continue
            out.append({"id": str(q["id"]), "task_id": str(task or ""),
                        "addressee": str(q.get("addressee") or ""),
                        "asked_at": q.get("askedAt"), "text": q.get("text"),
                        "answered": isinstance(q.get("answer"), Mapping),
                        "withdrawn": isinstance(q.get("withdrawal"), Mapping)
                        or str(q.get("status") or "") == "withdrawn",
                        "domains": domains})
        return out
    return read


def alerts(pg: Any, kinds: Sequence[str]) -> tuple[list[dict], list[dict]]:
    """Open alerts of the shown kinds that name their person (``mailbox``): a record matter
    each; a closed one is ``alert_closed``. Automated IT/system monitoring alerts are skipped
    as ``system_noise``. No kinds named: nothing (the default)."""
    if not kinds:
        return [], []
    from operating.assist_readings import is_automated_system_alert
    rows = pg.execute(
        "SELECT rule, handle, topic, concerns, mailbox, why, since, state, kind FROM biz.alerts "
        "WHERE kind = ANY(%s) AND mailbox IS NOT NULL", (list(kinds),)).fetchall()
    opened, verdicts = [], []
    for rule, handle, topic, concerns, mailbox, why, since, state, kind in rows:
        subject = "|".join((rule, handle, topic, concerns))
        if state != "open":
            verdicts.append({"subject": subject, "state": "done", "reason": "alert_closed"})
            continue
        if is_automated_system_alert(subject=topic, summary=why):
            verdicts.append({"subject": subject, "state": "done", "reason": "system_noise"})
            continue
        verdicts.append({"subject": subject, "state": "open", "reason": ""})
        opened.append({"owner": mailbox, "kind": "record", "subject": subject,
                       "sources": {"alert": subject, "records": record_group([handle])},
                       "basis_at": since, "title": why, "detail": kind,
                       # no audience of its own: the door decides it by its cited record
                       "next_act": "ask_person", "audiences": [], "markings": []})
    return opened, verdicts


def queue_items(path: Path, autopilots: Optional[Sequence[str]]) -> list[dict]:
    """The queue's open items of ``autopilots`` (all when None), read only."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        sql = ("SELECT item_id, autopilot, owner, title, body, audiences, markings, citations, "
               "due_at, created_at FROM items WHERE state = 'open'")
        args: list[Any] = []
        if autopilots is not None:
            sql += " AND autopilot IN (%s)" % ",".join("?" * len(autopilots))
            args = list(autopilots)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    out = []
    for item_id, autopilot, owner, title, body, aud, marks, cites, due, created in rows:
        b = json.loads(body or "{}")
        out.append({"item_id": item_id, "autopilot": autopilot, "owner": owner,
                    "title": title, "body": b, "audiences": json.loads(aud or "[]"),
                    "markings": json.loads(marks or "[]"), "citations": json.loads(cites or "[]"),
                    "due_at": due, "created_at": created})
    return out


QC_HOLD_AUTOPILOTS = frozenset({"qc_hold", "lot_on_hold", "quality_hold"})


def _staff_role_sets(pg: Any) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Read ``biz.staff`` department and title metadata (when columns exist) to identify
    ``(quality_ids_lower, non_operational_ids_lower, role_pools_by_person_lower)``."""
    if pg.execute("SELECT to_regclass('biz.staff')").fetchone()[0] is None:
        return set(), set(), {}
    from operating import autopilot_roster as ar_roster
    rows = pg.execute(
        "SELECT person_id, to_jsonb(s)->>'department', to_jsonb(s)->>'title' "
        "FROM biz.staff s WHERE person_id IS NOT NULL"
    ).fetchall()
    quality_ids: set[str] = set()
    non_op_ids: set[str] = set()
    pools_by_person: dict[str, set[str]] = {}
    for pid, dept, title in rows:
        pl = str(pid or "").strip().lower()
        if not pl:
            continue
        if ar_roster.is_non_operational_staff(dept, title):
            non_op_ids.add(pl)
            continue
        for role in ar_roster.DEPARTMENT_RULES:
            if ar_roster.staff_matches_role(role, dept, title):
                pools_by_person.setdefault(pl, set()).add(f"pool:{role}")
                if role == "quality":
                    quality_ids.add(pl)
    return quality_ids, non_op_ids, pools_by_person


def _expand_owner_role_pools(pg: Any, owners: Sequence[str]) -> list[str]:
    """Add department-matched role pools (such as ``pool:quality`` for QA/QC staff) for
    the given ``owners`` based on ``biz.staff`` department and title."""
    base = [str(o) for o in owners if o]
    if not base:
        return []
    _qa, _non_op, pools_by_person = _staff_role_sets(pg)
    out = list(dict.fromkeys(base))
    seen = {o.strip().lower() for o in out}
    for owner in base:
        for pool in sorted(pools_by_person.get(owner.strip().lower(), ())):
            if pool not in seen:
                seen.add(pool)
                out.append(pool)
    return out


def reroute_qc_hold_matters(pg: Any) -> dict[str, int]:
    """Safely and idempotently reroute open or unverified ``qc_hold`` / ``lot_on_hold`` /
    ``quality_hold`` matters assigned to non-QA staff (for example software/IT engineers or
    executives) into ``pool:quality`` so QA/QC staff own them instead."""
    rows = pg.execute(
        f"SELECT matter_id, owner_person, kind, subject_key, sources, basis_at, title, detail, "
        f"next_act, due_at, audiences, markings, legacy_item_id "
        f"FROM {SCHEMA}.matters "
        "WHERE state IN ('open', 'unverified') AND owner_person <> 'pool:quality' "
        "AND (sources->>'autopilot' = ANY(%s) "
        "OR split_part(coalesce(sources->>'alert', ''), '|', 1) = ANY(%s))",
        (sorted(QC_HOLD_AUTOPILOTS), sorted(QC_HOLD_AUTOPILOTS)),
    ).fetchall()
    if not rows:
        return {"rerouted": 0, "closed": 0}
    qa_ids, non_op_ids, _pools = _staff_role_sets(pg)
    if not qa_ids and not non_op_ids:
        return {"rerouted": 0, "closed": 0}
    verdicts: list[dict[str, str]] = []
    canonical: dict[tuple[str, str], dict[str, Any]] = {}
    for (mid, owner, kind, subject, sources, basis_at, title, detail,
         next_act, due_at, audiences, markings, legacy) in rows:
        ol = str(owner or "").strip().lower()
        misrouted = ol in non_op_ids or (bool(qa_ids) and ol not in qa_ids)
        if not misrouted:
            continue
        verdicts.append({"matter": mid, "state": "done", "reason": "misrouted_role"})
        key = (str(kind), str(subject))
        if key not in canonical:
            canonical[key] = {
                "owner": "pool:quality",
                "kind": kind,
                "subject": subject,
                "sources": dict(sources or {}),
                "basis_at": basis_at,
                "title": title,
                "detail": detail,
                "next_act": next_act or "ask_person",
                "due_at": due_at,
                "audiences": list(audiences or ()),
                "markings": list(markings or ()),
                "legacy": legacy,
            }
    if not verdicts:
        return {"rerouted": 0, "closed": 0}
    closed = set_states(pg, verdicts)
    upserted = upsert(pg, list(canonical.values()))
    return {
        "closed": int(closed.get("done", 0)),
        "rerouted": int(upserted.get("changed", 0)),
    }


def from_queue(pg: Any, items: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Matters for queue items: a message item becomes its thread's conversation matter
    (the thread read from the comms views by the event's object, one statement), an
    approval item the approval's matter, any other its own record or routine matter keyed
    by the item. QC hold items assigned to non-QA staff are routed to ``pool:quality``."""
    objs = sorted({str((i["body"].get("happened") or {}).get("object_id") or "")
                   for i in items} - {""})
    threads: dict[str, str] = {}
    if objs:
        for oid, thread in pg.execute(
                "SELECT e.object_ids[1], min(e.thread) FROM biz.emails e WHERE e.object_ids[1] "
                "= ANY(%(o)s) GROUP BY 1 UNION ALL SELECT t.object_ids[1], min(t.chat) FROM "
                "biz.teams_messages t WHERE t.object_ids[1] = ANY(%(o)s) GROUP BY 1",
                {"o": objs}).fetchall():
            if thread:
                threads[oid] = thread
    cited = sorted({str(h) for i in items for h in i["citations"]})
    read: dict[str, list[str]] = {}
    if cited:
        for h, src, key in pg.execute(
                f"SELECT h, r.source, r.message_key FROM {SCHEMA}.readings r, unnest(r.handles) h "
                "WHERE r.handles && %s::text[] AND h = ANY(%s::text[])", (cited, cited)).fetchall():
            read[h] = [src, key]
    qa_ids: set[str] = set()
    non_op_ids: set[str] = set()
    if any(i["autopilot"] in QC_HOLD_AUTOPILOTS for i in items):
        qa_ids, non_op_ids, _pools = _staff_role_sets(pg)
    from operating.assist_readings import is_automated_system_alert
    out = []
    for i in items:
        happened = i["body"].get("happened") or {}
        if is_automated_system_alert(
            sender=happened.get("sender") or (happened.get("fields") or {}).get("sender") or "",
            subject=happened.get("subject") or (happened.get("fields") or {}).get("subject") or "",
            summary=i["title"],
            note=i["body"].get("label") or "",
        ):
            continue
        oid = str(happened.get("object_id") or "")
        kind = QUEUE_KIND.get(i["autopilot"])
        if kind is None:
            kind = "conversation" if happened.get("event_type") in (
                "message.received", "message.waiting") else "record"
        subject = (threads.get(oid) or oid or i["item_id"]) if kind == "conversation" else (
            oid if kind == "approval" and oid else i["item_id"])
        owner = i["owner"]
        if i["autopilot"] in QC_HOLD_AUTOPILOTS and owner != "pool:quality":
            ol = str(owner or "").strip().lower()
            if ol in non_op_ids or (bool(qa_ids) and ol not in qa_ids):
                owner = "pool:quality"
        out.append({
            "owner": owner, "kind": kind, "subject": subject,
            "sources": {"queue_item": i["item_id"], "records": i["citations"],
                        "autopilot": i["autopilot"],
                        **({"readings": [read[h] for h in i["citations"] if h in read][-1:]}
                           if kind == "conversation" and any(h in read for h in i["citations"])
                           else {})},
            "basis_at": _instant(happened.get("at")) or _instant(i["created_at"]),
            "title": i["title"], "detail": str(i["body"].get("label") or i["autopilot"]),
            "next_act": "reply" if kind == "conversation" else "ask_person",
            "due_at": _instant(i["due_at"]), "audiences": i["audiences"],
            "markings": i["markings"], "legacy": i["item_id"]})
    return out


# --------------------------------------------------------------------------- reverify

_APPLY = f"""
WITH v AS (SELECT * FROM jsonb_to_recordset(%(v)s::jsonb) AS x(subject text, owner text,
    state text, reason text)),
cur AS (
  SELECT m.matter_id, m.state AS was,
         coalesce(v1.state, v0.state, 'unverified') AS st,
         coalesce(v1.reason, v0.reason, 'source not found') AS why
  FROM {SCHEMA}.matters m
  LEFT JOIN v v1 ON v1.subject = m.subject_key AND v1.owner = m.owner_person
  LEFT JOIN v v0 ON v0.subject = m.subject_key AND v0.owner IS NULL
  WHERE m.kind = %(kind)s AND m.state IN ('open', 'unverified') AND {{only}}),
upd AS (
  UPDATE {SCHEMA}.matters m SET state = c.st,
      done_reason = CASE WHEN c.st = 'done' THEN c.why END,
      done_at = CASE WHEN c.st = 'done' THEN now() END,
      verified_at = CASE WHEN c.st = 'open' THEN now() ELSE m.verified_at END,
      verify_note = CASE WHEN c.st = 'unverified' THEN c.why ELSE '' END
  FROM cur c WHERE m.matter_id = c.matter_id
  RETURNING m.matter_id, c.was, c.st, c.why),
lg AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT matter_id, CASE st WHEN 'done' THEN 'closed' WHEN 'unverified' THEN 'unverified'
         ELSE 'verified' END, jsonb_build_object('reason', why)
  FROM upd WHERE st <> was RETURNING 1)
SELECT st, why, count(*) FROM upd GROUP BY 1, 2"""


def apply(pg: Any, kind: str, verdicts: Sequence[Mapping[str, Any]], *,
          only: str = "true") -> dict[str, int]:
    """Each open or unverified matter of ``kind`` takes its source's verdict (one
    statement): ``open`` (verified now), ``done`` with the reason, ``unverified`` with why;
    a matter its source no longer lists is ``unverified`` ("source not found"), never
    closed. Transitions are logged. ``only``: a SQL condition on ``m`` limiting the matters
    (the queue's: those with a ``legacy_item_id``)."""
    rows = pg.execute(_APPLY.format(only=only),
                      {"v": json.dumps(list(verdicts)), "kind": kind}).fetchall()
    return {f"{st}:{why}" if st != "open" else "verified": int(n) for st, why, n in rows}


_CONVERSATIONS = f"""
WITH m AS MATERIALIZED (
  SELECT m.matter_id, m.owner_person, m.subject_key, m.basis_at, m.state,
         m.sources->'readings'->-1->>0 AS src, m.sources->'readings'->-1->>1 AS mkey
  FROM {SCHEMA}.matters m
  WHERE m.kind = 'conversation' AND m.state IN ('open', 'unverified')),
j AS (
  SELECT m.*, br.handles,
    CASE WHEN m.mkey IS NULL THEN 'its message has not been read yet'
         WHEN br.message_key IS NULL THEN 'the reading of its message is gone'
         WHEN m.src = 'emails' AND NOT EXISTS (SELECT 1 FROM biz.emails e
              WHERE e.handle = ANY(br.handles)) THEN 'its message is no longer in the mail store'
         WHEN m.src = 'teams_messages' AND NOT EXISTS (SELECT 1 FROM biz.teams_messages t
              WHERE t.handle = br.handles[1]) THEN 'its message is no longer in the Teams store'
    END AS missing,
    (SELECT lower(e.sender) FROM biz.emails e WHERE m.src = 'emails'
       AND e.handle = ANY(br.handles) LIMIT 1) AS asker,
    coalesce((SELECT o.addrs FROM biz.mailbox_owners o WHERE o.mailbox = m.owner_person),
             '{{}}') AS mine
  FROM m LEFT JOIN {SCHEMA}.readings br ON br.source = m.src AND br.message_key = m.mkey),
v AS (
  SELECT j.matter_id, j.state AS was,
    CASE WHEN j.missing IS NOT NULL THEN 'unverified'
         WHEN j.src = 'emails' AND EXISTS (SELECT 1 FROM biz.emails e
              WHERE e.thread = j.subject_key AND e.mailbox = j.owner_person
                AND e.received_at > j.basis_at AND lower(e.sender) = ANY(j.mine))
           THEN 'done:owner_acted'
         WHEN j.src = 'teams_messages' AND EXISTS (SELECT 1 FROM biz.teams_messages t
              WHERE t.chat = j.subject_key AND t.created_at > j.basis_at
                AND ((t.sender LIKE '{{%%' AND lower(coalesce(t.sender::jsonb->>'id', '')) = lower(j.owner_person))
                     OR lower(coalesce(t.sender, '')) = lower(j.owner_person)))
           THEN 'done:owner_acted'
         -- the owner's own message or logged act on the thread after the basis, wherever it
         -- is stored (the thread's other mailbox copies, the readings, the actions log)
         WHEN EXISTS (SELECT 1 FROM {SCHEMA}.readings o WHERE o.source = j.src
              AND o.thread_key = j.subject_key AND o.sender_person = j.owner_person
              AND o.said_at > j.basis_at)
           OR EXISTS (SELECT 1 FROM {SCHEMA}.actions a WHERE a.person = j.owner_person
              AND a.thread_key = j.subject_key AND a.at > j.basis_at AND a.not_human = '')
           THEN 'done:owner_acted'
         -- a colleague (one of our mailboxes' or staff addresses, not the owner's) wrote in
         -- the thread after the basis, to the person who asked: answered for the owner.
         -- Teams messages carry no recipients, so a chat is never closed this way.
         WHEN j.src = 'emails' AND j.asker IS NOT NULL AND EXISTS (SELECT 1 FROM biz.emails e
              WHERE e.thread = j.subject_key AND e.received_at > j.basis_at
                AND NOT (lower(e.sender) = ANY(j.mine))
                AND (EXISTS (SELECT 1 FROM biz.mailbox_owners o2 WHERE lower(e.sender) = ANY(o2.addrs))
                     OR EXISTS (SELECT 1 FROM biz.staff s WHERE lower(s.email) = lower(e.sender)))
                AND j.asker IN (SELECT lower(x) FROM unnest(coalesce(e.to_addresses, '{{}}')
                                || coalesce(e.cc_addresses, '{{}}')) x))
           THEN 'done:answered_in_thread'
         ELSE 'open' END AS verdict, j.missing
  FROM j),
upd AS (
  UPDATE {SCHEMA}.matters mm SET
      state = split_part(v.verdict, ':', 1),
      done_reason = CASE WHEN v.verdict LIKE 'done:%%' THEN split_part(v.verdict, ':', 2) END,
      done_at = CASE WHEN v.verdict LIKE 'done:%%' THEN now() END,
      verified_at = CASE WHEN v.verdict = 'open' THEN now() ELSE mm.verified_at END,
      verify_note = coalesce(v.missing, '')
  FROM v WHERE mm.matter_id = v.matter_id
  RETURNING mm.matter_id, v.was, v.verdict, v.missing),
lg AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT matter_id, CASE WHEN verdict LIKE 'done:%%' THEN 'closed' WHEN verdict = 'unverified'
         THEN 'unverified' ELSE 'verified' END,
         jsonb_build_object('reason', coalesce(missing, split_part(verdict, ':', 2)))
  FROM upd WHERE split_part(verdict, ':', 1) <> was RETURNING 1)
SELECT verdict, count(*) FROM upd GROUP BY 1"""


def reverify_conversations(pg: Any) -> dict[str, int]:
    """Every open or unverified conversation matter re-checked in one statement (module
    text), queue-made ones too (their message's reading, once read, is their source)."""
    return {("verified" if v == "open" else v): int(n)
            for v, n in pg.execute(_CONVERSATIONS).fetchall()}


_TOPIC_STOPWORDS = frozenset({
    "the", "and", "for", "that", "with", "this", "from", "have", "your", "about", "please",
    "thanks", "thank", "hello", "good", "morning", "afternoon", "just", "quick", "need",
    "needs", "want", "will", "would", "could", "should", "can", "reply", "sent", "mail",
    "email", "message", "chat", "call", "today", "tomorrow", "yesterday", "still", "here",
    "there", "some", "more", "also", "been", "were", "what", "when", "where", "which",
    "their", "them", "they", "into", "onto", "over", "under", "only", "very", "much",
    "make", "made", "done", "sure", "okay", "not", "but", "are", "was", "has", "had",
    "you", "our", "his", "her", "its", "all", "any", "one", "two", "see", "let", "know",
    "get", "got", "out", "off", "back", "now", "new", "check", "following", "follow",
    "hey", "dear", "regards", "best", "cheers", "fwd", "answer", "asked", "asking",
})
_TOPIC_WORD_RE = re.compile(r"[a-z]{3,}", re.IGNORECASE)
_CODE_TOKEN_RE = re.compile(r"\b[a-z]{2,8}[-_#\s]?\d{3,}\b", re.IGNORECASE)
_CROSS_CHANNEL_OPEN_SQL = f"""
SELECT m.matter_id, m.owner_person, m.subject_key, m.basis_at,
       m.title, m.detail, m.counterpart,
       b.source, b.handles, b.sender_person, b.summary, b.quote, b.records,
       e.sender AS e_sender, e.subject AS e_subject,
       t.sender AS t_sender, t.subject AS t_subject,
       t.chat AS t_chat, t.chat_type AS t_chat_type,
       t.member_ids AS t_member_ids, t.addresses AS t_addresses,
       t.said AS t_said
FROM {SCHEMA}.matters m
JOIN {SCHEMA}.readings b
  ON jsonb_typeof(m.sources->'readings') = 'array'
 AND b.source = m.sources->'readings'->-1->>0
 AND b.message_key = m.sources->'readings'->-1->>1
LEFT JOIN LATERAL (
    SELECT x.sender, x.subject FROM biz.emails x
    WHERE b.source = 'emails' AND x.handle = ANY(b.handles)
    LIMIT 1
) e ON true
LEFT JOIN LATERAL (
    SELECT y.sender, y.subject, y.chat,
           to_jsonb(y)->>'chat_type' AS chat_type,
           coalesce(to_jsonb(y)->'member_ids', '[]'::jsonb) AS member_ids,
           coalesce(to_jsonb(y)->'addresses', '[]'::jsonb) AS addresses,
           to_jsonb(y)->>'said' AS said
    FROM biz.teams_messages y
    WHERE b.source = 'teams_messages' AND y.handle = ANY(b.handles)
    LIMIT 1
) t ON true
WHERE m.kind = 'conversation' AND m.state IN ('open', 'unverified')
  AND m.basis_at IS NOT NULL
  AND b.source IN ('emails', 'teams_messages')"""


def _stem_topic_word(word: str) -> str:
    w = word.lower()
    if len(w) >= 5 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _extract_topic_tokens(texts: Sequence[Any], ignore: set[str]) -> tuple[set[str], set[str]]:
    words: set[str] = set()
    codes: set[str] = set()
    for raw in texts:
        text = str(raw or "")
        if not text:
            continue
        for match in _CODE_TOKEN_RE.findall(text):
            norm = re.sub(r"[^a-z0-9]", "", match.lower())
            if len(norm) >= 4:
                codes.add(norm)
        for match in _TOPIC_WORD_RE.findall(text):
            low = match.lower()
            if low in _TOPIC_STOPWORDS or low in ignore:
                continue
            stemmed = _stem_topic_word(low)
            if stemmed in _TOPIC_STOPWORDS or stemmed in ignore:
                continue
            words.add(stemmed)
    return words, codes


def cross_channel_topic_overlap(
    matter_texts: Sequence[Any],
    reply_texts: Sequence[Any],
    *,
    ignore_names: Iterable[str] = (),
) -> bool:
    """Whether a direct cross-channel message overlaps the open matter's topic (at least
    1 shared record code such as ``PO-90412`` or ``TASK-3601``, or at least 2 shared
    content words excluding stopwords and participant names)."""
    ignore: set[str] = set()
    for name in ignore_names:
        for part in _TOPIC_WORD_RE.findall(str(name or "")):
            ignore.add(part.lower())
            ignore.add(_stem_topic_word(part.lower()))
    m_words, m_codes = _extract_topic_tokens(matter_texts, ignore)
    r_words, r_codes = _extract_topic_tokens(reply_texts, ignore)
    if m_codes & r_codes:
        return True
    return len(m_words & r_words) >= 2


def reverify_cross_channel_replies(pg: Any, people: Optional[People] = None) -> dict[str, int]:
    """Close open conversation matters when the owner answered the same counterpart after
    ``basis_at`` on the other direct channel (email -> 1:1 Teams chat, or 1:1 Teams chat ->
    direct email) with overlapping topic tokens (``done_reason = 'replied_cross_channel'``)."""
    from operating import assist_readings as ar
    rows = pg.execute(_CROSS_CHANNEL_OPEN_SQL).fetchall()
    if not rows:
        return {}
    staff = ar.staff_of(pg, "biz")
    if people is None:
        people = people_of(pg)
    owner_addrs_map: dict[str, set[str]] = {}
    for pid, addrs in people.addresses.items():
        owner_addrs_map.setdefault(pid.lower(), set()).update(a.lower() for a in addrs)
    for pid, (_name, addr) in staff.by_id.items():
        if addr:
            owner_addrs_map.setdefault(pid.lower(), set()).add(addr.lower())

    email_matters: list[dict[str, Any]] = []
    teams_matters: list[dict[str, Any]] = []
    for (mid, owner_person, _subj_key, basis_at, title, detail, counterpart,
         source, handles, sender_person, summary, quote, records,
         e_sender, e_subject, t_sender, t_subject, t_chat, _t_chat_type,
         _t_member_ids, _t_addresses, t_said) in rows:
        owner_l = str(owner_person or "").strip().lower()
        if not owner_l or basis_at is None:
            continue
        cp_pids: set[str] = set()
        cp_addrs: set[str] = set()
        if sender_person:
            sp = staff.lookup_id(sender_person) or str(sender_person)
            cp_pids.add(sp.lower())
            cp_addrs.update(owner_addrs_map.get(sp.lower(), ()))
        if source == "emails":
            addr = ar._bare_email(e_sender)
            if addr and "@" in addr:
                cp_addrs.add(addr)
                for p in staff.by_address.get(addr, ()):
                    cp_pids.add(p.lower())
                    cp_addrs.update(owner_addrs_map.get(p.lower(), ()))
                dp = people.directory.get(addr)
                if dp:
                    cp_pids.add(dp.lower())
                    cp_addrs.update(owner_addrs_map.get(dp.lower(), ()))
            if not cp_pids and not cp_addrs:
                continue
            email_matters.append({
                "matter_id": mid,
                "owner_l": owner_l,
                "basis_at": basis_at,
                "cp_pids": cp_pids,
                "cp_addrs": cp_addrs,
                "texts": [e_subject, title, summary, quote, detail, " ".join(records or ())],
                "names": [counterpart, (staff.by_id.get(owner_person) or ("", ""))[0]]
                + [(staff.by_id.get(staff.lookup_id(p) or "") or ("", ""))[0] for p in cp_pids],
            })
        else:
            tid, shown, _kind = ar.teams_sender(t_sender)
            if tid:
                if "@" in tid:
                    cp_addrs.add(tid)
                    for p in staff.by_address.get(tid, ()):
                        cp_pids.add(p.lower())
                        cp_addrs.update(owner_addrs_map.get(p.lower(), ()))
                else:
                    cp_pids.add(tid)
                    canon = staff.lookup_id(tid)
                    if canon:
                        cp_pids.add(canon.lower())
                        cp_addrs.update(owner_addrs_map.get(canon.lower(), ()))
                    cp_addrs.update(owner_addrs_map.get(tid, ()))
            if not cp_addrs:
                continue
            teams_matters.append({
                "matter_id": mid,
                "owner_l": owner_l,
                "basis_at": basis_at,
                "cp_pids": cp_pids,
                "cp_addrs": cp_addrs,
                "texts": [t_subject, t_said, title, summary, quote, detail, " ".join(records or ())],
                "names": [shown, counterpart, (staff.by_id.get(owner_person) or ("", ""))[0]]
                + [(staff.by_id.get(staff.lookup_id(p) or "") or ("", ""))[0] for p in cp_pids],
            })

    verdicts: list[dict[str, str]] = []
    # Direction A: open email matter -> owner sent a 1:1 Teams message to the counterpart
    if email_matters:
        owners = sorted({m["owner_l"] for m in email_matters})
        owner_addrs = sorted({a for o in owners for a in owner_addrs_map.get(o, ())})
        since = min(m["basis_at"] for m in email_matters)
        t_rows = pg.execute(
            "SELECT t.handle, t.chat, t.sender, t.created_at, t.subject, "
            "to_jsonb(t)->>'chat_type' AS chat_type, "
            "coalesce(to_jsonb(t)->'member_ids', '[]'::jsonb) AS member_ids, "
            "coalesce(to_jsonb(t)->'addresses', '[]'::jsonb) AS addresses, "
            "to_jsonb(t)->>'said' AS said, r.summary, r.quote "
            "FROM biz.teams_messages t "
            f"LEFT JOIN {SCHEMA}.readings r "
            "  ON r.source = 'teams_messages' AND r.message_key = t.handle "
            "WHERE t.created_at > %(since)s AND ("
            "  (t.sender LIKE '{%%' AND lower(coalesce(t.sender::jsonb->>'id', '')) = ANY(%(owners)s::text[])) "
            "  OR lower(coalesce(t.sender, '')) = ANY(%(owners)s::text[]) "
            "  OR lower(coalesce(t.sender, '')) = ANY(%(addrs)s::text[]))",
            {"since": since, "owners": owners, "addrs": owner_addrs},
        ).fetchall()
        by_owner_teams: dict[str, list[dict[str, Any]]] = {}
        for (handle, chat, sender, created_at, subject, chat_type,
             member_ids, addresses, said, r_summary, r_quote) in t_rows:
            tid, _shown, _kind = ar.teams_sender(sender)
            if not tid:
                continue
            sender_l = tid
            if "@" in tid:
                pids = staff.by_address.get(tid, [])
                if len(pids) == 1:
                    sender_l = pids[0].lower()
            mids = ar.teams_chat_member_ids(chat, member_ids or ())
            mids = ar.teams_chat_member_ids(handle, mids)
            one_on_one = (
                ar.is_one_on_one_teams_chat(chat, chat_type or "", mids)
                or ar.is_one_on_one_teams_chat(handle, chat_type or "", mids)
            )
            if not one_on_one or len(mids) > 2:
                continue
            addr_set = {str(a).strip().lower() for a in (addresses or ()) if a}
            by_owner_teams.setdefault(sender_l, []).append({
                "at": created_at,
                "mids": set(mids),
                "addrs": addr_set,
                "texts": [subject, said, r_summary, r_quote],
            })
        for m in email_matters:
            for msg in by_owner_teams.get(m["owner_l"], ()):
                if msg["at"] is None or msg["at"] <= m["basis_at"]:
                    continue
                if not ((msg["mids"] & m["cp_pids"]) or (msg["addrs"] & m["cp_addrs"])):
                    continue
                if cross_channel_topic_overlap(m["texts"], msg["texts"], ignore_names=m["names"]):
                    verdicts.append({
                        "matter": m["matter_id"],
                        "state": "done",
                        "reason": "replied_cross_channel",
                    })
                    break

    # Direction B: open Teams matter -> owner sent a direct email to the counterpart
    if teams_matters:
        owners = sorted({m["owner_l"] for m in teams_matters})
        owner_addrs = sorted({a for o in owners for a in owner_addrs_map.get(o, ())})
        if owner_addrs:
            since = min(m["basis_at"] for m in teams_matters)
            e_rows = pg.execute(
                "SELECT e.sender, e.to_addresses, e.subject, e.received_at, r.summary, r.quote "
                "FROM biz.emails e "
                f"LEFT JOIN {SCHEMA}.readings r "
                "  ON r.source = 'emails' AND r.message_key = coalesce(e.message_id, e.handle) "
                "WHERE e.received_at > %(since)s AND lower(e.sender) = ANY(%(addrs)s::text[])",
                {"since": since, "addrs": owner_addrs},
            ).fetchall()
            addr_to_owner: dict[str, set[str]] = {}
            for o in owners:
                for a in owner_addrs_map.get(o, ()):
                    addr_to_owner.setdefault(a, set()).add(o)
            by_owner_mail: dict[str, list[dict[str, Any]]] = {}
            for sender, to_addrs, subject, received_at, r_summary, r_quote in e_rows:
                sl = ar._bare_email(sender)
                to_set = {ar._bare_email(a) for a in (to_addrs or ()) if a}
                entry = {"at": received_at, "to": to_set, "texts": [subject, r_summary, r_quote]}
                for o in addr_to_owner.get(sl, ()):
                    by_owner_mail.setdefault(o, []).append(entry)
            for m in teams_matters:
                for msg in by_owner_mail.get(m["owner_l"], ()):
                    if msg["at"] is None or msg["at"] <= m["basis_at"]:
                        continue
                    if not (msg["to"] & m["cp_addrs"]):
                        continue
                    if cross_channel_topic_overlap(m["texts"], msg["texts"], ignore_names=m["names"]):
                        verdicts.append({
                            "matter": m["matter_id"],
                            "state": "done",
                            "reason": "replied_cross_channel",
                        })
                        break

    if not verdicts:
        return {}
    got = set_states(pg, verdicts)
    return {"replied_cross_channel": int(got.get("done", 0))}


STILL_OPS = {"in": "= ANY(%s)", "eq": "= %s", "is_null": "IS NULL", "not_null": "IS NOT NULL"}


def reverify_still_true(pg: Any) -> dict[str, int]:
    """``sources.still_true`` conditions of open matters: one statement per (view, column,
    op) for all matters at once; views only of the declared business model
    (``business_views.VIEWS``) and their declared columns. A false one closes the matter
    ``no_longer_true``; an undeclared view or column, or a read error, makes it
    ``unverified`` (said, never counted true)."""
    from operating.pg import business_views as bv
    rows = pg.execute(
        f"SELECT matter_id, c FROM {SCHEMA}.matters, jsonb_array_elements(sources->"
        "'still_true') c WHERE state IN ('open', 'unverified') AND jsonb_typeof(sources->"
        "'still_true') = 'array'").fetchall()
    groups: dict[tuple[str, str, str], list[tuple[str, Mapping]]] = {}
    bad: dict[str, str] = {}
    for mid, c in rows:
        view, col, op = str(c.get("view")), str(c.get("column")), str(c.get("op"))
        v = bv.VIEWS.get(view)
        if v is None or col not in {x.name for x in v.cols} or op not in STILL_OPS:
            bad[mid] = f"condition not checkable: {view}.{col} {op}"
            continue
        groups.setdefault((view, col, op), []).append((mid, c))
    false: set[str] = set()
    for (view, col, op), items in groups.items():
        handles = sorted({str(c.get("handle")) for _m, c in items})
        try:
            with pg.transaction():
                if op in ("in", "eq"):
                    got = pg.execute(
                        f'SELECT handle, "{col}"::text FROM {bv.SCHEMA}.{view} WHERE handle = '
                        "ANY(%s)", (handles,)).fetchall()
                else:
                    got = pg.execute(
                        f'SELECT handle, "{col}" {STILL_OPS[op]} FROM {bv.SCHEMA}.{view} '
                        "WHERE handle = ANY(%s)", (handles,)).fetchall()
        except Exception as exc:  # noqa: BLE001 - said: the matters become unverified
            LOG.warning("still_true %s.%s unreadable: %s", view, col, exc)
            for mid, _c in items:
                bad[mid] = f"{view} could not be read"
            continue
        have = dict(got)
        for mid, c in items:
            h = str(c.get("handle"))
            if h not in have:
                bad[mid] = f"{view} row {h} not found"
            elif op == "in":
                if have[h] not in [str(x) for x in (c.get("value") or [])]:
                    false.add(mid)
            elif op == "eq":
                if have[h] != str(c.get("value")):
                    false.add(mid)
            elif not have[h]:
                false.add(mid)
    verdicts = [{"matter": m, "state": "done", "reason": "no_longer_true"} for m in false] + [
        {"matter": m, "state": "unverified", "reason": why} for m, why in bad.items()
        if m not in false]
    if not verdicts:
        return {}
    return {f"still_true_{k}": n for k, n in set_states(pg, verdicts).items()}


_SET = f"""
WITH v AS (SELECT * FROM jsonb_to_recordset(%(v)s::jsonb) AS x(matter text, state text,
    reason text)),
old AS (SELECT m.matter_id, m.state FROM {SCHEMA}.matters m JOIN v ON v.matter = m.matter_id),
upd AS (
  UPDATE {SCHEMA}.matters m SET state = v.state,
      done_reason = CASE WHEN v.state = 'done' THEN v.reason END,
      done_at = CASE WHEN v.state = 'done' THEN now() END,
      verified_at = CASE WHEN v.state = 'open' THEN now() ELSE m.verified_at END,
      verify_note = CASE WHEN v.state = 'unverified' THEN v.reason ELSE '' END
  FROM v WHERE m.matter_id = v.matter AND m.state IN ('open', 'unverified')
  RETURNING m.matter_id, v.state, v.reason),
lg AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT u.matter_id, CASE u.state WHEN 'done' THEN 'closed' WHEN 'unverified'
         THEN 'unverified' ELSE 'verified' END, jsonb_build_object('reason', u.reason)
  FROM upd u JOIN old o USING (matter_id) WHERE o.state <> u.state RETURNING 1)
SELECT state, count(*) FROM upd GROUP BY 1"""


def set_states(pg: Any, verdicts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Matters by id (``matter``) take a verdict's ``state`` and ``reason`` in one
    statement (open ones verified now, done ones closed with the reason, unverified ones
    with why); a change of state is logged."""
    if not verdicts:
        return {}
    return {s: int(n) for s, n in pg.execute(_SET, {"v": json.dumps(list(verdicts))}).fetchall()}


_PERSONS = f"""
WITH said AS (
  SELECT DISTINCT ON (l.matter_id) l.matter_id, l.at, l.detail->>'decision' AS decision
  FROM {SCHEMA}.matter_log l JOIN {SCHEMA}.matters m USING (matter_id)
  WHERE l.what = 'closed' AND l.detail ? 'by' AND m.state IN ('open', 'unverified')
    AND m.legacy_item_id IS NULL AND l.at > m.basis_at
  ORDER BY l.matter_id, l.seq DESC),
upd AS (
  UPDATE {SCHEMA}.matters m SET
      state = CASE WHEN s.decision = 'reject' THEN 'dropped' ELSE 'done' END,
      done_reason = CASE WHEN s.decision = 'reject' THEN 'person_dropped' ELSE 'person_done' END,
      done_at = s.at, verify_note = ''
  FROM said s WHERE m.matter_id = s.matter_id
  RETURNING m.matter_id, m.state, m.done_reason),
lg AS (
  INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail)
  SELECT matter_id, 'closed', jsonb_build_object('reason', done_reason, 'applied', true)
  FROM upd RETURNING 1)
SELECT state, count(*) FROM upd GROUP BY 1"""


def apply_persons(pg: Any) -> dict[str, int]:
    """The person's own word on a matter (a ``closed`` row the API wrote, with ``by``, later
    than the matter's basis): ``done`` (accept) or ``dropped`` (not needed), in one
    statement, logged. A matter made from a queue item follows that item instead. A later
    ask (a newer basis) is a new matter state, not closed by an older word."""
    return {f"person_{s}": int(n) for s, n in pg.execute(_PERSONS).fetchall()}


# --------------------------------------------------------------------------- shown, listed

def shown_to(ids: Iterable[str], *, rows: Optional[Sequence[Mapping[str, Any]]],
             draft: Mapping[str, Sequence[str]]) -> Optional[bool]:
    """Whether the person (any of their ids and addresses, ``ids``) is shown their matters:
    an opt-in of theirs that is on and not paused (``autopilot_write.opt_ins`` /
    ``paused`` over the write journal's ``rows``), or the owner's draft file naming them
    (``autopilot_engine.load_draft``). ``rows`` None (the journal unreadable): None, and
    the caller fails closed (nobody is shown, said ``opt_in_unreadable``)."""
    if rows is None:
        return None
    from operating import autopilot_write as aw
    who = {str(i).strip().lower() for i in ids if i}
    if who & {str(k).strip().lower() for k in draft}:
        return True
    stopped = set(aw.paused(rows))
    return any(entry["on"] and person not in stopped
               for (person, _flow), entry in aw.opt_ins(rows).items() if person in who)


# The list's one face of the Assist tables (migration 0065): the API's role reads this view
# and nothing else of ``assist`` (plx.sql's role reads none of it).
ITEMS_VIEW = f"{SCHEMA}.matter_items"
HEARTBEAT_VIEW = f"{SCHEMA}.matters_heartbeat"   # the newest cycle's time (F7: a stale list says so)
STALE_MINUTES = 10


def _items_view_sql(biz: bool, extra: int = 0) -> str:
    """The list's view; ``biz``: with the sender and subject of the basis message from the
    business communications views (when those exist: a fresh database has none yet)."""
    # 0073 appends these (a view's columns may only be added at the end): when the draft was
    # last planned and when it was last asked for
    more = (", m.plan_at, (SELECT max(l.at) FROM " + SCHEMA + ".matter_log l WHERE l.matter_id = "
            "m.matter_id AND l.what = 'requested') AS requested_at") if extra else ""
    if extra and int(extra) >= 2:      # 0076 appends the matter's last activity ("still open?")
        more += ", m.touched_at"
    senders = ("e.sender, e.subject, t.sender AS tsender" if biz else
               "NULL::text AS sender, NULL::text AS subject, NULL::text AS tsender")
    joins = ("LEFT JOIN LATERAL (SELECT x.sender, x.subject FROM biz.emails x WHERE b.source = "
             "'emails' AND x.handle = b.handles[1] LIMIT 1) e ON true LEFT JOIN LATERAL "
             "(SELECT y.sender FROM biz.teams_messages y WHERE b.source = 'teams_messages' AND "
             "y.handle = b.handles[1] LIMIT 1) t ON true " if biz else "")
    return (f"CREATE OR REPLACE VIEW {ITEMS_VIEW} AS SELECT m.matter_id, m.owner_person, m.kind, "
            "m.subject_key, m.sources, m.basis_at, m.title, m.detail, m.next_act, m.due_at, "
            "m.state, m.verified_at, m.verify_note, m.plan, m.plan_status, m.plan_reason, "
            "m.audiences, m.markings, m.counterpart, m.legacy_item_id, m.opened_at, "
            "coalesce((SELECT array_agg(DISTINCT h) FROM jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(m.sources->'readings') = 'array' THEN m.sources->'readings' ELSE "
            f"'[]'::jsonb END) s JOIN {SCHEMA}.readings r ON r.source = s->>0 AND r.message_key = "
            f"s->>1, unnest(r.handles) h), '{{}}') AS handles, b.source, b.quote, {senders}{more} "
            f"FROM {SCHEMA}.matters m LEFT JOIN {SCHEMA}.readings b ON "
            "jsonb_typeof(m.sources->'readings') = 'array' AND b.source = m.sources->'readings'->-1"
            f"->>0 AND b.message_key = m.sources->'readings'->-1->>1 {joins}"
            "WHERE m.state IN ('open', 'unverified')")


_BIZ_THERE = "to_regclass('biz.emails') IS NOT NULL AND to_regclass('biz.teams_messages') IS NOT NULL"


def items_view_ddl(extra: int = 0) -> list[str]:
    """Migration 0065's statements (a test holds them equal to the file). The view is made
    with the business communications views' sender and subject when they exist and without
    them when they do not (a fresh database); ``ensure_items_view`` makes the full one."""
    return [
        "DO $view$ BEGIN IF " + _BIZ_THERE + " THEN EXECUTE $q$" + _items_view_sql(True, extra)
        + "$q$; ELSE EXECUTE $q$" + _items_view_sql(False, extra) + "$q$; END IF; END $view$",
        f"CREATE OR REPLACE VIEW {HEARTBEAT_VIEW} AS SELECT max(at) AS last_cycle_at FROM "
        f"{SCHEMA}.matter_log WHERE matter_id = '_cycle'",
        f"REVOKE ALL ON {ITEMS_VIEW} FROM PUBLIC",
        f"REVOKE ALL ON {HEARTBEAT_VIEW} FROM PUBLIC",
        f"REVOKE ALL ON {SCHEMA}.matter_log FROM PUBLIC",
        "DO $api$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN "
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO plx_api; GRANT SELECT ON {ITEMS_VIEW}, "
        f"{HEARTBEAT_VIEW} TO plx_api; "
        f"GRANT INSERT ON {SCHEMA}.matter_log TO plx_api; GRANT USAGE ON SEQUENCE "
        f"{SCHEMA}.matter_log_seq_seq TO plx_api; END IF; END $api$",
    ]


def ensure_items_view(pg: Any) -> str:
    """The full list view once the business communications views exist (a view made before
    them has no sender or subject); 'full', 'no_biz' (not there yet) or 'failed:<class>'
    (logged: the list keeps the view it has). The grants of the view stay."""
    try:
        if not pg.execute("SELECT " + _BIZ_THERE).fetchone()[0]:
            return "no_biz"
        with pg.transaction():
            pg.execute(_items_view_sql(True, 2))
        return "full"
    except Exception as exc:  # noqa: BLE001 - said in the cycle's counts and the log
        LOG.warning("assist list view not refreshed: %s", type(exc).__name__, exc_info=True)
        return f"failed:{type(exc).__name__}"


# a wait is out of the list until it is due (a due time of NULL: the person is asked at once)
_ITEMS = (f"SELECT * FROM {ITEMS_VIEW} WHERE (%(everyone)s OR owner_person = ANY(%(owners)s)) "
          "AND (kind <> 'wait' OR due_at IS NULL OR due_at <= now() OR state = 'unverified') "
          "ORDER BY basis_at DESC, matter_id")
_ITEM_ONE = (f"SELECT * FROM {ITEMS_VIEW} WHERE matter_id = %(matter_id)s "
             "AND (%(everyone)s OR owner_person = ANY(%(owners)s)) "
             "AND (kind <> 'wait' OR due_at IS NULL OR due_at <= now() OR state = 'unverified')")


def _channel(source: Optional[str]) -> str:
    return {"emails": "mail", "teams_messages": "teams"}.get(source or "", "system")


_HEX_OPAQUE = re.compile(r"[0-9a-f-]{16,}", re.IGNORECASE)
_PROTECTED_TOKEN = re.compile(r"(?:[a-z][a-z0-9+.-]*://\S+|\S+@\S+)", re.IGNORECASE)
_COMMA_NO_SPACE = re.compile(r",([A-Za-z])")
_PERIOD_NO_SPACE = re.compile(r"([a-z]{2,})\.([A-Z][a-z])")
_RAW_REASON_CODE = re.compile(r"^(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))$")
_PROMPT_LABEL_PAREN = re.compile(r"\s*\(P\d{1,2}\)")
_PROMPT_LABEL_FROM = re.compile(r"\bfrom\s+P\d{1,2}\b")
_EM_DASH_SPACED = re.compile(r"\s*\u2014\s*")
_MC_TRAILING_ON = re.compile(r"^Mission Control question on\s*$")
_ROBOTIC_OPENERS = re.compile(
    r"^(?:I have reviewed (?:the |your )?(?:records|data|thread|message|email|details)[,.:;]?\s*|"
    r"Based on (?:the |my review of the )?(?:data|records|available records|thread|information)[,.:;]?\s*|"
    r"Here is a summary(?: of [^:.\n]{1,60})?[:,]\s*|"
    r"I hope this (?:email |message )?finds you well[.!,]?\s*)",
    re.IGNORECASE,
)
_WAIT_FROM_NAME = re.compile(
    r"(?:\bfrom\s+([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})\b|"
    r"^([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,1})\s*:)"
)
_WAIT_NO_HISTORY = re.compile(
    r"\s*\(nothing in your mail says how long this usually takes:\s*when should it be looked for\?\)",
    re.IGNORECASE,
)
_PLAN_SYSTEM_NAMES: dict[str, str] = {
    "bc": "Business Central",
    "sage": "Sage 300",
    "portal": "Portal",
    "mc": "Mission Control",
    "filemaker": "FileMaker",
    "planner": "Planner",
    "calendar": "Calendar",
    "sharepoint": "SharePoint",
    "mail": "Mail",
    "teams": "Teams",
}
UNKNOWN_OWNER = "another team member"  # guard-ok: display fallback for an unresolved owner id, decides nothing


def clean_user_text(text: Any, *, strip_opener: bool = False) -> str:
    """Clean user-facing Assist text: replace em dashes with plain hyphens, remove leaked
    internal prompt labels like ``(P2)`` or ``from P2``, fix trailing empty MC labels, and
    optionally strip robotic AI boilerplate openers."""
    raw = str(text or "")
    if not raw:
        return ""
    s = _EM_DASH_SPACED.sub(" - ", raw)
    s = _PROMPT_LABEL_PAREN.sub("", s)
    s = _PROMPT_LABEL_FROM.sub("from them", s)
    s = _MC_TRAILING_ON.sub("Mission Control question", s)
    if strip_opener:
        trimmed = s.lstrip()
        replaced = _ROBOTIC_OPENERS.sub("", trimmed, count=1).lstrip()
        if replaced:
            s = replaced[0].upper() + replaced[1:] if replaced[0].islower() else replaced
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _infer_wait_person(detail: str) -> str:
    """Extract a concrete person or team name from a wait matter's detail when the stored
    title says 'No answer yet from them'."""
    cleaned = clean_user_text(detail)
    m = _WAIT_FROM_NAME.search(cleaned)
    if not m:
        return ""
    name = (m.group(1) or m.group(2) or "").strip(" .,;:")
    if not name or name.lower() in ("them", "the", "a", "an", "someone", "whoever", "source"):
        return ""
    return name


def _planned_details(plan: Mapping[str, Any], names: Optional[Mapping[str, str]] = None
                     ) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Extract concise human-readable facts, system to-dos, and recipients from ``plan.acts``
    when no full draft exists yet, so planned matters show Assist's recommended next step."""
    acts = plan.get("acts")
    if not isinstance(acts, list) or not acts:
        return [], [], []
    facts: list[str] = []
    todos: list[dict[str, str]] = []
    recipients: list[str] = []
    for a in acts[:3]:
        if not isinstance(a, Mapping):
            continue
        act_kind = str(a.get("act") or "").strip()
        if act_kind == "nothing":
            continue
        to_list = [str(t).strip() for t in (a.get("to") or ()) if str(t).strip()]
        for t in to_list:
            if t not in recipients:
                recipients.append(t)
        shown_to = [person_label(t, names) if "@" not in t and ":" in t else t for t in to_list]
        about = clean_user_text(a.get("about") or "")
        attach = clean_user_text(a.get("attach") or "")
        question = clean_user_text(a.get("question") or "")
        if act_kind == "system_write":
            sys_info = a.get("system") if isinstance(a.get("system"), Mapping) else {}
            sys_raw = str(sys_info.get("system") or "").strip().lower()
            op = str(sys_info.get("operation") or "").replace("_", " ").strip()
            rec = clean_user_text(sys_info.get("record") or "")
            what = about or op or "Update record"
            todos.append({
                "system": sys_raw,
                "record": rec,
                "what": what,
            })
            continue
        verb = ACT_WORDS.get(act_kind, "")
        if act_kind == "ask_person" and question:
            facts.append(f"Decision needed: {question}")
        elif verb:
            target = f" to {', '.join(shown_to)}" if shown_to else ""
            doc = f" ({attach})" if attach else ""
            detail_part = f": {about}" if about else ""
            facts.append(f"Suggested next step: {verb}{target}{doc}{detail_part}")
    return facts, todos, recipients


def clean_note_spacing(text: Any) -> str:
    """Fix missing spaces after commas and sentence periods in Assist card notes (such as
    already-stored rows reading 'list,not yours.Only you can see this.'), while leaving URLs,
    email addresses, decimals, and line breaks intact."""
    raw = str(text or "")
    if not raw:
        return ""
    lines: list[str] = []
    for line in raw.split("\n"):
        parts: list[str] = []
        pos = 0
        for match in _PROTECTED_TOKEN.finditer(line):
            seg = line[pos:match.start()]
            seg = _COMMA_NO_SPACE.sub(r", \1", seg)
            seg = _PERIOD_NO_SPACE.sub(r"\1. \2", seg)
            parts.append(seg)
            parts.append(match.group(0))
            pos = match.end()
        tail = line[pos:]
        tail = _COMMA_NO_SPACE.sub(r", \1", tail)
        tail = _PERIOD_NO_SPACE.sub(r"\1. \2", tail)
        parts.append(tail)
        lines.append("".join(parts))
    return "\n".join(lines)


def name_from_token(raw: str) -> str:
    """A human name derived from a plain name token or email local part, else '' (rejects
    UUIDs, digits, opaque hex strings, and object handles)."""
    from operating.autopilot_engine import person_name
    text = str(raw or "").strip()
    if not text or "://" in text or "{" in text or "}" in text or "/" in text:
        return ""
    if _HEX_OPAQUE.fullmatch(text):
        return ""
    words = text.replace(".", " ").replace("_", " ").strip()
    if not words or not person_name(words):
        return ""
    if any(not person_name(part) or _HEX_OPAQUE.fullmatch(part) for part in words.split()):
        return ""
    return words.title()


def _name_from_address(addr: str) -> str:
    text = str(addr or "").strip()
    return name_from_token(text.split("@", 1)[0]) if "@" in text else ""


def _staff_names(pg: Any) -> dict[str, str]:
    """Lower-case staff id and unambiguous staff address -> human display name, read once
    from ``biz.staff`` (``assist_readings.staff_of``: one directory definition)."""
    from operating import assist_readings as ar
    from operating.autopilot_engine import person_name
    staff = ar.staff_of(pg, "biz")
    out: dict[str, str] = {}
    for pid, (name, addr) in staff.by_id.items():
        shown = person_name(name) or _name_from_address(addr)
        if shown:
            out[pid.strip().lower()] = shown
    for addr, pids in staff.by_address.items():
        if len(pids) == 1:
            shown = out.get(pids[0].strip().lower())
            if shown:
                out[addr.strip().lower()] = shown
    return out


def _unresolved_ids(rows: Sequence[Mapping[str, Any]], *, mine: set[str], owner_view: bool,
                    names: Mapping[str, str]) -> list[str]:
    """Ids needed to render ``rows`` that ``biz.staff`` did not name: other owners (when
    ``owner_view``) and Teams or non-email senders without a stated display name."""
    from operating.autopilot_engine import person_name
    need: set[str] = set()
    for r in rows:
        owner = str(r.get("owner_person") or "").strip().lower()
        if owner_view and owner and owner not in mine:
            tail = owner.rsplit(":", 1)[-1]
            if owner not in names and tail not in names:
                need.add(tail)
        if r.get("source") == "teams_messages":
            try:
                got = json.loads(r.get("tsender") or "{}")
            except ValueError:
                got = {}
            if isinstance(got, Mapping) and not person_name(got.get("display_name")):
                sid = str(got.get("id") or "").strip().lower()
                if sid and sid not in names:
                    need.add(sid)
        else:
            raw = str(r.get("sender") or "").strip().lower()
            if raw and "@" not in raw and raw not in names:
                need.add(raw)
    return sorted(need)


def _sender(row: Mapping[str, Any], names: Optional[Mapping[str, str]] = None) -> str:
    from operating.autopilot_engine import person_name
    if row["source"] == "teams_messages":
        try:
            got = json.loads(row["tsender"] or "{}")
        except ValueError:
            got = {}
        if not isinstance(got, Mapping):
            return ""
        shown = person_name(got.get("display_name"))
        if shown:
            return shown
        sid = str(got.get("id") or "").strip().lower()
        if sid and names:
            found = person_name(names.get(sid))
            if found:
                return found
        return ""
    raw = str(row["sender"] or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    if names:
        found = person_name(names.get(raw.lower()))
        if found:
            return found
    if "://" in raw or "{" in raw or "}" in raw or _HEX_OPAQUE.fullmatch(raw):
        return ""
    return person_name(raw)


def person_label(owner: str, names: Optional[Mapping[str, str]] = None) -> str:
    """How a matter's owner reads on a card that is not the reader's: their display name
    from the staff directory (``names``), a role pool's name (``pool:quality`` ->
    ``the Quality team``), a plain name or email local part when the id itself carries
    one (``tenant:pat`` -> ``Pat``), or ``another team member`` when the id is an opaque
    token or UUID with no known name."""
    from operating.autopilot_engine import person_name
    from operating.autopilot_queue import pool_role
    raw = str(owner or "").strip()
    if not raw:
        return UNKNOWN_OWNER
    key = raw.lower()
    if names:
        for candidate in (key, key.rsplit(":", 1)[-1]):
            found = person_name(names.get(candidate))
            if found:
                return found
    role = pool_role(raw)
    if role:
        shown = name_from_token(role)
        return f"the {shown} team" if shown else UNKNOWN_OWNER
    if "://" in raw or "{" in raw or "}" in raw:
        return UNKNOWN_OWNER
    if "@" in raw:
        return _name_from_address(raw) or UNKNOWN_OWNER
    return name_from_token(raw.rsplit(":", 1)[-1]) or UNKNOWN_OWNER


def item_of(row: Mapping[str, Any], *, mark_unshown: bool = False, theirs: bool = False,
            names: Optional[Mapping[str, str]] = None,
            now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """A matter in the shape ``/api/autopilot/drafts`` lists (``m.AutopilotPrepared``), with
    its ``state``, ``verified_at``, ``plan_status`` and ``plan_reason``, and the door's
    inputs (``audiences``, ``markings``, ``citations``) for the API's check."""
    plan = row["plan"] or {}
    prepared = plan.get("prepared") if isinstance(plan, Mapping) else None
    draft = (prepared or {}).get("draft") if isinstance(prepared, Mapping) else None
    act = ACT_WORDS.get(row["next_act"] or "", "")
    raw_detail = clean_user_text(row["detail"] or "")
    raw_title = clean_user_text(row["title"] or "")
    if row["kind"] == "wait":
        if raw_title.lower() == "no answer yet from them":
            inferred = _infer_wait_person(raw_detail)
            if inferred:
                raw_title = f"No answer yet from {inferred}"
        if _WAIT_NO_HISTORY.search(raw_detail):
            raw_detail = _WAIT_NO_HISTORY.sub("", raw_detail).strip(" ;,")
            if raw_detail:
                raw_detail = f"{raw_detail} (no typical reply time on record yet)"
            else:
                raw_detail = "No typical reply time on record yet"
    note = raw_detail or act
    drafting = False
    if row["state"] == "unverified":
        vnote = clean_user_text(row.get("verify_note") or "")
        if vnote and not _RAW_REASON_CODE.match(vnote.split(":", 1)[0].strip()):
            note = f"This {UNVERIFIED_WORDS} ({vnote})."
        else:
            note = f"This {UNVERIFIED_WORDS}."
    elif (row.get("requested_at") is not None and row["requested_at"] > (
            row.get("plan_at") or dt.datetime.min.replace(tzinfo=dt.timezone.utc))):
        note = "Drafting. It shows here when it is ready."     # asked for, not yet answered
        drafting = True
    elif row["plan_status"] == "failed":
        note = "Could not prepare a draft for this item. Ask for it again, or handle it directly."
    elif row["plan_status"] == "deferred":
        note = "Draft on request."
    now = now or dt.datetime.now(dt.timezone.utc)
    from operating import assist_activity
    silent = assist_activity.still_open(row["due_at"], row.get("touched_at"), row["basis_at"], now)
    if silent and not drafting:   # nothing since it was due (a draft just asked for is the person acting)
        lead = f"Still open? Nothing has happened on it since it was due {row['due_at']:%Y-%m-%d}."
        note = f"{lead} {note}" if note else lead
    # the card's own words first; what only the owner's reading adds goes on its own line, so
    # it never runs into the sentence before it
    base = clean_note_spacing(note.strip())
    if theirs:
        owner_text = person_label(row["owner_person"], names)
        label = f"This is on {owner_text}'s list, not yours. Only you can see this."
        note = f"{base}\n{label}" if base else f"\n{label}"
    elif mark_unshown:
        label = "Only you can see this."
        note = f"{base}\n{label}" if base else f"\n{label}"
    else:
        note = base
    note = clean_note_spacing(note)
    body = clean_user_text((draft or {}).get("body") or "", strip_opener=True) if isinstance(draft, Mapping) else ""
    call = (prepared or {}).get("your_call") if isinstance(prepared, Mapping) else None
    ask = clean_user_text(call.get("ask") or "") if isinstance(call, Mapping) else ""
    raw_options = (call.get("options") or []) if isinstance(call, Mapping) else []
    options = [
        {"label": clean_user_text(o.get("label") or ""), "reply": clean_user_text(o.get("reply") or "", strip_opener=True)}
        for o in raw_options
        if isinstance(o, Mapping) and str(o.get("reply") or "").strip()
    ]
    planned_facts: list[str] = []
    planned_todos: list[dict[str, str]] = []
    planned_to: list[str] = []
    if not body and not ask and isinstance(plan, Mapping) and row["plan_status"] == "planned":
        planned_facts, planned_todos, planned_to = _planned_details(plan, names)
    sender = _sender(row, names)
    if row["kind"] in ("wait", "promise"):
        cp = clean_user_text(row.get("counterpart") or "")
        if cp:
            sender = cp
        elif row["kind"] == "wait":
            inferred = _infer_wait_person(raw_detail)
            if inferred:
                sender = inferred
    to_list = ([str(t) for t in (draft or {}).get("to") or ()]
               if isinstance(draft, Mapping) and (draft or {}).get("to")
               else planned_to)
    return {
        "item_id": row["matter_id"], "owner": row["owner_person"], "kind_of_matter": row["kind"],
        "sender": sender, "channel": _channel(row["source"]),
        "subject": clean_user_text(row["subject"] or raw_title or ""),
        "message": clean_user_text(row["quote"] or ""),
        "kind": ("your_call" if ask else "todo") if row["kind"] == "task" else (
            "reply" if body else "your_call" if ask else "todo"),
        "ask": ask, "summary": raw_title,
        "next_act": str(row["next_act"] or ""),
        "note": note, "body": body, "still_open": silent,
        "to": to_list,
        "facts": planned_facts,
        "todos": planned_todos,
        "options": options,
        "redo": bool(body),
        # a matter made from a queue item is listed as that item's own card
        "queue_card": bool(row["legacy_item_id"]),
        "state": row["state"], "verified_at": row["verified_at"],
        "plan_status": "working" if drafting else row["plan_status"],
        "plan_reason": row["plan_reason"], "plan_at": row.get("plan_at"),
        "requested_at": row.get("requested_at"), "basis_at": row["basis_at"], "due_at": row["due_at"],
        "sources_detail": dict(row["sources"] or {}),
        "audiences": list(row["audiences"] or []), "markings": list(row["markings"] or []),
        "citations": sorted(set(row["handles"] or [])
                            | {str(h) for h in (row["sources"] or {}).get("records") or ()
                               if str(h).count("://")}),
    }


def as_items(pg: Any, owners: Iterable[str], *, shown: Optional[bool], owner_view: bool = False,
             everyone: bool = False, all_ages: bool = False,
             matter_id: Optional[str] = None,
             door: Callable[[Sequence[Mapping[str, Any]]], set] = lambda _items: set(),
             names_of: Optional[Callable[[Iterable[str]], Mapping[str, str]]] = None,
             now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """A person's open and unverified matters as the C1 list envelope ``{items, total,
    next, status, as_of, older, reason}`` (``stale`` when the newest cycle is older than
    ``STALE_MINUTES``: F7), newest basis first: those of the last ``LIST_DAYS`` days
    unless ``all_ages`` (``older`` counts the rest). ``door(items)``: the reader's door,
    once for all items, giving the ids they may see (default: none, fail closed).
    ``shown``: ``shown_to`` for the person; False -> ``status = off`` with no items, None ->
    off with ``opt_in_unreadable``; the owner (``owner_view``) gets them anyway, each marked
    "not shown to staff" (``everyone``: every person's)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not owner_view and not shown:
        return {"items": [], "total": 0, "next": None, "older": 0,
                "status": "off", "reason": OPT_IN_UNREADABLE if shown is None else "not_opted_in"}
    owner_list = sorted(set(owners)) if everyone else _expand_owner_role_pools(pg, list(owners))
    if matter_id is not None:
        cur = pg.execute(_ITEM_ONE, {"matter_id": matter_id, "everyone": everyone,
                                     "owners": owner_list})
    else:
        cur = pg.execute(_ITEMS, {"everyone": everyone, "owners": owner_list})
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    mine = {str(o).strip().lower() for o in owner_list}
    names = _staff_names(pg) if rows else {}
    if names_of is not None and rows:
        missing = _unresolved_ids(rows, mine=mine, owner_view=owner_view, names=names)
        if missing:
            for k, v in (names_of(missing) or {}).items():
                kl = str(k or "").strip().lower()
                if kl and kl not in names and v:
                    names[kl] = str(v)
    candidates = [item_of(r, mark_unshown=owner_view and not shown,
                          theirs=owner_view and str(r["owner_person"] or "").strip().lower() not in mine,
                          names=names, now=now)
                  for r in rows]
    allowed = door(candidates) if candidates else set()
    since = now - dt.timedelta(days=LIST_DAYS)
    items, older = [], 0
    for row, item in zip(rows, candidates):
        if item["item_id"] not in allowed:
            continue
        if not all_ages and row["basis_at"] is not None and row["basis_at"] < since:
            older += 1
            continue
        items.append(item)
    last = pg.execute(f"SELECT last_cycle_at FROM {HEARTBEAT_VIEW}").fetchone()[0]
    stale = last is None or last < now - dt.timedelta(minutes=STALE_MINUTES)
    return {"items": items, "total": len(items) + older, "next": None, "older": older,
            "status": "stale" if stale else "ok", "as_of": last,
            "reason": ("the matters were last re-checked " + (last.isoformat() if last else
                       "never")) if stale else ""}


# --------------------------------------------------------------------------- cycle

def cycle(pg: Any, *, now: Optional[dt.datetime] = None, queue: Optional[Path] = None,
          state: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None
          ) -> dict[str, Any]:
    """One matters cycle (module text) under one advisory lock, in one transaction: open
    from each source, re-verify every open matter, then a heartbeat row
    (``matter_log`` what = 'verified', matter ``_cycle``, the counts)."""
    env = os.environ if environ is None else environ
    now = now or dt.datetime.now(dt.timezone.utc)
    with pg.transaction():
        if not pg.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK,)).fetchone()[0]:
            return {"status": "skipped", "reason": "another matters cycle holds the lock"}
        out: dict[str, Any] = {"status": "ok"}
        out["items_view"] = ensure_items_view(pg)
        out["readings"] = open_from_readings(pg, now=now)
        people = people_of(pg)
        opened, verdicts = approvals(pg, people)
        out["approvals"] = {**upsert(pg, opened), **apply(pg, "approval", verdicts)}
        questions = mc_questions(state) if state and (state / "mc-capture" / "capture.sqlite"
                                                      ).exists() else None
        opened, verdicts, counts = tasks(pg, people, questions, now=now)
        if counts["questions_unreadable"]:
            verdicts = [v for v in verdicts if not v["subject"].startswith("mc-question:")]
        out["tasks"] = {**counts, **upsert(pg, opened),
                        **apply(pg, "task", verdicts, only="m.legacy_item_id IS NULL")}
        kinds = [k for k in str(env.get(ALERT_KINDS_ENV) or "").split(",") if k.strip()]
        opened, verdicts = alerts(pg, [k.strip() for k in kinds])
        out["alerts"] = {**upsert(pg, opened), **(apply(
            pg, "record", verdicts, only="m.sources ? 'alert'") if kinds else {})}
        if queue is not None and queue.exists():
            items = queue_items(queue, REMAINING)
            out["queue"] = upsert(pg, from_queue(pg, items))
            out["queue"].update(settle_queue(pg, queue))
        out["qc_reroute"] = reroute_qc_hold_matters(pg)
        from operating import assist_waits
        out["waits"] = assist_waits.open_from_readings(pg, _tz())
        out["waits_deduped"] = assist_waits.reconcile_duplicate_promises(pg)
        out["system_noise"] = retire_system_noise_matters(pg)
        from operating import assist_routines
        out["routines"] = {**assist_routines.open_from_routines(pg, now), **assist_routines.reverify(pg)}
        out["persons"] = apply_persons(pg)
        out["conversations"] = reverify_conversations(pg)
        out["cross_channel"] = reverify_cross_channel_replies(pg, people=people)
        from operating import assist_activity
        out["activity"] = assist_activity.cycle(pg)
        out["waits_closed"] = assist_waits.reverify(pg)
        out["still_true"] = reverify_still_true(pg)
        pg.execute(f"INSERT INTO {SCHEMA}.matter_log (matter_id, what, detail) VALUES "
                   "('_cycle', 'verified', %s)", (json.dumps({"cycle": out}, default=str),))
    return out


def settle_queue(pg: Any, queue: Path) -> dict[str, int]:
    """Queue-made matters follow their item: open while it is open, done with the item's
    own done reason once the queue settled it, unverified when the item is gone."""
    conn = sqlite3.connect(f"file:{queue}?mode=ro", uri=True, timeout=30)
    try:
        # approvals and conversations are re-checked against their own source; the rest
        # (the six remaining triggers' records and routines) follow their item
        legacy = [r[0] for r in pg.execute(
            f"SELECT DISTINCT legacy_item_id FROM {SCHEMA}.matters WHERE legacy_item_id IS NOT "
            "NULL AND kind NOT IN ('approval', 'conversation') AND state IN ('open', "
            "'unverified')").fetchall()]
        found = {}
        for i in range(0, len(legacy), 500):
            part = legacy[i:i + 500]
            found.update({r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT item_id, state, done_reason FROM items WHERE item_id IN (%s)"
                % ",".join("?" * len(part)), part)})
    finally:
        conn.close()
    verdicts = []
    for item in legacy:
        st, why = found.get(item, (None, None))
        verdicts.append({"subject": item, "state": "unverified", "reason": "queue item gone"}
                        if st is None else {"subject": item, "state": "open", "reason": ""}
                        if st == "open" else {"subject": item, "state": "done",
                                              "reason": f"queue:{why or st}"})
    if not verdicts:
        return {}
    ids = dict(pg.execute(
        f"SELECT legacy_item_id, array_agg(matter_id) FROM {SCHEMA}.matters WHERE "
        "legacy_item_id = ANY(%s) AND kind NOT IN ('approval', 'conversation') AND state IN "
        "('open', 'unverified') GROUP BY 1", ([v["subject"] for v in verdicts],)).fetchall())
    got = set_states(pg, [{**v, "matter": m} for v in verdicts for m in ids.get(v["subject"], ())])
    return {f"queue_{k}": n for k, n in got.items()}


def adopt(pg: Any, queue: Path, *, now: Optional[dt.datetime] = None,
          state: Optional[Path] = None) -> dict[str, int]:
    """The one-off move (spec 2.3): every open queue item becomes a matter
    (``legacy_item_id``), then the first cycle decides them. Done items are not moved."""
    with pg.transaction():
        items = queue_items(queue, None)
        moved = upsert(pg, from_queue(pg, items))
    out = cycle(pg, now=now, queue=queue, state=state, environ={})
    got = pg.execute(
        f"SELECT state, count(*) FROM {SCHEMA}.matters WHERE legacy_item_id = ANY(%s) GROUP BY 1",
        ([i["item_id"] for i in items],)).fetchall()
    by = {s: int(n) for s, n in got}
    return {"items": len(items), "moved": moved["opened"], "open": by.get("open", 0),
            "closed_by_first_reverify": by.get("done", 0), "unverified": by.get("unverified", 0),
            "cycle": out.get("status")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.assist_matters")
    p.add_argument("command", choices=("cycle", "adopt"))
    p.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"))
    p.add_argument("--queue", default=os.environ.get(QUEUE_ENV) or None)
    p.add_argument("--state-dir", default="/data/plx-state")
    a = p.parse_args(argv)
    import psycopg
    logging.basicConfig(level=logging.INFO)
    queue = Path(a.queue) if a.queue else None
    with psycopg.connect(a.dsn, autocommit=True) as pg:
        if pg.execute("SELECT to_regclass(%s) IS NULL", (f"{SCHEMA}.matters",)).fetchone()[0]:
            print(json.dumps({"status": "not_run", "reason": "assist.matters not migrated yet "
                              "(0063)"}))
            return 0
        if a.command == "adopt":
            if queue is None or not queue.exists():
                print(json.dumps({"status": "failed", "reason": "no queue file"}))
                return 2
            out: dict[str, Any] = adopt(pg, queue, state=Path(a.state_dir))
        else:
            out = cycle(pg, queue=queue, state=Path(a.state_dir))
    print(json.dumps(out, default=str))
    return 0 if out.get("status", "ok") in ("ok", "skipped") or a.command == "adopt" else 1


if __name__ == "__main__":
    sys.exit(main())
