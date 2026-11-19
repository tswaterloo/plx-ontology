"""A person's recurring work, read from what they actually did in every captured system.

The owner asked for "being able to do everyone's full jobs": not only replies, but the work a
person does again and again (every morning, when a supplier confirms, at the end of the
week). This module reads one person's own actions over the last ``DAYS`` days, read-only,
from the capture stores (mail they sent, Teams messages they posted, Mission Control
approvals they decided and questions they answered, SharePoint files they changed, Planner
tasks they created or completed, meeting series they organise) and finds what recurs:

* **on an event**: an action that answers something someone else did in the same thread or
  record (a reply in the mail conversation or chat, a decision on an approval, an answer to
  a question), grouped by what it answers (the counterpart's domain, the chat kind, the
  approval or question); recurring when it happened on at least ``MIN_DAYS`` days;
* **on a schedule**: an action the person started themselves in the same series (the mail
  subject without dates and numbers to the same people, the same chat, the same file, the
  same meeting series) on most weekdays (daily) or on the same weekday most weeks (weekly),
  at about the same time of day (US Eastern).

Each job says which autopilot prepares it (``COVERAGE``) and, when none can today, what
blocks it (the source is not captured or is refused, the autopilot is off, nothing
prepares that kind of trigger yet).

People first, fail closed: with the people exclusion unavailable nothing is read; an
action whose counterpart (the sender it answers, a recipient, a chat member who wrote) the
exclusion does not clear is left out. No content leaves here beyond what names a series to
its own person: counts, times, systems, domains, digests and the handles of past instances
(the jobs mechanism opens those as the person, through the permission door).

    python3 -m operating.recurring_work --state /data/plx-state --person <address> [--days 60]

Evidence: isolated-fixture (``operating/tests/test_recurring_work.py``); a run on the host's
stores is a live read-only measurement.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import statistics
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

SCHEMA = "plx-recurring-work/v1"
TZ = ZoneInfo("America/New_York")
DAYS = 60
MIN_DAYS = 5               # an event job: done on at least this many days
TRIGGER_WINDOW = timedelta(days=3)   # an action answers what arrived at most this long before
DAILY_SHARE = 0.6          # daily: active on this share of the weekdays it spans
WEEKLY_MIN = 3             # weekly: on its weekday in at least this many weeks...
WEEKLY_SHARE = 0.5         # ...and on this share of the weeks it spans
WEEKDAY_SHARE = 0.6        # ...and that weekday holds this share of all its days
SPREAD_MINUTES = 150       # a schedule: half its instances within this of the usual time
EXAMPLES = 5
RECENT = timedelta(days=7)  # coverage: what was prepared, and what happened, in the last week

_REPLY = re.compile(r"^\s*(?:re|fw|fwd|aw|tr|sv|wg)\s*:", re.I)
_DROP = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mon|tue|wed|thu|"
                   r"fri|sat|sun)[a-z]*\b|\d+")
_ANSWER = re.compile(r"^\s*(?:accepted|declined|tentative|cancel+ed|automatic reply|"
                     r"out of office)\s*:", re.I)


@dataclass(frozen=True)
class Act:
    """One thing the person did: ``kind`` (what), ``series`` (the same thing again: a
    digest), ``trigger`` (what it answered, when it answered something: a group name) and
    ``trigger_at``, ``handle`` (the instance, for the jobs mechanism to open)."""
    system: str
    kind: str
    at: datetime
    series: str
    trigger: Optional[str] = None
    trigger_at: Optional[datetime] = None
    handle: str = ""
    internal: bool = True
    label: str = ""
    ref: str = ""            # the series it belongs to, as its system names it (a meeting's)


@dataclass
class Person:
    oid: str
    addresses: frozenset[str]
    domains: frozenset[str]
    names: frozenset[str] = frozenset()

    def me(self, value: Any) -> bool:
        """``value`` names this person: their object id, one of their addresses, or a bare
        name (Mission Control's actors) that is the local part of one of their addresses
        and of nobody else's in the directory (``names``)."""
        text = str(value or "").strip().lower()
        return bool(text) and (text == self.oid or text in self.addresses or
                               ("@" not in text and text in self.names))


def _digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        found = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


# a capture kind's handle scheme (``integrations.capture_store.make_handle``)
SCHEMES = {"teams_chat": "teams-chat", "teams_channel": "teams-channel",
           "calendar_event": "calendar-event", "planner_task": "planner-task"}


def _generation(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


def _citation(kind: str, path: Any, generation: str, capture_id: Any) -> str:
    """The version's capture handle, as the projection's ``l1_handle`` names it (the
    stored path is already the handle's path text); "" when it cannot be made."""
    scheme = SCHEMES.get(kind)
    if not scheme or not generation or not path or capture_id is None:
        return ""
    return f"{scheme}://{path}@{generation}-{int(capture_id)}"


def subject_series(subject: str) -> Optional[str]:
    """The subject without dates, numbers and punctuation, as a series key (None for a
    reply or forward, a calendar answer, or a subject of fewer than two words)."""
    if not subject or _REPLY.match(subject) or _ANSWER.match(subject):
        return None
    key = " ".join(re.sub(r"[^a-z]+", " ", _DROP.sub(" ", subject.lower())).split())
    return key if len(re.findall(r"[a-z]{3,}", key)) >= 2 else None


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[1].lower() if "@" in address else ""


def _addr(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("address") or (value.get("emailAddress") or {}).get("address")
    return str(value or "").strip().lower()


# ----------------------------------------------------------------------------- sources

def mail_acts(state: str, person: Person, since: str, cleared: Any) -> list[Act]:
    """Mail the person sent from their own mailbox: a reply answers the latest message from
    someone else in the conversation (grouped by that sender's domain, or ``colleague``);
    a mail they started is a series (subject without dates and numbers, same people)."""
    path = Path(state) / "mail-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    try:
        boxes = [r[0] for r in conn.execute(
            "SELECT mailbox_id FROM mailboxes WHERE lower(mailbox_id) = ? OR lower(upn) IN "
            f"({','.join('?' * len(person.addresses))})",
            (person.oid, *sorted(person.addresses)))] if person.addresses else []
        threads: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        mine: list[tuple[datetime, dict[str, Any], str]] = []
        for box in boxes:
            for handle, record in conn.execute(
                    "SELECT l1_handle, record FROM messages WHERE mailbox_id = ? AND "
                    "superseded_at IS NULL", (box,)):
                rec = json.loads(record)
                sender = _addr(rec.get("from"))
                if person.me(sender):
                    at = _at(rec.get("sent_at"))
                    if at and at.isoformat() >= since:
                        mine.append((at, rec, handle))
                else:
                    at = _at(rec.get("received_at") or rec.get("sent_at"))
                    if at and rec.get("conversation_id"):
                        threads[str(rec["conversation_id"])].append((at, sender))
    finally:
        conn.close()
    out = []
    for at, rec, handle in mine:
        to = sorted({_addr(a) for a in [*(rec.get("to") or ()), *(rec.get("cc") or ())]} - {""})
        if not to or not all(cleared(a) for a in to):
            continue
        internal = all(_domain(a) in person.domains for a in to)
        before = [(t, s) for t, s in threads.get(str(rec.get("conversation_id") or ""), ())
                  if t <= at and at - t <= TRIGGER_WINDOW]
        subject = str(rec.get("subject") or "")
        if before and _REPLY.match(subject):
            t, sender = max(before)
            if not sender or not cleared(sender):
                continue
            group = "colleague" if _domain(sender) in person.domains else _domain(sender)
            out.append(Act("mail", "reply", at, _digest("mail-reply", group),
                           f"mail from {group}", t, handle, internal))
            continue
        key = subject_series(subject)
        if key:
            out.append(Act("mail", "send", at, _digest("mail", key, *to), None, None, handle,
                           internal))
    return out


def teams_acts(state: str, person: Person, since: str, cleared: Any) -> list[Act]:
    """Teams messages the person posted: one within ``TRIGGER_WINDOW`` of a message from
    someone else in the same chat answers it (grouped by one-to-one or group chat); one
    that answers nothing starts the chat's series."""
    path = Path(state) / "teams-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    chats: dict[str, list[tuple[datetime, str, str, str]]] = defaultdict(list)
    try:
        generation = _generation(conn)
        for kind, payload, path_, capture_id in conn.execute(
                "SELECT kind, payload, path, capture_id FROM versions WHERE superseded_at IS "
                "NULL AND kind IN ('teams_chat', 'teams_channel')"):
            row = json.loads(payload).get("row") or {}
            if row.get("deleted_at") or row.get("message_type") not in (None, "message"):
                continue
            at = _at(row.get("created_at"))
            if not at or at.isoformat() < since[:10]:
                continue
            sender = row.get("from") if isinstance(row.get("from"), Mapping) else {}
            who = str(sender.get("id") or (sender.get("user") or {}).get("id") or "").lower() \
                if sender.get("kind") in (None, "user") else ""
            chat = str(row.get("chat_id") or row.get("channel_id") or "")
            if chat and who:
                chats[f"{kind}:{chat}"].append(
                    (at, who, _citation(kind, path_, generation, capture_id), kind))
    finally:
        conn.close()
    out = []
    for chat, rows in chats.items():
        rows.sort()
        members = {w for _a, w, _i, _k in rows}
        if person.oid not in members:
            continue
        if not all(cleared(w) for w in members):
            continue                     # a member the exclusion does not clear: nobody's
        group_chat = len(members) > 2 or chat.startswith("teams_channel")
        last_other: Optional[datetime] = None
        for at, who, mid, _kind in rows:
            if who != person.oid:
                last_other = at
                continue
            if at.isoformat() < since:
                continue
            handle = mid
            if last_other and at - last_other <= TRIGGER_WINDOW:
                group = "group chat" if group_chat else "one-to-one chat"
                out.append(Act("teams", "reply", at, _digest("teams-reply", group),
                               f"Teams {group} message", last_other, handle))
            else:
                out.append(Act("teams", "post", at, _digest("teams", chat), None, None, handle,
                               ref=chat.split(":", 1)[1] if chat.startswith("teams_chat:")
                               else ""))
    return out


def mc_acts(state: str, person: Person, since: str, cleared: Any) -> list[Act]:
    """Mission Control: approvals the person decided (answering the request) and questions
    they answered (answering the question on the same task)."""
    path = Path(state) / "mc-capture" / "capture.sqlite"
    if not path.is_file():
        return []
    conn = _ro(path)
    asked: dict[str, list[datetime]] = defaultdict(list)
    answers: list[tuple[datetime, str, str]] = []
    homes: set[str] = set()
    out = []
    try:
        for path_, payload in conn.execute(
                "SELECT path, payload FROM versions WHERE superseded_at IS NULL AND "
                "(path LIKE '%/approvals/%' OR path LIKE '%/task_events/%')"):
            row = json.loads(payload).get("row") or {}
            if "/approvals/" in path_:
                at = _at(row.get("decidedAt"))
                if not at or at.isoformat() < since or not person.me(row.get("decidedBy")):
                    continue
                requester = str(row.get("requestedBy") or "")
                if "@" in requester and not cleared(requester):
                    continue
                out.append(Act("mission_control", "decide_approval", at,
                               _digest("mc-approval"), "approval requested in Mission Control",
                               _at(row.get("requestedAt")), f"mc:approval/{row.get('id')}"))
                continue
            kind = str(row.get("kind") or "")
            at = _at(row.get("ts"))
            task = str(row.get("task_id") or row.get("taskId") or "")
            if not at or not task:
                continue
            if kind == "question.asked":
                asked[task].append(at)
            actor = str(row.get("actor") or "").strip().lower()
            if re.fullmatch(r"[^@\s:]+@[a-z0-9.-]+", actor):
                homes.add(actor.rsplit("@", 1)[1])
            if kind == "question.answered" and at.isoformat() >= since:
                answers.append((at, task, actor))
    finally:
        conn.close()
    for at, task, actor in answers:
        # a bare MC name (``vince``) is the person only at a domain MC writes full
        # addresses in (``autopilot_work.MCReader.home_domains``), never by itself
        if not (person.me(actor) or ("@" not in actor and any(
                person.me(f"{actor}@{home}") for home in homes))):
            continue
        before = [t for t in asked.get(task, ()) if t <= at]
        out.append(Act("mission_control", "answer_question", at, _digest("mc-question"),
                       "question asked in Mission Control", max(before) if before else None,
                       f"mc:task/{task}"))
    return out


def system_file(rec: Mapping[str, Any]) -> bool:
    """A file SharePoint or OneDrive keeps by itself (a hidden library or folder, a cache
    or lock file): its versions name the person only because their session refreshed it,
    so changing it is nobody's work (a live dry run proposed nothing for one, 2026-11-18)."""
    name = str(rec.get("name") or "")
    parts = [p for p in str(rec.get("path") or "").split("/") if p]
    return name.startswith(("__", "~$", ".")) or any(
        p.startswith(("_", ".")) or p.lower() in ("personalcachelibrary", "forms")
        for p in parts)


def sharepoint_acts(state: str, person: Person, since: str, cleared: Any) -> list[Act]:
    """SharePoint files the person changed (the last modifier of each captured version),
    one action per file and modified time: a series per file."""
    path = Path(state) / "sharepoint-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    seen: set[tuple[str, str, str]] = set()
    out = []
    try:
        for handle, record in conn.execute("SELECT l1_handle, record FROM items"):
            rec = json.loads(record)
            if not person.me(rec.get("last_modified_by")) or system_file(rec):
                continue
            at = _at(rec.get("last_modified"))
            key = (str(rec.get("drive_id")), str(rec.get("id")))
            if not at or at.isoformat() < since or key + (at.isoformat(),) in seen:
                continue
            seen.add(key + (at.isoformat(),))
            out.append(Act("sharepoint", "edit_file", at, _digest("sharepoint", *key), None,
                           None, str(handle or ""), label=str(rec.get("name") or "")[:120]))
    finally:
        conn.close()
    return out


def meetings_acts(state: str, person: Person, since: str, cleared: Any) -> list[Act]:
    """Planner tasks the person created or completed; meetings of a series they organise
    (one per occurrence: the series is the job of running it)."""
    path = Path(state) / "meetings-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    copies: dict[tuple[str, str], tuple[bool, Act]] = {}
    out = []
    try:
        generation = _generation(conn)
        for kind, payload, path_, capture_id in conn.execute(
                "SELECT kind, payload, path, capture_id FROM versions WHERE "
                "superseded_at IS NULL"):
            row = json.loads(payload).get("row") or {}
            if kind == "planner_task":
                for verb, who, when in (("create_task", "created_by", "created_at"),
                                        ("complete_task", "completed_by", "completed_at")):
                    at = _at(row.get(when))
                    if at and at.isoformat() >= since and person.me(row.get(who)):
                        out.append(Act("planner", verb, at,
                                       _digest("planner", verb, row.get("plan_id"),
                                               row.get("bucket_id")),
                                       None, None,
                                       _citation(kind, path_, generation, capture_id),
                                       label=" / ".join(str(row.get(k) or "") for k in (
                                           "plan_title", "bucket_name") if row.get(k))[:120]))
            elif kind == "calendar_event":
                organiser = _addr((row.get("organizer") or {}))
                series = row.get("series_master_id")
                start = _at(row.get("start") if not isinstance(row.get("start"), Mapping)
                            else (row.get("start") or {}).get("dateTime"))
                if not series or row.get("is_cancelled") or not person.me(organiser) or \
                        not start or start.isoformat() < since:
                    continue
                # one meeting in several captured calendars (the organizer's and each
                # attendee's copy) is one: each copy has its own series id (and Graph's
                # iCal uid is per occurrence), so the series is the person's own meeting
                # of that subject
                uid = _digest(" ".join(str(row.get("subject") or series).lower().split()))
                if not all(cleared(_addr(a)) for a in row.get("attendees") or ()
                           if _addr(a)):
                    continue
                own = person.me(row.get("mailbox"))
                key = (uid, start.isoformat())
                if key in copies and (copies[key][0] or not own):
                    continue
                copies[key] = (own, Act(
                    "calendar", "run_meeting", start, _digest("meeting", uid), None, None,
                    _citation(kind, path_, generation, capture_id),
                    label=str(row.get("subject") or "")[:120], ref=str(series)))
    finally:
        conn.close()
    return out + [act for _own, act in copies.values()]


SOURCES = {"mail": mail_acts, "teams": teams_acts, "mission_control": mc_acts,
           "sharepoint": sharepoint_acts, "meetings": meetings_acts}


# ----------------------------------------------------------------------------- recurrence

def _local(at: datetime) -> datetime:
    return at.astimezone(TZ)


def cadence(times: Sequence[datetime], as_of: date) -> Optional[dict[str, Any]]:
    """``daily`` (active on ``DAILY_SHARE`` of the weekdays from the first to ``as_of``) or
    ``weekly`` (on its usual weekday in ``WEEKLY_MIN`` weeks, ``WEEKLY_SHARE`` of the
    weeks it spans and ``WEEKDAY_SHARE`` of the days it happened), at a usual time of day
    (half the instances within ``SPREAD_MINUTES`` of the median), and seen within the last
    week or two; None otherwise."""
    local = sorted(_local(t) for t in times)
    if not local:
        return None
    days = sorted({t.date() for t in local})
    minutes = [t.hour * 60 + t.minute for t in local]
    usual = int(statistics.median(minutes))
    near = sum(1 for m in minutes if abs(m - usual) <= SPREAD_MINUTES)
    if near * 2 < len(minutes):
        return None
    at = f"{usual // 60:02d}:{usual % 60:02d}"
    span = [days[0] + timedelta(i) for i in range((as_of - days[0]).days + 1)]
    weekdays = [d for d in span if d.weekday() < 5]
    active = [d for d in days if d.weekday() < 5]
    if len(weekdays) >= 10 and len(active) >= DAILY_SHARE * len(weekdays) and \
            (as_of - days[-1]).days <= 4:
        return {"cadence": "daily", "at": at, "days": len(days)}
    by_day = Counter(d.weekday() for d in days)
    weekday, n = by_day.most_common(1)[0]
    weeks = max(1, len({d - timedelta(d.weekday()) for d in span}))
    # and on that weekday most of the days it happens at all: a conversation started on
    # many days (34 posts on 14 days, a third of them Wednesdays) is no weekly job
    if n >= WEEKLY_MIN and n >= WEEKLY_SHARE * weeks and n >= WEEKDAY_SHARE * len(days) \
            and (as_of - days[-1]).days <= 10:
        return {"cadence": "weekly", "weekday": weekday, "at": at, "days": len(days)}
    return None


def find(acts: Iterable[Act], as_of: date) -> list[dict[str, Any]]:
    """The recurring jobs in ``acts``: per event group, one when done on ``MIN_DAYS`` days;
    per series of self-started actions, one when ``cadence`` finds a schedule."""
    groups: dict[tuple[str, str, str], list[Act]] = defaultdict(list)
    for act in acts:
        groups[(act.system, act.kind, act.series)].append(act)
    jobs = []
    for (system, kind, series), rows in groups.items():
        rows.sort(key=lambda a: a.at)
        days = {_local(a.at).date() for a in rows}
        base = {"id": _digest(system, kind, series), "system": system, "kind": kind,
                "series": series, "n": len(rows), "days": len(days),
                "first_at": rows[0].at.isoformat(), "last_at": rows[-1].at.isoformat(),
                "examples": [a.handle for a in rows[-EXAMPLES:] if a.handle],
                "label": rows[-1].label, "ref": rows[-1].ref,
                "last_week": sum(1 for a in rows if _local(a.at).date() > as_of - RECENT),
                "internal": all(a.internal for a in rows)}
        if rows[0].trigger:
            if len(days) < MIN_DAYS:
                continue
            lags = [(a.at - a.trigger_at).total_seconds() / 60 for a in rows
                    if a.trigger_at and a.at >= a.trigger_at]
            jobs.append({**base, "trigger": {"type": "event", "event": rows[0].trigger},
                         "median_minutes": round(statistics.median(lags)) if lags else None,
                         "per_week": round(len(rows) / max(1.0, DAYS / 7), 1)})
            continue
        found = cadence([a.at for a in rows], as_of)
        if found is None and (system, kind) in PREP_KINDS and len(days) >= WEEKLY_MIN:
            # a meeting series keeps its own schedule (Mon/Wed/Fri, every other week...):
            # the calendar says when the next one is, so it needs no cadence of ours
            minutes = sorted(_local(a.at).hour * 60 + _local(a.at).minute for a in rows)
            usual = minutes[len(minutes) // 2]
            found = {"cadence": "calendar", "at": f"{usual // 60:02d}:{usual % 60:02d}",
                     "days": len(days)}
        if found:
            jobs.append({**base, "trigger": {"type": "schedule", **found},
                         "per_week": round(len(rows) / max(1.0, DAYS / 7), 1)})
    jobs.sort(key=lambda j: (-j["n"], j["id"]))
    return jobs


# ----------------------------------------------------------------------------- coverage

# (system, kind, trigger type) -> the registry autopilots that prepare it
COVERAGE: dict[tuple[str, str, str], tuple[str, ...]] = {
    ("mail", "reply", "event"): ("reply_prep", "customer_request", "desk", "supplier_eta",
                                 "customer_po", "mail_approval_request", "job_message",
                                 "mail_thread_turn"),
    ("mail", "send", "schedule"): ("routine_update",),
    ("teams", "reply", "event"): ("teams_reply", "teams_group_turn"),
    ("mission_control", "decide_approval", "event"): ("approval_request", "job_approval"),
    ("mission_control", "answer_question", "event"): ("mc_question",),
    ("calendar", "run_meeting", "schedule"): ("job_recurring", "meeting_followup",
                                              "job_meeting"),
    ("teams", "post", "schedule"): ("job_recurring",),
}
def coverage(job: Mapping[str, Any], registry: Mapping[str, Any],
             blocked_sources: Mapping[str, str], prepared: Mapping[str, int],
             as_of: date) -> dict[str, Any]:
    """Which autopilots prepare ``job`` and whether they can today, measured: its source
    must be current (``blocked_sources``: system -> why not), one of them on or seen
    preparing (``prepared``: autopilot -> items prepared for the person in the last
    ``RECENT``), and when the job happened in that week, something was prepared for it.
    A scheduled mail to outside people is not a routine update (colleagues only)."""
    trigger = job["trigger"]["type"]
    autopilots = COVERAGE.get((job["system"], job["kind"], trigger), ())
    seen = sum(prepared.get(a, 0) for a in autopilots)
    out = {"autopilots": list(autopilots), "prepared_last_week": seen}
    if job["system"] in blocked_sources:
        return {**out, "can_prepare": False, "blocked": blocked_sources[job["system"]]}
    if not autopilots:
        return {**out, "can_prepare": False,
                "blocked": f"nothing prepares a {trigger} {job['system']} {job['kind']} yet"}
    specs = {a.get("id"): a for a in registry.get("autopilots") or ()}
    if not seen and not any(a in specs and specs[a].get("enabled") is not False
                            for a in autopilots):
        return {**out, "can_prepare": False,
                "blocked": f"the {' / '.join(autopilots)} autopilot is off"}
    if "routine_update" in autopilots and not job.get("internal"):
        return {**out, "can_prepare": False,
                "blocked": "a scheduled mail to outside people: routine updates go to "
                           "colleagues only"}
    last = _at(job.get("last_at"))
    recent = last is not None and _local(last).date() > as_of - RECENT
    if recent and not seen:
        return {**out, "can_prepare": False,
                "blocked": f"{' / '.join(autopilots)} prepared nothing for it in the last "
                           "week although it happened"}
    return {**out, "can_prepare": True, "blocked": None}


def prepared_recently(state: Path, oid: str, as_of: date) -> dict[str, int]:
    """autopilot -> items prepared for ``oid`` in the week before ``as_of`` (read-only)."""
    path = Path(state) / "autopilot" / "autopilot.sqlite"
    if not path.is_file():
        return {}
    start = (datetime(as_of.year, as_of.month, as_of.day, tzinfo=TZ) - RECENT
             + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn = _ro(path)
        try:
            return {a: n for a, n in conn.execute(
                "SELECT autopilot, COUNT(*) FROM items WHERE owner = ? AND created_at >= ? "
                "GROUP BY autopilot", (oid, start))}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def mail_blocked(state: Path, person: Person) -> Optional[str]:
    """Why the person's own mailbox is not current in the mail capture (every folder
    refused or none complete in the last day), else None."""
    path = Path(state) / "mail-capture" / "capture.db"
    if not path.is_file():
        return "mail is not captured on this server"
    try:
        conn = _ro(path)
        try:
            rows = conn.execute(
                "SELECT f.status, f.reason, f.last_complete_at FROM folders f JOIN mailboxes m "
                "ON m.mailbox_id = f.mailbox_id WHERE lower(m.mailbox_id) = ?",
                (person.oid,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return "the mail capture cannot be read"
    if not rows:
        return "the person's mailbox is not captured"
    if all(r[0] == "incomplete" and r[1] == "forbidden" for r in rows):
        last = max((r[2] or "" for r in rows), default="")
        return "the mail capture is refused (403) on the person's mailbox" + (
            f" since {last[:16]}Z" if last else "")
    return None


# ----------------------------------------------------------------------------- due

# a scheduled job no autopilot covers, in a system the jobs mechanism changes
# (``autopilot_jobs.SYSTEMS``; mail and Teams are the drafts'): prepared by ``job_recurring``
JOB_KINDS = frozenset({("sharepoint", "edit_file"), ("planner", "create_task"),
                       ("planner", "complete_task")})
LEAD = timedelta(minutes=30)        # prepared this long before the usual time
FOLDER = "recurring-work"           # beside the queue: one file per person, kept a day
WORDS = {("sharepoint", "edit_file"): "change the SharePoint file",
         ("planner", "create_task"): "create Planner tasks in",
         ("planner", "complete_task"): "complete Planner tasks in"}
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# a post the person makes in a chat on a schedule: drafted for them when due, from their
# last posts and what happened since, to edit and send as a Teams message (a chat's own:
# a channel post is not drafted)
POST_KINDS = frozenset({("teams", "post")})
# a meeting series the person runs: a brief before it (what is open since the last one);
# its follow-up after it is ``meeting_followup``'s
PREP_KINDS = frozenset({("calendar", "run_meeting")})
PREP_LEAD = timedelta(minutes=60)   # a meeting's brief: from this long before it starts


def _usual(job: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    """Today's usual time of a scheduled ``job`` (US Eastern), or None when today is not
    one of its days (a weekday for daily, its weekday for weekly)."""
    trigger = job.get("trigger") or {}
    if trigger.get("type") != "schedule" or trigger.get("cadence") not in ("daily", "weekly"):
        return None
    local = _local(now)
    if trigger["cadence"] == "daily" and local.weekday() >= 5:
        return None
    if trigger["cadence"] == "weekly" and local.weekday() != trigger.get("weekday"):
        return None
    try:
        hour, minute = (int(x) for x in str(trigger.get("at") or "").split(":"))
    except ValueError:
        return None
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def due_now(job: Mapping[str, Any], now: datetime) -> bool:
    """A scheduled ``job`` is due at ``now``: one of its days, from ``LEAD`` before its usual
    time. Never a meeting's brief (``PREP_KINDS``): its calendar says when (``prep_due``)."""
    usual = _usual(job, now)
    if usual is None or (job.get("system"), job.get("kind")) in PREP_KINDS:
        return False                     # a meeting's brief: ``prep_due``
    return _local(now) >= usual - LEAD


def prep_due(start: datetime, now: datetime) -> bool:
    """A meeting's brief is due in the ``PREP_LEAD`` before today's occurrence starts."""
    return start - PREP_LEAD <= now < start


def event_of(owner: str, job: Mapping[str, Any], now: datetime,
             object_id: str, handles: Sequence[str] = (),
             starts: Optional[datetime] = None) -> dict[str, Any]:
    """The ``record.due`` event of one due job: once per job and day (its id), for
    ``owner`` only, on the record it is about (``object_id``: its audiences and markings
    are the item's), citing ``handles`` (else its last instances; the reader must read
    each). A meeting's (``PREP_KINDS``) asks for a brief, not a change."""
    trigger = job["trigger"]
    when = ("every weekday" if trigger["cadence"] == "daily" else "on its calendar's days"
            if trigger["cadence"] == "calendar"
            else f"every {DAY_NAMES[int(trigger['weekday'])]}")
    label = str(job.get("label") or "")
    prep = (job["system"], job["kind"]) in PREP_KINDS
    post = (job["system"], job["kind"]) in POST_KINDS
    if post:
        title = f"Your {trigger['cadence']} Teams post is due"
        ask = ("This is a message the person posts in this Teams chat on this schedule. Open "
               "their last posts and what happened since in the records they speak of, and "
               "write today's post in their words and format as the brief (only the post "
               "text: it is offered to them to edit and send). Propose no change. When "
               "nothing new would go in it, leave the brief empty.")
    elif prep:
        at = _local(starts).strftime("%H:%M") if starts else trigger["at"]
        title = f"Brief for {label or 'your meeting'} at {at}"
        ask = ("This is a meeting the person runs on this schedule, starting soon. Read its "
               "last occurrences and what happened since the last one in the mail, chats, "
               "tasks and records its attendees and subject point to, and write a short "
               "brief for the person: the open items to raise, each with where it comes "
               "from (the record or message). Propose a change only when one is plainly "
               "due; when nothing is open, say so in one line.")
    else:
        what = WORDS.get((job["system"], job["kind"]), f"{job['kind']} in {job['system']}")
        title = f"Your {trigger['cadence']} work: {what}" + (f" {label}" if label else "")
        ask = ("This is recurring work the person does on this schedule, read from what "
               "they did before. Open the last instances, work out what they change each "
               "time, and propose today's change; propose nothing when today needs none.")
    fields: dict[str, Any] = {
        "title": title[:200], "cadence": trigger["cadence"],
        "usually": f"{when} around {trigger['at']} (US Eastern)",
        "done": f"{job['n']} times on {job['days']} days of the last {DAYS} days, the last "
                f"on {str(job['last_at'])[:10]}",
        "ask": ask}
    if prep or post:
        fields["brief"] = True
    if post:
        fields["post_to"] = str(job.get("ref") or "")
    cited = list(handles) or list(job.get("examples") or ())[-EXAMPLES:]
    return {"event_id": _digest("record.due", "job_recurring", owner, job["id"],
                                _local(now).date().isoformat(), "prep" if prep else ""),
            "event_type": "record.due", "object_id": object_id,
            "object_type": "RecurringJob", "at": now.astimezone(timezone.utc).isoformat(),
            "status": "recurring", "owner_hint": owner, "audience": f"mail:{owner}",
            "handles": cited[-EXAMPLES:], "subject": title[:200], "fields": fields}


def todays_meeting(state: Path, series: str, now: datetime
                   ) -> Optional[tuple[str, datetime]]:
    """(capture handle, start) of today's occurrence of a meeting series (US Eastern), not
    cancelled, else None; read-only."""
    path = Path(state) / "meetings-capture" / "capture.db"
    if not series or not path.is_file():
        return None
    today = _local(now).date()
    try:
        conn = _ro(path)
        try:
            generation = _generation(conn)
            for kind, payload, path_, capture_id in conn.execute(
                    "SELECT kind, payload, path, capture_id FROM versions WHERE kind = "
                    "'calendar_event' AND superseded_at IS NULL AND json_extract(payload, "
                    "'$.row.series_master_id') = ?", (series,)):
                row = json.loads(payload).get("row") or {}
                start = row.get("start")
                start = _at(start.get("dateTime") if isinstance(start, Mapping) else start)
                handle = _citation(kind, path_, generation, capture_id)
                if start and _local(start).date() == today and not row.get("is_cancelled") \
                        and handle:
                    return handle, start
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return None
    return None


RETRY = timedelta(hours=1)       # a scan that could not be built is tried again after this


def stale(found: Any, today: date, now: datetime) -> bool:
    """Whether a kept scan must be measured again: none, another day's, or one that was
    not built (the exclusion unavailable at that moment) and is ``RETRY`` old."""
    if not isinstance(found, Mapping) or found.get("as_of") != today.isoformat():
        return True
    if found.get("status") == "built":
        return False
    at = _at(found.get("measured_at"))
    return at is None or now - at >= RETRY


def due_events(state: Path, st: Any, owners: Iterable[str], now: datetime,
               measure_fn: Any = None, failed: Optional[Counter] = None
               ) -> list[dict[str, Any]]:
    """``record.due`` events for each of ``owners`` (the people the live prepare gate
    passes ``routine_update`` for): their recurring work (``measure``, kept in ``FOLDER``
    and re-measured once a day), each scheduled job of ``JOB_KINDS`` no other autopilot
    covers, and each meeting series they run (``PREP_KINDS``: a brief before it), that is
    due now and whose record the store holds (today's occurrence, else the last instance:
    its object id; none: no event, never a guess). One person's scan or store read that
    fails costs only that person's events: counted in ``failed`` ("recurring_unreadable"),
    the others' still returned."""
    measure_fn = measure_fn or measure
    out = []
    for owner in sorted({str(o).lower() for o in owners}):
        try:
            out += _owner_due(Path(state), st, owner, now, measure_fn)
        except Exception:  # noqa: BLE001 - one person's failure never costs another's work
            if failed is not None:
                failed["recurring_unreadable"] += 1
    return out


def _owner_due(state: Path, st: Any, owner: str, now: datetime,
               measure_fn: Any) -> list[dict[str, Any]]:
    """``due_events`` for one ``owner``."""
    folder = state / "autopilot" / FOLDER
    today = _local(now).date()
    out: list[dict[str, Any]] = []
    path = folder / f"{_digest('person', owner)}.json"
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        found = None
    if stale(found, today, now):
        found = {**measure_fn(state, owner, as_of=today),
                 "measured_at": now.astimezone(timezone.utc).isoformat()}
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(found, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    if found.get("status") != "built":
        return out
    for job in found.get("jobs") or ():
        kind = (job.get("system"), job.get("kind"))
        handles: list[str] = []
        starts = None
        if kind in PREP_KINDS:
            meeting = todays_meeting(state, str(job.get("ref") or ""), now)
            if meeting is None or not prep_due(meeting[1], now):
                continue
            handles, starts = [meeting[0]], meeting[1]
        elif kind in POST_KINDS:
            if not job.get("ref") or not due_now(job, now):
                continue
        elif kind not in JOB_KINDS or (job.get("coverage") or {}).get("autopilots") \
                or not due_now(job, now):
            continue
        for handle in [*handles, *reversed(job.get("examples") or ())]:
            row = st.conn.execute(
                "SELECT object_id FROM object_versions WHERE l1_handle = ? AND "
                "superseded_at IS NULL", (handle,)).fetchone() if handle else None
            if row:
                out.append(event_of(owner, job, now, str(row[0]),
                                    [*(job.get("examples") or ()), *handles][-EXAMPLES:]
                                    if handles else (), starts))
                break
    return out


# ----------------------------------------------------------------------------- run

def _source(args: tuple[str, str, Person, str, Any]) -> list[Act]:
    name, state, person, since, guard = args
    return SOURCES[name](state, person, since, _cleared(guard))


def _cleared(guard: Any):
    """value -> whether the people exclusion clears it: an object id or an address at one
    of the tenant's domains must be decided by the guard (undecidable: not cleared); an
    address outside every tenant domain is no tenant person's, so no excluded one's (the
    rule ``team_activity`` reads mail by)."""
    try:
        ours = frozenset(guard.known_domains())
    except Exception:  # noqa: BLE001 - no domains known: every address must be decided
        ours = None

    def clear(value: str) -> bool:
        text = str(value or "").strip().lower()
        if "@" in text and ours is not None and _domain(text) and not any(
                _domain(text) == d or _domain(text).endswith("." + d) for d in ours):
            return True
        try:
            return not guard.is_excluded(text)
        except Exception:  # noqa: BLE001 - undecidable: not cleared
            return False
    return clear


def person_of(state: Path, who: str, guard: Any) -> Optional[Person]:
    """The person ``who`` names (an object id or an address) with every address the mail
    capture's directory holds for them; None when the directory does not know them."""
    who = who.strip().lower()
    oid = who if re.fullmatch(r"[0-9a-f-]{36}", who) else (guard.object_id_of(who) or "")
    if not oid:
        return None
    oid = str(oid).lower()
    addresses = {who} if "@" in who else set()
    directory = getattr(guard, "_directory", None) or {}
    addresses |= {a for a, o in directory.items() if str(o).lower() == oid}
    others = Counter(a.split("@", 1)[0].lower() for a, o in directory.items()
                     if str(o).lower() != oid)
    names = {a.split("@", 1)[0] for a in addresses if "@" in a} - set(others)
    return Person(oid=oid, addresses=frozenset(a.lower() for a in addresses),
                  domains=frozenset(guard.known_domains()), names=frozenset(names))


def measure(state: Path, who: str, *, days: int = DAYS, as_of: Optional[date] = None,
            guard: Any = None, registry: Optional[Mapping[str, Any]] = None,
            workers: Optional[int] = None) -> dict[str, Any]:
    """The person's recurring jobs with their coverage (``SCHEMA``); ``status`` ``blocked``
    when the people exclusion is unavailable or does not clear the person."""
    from . import autopilot as ap
    from .card_actions import exclusion_for
    state = Path(state)
    guard = guard if guard is not None else exclusion_for(os.environ, state)
    as_of = as_of or datetime.now(TZ).date()
    out: dict[str, Any] = {"schema": SCHEMA, "as_of": as_of.isoformat(), "days": days}
    if getattr(guard, "unavailable", "absent"):
        return {**out, "status": "blocked", "reason": "people exclusion unavailable"}
    person = person_of(state, who, guard)
    if person is None or guard.is_excluded(person.oid):
        return {**out, "status": "blocked", "reason": "person unknown or not cleared"}
    since = (datetime(as_of.year, as_of.month, as_of.day, tzinfo=TZ)
             - timedelta(days=days)).astimezone(timezone.utc).isoformat()
    jobs_in = [(name, str(state), person, since, guard) for name in SOURCES]
    acts: list[Act] = []
    from ontology import parent_watch
    with ProcessPoolExecutor(max_workers=workers or min(len(jobs_in), os.cpu_count() or 1),
                             **parent_watch.pool_init()) as pool:
        for found in pool.map(_source, jobs_in):
            acts.extend(found)
    # what has happened by now (a calendar holds meetings ahead; a past as_of: its day's end)
    cutoff = datetime(as_of.year, as_of.month, as_of.day, tzinfo=TZ) + timedelta(days=1)
    if as_of == datetime.now(TZ).date():
        cutoff = datetime.now(timezone.utc)
    acts = [a for a in acts if a.at <= cutoff]
    registry = registry if registry is not None else ap.load_registry()
    blocked = {}
    why = mail_blocked(state, person)
    if why:
        blocked["mail"] = why
    jobs = find(acts, as_of)
    prepared = prepared_recently(state, person.oid, as_of)
    for job in jobs:
        job["coverage"] = coverage(job, registry, blocked, prepared, as_of)
    counted = Counter(a.system for a in acts)
    return {**out, "status": "built", "person": person.oid, "actions": dict(counted),
            "jobs": jobs,
            "summary": {"found": len(jobs),
                        "can_prepare": sum(1 for j in jobs if j["coverage"]["can_prepare"]),
                        "blocked": dict(Counter(j["coverage"]["blocked"] for j in jobs
                                                if not j["coverage"]["can_prepare"]))}}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--state", required=True, type=Path)
    p.add_argument("--person", required=True)
    p.add_argument("--days", type=int, default=DAYS)
    p.add_argument("--out", type=Path, help="write the JSON here (else print the summary)")
    a = p.parse_args(argv)
    found = measure(a.state, a.person, days=a.days)
    if a.out:
        a.out.write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps({k: found.get(k) for k in ("status", "reason", "actions", "summary")},
                     sort_keys=True))
    return 0 if found.get("status") == "built" else 1


if __name__ == "__main__":
    raise SystemExit(main())
