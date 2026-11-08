"""Versioned meetings capture (L1): calendar events, Planner tasks and Teams meeting
transcripts into Workbench state.

Read calendars, Planner tasks and Teams meeting transcripts with
the company's Entra admin application (``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` /
``AZURE_CLIENT_SECRET``: Calendars.Read, Tasks.ReadWrite.All used read-only,
OnlineMeetings.Read.All, OnlineMeetingTranscript.Read.All); the mail application
(``MICROSOFT_GRAPH_*``) lacks those grants. ``PLX_MEETINGS_GRAPH_CREDENTIALS`` names the
credential prefix (``AZURE``, the default, or ``MICROSOFT_GRAPH``); nothing else is
accepted. Every request goes through the one GET-only Graph client
(``integrations.graph_read.build_graph`` -> ``live_source_guard``) and the closed grammar
(``graph.user_calendar_view``, ``graph.user_event``, ``graph.user_online_meetings``,
``graph.online_meeting_transcripts``, ``graph.online_meeting_transcript_content``,
``graph.user_planner_tasks``, ``graph.planner_plan``, ``graph.planner_plan_buckets``,
``graph.planner_plan_tasks``, ``graph.planner_task_details``). Nothing is written, accepted,
declined or marked: Tasks.ReadWrite.All is held, never used to write (the client sends no
method but GET and the token POST).

People exclusion (``integrations.people_exclusion``, fail closed). The guard must hold its
ID list and bind (one directory listing that names nobody) before anything else; else the
run reads nothing and reports the typed part ``meetings.people_exclusion``. Then, before
any request naming them:

* a mailbox (scope entry or directory listing) any of whose identities is an excluded
  person's is never opened: no calendar, Planner or directory request names it
  (``GraphMailReader.open_mailbox``: the object id is refused before its lookup, an address
  once the guard is bound, every resolved identity after it);
* a calendar is listed with identity fields only (``LISTING_SELECT``: no subject, body,
  location or preview). An event whose organizer or any attendee is an excluded person,
  or whose people the guard cannot decide (an address with no decision), is withheld: no
  content request, no record, only the count (``events_withheld``); its online meeting is
  never looked up and its transcript never read. The calendar view's delta form answers
  every event with its body whatever ``$select`` names (live 2026-11-04), so the capture
  lists the view itself and reads one event's content (``CONTENT_SELECT``) only once its
  people cleared the guard; private and confidential events are kept without content
  (``content: "withheld_sensitivity"``);
* a Planner task assigned to, created or completed by an excluded person is withheld the
  same way: its details are never requested and nothing of it is kept (``tasks_withheld``).
  Plans are found through the captured mailboxes' own tasks (``graph.user_planner_tasks``;
  an excluded person's own task list is never requested) and, with the scope's
  ``plan_groups``, through every Microsoft 365 group (``graph.groups``, listed with
  ``directory_credentials``: the admin application answers 403 to ``/groups``, live
  2026-11-04/08): a group whose members are all excluded people is skipped, one whose
  members cannot be listed or decided is skipped whole (fail closed);
* a transcript is read only of a meeting whose event cleared the guard, whose organizer is
  a tenant user who is not excluded (looked up by address after the guard decided it), and
  is kept only when every speaker the WebVTT names is the organizer or an attendee of the
  cleared event by display name (``unknown_speaker`` withholds it whole: a guest the
  invitation did not list could be anyone, an excluded person included).

Reads per run (``MeetingsCapture.run``), each part within the scope's caps and the
caller's time budget (``out_of_time``; a part stopped there keeps its cursor and resumes):

* Calendar (kind ``calendar_event``): per mailbox the calendar view over the rolling window
  (``window_days_back`` before now to ``window_days_ahead`` after), times in UTC, paged by
  the server's nextLink (the cursor). Incremental: an event whose ``changeKey`` equals the
  current captured version's is kept as it is (no content request); a new or changed one
  is read once (``max_event_reads_per_run``, at most ``workers`` at a time). A run that read
  the whole window finishes complete: an event no longer in it is marked missing from the
  source (out of the window or deleted; never "deleted").
* Planner (kind ``planner_task``): each mailbox's own tasks name the plans; per plan its
  record (title, container), buckets and every task (paged), and the details
  (description, checklist, reference titles) of each new or changed task (``@odata.etag``)
  that cleared the guard (``max_task_reads_per_run``).
* Transcripts (kind ``meeting_transcript``): for each cleared online meeting that ended in
  the last ``transcript_days_back`` days, once per join URL until a day after it ended
  (the state ``meetings_transcript_checks``), the organizer's online meeting by its join
  URL, its transcripts and each one's WebVTT content (``max_transcript_bytes``). Graph
  refuses the online-meeting read without an application access policy for the app (403,
  live 2026-11-04): the part ``meetings.online_meetings`` is then blocked naming
  OnlineMeetings.Read.All and no further meeting is looked up in the run.

Records (``integrations.capture_store.CaptureStore``, ``<state>/meetings-capture/capture.db``):

* ``calendar-event://<mailbox-oid>/<event-id>@<generation>-<capture-id>``, unit
  ``meetings:calendar:<mailbox-oid>``, resource ``mail:<mailbox-oid>`` (a calendar is part
  of its mailbox: whoever the audience grants the mailbox), source ACL the mailbox owner's
  (``mailbox_owner``, the mail capture's shape). ``row`` (``event_row``): ids, iCalUId,
  subject, body preview (text), organizer and attendees (name, address, type, response),
  start and end (UTC), all-day, cancelled, show-as, sensitivity, categories, location,
  online meeting (join URL, conference id, provider), series master, type, is-organizer,
  times, web link.
* ``planner-task://<plan-id>/<task-id>@...``, unit ``meetings:plan:<plan-id>``, resource
  ``teams:planner:<plan-id>`` (a plan is its Microsoft 365 group's, as a team is; every
  ``teams:`` resource needs the item's source ACL, ``live_source_guard._ALWAYS_SOURCE_ACL``),
  source ACL the plan's group (``group:<group-id>``, the identity employee permissions give
  a member of that group) and the task's own people (assignees, creator, completer: object
  ids). ``row`` (``task_row``): ids,
  plan title and group, bucket and its name, title, percent complete, priority, start,
  due, completion, creation, people, description, checklist (title, checked), reference
  titles and types (never their URLs).
* ``meeting-transcript://<organizer-oid>/<meeting-id>/<transcript-id>@...``, unit
  ``meetings:organizer:<organizer-oid>``, resource ``mail:<organizer-oid>``, source ACL
  the organizer's and the cleared attendees' identities (``meeting_participants``).
  ``row`` (``transcript_row``): ids, join URL, the event's iCalUId and subject, times, the
  cues (start, end, speaker, text), ``content_text`` (one ``[hh:mm:ss] speaker: text``
  line per cue, the document the text index and mentions read), sha256 of the bytes.

Lineage ``mail`` (the C0 lineage enum has no calendar lineage; Teams keeps ``mail`` too).
Captures are not an atomic snapshot and never prove a meeting, task or transcript absent.

Storage: the state directory must lie outside every git work tree. CLI (counts, booleans
and enums only, never ids, names, addresses or text)::

    python3 -m integrations.graph_read meetings-capture --state-dir D \\
        --live-audience examples/meetings_capture_audience.live-check.json --runs 1

Live-unverified until live check ``graph.meetings_capture`` passes.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.types import Actor

from . import live_source_grammar as grammar
from . import people_exclusion
from .capture_mail import assert_outside_git, blocked_reason, graph_block
from .capture_store import (CaptureError, CaptureStore, CapturedRecord, SourceKind,
                            register_source_kind)
from .live_source_guard import (LiveSourceAudience, NotVisible, SourceUnavailable, jsonable,
                                nextlink_refused, unavailable_detail)

SCOPE_SCHEMA = "plx-meetings-capture-scope/v1"
STORE_DIR = "meetings-capture"
DB_FILE = "capture.db"
LINEAGE = "mail"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "meetings_capture_scope.json"
CAPTURE_PRINCIPAL = "svc:meetings-capture"
CREDENTIALS_ENV = "PLX_MEETINGS_GRAPH_CREDENTIALS"
CREDENTIAL_PREFIXES = ("AZURE", "MICROSOFT_GRAPH")
DEFAULT_CREDENTIALS = "AZURE"
# The ontology relations of its records (source family ``meetings``).
RELATIONS = {"calendar_event": "calendarEvents", "planner_task": "plannerTasks",
             "meeting_transcript": "meetingTranscripts"}
GRAPH_PERMISSIONS = ("User.Read.All", "Calendars.Read", "Tasks.Read.All",
                     "OnlineMeetings.Read.All", "OnlineMeetingTranscript.Read.All")
_PERMISSION = {"graph.users": "User.Read.All", "graph.user": "User.Read.All",
               "graph.user_calendar_view": "Calendars.Read",
               "graph.user_event": "Calendars.Read",
               "graph.user_planner_tasks": "Tasks.Read.All",
               "graph.groups": "GroupMember.Read.All",
               "graph.group_members": "GroupMember.Read.All",
               "graph.group_planner_plans": "Tasks.Read.All",
               "graph.planner_plan": "Tasks.Read.All",
               "graph.planner_plan_buckets": "Tasks.Read.All",
               "graph.planner_plan_tasks": "Tasks.Read.All",
               "graph.planner_task_details": "Tasks.Read.All",
               "graph.user_online_meetings": "OnlineMeetings.Read.All",
               "graph.online_meeting_transcripts": "OnlineMeetingTranscript.Read.All",
               "graph.online_meeting_transcript_content": "OnlineMeetingTranscript.Read.All"}
# Identity fields only: what the people guard decides an event on (no subject, body,
# preview or location).
LISTING_SELECT = ("id,iCalUId,changeKey,organizer,attendees,start,end,isAllDay,isCancelled,"
                  "isOrganizer,isOnlineMeeting,onlineMeeting,onlineMeetingProvider,"
                  "seriesMasterId,type,sensitivity,showAs,categories,createdDateTime,"
                  "lastModifiedDateTime")
# One cleared event's content, read with its people again (decided on what is read).
CONTENT_SELECT = "id,changeKey,subject,bodyPreview,location,webLink,organizer,attendees"
UTC_PREFER = {"Prefer": 'outlook.timezone="UTC"'}
SENSITIVE = frozenset({"private", "confidential"})
JOIN_PREFIX = "https://teams.microsoft.com/l/meetup-join/"
TRANSCRIPT_CHECKS = "meetings_transcript_checks"
# Transcripts stay dormant until Vince grants OnlineMeetingTranscript.Read.All (and the
# application access policy OnlineMeetings.Read.All needs): no online-meeting or transcript
# request is made unless this is "1" in the capture's environment.
TRANSCRIPTS_GRANTED = "PLX_MEETINGS_TRANSCRIPTS_GRANTED"
UNIT_PREFIX = "meetings:"
REASON_MAX = 64
MAX_TEXT = 1000
MAX_PREVIEW = 2000
MAX_DESCRIPTION = 20_000
MAX_PEOPLE = 500
MAX_CUES = 20_000
TIME_BUDGET = "cap:time_budget"
# Content reads at once: Planner answered 429 to most of 3,066 detail reads at 8 (live
# 2026-11-04); 4 with the bounded retry kept it under the throttle.
WORKERS = 4
# The run's GET retry sleep (``BoundedBackoff``): the whole run's, within its budget.
RETRY_BUDGET_SECONDS = 900
_LIMITS = {"window_days_back": (365, 0, 3650), "window_days_ahead": (90, 0, 730),
           "page_size": (100, 1, 1000), "max_pages_per_mailbox": (200, 1, 5000),
           "max_event_reads_per_run": (5000, 0, 200_000),
           "max_plans_per_run": (500, 0, 10_000), "max_task_reads_per_run": (5000, 0, 200_000),
           "max_transcript_reads_per_run": (200, 0, 10_000),
           "transcript_days_back": (90, 0, 3650),
           "max_transcript_bytes": (2_000_000, 10_000, 8_000_000)}
_PARTS = ("calendar", "planner", "transcripts")


class MeetingsCaptureError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _valid(type_name: str, value: Any) -> bool:
    return grammar.TYPES[type_name].valid(value)


def _oid(value: Any) -> str:
    if not isinstance(value, str) or not _valid("user_id", value.lower()):
        raise ValueError("meetings_path_invalid")
    return value.lower()


def _graph_id(value: Any, type_name: str = "mail_id") -> str:
    if not isinstance(value, str) or not _valid(type_name, value):
        raise ValueError("meetings_path_invalid")
    return value


def _parts(parts: Sequence[str], count: int) -> Sequence[str]:
    if isinstance(parts, (str, bytes)) or len(parts) != count:
        raise ValueError("meetings_path_invalid")
    return parts


EVENT_KIND = register_source_kind(SourceKind(
    name="calendar_event", scheme="calendar-event", path_parts=2,
    canonical_path=lambda p: (_oid(_parts(p, 2)[0]), _graph_id(p[1])),
    unit=lambda p: f"{UNIT_PREFIX}calendar:{p[0]}", resources=lambda p: (f"mail:{p[0]}",),
    key=lambda p: {"mailbox": p[0], "id": p[1]}))
TASK_KIND = register_source_kind(SourceKind(
    name="planner_task", scheme="planner-task", path_parts=2,
    canonical_path=lambda p: (_graph_id(_parts(p, 2)[0], "planner_id"),
                              _graph_id(p[1], "planner_id")),
    unit=lambda p: f"{UNIT_PREFIX}plan:{p[0]}",
    resources=lambda p: (f"teams:planner:{p[0]}",),
    key=lambda p: {"plan_id": p[0], "id": p[1]}, current_acl=True))
TRANSCRIPT_KIND = register_source_kind(SourceKind(
    name="meeting_transcript", scheme="meeting-transcript", path_parts=3,
    canonical_path=lambda p: (_oid(_parts(p, 3)[0]), _graph_id(p[1], "meeting_id"),
                              _graph_id(p[2], "meeting_id")),
    unit=lambda p: f"{UNIT_PREFIX}organizer:{p[0]}", resources=lambda p: (f"mail:{p[0]}",),
    key=lambda p: {"organizer": p[0], "meeting_id": p[1], "id": p[2]}, current_acl=True))
KINDS = (EVENT_KIND, TASK_KIND, TRANSCRIPT_KIND)


# ------------------------------------------------------------------ scope

class MeetingsScope:
    """``plx-meetings-capture-scope/v1``: ``mailboxes`` (addresses or object ids) and/or
    ``all_mailboxes`` (every directory mailbox less the excluded people and the mail
    capture's HR / payroll deny list, ``directory_scope``), which ``parts`` run
    (``calendar``, ``planner``, ``transcripts``) and the caps of ``_LIMITS``.
    ``plan_groups``: Planner plans are also found through every Microsoft 365 group of
    the tenant (a group whose members are all excluded people is skipped), listed with
    ``directory_credentials`` (a credential prefix holding GroupMember.Read.All; default
    the capture's own). Anything unknown or out of bounds is refused."""

    KEYS = {"schema", "note", "mailboxes", "all_mailboxes", "directory_scope", "parts",
            "plan_groups", "directory_credentials", *_LIMITS}

    def __init__(self, document: Any, *, base: Path | None = None) -> None:
        if not isinstance(document, Mapping) or document.get("schema") != SCOPE_SCHEMA \
                or set(document) - self.KEYS:
            raise MeetingsCaptureError("scope_invalid")
        mailboxes = document.get("mailboxes", [])
        if not isinstance(mailboxes, list) or len(mailboxes) > 1000 or not all(
                isinstance(m, str) and _valid("user_ref", m) for m in mailboxes):
            raise MeetingsCaptureError("scope_mailboxes_invalid")
        self.mailboxes = tuple(dict.fromkeys(m.lower() for m in mailboxes))
        self.all_mailboxes = document.get("all_mailboxes", False)
        if not isinstance(self.all_mailboxes, bool) or not (self.mailboxes or
                                                             self.all_mailboxes):
            raise MeetingsCaptureError("scope_mailboxes_invalid")
        directory = document.get("directory_scope", "mail_all_mailboxes_scope.json")
        if not isinstance(directory, str) or not directory or "/" in directory:
            raise MeetingsCaptureError("scope_directory_invalid")
        self.directory_scope = (base or Path(__file__).parent) / directory
        parts = document.get("parts", list(_PARTS))
        if not isinstance(parts, list) or not parts or set(parts) - set(_PARTS) \
                or len(set(parts)) != len(parts):
            raise MeetingsCaptureError("scope_parts_invalid")
        self.parts = frozenset(parts)
        self.plan_groups = document.get("plan_groups", False)
        if not isinstance(self.plan_groups, bool):
            raise MeetingsCaptureError("scope_plan_groups_invalid")
        self.directory_credentials = document.get("directory_credentials")
        if self.directory_credentials is not None \
                and self.directory_credentials not in CREDENTIAL_PREFIXES:
            raise MeetingsCaptureError("scope_directory_credentials_invalid")
        for key, (default, low, high) in _LIMITS.items():
            value = document.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise MeetingsCaptureError(f"scope_{key}_invalid")
            setattr(self, key, value)

    @classmethod
    def from_file(cls, path: str | Path) -> "MeetingsScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MeetingsCaptureError("scope_unreadable") from None
        return cls(document, base=Path(__file__).parent)


def credential_prefix(environ: Mapping[str, str]) -> str:
    """The Graph credential prefix the capture reads with (``CREDENTIALS_ENV``)."""
    value = (environ.get(CREDENTIALS_ENV) or DEFAULT_CREDENTIALS).strip()
    if value not in CREDENTIAL_PREFIXES:
        raise MeetingsCaptureError("credentials_prefix_invalid")
    return value


# ------------------------------------------------------------------ rows

def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _party(raw: Any) -> dict[str, Any] | None:
    """``{name, address}`` of a Graph recipient (``emailAddress``), else None."""
    email = raw.get("emailAddress") if isinstance(raw, Mapping) else None
    if not isinstance(email, Mapping):
        return None
    address = email.get("address")
    address = address.strip().lower() if isinstance(address, str) and "@" in address else None
    name = _text(email.get("name"), 200)
    return {"name": name, "address": address} if (name or address) else None


def _attendee(raw: Any) -> dict[str, Any] | None:
    party = _party(raw)
    if party is None:
        return None
    status = raw.get("status") if isinstance(raw.get("status"), Mapping) else {}
    return {**party, "type": _text(raw.get("type"), 32),
            "response": _text(status.get("response"), 32)}


def _instant(value: Any) -> str | None:
    """A Graph ``dateTimeTimeZone`` (asked in UTC) or ISO instant as ISO-8601 UTC."""
    if isinstance(value, Mapping):
        zone = value.get("timeZone")
        value = value.get("dateTime")
        if zone not in (None, "UTC", "Etc/UTC", "Coordinated Universal Time"):
            return None
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    # Graph writes seven fractional digits; ``fromisoformat`` (3.10) takes three or six.
    value = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], value.strip())
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_FRACTION = re.compile(r"\.(\d+)")


def event_people(raw: Mapping[str, Any]) -> list[str]:
    """Every address an event names (organizer, attendees), for the people guard."""
    out = []
    for entry in [raw.get("organizer"), *(raw.get("attendees") or [])]:
        party = _party(entry)
        if party and party["address"]:
            out.append(party["address"])
    return out


def event_row(mailbox: str, listed: Mapping[str, Any],
              content: Mapping[str, Any] | None, content_state: str) -> dict[str, Any]:
    """The captured row of one event: identity fields from the listing, content
    (subject, preview, location, link) only from its cleared content read."""
    online = listed.get("onlineMeeting") if isinstance(listed.get("onlineMeeting"),
                                                        Mapping) else {}
    private = content_state == "withheld_sensitivity"   # who attends and its labels neither
    join = online.get("joinUrl")
    location = (content or {}).get("location")
    return jsonable({
        "source": "graph_calendar", "kind": "calendar_event", "mailbox": mailbox,
        "id": listed["id"], "ical_uid": _text(listed.get("iCalUId"), 512),
        "change_key": listed.get("changeKey"),
        "subject": _text((content or {}).get("subject")),
        "body_preview": _text((content or {}).get("bodyPreview"), MAX_PREVIEW),
        "location": _text(location.get("displayName")) if isinstance(location, Mapping)
        else None,
        "web_link": _text((content or {}).get("webLink"), 2048),
        "content": content_state,
        "organizer": _party(listed.get("organizer")),
        "attendees": [] if private else [a for a in (_attendee(x) for x in (
                      listed.get("attendees") or [])
                                  [:MAX_PEOPLE]) if a],
        "start": _instant(listed.get("start")), "end": _instant(listed.get("end")),
        "is_all_day": listed.get("isAllDay") is True,
        "is_cancelled": listed.get("isCancelled") is True,
        "is_organizer": listed.get("isOrganizer") is True,
        "is_online_meeting": listed.get("isOnlineMeeting") is True,
        "online_meeting": {"join_url": join if isinstance(join, str)
                           and join.startswith(JOIN_PREFIX) and len(join) <= 4096 else None,
                           "conference_id": _text(online.get("conferenceId"), 64),
                           "provider": _text(listed.get("onlineMeetingProvider"), 64)}
        if online or listed.get("onlineMeetingProvider") else None,
        "series_master_id": listed.get("seriesMasterId")
        if _valid("mail_id", listed.get("seriesMasterId")) else None,
        "type": _text(listed.get("type"), 32), "sensitivity": _text(listed.get("sensitivity"), 32),
        "show_as": _text(listed.get("showAs"), 32),
        "categories": [] if private else [
            c for c in (_text(x, 100) for x in (listed.get("categories") or [])[:50]) if c],
        "created_at": _instant(listed.get("createdDateTime")),
        "last_modified": _instant(listed.get("lastModifiedDateTime"))})


def _task_people(raw: Mapping[str, Any]) -> list[str]:
    """Every object id a task names: assignees, creator, completer."""
    people = [k for k in (raw.get("assignments") or {}) if isinstance(k, str)] \
        if isinstance(raw.get("assignments"), Mapping) else []
    for key in ("createdBy", "completedBy"):
        user = (raw.get(key) or {}).get("user") if isinstance(raw.get(key), Mapping) else None
        if isinstance(user, Mapping) and isinstance(user.get("id"), str):
            people.append(user["id"])
    return [p.lower() for p in people]


def _has_details(raw: Mapping[str, Any]) -> bool:
    """Whether a listed task has details worth a read (unknown counts read them)."""
    counts = [raw.get(k) for k in ("checklistItemCount", "referenceCount")]
    return raw.get("hasDescription") is not False or any(
        type(c) is not int or c > 0 for c in counts)


def _who(raw: Any) -> str | None:
    user = raw.get("user") if isinstance(raw, Mapping) else None
    oid = user.get("id") if isinstance(user, Mapping) else None
    return oid.lower() if isinstance(oid, str) and _valid("user_id", oid.lower()) else None


def task_row(plan: Mapping[str, Any], buckets: Mapping[str, str], raw: Mapping[str, Any],
             details: Mapping[str, Any] | None) -> dict[str, Any]:
    checklist = (details or {}).get("checklist")
    references = (details or {}).get("references")
    container = plan.get("container") if isinstance(plan.get("container"), Mapping) else {}
    group = container.get("containerId") if container.get("type") == "group" else None
    assignees = sorted(p for p in (raw.get("assignments") or {})
                       if isinstance(p, str) and _valid("user_id", p.lower()))
    return jsonable({
        "source": "graph_planner", "kind": "planner_task", "id": raw["id"],
        "plan_id": raw.get("planId"), "plan_title": _text(plan.get("title")),
        "group_id": group.lower() if isinstance(group, str) and _valid("guid", group) else None,
        "bucket_id": raw.get("bucketId"),
        "bucket_name": _text(buckets.get(raw.get("bucketId") or "")),
        "title": _text(raw.get("title")),
        "percent_complete": raw.get("percentComplete")
        if type(raw.get("percentComplete")) is int else None,
        "priority": raw.get("priority") if type(raw.get("priority")) is int else None,
        "start_at": _instant(raw.get("startDateTime")), "due_at": _instant(raw.get("dueDateTime")),
        "completed_at": _instant(raw.get("completedDateTime")),
        "created_at": _instant(raw.get("createdDateTime")),
        "created_by": _who(raw.get("createdBy")), "completed_by": _who(raw.get("completedBy")),
        "assignees": [a.lower() for a in assignees][:MAX_PEOPLE],
        "description": _text((details or {}).get("description"), MAX_DESCRIPTION)
        if details is not None else None,
        "checklist": [{"title": _text(item.get("title")), "checked": item.get("isChecked") is True}
                      for item in (checklist.values() if isinstance(checklist, Mapping) else ())
                      if isinstance(item, Mapping)][:200],
        "references": [{"title": _text(item.get("alias")), "type": _text(item.get("type"), 32)}
                       for item in (references.values() if isinstance(references, Mapping)
                                    else ()) if isinstance(item, Mapping)][:200],
        "etag": raw.get("@odata.etag")})


_CUE_TIME = re.compile(r"^\s*(\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3}\s+-->\s+(\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3}")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_TAG = re.compile(r"<[^>]*>")


def _clock(value: str) -> str:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        parts.insert(0, "00")
    return f"{int(parts[0]):02d}:{parts[1]}:{parts[2]}"


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """The cues of a WebVTT document: start, end, speaker (the ``<v>`` voice) and text."""
    cues: list[dict[str, Any]] = []
    block: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") + [""]:
        if line.strip():
            block.append(line)
            continue
        timing = next((i for i, l in enumerate(block) if _CUE_TIME.match(l)), None)
        if timing is not None:
            start, end = (s.strip().split()[0] for s in block[timing].split("-->", 1))
            body = " ".join(block[timing + 1:])
            voice = _VOICE.search(body)
            words = " ".join(_TAG.sub("", body).split())
            if words:
                cues.append({"start": _clock(start), "end": _clock(end),
                             "speaker": _text(voice.group(1), 200) if voice else None,
                             "text": words[:MAX_TEXT * 4]})
        block = []
    return cues


def transcript_row(organizer: str, meeting_id: str, raw: Mapping[str, Any],
                   event: Mapping[str, Any], cues: Sequence[Mapping[str, Any]],
                   digest: str) -> dict[str, Any]:
    lines = [f"[{c['start'][:8]}] {c['speaker'] or 'unknown'}: {c['text']}" for c in cues]
    return jsonable({
        "source": "graph_meeting_transcript", "kind": "meeting_transcript",
        "id": raw["id"], "meeting_id": meeting_id, "organizer": organizer,
        "join_url": (event.get("online_meeting") or {}).get("join_url"),
        "ical_uid": event.get("ical_uid"), "subject": event.get("subject"),
        "meeting_start": event.get("start"), "meeting_end": event.get("end"),
        "created_at": _instant(raw.get("createdDateTime")),
        "end_at": _instant(raw.get("endDateTime")),
        "speakers": sorted({c["speaker"] for c in cues if c["speaker"]}),
        "cue_count": len(cues), "cues": list(cues), "content_text": "\n".join(lines),
        "content_sha256": digest})


# ------------------------------------------------------------------ capture

def _failure(part: str, op: str, exc: BaseException, blocked: list[dict[str, Any]]) -> str:
    """``blocked:<code>[:<permission>]`` where the one Graph refusal rule blocks (the
    part added to ``blocked``), else ``source_error:<Class>[:<detail>]``."""
    found = graph_block(part, op, _PERMISSION, getattr(exc, "http_status", None))
    if found is not None:
        if found not in blocked:
            blocked.append(found)
        return blocked_reason(found)
    reason, detail = f"source_error:{type(exc).__name__}", unavailable_detail(exc)
    if detail:
        reason = (reason if len(f"{reason}:{detail}") <= REASON_MAX
                  else "source_error:SourceUnavailable") + ":" + detail
    return reason[:REASON_MAX * 2]


def _status(reasons: Iterable[str]) -> str:
    reasons = set(reasons)
    return ("blocked" if any(r.startswith("blocked:") for r in reasons)
            else "incomplete" if reasons else "complete")


def open_store(state_dir: str | Path, audience: Any) -> CaptureStore:
    """The meetings capture store under ``state_dir`` (outside every git work tree)."""
    root = assert_outside_git(state_dir) / STORE_DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = CaptureStore(root / DB_FILE, audience)
    os.chmod(root / DB_FILE, 0o600)
    return store


def _new_counts() -> dict[str, int]:
    return {key: 0 for key in (
        "mailboxes_listed", "mailboxes_excluded", "mailboxes_denied", "mailboxes_open",
        "mailboxes_not_visible", "mailboxes_no_calendar", "pages", "events_listed",
        "events_withheld", "events_undecided", "event_reads", "events_unchanged",
        "events_sensitive", "events_changed_during_read", "event_reads_failed",
        "versions_new", "task_reads_failed", "plans_found",
        "plans_read", "tasks_listed", "tasks_withheld", "task_reads", "tasks_unchanged",
        "meetings_checked", "meetings_external_organizer", "meetings_not_found",
        "transcripts_listed", "transcripts_read", "transcripts_withheld_unknown_speaker",
        "transcripts_withheld_excluded",
        "transcripts_unaddressable", "transcripts_over_cap", "provenance_dropped",
        "transcripts_withheld_no_occurrence", "events_denied", "groups_listed",
        "groups_excluded", "groups_unread", "plans_from_groups", "tasks_denied")}


@dataclass
class _Occurrence:
    """One cleared calendar occurrence of an online meeting."""
    start: datetime
    end: datetime
    event: dict[str, Any]
    names: frozenset[str]          # display names of the organizer and attendees, folded
    identities: tuple[str, ...]    # their addresses (the transcript's audience)


@dataclass
class _Meeting:
    """A cleared online meeting found on a calendar: where to look its transcripts up. A
    recurring Teams meeting has one join URL for every occurrence: each transcript is
    matched to the occurrence it was recorded in (``occurrence_of``)."""
    join_url: str
    organizer_address: str
    occurrences: dict[str, _Occurrence]   # by iCalUId and start

    def last_end(self, now: datetime) -> datetime | None:
        ended = [o.end for o in self.occurrences.values() if o.end <= now]
        return max(ended) if ended else None

    def occurrence_of(self, created: str | None) -> _Occurrence | None:
        """The occurrence whose start..end (15 minutes early to an hour late: a meeting
        opens early and overruns) holds the transcript's creation, the latest-starting
        one when several do; None when none does (withheld: its people are unknown)."""
        moment = _moment(created)
        if moment is None:
            return None
        held = [o for o in self.occurrences.values()
                if o.start - timedelta(minutes=15) <= moment <= o.end + timedelta(hours=1)]
        return max(held, key=lambda o: o.start) if held else None


def _moment(value: Any) -> datetime | None:
    text = _instant(value)
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


class MeetingsCapture:
    """One capture pass over a ``MeetingsScope`` into a ``CaptureStore`` as ``actor``."""

    def __init__(self, store: CaptureStore, api: Any, audience: LiveSourceAudience,
                 actor: Actor, *, workers: int = WORKERS,
                 clock: Callable[[], datetime] | None = None,
                 transcripts_granted: bool | None = None,
                 directory_api: Any = None) -> None:
        self.store, self.api, self.audience, self.actor = store, api, audience, actor
        self.transcripts_granted = (os.environ.get(TRANSCRIPTS_GRANTED) == "1"
                                    if transcripts_granted is None else bool(transcripts_granted))
        # the group listing and group members (``plan_groups``); default the capture's own
        self.directory_api = api if directory_api is None else directory_api
        self.people = people_exclusion.from_api(api)
        self.workers = max(1, min(int(workers), 32))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._deny: Any = None   # the mail capture's HR / payroll deny list (``run``)
        # object ids the deny list matched (directory listing, group members): a task
        # naming one is withheld like an excluded person's
        self._denied_ids: set[str] = set()

    def _denied(self, address: Any, department: Any = None) -> bool:
        """An organizer the HR / payroll deny list matches (their mail is never read, so
        neither are their meetings); an unloadable list denies everyone (fail closed)."""
        if self._deny is None:
            return True
        user = {"mail": address, "userPrincipalName": address}
        if department is not None:
            user["department"] = department
        return bool(address) and self._deny.denies(user)

    def _task_withheld(self, raw: Mapping[str, Any]) -> str | None:
        """``withheld`` (an excluded or undecided person), ``denied`` (a deny-listed person,
        or the deny list unloadable: fail closed) or None (the task may be read)."""
        people = _task_people(raw)
        if self._decides(people) is not False:
            return "withheld"
        if self._deny is None or self._denied_ids.intersection(people):
            return "denied"
        return None

    # ---------------------------------------------------------------- run

    def run(self, scope: MeetingsScope, *, out_of_time: Callable[[], bool] = lambda: False
            ) -> dict[str, Any]:
        counts, blocked = _new_counts(), []
        report: dict[str, Any] = {"counts": counts, "blocked_upstream": blocked,
                                  "mailboxes": [], "plans": [], "transcripts": {},
                                  "reasons": []}
        try:
            self.people.require()
            self.people.bind(self.api)
        except people_exclusion.ExclusionUnavailable:
            blocked.append(self.people.block_part("meetings.people_exclusion"))
            report["reasons"] = ["blocked:people_exclusion"]
            report["status"] = "blocked"
            return report
        from .mail_directory_scope import DirectoryScope, DirectoryScopeError
        try:
            self._deny = DirectoryScope.from_file(scope.directory_scope).deny
        except DirectoryScopeError:
            self._deny = None
        mailboxes, listing = self._mailboxes(scope, counts, blocked)
        reasons = set(listing)
        meetings: dict[str, _Meeting] = {}
        plan_ids: dict[str, None] = {}
        if "planner" in scope.parts and scope.plan_groups:
            found, why = self._group_plans(counts, blocked, out_of_time)
            plan_ids.update(dict.fromkeys(found))
            reasons |= why
        budget = {"events": scope.max_event_reads_per_run, "tasks": scope.max_task_reads_per_run,
                  "transcripts": scope.max_transcript_reads_per_run}
        for box in mailboxes:
            if out_of_time():
                reasons.add(TIME_BUDGET)
                break
            entry = {"status": "complete", "reasons": [], "resumed_from": None}
            if "calendar" in scope.parts:
                entry.update(self._calendar(box, scope, counts, blocked, budget, meetings,
                                            out_of_time))
            if "planner" in scope.parts:
                found = self._mailbox_plans(box, counts, blocked)
                if isinstance(found, str):
                    entry["reasons"] = sorted({*entry["reasons"], found})
                    entry["status"] = _status(entry["reasons"])
                else:
                    plan_ids.update(dict.fromkeys(found))
            report["mailboxes"].append(entry)
            reasons |= set(entry["reasons"])
        if "planner" in scope.parts:
            for plan_id in list(plan_ids)[:scope.max_plans_per_run]:
                if out_of_time():
                    reasons.add(TIME_BUDGET)
                    break
                result = self._plan(plan_id, scope, counts, blocked, budget)
                report["plans"].append(result)
                reasons |= set(result["reasons"])
            if len(plan_ids) > scope.max_plans_per_run:
                reasons.add("cap:max_plans")
        if "transcripts" in scope.parts and not self.transcripts_granted:
            # dormant: no online-meeting or transcript request until the grant is recorded
            report["transcripts"] = {"status": "dormant", "reasons": ["dormant:transcript_grant"],
                                     "needs": ["OnlineMeetingTranscript.Read.All",
                                               "OnlineMeetings.Read.All"]}
        elif "transcripts" in scope.parts:
            # Every held meeting is a candidate, not only the events this run read: an
            # incremental calendar read re-reads only changed events (mostly future ones),
            # so a meeting that ended after its event was last read was never looked up
            # (2026-11-08: 3,167 events held, 272 ended online meetings in the window,
            # no transcript lookup ever recorded).
            self._held_meetings(meetings, scope)
            result = self._transcripts(meetings, scope, counts, blocked, budget, out_of_time)
            report["transcripts"] = result
            reasons |= set(result["reasons"])
        report["reasons"] = sorted(reasons)
        report["status"] = _status(reasons)
        return report

    # ---------------------------------------------------------------- mailboxes

    def _mailboxes(self, scope: MeetingsScope, counts: dict[str, int],
                   blocked: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
        """The scope's mailboxes, opened (identity, owner ACL) as the capture actor; an
        excluded person's is refused before any request naming it."""
        from .graph_read import GraphMailReader

        reasons: set[str] = set()
        names = list(scope.mailboxes)
        if scope.all_mailboxes:
            from .mail_directory_scope import DirectoryScope, enumerate_mailboxes
            try:
                listing = enumerate_mailboxes(self.api,
                                              DirectoryScope.from_file(scope.directory_scope),
                                              self.people)
            except SourceUnavailable as exc:
                reasons.add(_failure("meetings.directory", "graph.users", exc, blocked))
                listing = None
            if listing is not None:
                counts["mailboxes_listed"] += listing.listed
                counts["mailboxes_excluded"] += listing.excluded
                counts["mailboxes_denied"] += listing.denied
                self._denied_ids |= set(listing.withheld)
                if listing.status != "complete":
                    reasons.add("cap:directory_listing")
                names += [m for m in listing.mailboxes if m not in names]
        reader = GraphMailReader(self.api, self.audience, people=self.people)
        boxes, seen = [], set()
        for name in names:
            try:
                box = reader.open_mailbox(self.actor, name)
            except people_exclusion.PeopleExcluded:
                counts["mailboxes_excluded"] += 1
                continue
            except NotVisible:
                counts["mailboxes_not_visible"] += 1
                reasons.add("mailbox_not_visible")
                continue
            except SourceUnavailable as exc:
                reasons.add(_failure("meetings.mailbox_lookup", "graph.user", exc, blocked))
                continue
            if box["object_id"] in seen:
                continue
            seen.add(box["object_id"])
            counts["mailboxes_open"] += 1
            boxes.append(box)
        return boxes, reasons

    # ---------------------------------------------------------------- calendar

    def _window(self, scope: MeetingsScope) -> dict[str, str]:
        now = self._clock().replace(microsecond=0)
        start = (now - timedelta(days=scope.window_days_back)).replace(hour=0, minute=0,
                                                                       second=0)
        end = (now + timedelta(days=scope.window_days_ahead)).replace(hour=0, minute=0,
                                                                      second=0)
        return {"startDateTime": start.isoformat().replace("+00:00", "Z"),
                "endDateTime": end.isoformat().replace("+00:00", "Z")}

    def _decides(self, addresses: Iterable[str]) -> bool | None:
        """True: someone excluded; False: everyone cleared; None: undecided."""
        try:
            return self.people.excluded(addresses)
        except people_exclusion.ExclusionUnavailable:
            return None

    def _calendar(self, box: Mapping[str, Any], scope: MeetingsScope, counts: dict[str, int],
                  blocked: list[dict[str, Any]], budget: dict[str, int],
                  meetings: dict[str, _Meeting], out_of_time: Callable[[], bool]
                  ) -> dict[str, Any]:
        oid = box["object_id"]
        unit = EVENT_KIND.unit((oid, "x"))
        acl = dict(box["source_acl"])
        current = {r.path[1]: r for r in self.store.current_records(EVENT_KIND.name, unit)}
        run = self.store.begin_run(EVENT_KIND.name, unit, list(EVENT_KIND.resources((oid, "x"))))
        run_id, resumed_from = run["run_id"], None
        url = grammar.url("graph.user_calendar_view",
                          {**self._window(scope), "$select": LISTING_SELECT,
                           "$top": str(scope.page_size)}, user=oid)
        if run["resumed"] and run["cursor"]:
            url, resumed_from = run["cursor"], "cursor"
        pages, reasons, unread = 0, set(), [0]
        while True:
            if pages >= scope.max_pages_per_mailbox or out_of_time():
                cap = TIME_BUDGET if out_of_time() else "cap:page_cap"
                self.store.pause_run(run_id, reason=cap)
                reasons.add(cap)
                break
            try:
                items, next_url = self.api.get_page(url, headers=UTC_PREFER)
            except SourceUnavailable as exc:
                if getattr(exc, "http_status", None) == 404:
                    # no Exchange calendar for this user: settled, nothing to read
                    counts["mailboxes_no_calendar"] += 1
                    self.store.finish_run(run_id, "complete", reason="no_calendar")
                    break
                failed = _failure("meetings.calendar_view", "graph.user_calendar_view", exc,
                                  blocked)
                self.store.pause_run(run_id, reason=failed[:200])
                reasons.add(failed)
                break
            records, stop = self._events(oid, acl, items, current, counts, budget, meetings,
                                         unread)
            saved = self.store.save_page(run_id, records, url if stop else next_url)
            counts["versions_new"] += saved["new_versions"]
            counts["pages"] += 1
            pages += 1
            if stop:
                self.store.pause_run(run_id, reason="cap:event_reads")
                reasons.add("cap:event_reads")
                break
            if next_url is None:
                if unread[0]:
                    # an event whose content read failed (or changed meanwhile) keeps its
                    # held version; a partial run marks nothing missing
                    reasons.add("event_content_unread")
                    self.store.finish_run(run_id, "partial", reason="event_content_unread")
                else:
                    self.store.finish_run(run_id, "complete")
                break
            url = next_url
        return {"status": _status(reasons), "reasons": sorted(reasons),
                "resumed_from": resumed_from}

    def _events(self, oid: str, acl: Mapping[str, Any], items: Sequence[Mapping[str, Any]],
                current: Mapping[str, CapturedRecord], counts: dict[str, int],
                budget: dict[str, int], meetings: dict[str, _Meeting], unread: list[int]
                ) -> tuple[list[CapturedRecord], bool]:
        """The records of one listed page (identity fields decided first), and whether the
        content-read cap stopped it (the page is listed again next run)."""
        kept: list[tuple[Mapping[str, Any], str]] = []
        for raw in items:
            counts["events_listed"] += 1
            if not isinstance(raw, Mapping) or not _valid("mail_id", raw.get("id")) \
                    or not isinstance(raw.get("changeKey"), str) or not raw["changeKey"]:
                counts["provenance_dropped"] += 1
                continue
            decided = self._decides(event_people(raw))
            if decided is None:
                counts["events_undecided"] += 1
                continue
            if decided:
                counts["events_withheld"] += 1
                continue
            organizer = _party(raw.get("organizer"))
            if self._denied((organizer or {}).get("address")):
                counts["events_denied"] += 1
                continue
            kept.append((raw, raw["changeKey"]))
        need = [raw for raw, key in kept
                if not (raw["id"] in current and current[raw["id"]].source_version == key)
                and str(raw.get("sensitivity") or "").lower() not in SENSITIVE]
        stop = len(need) > budget["events"]
        need = need[:budget["events"]]
        budget["events"] -= len(need)
        contents = dict(zip((r["id"] for r in need), self._read_all(
            [grammar.url("graph.user_event", {"$select": CONTENT_SELECT}, user=oid,
                         event=r["id"]) for r in need])))
        records = []
        for raw, key in kept:
            held = current.get(raw["id"])
            if raw["id"] in contents:
                content = contents[raw["id"]]
                counts["event_reads"] += 1
                again = self._decides(event_people(content)) \
                    if isinstance(content, Mapping) else False
                if again is not False:
                    counts["events_withheld" if again else "events_undecided"] += 1
                    continue
                if not isinstance(content, Mapping) or content.get("id") != raw["id"] \
                        or content.get("changeKey") != key:
                    counts["events_changed_during_read" if isinstance(content, Mapping)
                           else "event_reads_failed"] += 1
                    unread[0] += 1
                    if held is not None:   # the version held stays seen
                        records.append(held)
                        self._note_meeting(held.payload["row"], meetings)
                    continue
                row = event_row(oid, raw, content, "read")
            elif held is not None and held.source_version == key:
                counts["events_unchanged"] += 1
                row = dict(held.payload["row"])
            elif str(raw.get("sensitivity") or "").lower() in SENSITIVE:
                counts["events_sensitive"] += 1
                row = event_row(oid, raw, None, "withheld_sensitivity")
            else:
                continue   # past the content-read cap: listed again next run
            records.append(CapturedRecord(
                (oid, raw["id"]), {"row": row, "meta": {"source_acl": dict(acl),
                                                        "source_api": "graph.user_calendar_view"}},
                key, LINEAGE))
            self._note_meeting(row, meetings)
        return records, stop

    def _read_all(self, urls: Sequence[str]) -> list[Any]:
        """GET each URL (at most ``workers`` at once); a failure reads as None."""
        def one(url: str) -> Any:
            try:
                return self.api.get_json(url, headers=UTC_PREFER)
            except SourceUnavailable:
                return None
        if len(urls) <= 1 or self.workers == 1:
            return [one(u) for u in urls]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(one, urls))

    def _held_meetings(self, meetings: dict[str, _Meeting], scope: MeetingsScope) -> None:
        """Add the online meetings of every calendar event the store holds (current
        versions, each cleared by the guard when it was read) that ended within
        ``transcript_days_back``; an event this run read stays as read."""
        floor = self._clock() - timedelta(days=scope.transcript_days_back)
        for unit in self.store.units(EVENT_KIND.name):
            records = self.store.current_records(EVENT_KIND.name, unit)
            # A mailbox whose owner is excluded now (or undecided) gives no candidate.
            owners = {r.path[0] for r in records}
            if not owners or self._decides(sorted(owners)) is not False:
                continue
            for record in records:
                row = (record.payload or {}).get("row") if isinstance(record.payload,
                                                                      Mapping) else None
                if not isinstance(row, Mapping):
                    continue
                end = _moment(row.get("end"))
                if end is None or end < floor:
                    continue
                self._note_meeting(row, meetings)

    def _note_meeting(self, row: Mapping[str, Any], meetings: dict[str, _Meeting]) -> None:
        join = (row.get("online_meeting") or {}).get("join_url")
        organizer = (row.get("organizer") or {}).get("address")
        start, end = _moment(row.get("start")), _moment(row.get("end"))
        if not join or not organizer or row.get("is_cancelled") or start is None \
                or end is None or row.get("content") != "read":
            return   # a private occurrence's transcript neither: it matches no occurrence
        people = [row.get("organizer") or {}, *(row.get("attendees") or [])]
        names = frozenset(p["name"].casefold() for p in people if p.get("name"))
        identities = tuple(sorted({p["address"] for p in people if p.get("address")}))
        meeting = meetings.setdefault(join, _Meeting(join, organizer, {}))
        if meeting.organizer_address != organizer:
            return   # one join URL, two organizers: decided by the first, never mixed
        key = f"{row.get('ical_uid')}|{row.get('start')}"
        meeting.occurrences.setdefault(key, _Occurrence(start, end, dict(row), names,
                                                        identities))

    # ---------------------------------------------------------------- planner

    def _group_plans(self, counts: dict[str, int], blocked: list[dict[str, Any]],
                     out_of_time: Callable[[], bool] = lambda: False
                     ) -> tuple[list[str], set[str]]:
        """Every plan of every Microsoft 365 group the app can read, less the groups whose
        members are all excluded or deny-listed people. Groups left when the run's time
        budget ends are not read (``TIME_BUDGET``). A group whose members cannot be listed or decided
        is skipped whole (fail closed); its plans may still be found through a mailbox's
        tasks. No excluded person's id is ever placed in a request."""
        try:
            self.people.require_bound()
        except people_exclusion.ExclusionUnavailable:
            blocked.append(self.people.block_part("meetings.people_exclusion"))
            return [], {"blocked:people_exclusion"}
        if out_of_time():
            return [], {TIME_BUDGET}
        try:
            groups, done = self.directory_api.get_pages(grammar.url(
                "graph.groups", {"$filter": "groupTypes/any(c:c eq 'Unified')",
                                 "$select": "id", "$top": "999"}),
                max_pages=50, max_items=5000)
        except SourceUnavailable as exc:
            return [], {_failure("meetings.groups", "graph.groups", exc, blocked)}
        reasons = set() if done.get("complete") else {"cap:group_listing"}
        ids = list(dict.fromkeys(str(g.get("id") or "").lower() for g in groups
                                 if isinstance(g, Mapping) and _valid("guid", g.get("id"))))
        counts["groups_listed"] += len(ids)

        def one(group: str) -> tuple[str, Any]:
            if out_of_time():
                return "late", None
            try:
                members, whole = self.directory_api.get_pages(grammar.url(
                    "graph.group_members", {"$select": "id,mail,userPrincipalName,department",
                                            "$top": "999"}, group=group),
                    max_pages=20, max_items=5000)
            except SourceUnavailable as exc:
                return "unread", _failure("meetings.group_members", "graph.group_members",
                                          exc, blocked)
            if not whole.get("complete"):
                return "unread", "cap:group_members"
            verdicts = []
            for m in members:
                if not isinstance(m, Mapping):
                    continue
                verdict = self._decides([v for v in (m.get("id"), m.get("mail"),
                                                     m.get("userPrincipalName"))
                                         if isinstance(v, str) and v])
                address = m.get("mail") or m.get("userPrincipalName")
                if verdict is False and address and self._denied(address,
                                                                 m.get("department")):
                    if isinstance(m.get("id"), str):
                        self._denied_ids.add(m["id"].lower())
                    verdict = True
                verdicts.append(verdict)
            if None in verdicts:
                return "unread", "group_members_undecided"
            if verdicts and all(verdicts):
                return "excluded", None
            try:
                plans, whole = self.api.get_pages(grammar.url(
                    "graph.group_planner_plans", {"$select": "id"}, group=group),
                    max_pages=20, max_items=2000)
            except SourceUnavailable as exc:
                return "unread", _failure("meetings.group_plans", "graph.group_planner_plans",
                                          exc, blocked)
            found = [p["id"] for p in plans
                     if isinstance(p, Mapping) and _valid("planner_id", p.get("id"))]
            return "read", (found, None if whole.get("complete") else "cap:group_plans")

        if self.workers == 1 or len(ids) <= 1:
            results = [one(g) for g in ids]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(one, ids))
        plan_ids: dict[str, None] = {}
        for state, value in results:
            if state == "late":
                reasons.add(TIME_BUDGET)
            elif state == "excluded":
                counts["groups_excluded"] += 1
            elif state == "unread":
                counts["groups_unread"] += 1
                reasons.add(value)
            else:
                found, cap = value
                plan_ids.update(dict.fromkeys(found))
                if cap:
                    reasons.add(cap)
        counts["plans_from_groups"] += len(plan_ids)
        return list(plan_ids), reasons

    def _mailbox_plans(self, box: Mapping[str, Any], counts: dict[str, int],
                       blocked: list[dict[str, Any]]) -> list[str] | str:
        """The plans the mailbox's own (cleared) tasks belong to, or a failure reason."""
        try:
            tasks, done = self.api.get_pages(grammar.url(
                "graph.user_planner_tasks", {"$select": "id,planId,assignments,createdBy,"
                                                        "completedBy"},
                user=box["object_id"]), max_pages=20, max_items=5000)
        except SourceUnavailable as exc:
            return _failure("meetings.planner_tasks", "graph.user_planner_tasks", exc, blocked)
        plans = [t["planId"] for t in tasks if _valid("planner_id", t.get("planId"))
                 and self._task_withheld(t) is None]
        counts["plans_found"] += len(set(plans))
        return list(dict.fromkeys(plans)) if done.get("complete") else "cap:planner_tasks"

    def _plan(self, plan_id: str, scope: MeetingsScope, counts: dict[str, int],
              blocked: list[dict[str, Any]], budget: dict[str, int]) -> dict[str, Any]:
        unit = TASK_KIND.unit((plan_id, "x"))
        result: dict[str, Any] = {"status": "incomplete", "reasons": []}
        try:
            plan = self.api.get_json(grammar.url("graph.planner_plan", None, plan=plan_id))
            buckets, _ = self.api.get_pages(grammar.url("graph.planner_plan_buckets", None,
                                                        plan=plan_id), max_pages=5,
                                            max_items=1000)
            tasks, done = self.api.get_pages(grammar.url("graph.planner_plan_tasks", None,
                                                         plan=plan_id), max_pages=50,
                                             max_items=5000)
        except SourceUnavailable as exc:
            result["reasons"] = [_failure("meetings.planner_plan", "graph.planner_plan_tasks",
                                          exc, blocked)]
            result["status"] = _status(result["reasons"])
            return result
        if not isinstance(plan, Mapping) or plan.get("id") != plan_id:
            result["reasons"] = ["plan_identity_mismatch"]
            return result
        counts["plans_read"] += 1
        names = {b["id"]: b.get("name") for b in buckets if isinstance(b.get("id"), str)}
        container = plan.get("container") if isinstance(plan.get("container"), Mapping) else {}
        group = container.get("containerId") if container.get("type") == "group" else None
        group_identity = [f"group:{group.lower()}"] if isinstance(group, str) \
            and _valid("guid", group) else []
        current = {r.path[1]: r for r in self.store.current_records(TASK_KIND.name, unit)}
        run = self.store.begin_run(TASK_KIND.name, unit, list(TASK_KIND.resources((plan_id, "x"))))
        kept, reasons = [], set()
        for raw in tasks:
            counts["tasks_listed"] += 1
            if not isinstance(raw, Mapping) or not _valid("planner_id", raw.get("id")) \
                    or raw.get("planId") != plan_id or not isinstance(raw.get("@odata.etag"), str):
                counts["provenance_dropped"] += 1
                continue
            why = self._task_withheld(raw)
            if why is not None:
                counts["tasks_withheld" if why == "withheld" else "tasks_denied"] += 1
                continue
            kept.append(raw)
        changed = [raw for raw in kept if not (raw["id"] in current and current[raw["id"]]
                                               .source_version == raw["@odata.etag"])]
        # Details hold a description, checklist and references; a task with none of them
        # (``hasDescription``, the item counts) has nothing more to read.
        need = [raw for raw in changed if _has_details(raw)]
        bare = {raw["id"] for raw in changed if not _has_details(raw)}
        if len(need) > budget["tasks"]:
            reasons.add("cap:task_reads")
        need = need[:budget["tasks"]]
        budget["tasks"] -= len(need)
        details = dict(zip((r["id"] for r in need), self._read_all(
            [grammar.url("graph.planner_task_details", None, task=r["id"]) for r in need])))
        records = []
        for raw in kept:
            held = current.get(raw["id"])
            if raw["id"] in bare:
                row = task_row(plan, names, raw, {})
            elif raw["id"] in details:
                counts["task_reads"] += 1
                found = details[raw["id"]]
                if not isinstance(found, Mapping) or found.get("id") != raw["id"]:
                    counts["task_reads_failed"] += 1
                    reasons.add("task_details_unread")
                    if held is not None:   # the version held stays seen
                        records.append(held)
                    continue
                row = task_row(plan, names, raw, found)
            elif held is not None and held.source_version == raw["@odata.etag"]:
                counts["tasks_unchanged"] += 1
                row = dict(held.payload["row"])
            else:
                continue
            acl = {"status": "derived", "kind": "plan_people",
                   "identities": sorted({*group_identity, *_task_people(raw)})}
            records.append(CapturedRecord((plan_id, raw["id"]),
                                          {"row": row, "meta": {"source_acl": acl,
                                                                "source_api": "graph.planner_plan_tasks"}},
                                          raw["@odata.etag"], LINEAGE))
        saved = self.store.save_page(run["run_id"], records, None)
        counts["versions_new"] += saved["new_versions"]
        if not done.get("complete"):
            reasons.add("cap:plan_tasks")
        if reasons:
            self.store.pause_run(run["run_id"], reason=min(reasons))
        else:
            self.store.finish_run(run["run_id"], "complete")
        result["reasons"] = sorted(reasons)
        result["status"] = _status(reasons)
        return result

    # ---------------------------------------------------------------- transcripts

    def _transcripts(self, meetings: Mapping[str, _Meeting], scope: MeetingsScope,
                     counts: dict[str, int], blocked: list[dict[str, Any]],
                     budget: dict[str, int], out_of_time: Callable[[], bool]
                     ) -> dict[str, Any]:
        now = self._clock()
        try:
            checks = json.loads(self.store.state(TRANSCRIPT_CHECKS) or "{}")
        except ValueError:
            checks = {}
        reasons: set[str] = set()
        organizers: dict[str, str | None] = {}
        found: dict[str, list[CapturedRecord]] = {}
        held: dict[str, set[str]] = {}   # organizer -> transcript ids held
        for join, meeting in sorted(meetings.items(),
                                    key=lambda m: m[1].last_end(now) or now):
            ended = meeting.last_end(now)
            if ended is None or ended < now - timedelta(days=scope.transcript_days_back):
                continue
            # Due until a day after its latest ended occurrence (a series is checked again
            # whenever another occurrence ends).
            key = sha256(join.encode("utf-8")).hexdigest()[:32]
            last = checks.get(key)
            if isinstance(last, str) and last > (ended + timedelta(days=1)).isoformat():
                continue
            if out_of_time():
                reasons.add(TIME_BUDGET)
                break
            if budget["transcripts"] <= 0:
                reasons.add("cap:transcript_reads")
                break
            # Every participant of every occurrence cleared under the exclusion as it is
            # now (an attendee excluded since the event was read withholds the meeting;
            # undecided withholds it too), before any request names the meeting.
            parties = {meeting.organizer_address, *(i for o in meeting.occurrences.values()
                                                    for i in o.identities)}
            if self._decides(sorted(p for p in parties if p)) is not False:
                counts["transcripts_withheld_excluded"] += 1
                continue   # not marked checked: decided again each run, no request made
            organizer = self._organizer(meeting.organizer_address, organizers, counts,
                                        blocked, reasons)
            if organizer is None:
                checks[key] = now.isoformat()
                continue
            counts["meetings_checked"] += 1
            try:
                listed = self.api.get_json(grammar.url(
                    "graph.user_online_meetings",
                    {"$filter": "JoinWebUrl eq '" + join.replace("'", "''") + "'"},
                    user=organizer))
                online = [m for m in (listed.get("value") or []) if isinstance(m, Mapping)
                          and m.get("joinWebUrl") in (join, None)]
            except SourceUnavailable as exc:
                failed = _failure("meetings.online_meetings", "graph.user_online_meetings",
                                  exc, blocked)
                reasons.add(failed)
                if failed.startswith("blocked:"):
                    break   # the application may not read online meetings: none will answer
                continue
            if not online:
                counts["meetings_not_found"] += 1
                checks[key] = now.isoformat()
                continue
            if organizer not in held:
                held[organizer] = {r.path[2] for r in self.store.current_records(
                    TRANSCRIPT_KIND.name, TRANSCRIPT_KIND.unit((organizer, "x", "x")))}
            for record in self._meeting_transcripts(organizer, online[0], meeting, scope,
                                                    counts, blocked, budget, reasons,
                                                    held[organizer]):
                found.setdefault(organizer, []).append(record)
            checks[key] = now.isoformat()
        for organizer, records in found.items():
            unit = TRANSCRIPT_KIND.unit((organizer, "x", "x"))
            # Every transcript held of this organizer is seen again (a run checks only the
            # meetings due), so finishing complete marks nothing missing it did not check.
            held = {r.path: r for r in self.store.current_records(TRANSCRIPT_KIND.name, unit)}
            held.update({r.path: r for r in records})
            run = self.store.begin_run(TRANSCRIPT_KIND.name, unit,
                                       list(TRANSCRIPT_KIND.resources((organizer, "x", "x"))))
            saved = self.store.save_page(run["run_id"], list(held.values()), None)
            counts["versions_new"] += saved["new_versions"]
            self.store.finish_run(run["run_id"], "complete")
        self.store.set_state(TRANSCRIPT_CHECKS, json.dumps(checks, sort_keys=True))
        return {"status": _status(reasons), "reasons": sorted(reasons)}

    def _organizer(self, address: str, cache: dict[str, str | None], counts: dict[str, int],
                   blocked: list[dict[str, Any]], reasons: set[str]) -> str | None:
        """The organizer's object id: a tenant user the guard cleared, else None."""
        if address in cache:
            return cache[address]
        cache[address] = None
        if self._decides([address]) is not False or not _valid("user_ref", address):
            return None
        try:
            raw = self.api.get_json(grammar.url(
                "graph.user", {"$select": "id,mail,userPrincipalName,proxyAddresses,department"},
                user=address))
        except SourceUnavailable as exc:
            if getattr(exc, "http_status", None) == 404:
                counts["meetings_external_organizer"] += 1
            else:
                reasons.add(_failure("meetings.organizer_lookup", "graph.user", exc, blocked))
            return None
        raw = raw if isinstance(raw, Mapping) else {}
        oid = str(raw.get("id") or "").lower()
        identities = [oid, raw.get("mail"), raw.get("userPrincipalName"),
                      *(p[5:] for p in raw.get("proxyAddresses") or ()
                        if isinstance(p, str) and p.lower().startswith("smtp:"))]
        if not _valid("user_id", oid) or self._decides(
                [i for i in identities if isinstance(i, str) and i]) is not False:
            return None
        if any(self._denied(i, raw.get("department")) for i in identities[1:]
               if isinstance(i, str) and i):
            counts["events_denied"] += 1
            return None
        cache[address] = oid
        return oid

    def _meeting_transcripts(self, organizer: str, online: Mapping[str, Any],
                             meeting: _Meeting, scope: MeetingsScope, counts: dict[str, int],
                             blocked: list[dict[str, Any]], budget: dict[str, int],
                             reasons: set[str], held: set[str]) -> list[CapturedRecord]:
        meeting_id = online.get("id")
        if not _valid("meeting_id", meeting_id):
            counts["transcripts_unaddressable"] += 1
            return []
        try:
            listed, _ = self.api.get_pages(grammar.url(
                "graph.online_meeting_transcripts", None, user=organizer, meeting=meeting_id),
                max_pages=5, max_items=100)
        except SourceUnavailable as exc:
            reasons.add(_failure("meetings.transcripts", "graph.online_meeting_transcripts",
                                 exc, blocked))
            return []
        records = []
        for raw in listed:
            counts["transcripts_listed"] += 1
            if not _valid("meeting_id", raw.get("id")):
                counts["transcripts_unaddressable"] += 1
                continue
            if raw["id"] in held:
                continue   # held already: a transcript's text does not change
            occurrence = meeting.occurrence_of(raw.get("createdDateTime"))
            if occurrence is None:
                counts["transcripts_withheld_no_occurrence"] += 1
                continue
            acl = {"status": "derived", "kind": "meeting_participants",
                   "identities": sorted({organizer, *occurrence.identities})}
            if budget["transcripts"] <= 0:
                reasons.add("cap:transcript_reads")
                break
            budget["transcripts"] -= 1
            try:
                data = self.api.get_document(grammar.url(
                    "graph.online_meeting_transcript_content", None, user=organizer,
                    meeting=meeting_id, transcript=raw["id"]),
                    max_bytes=scope.max_transcript_bytes)
            except SourceUnavailable as exc:
                if unavailable_detail(exc) == "response_too_large":
                    counts["transcripts_over_cap"] += 1
                    reasons.add("cap:transcript_bytes")
                else:
                    reasons.add(_failure("meetings.transcript_content",
                                         "graph.online_meeting_transcript_content", exc,
                                         blocked))
                continue
            counts["transcripts_read"] += 1
            try:
                cues = parse_vtt(data.decode("utf-8-sig"))[:MAX_CUES]
            except UnicodeDecodeError:
                counts["provenance_dropped"] += 1
                continue
            speakers = {c["speaker"].casefold() for c in cues if c["speaker"]}
            if speakers - occurrence.names:
                counts["transcripts_withheld_unknown_speaker"] += 1
                continue
            digest = sha256(data).hexdigest()
            row = transcript_row(organizer, meeting_id, raw, occurrence.event, cues, digest)
            records.append(CapturedRecord((organizer, meeting_id, raw["id"]),
                                          {"row": row, "meta": {
                                              "source_acl": acl,
                                              "source_api": "graph.online_meeting_transcript_content"}},
                                          digest, LINEAGE))
        return records


# ------------------------------------------------------------------ CLI (counts only)

def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory (outside every git work tree)")
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE),
                        help="plx-meetings-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default="staff:meetings-withheld",
                        help="a principal the audience does not grant: it must see nothing")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="no part starts after this many seconds (resumes next run)")
    parser.add_argument("--workers", type=int, default=WORKERS)


def _read_back(store: CaptureStore, actor: Actor) -> tuple[dict[str, int], list[str]]:
    """Records ``actor`` may see per kind, and their citations."""
    seen: dict[str, int] = {}
    handles: list[str] = []
    for kind in KINDS:
        for unit in store.units(kind.name):
            after = None
            while True:
                try:
                    page = store.records(actor, kind.name, unit, after_path=after, limit=1000)
                except CaptureError:
                    break
                seen[kind.name] = seen.get(kind.name, 0) + len(page["items"])
                handles += [item["citation"] for item in page["items"]]
                after = page["next_after_path"]
                if after is None:
                    break
    return seen, handles


def build_api(environ: Mapping[str, str] | None, *, sender: Any = None, retry: Any = None
              ) -> tuple[Any, Any]:
    """The guarded Graph client over the credential prefix ``CREDENTIALS_ENV`` names."""
    from .graph_read import build_graph

    env = os.environ if environ is None else environ
    from .live_source_guard import meetings_read_credentials

    credential_prefix(env)   # an unknown prefix refuses before anything is read
    return build_graph(env, sender=sender, retry=retry,
                       credentials=meetings_read_credentials(env))


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Capture ``--runs`` times, then read the store back as the capture principal and as
    a withheld one. Counts, booleans and enums only."""
    from .live_source_guard import BoundedBackoff

    if isinstance(args.runs, bool) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    scope = MeetingsScope.from_file(args.scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
    withheld = Actor(audience.tenant, frozenset({args.withheld_principal}), "restricted")
    store = open_store(args.state_dir, audience)
    started = clock()
    budget = args.budget_seconds

    def out_of_time() -> bool:
        return budget is not None and clock() - started >= budget

    try:
        api, transport = build_api(environ, sender=sender, retry=BoundedBackoff(
            budget_seconds=min(RETRY_BUDGET_SECONDS, budget or RETRY_BUDGET_SECONDS)))
        directory_api, directory_transport = None, None
        if scope.plan_groups and scope.directory_credentials not in (
                None, credential_prefix(os.environ if environ is None else environ)):
            directory_api, directory_transport = build_api(
                {**(os.environ if environ is None else environ),
                 CREDENTIALS_ENV: scope.directory_credentials},
                sender=sender, retry=BoundedBackoff(budget_seconds=min(
                    RETRY_BUDGET_SECONDS, budget or RETRY_BUDGET_SECONDS)))
        capture = MeetingsCapture(
            store, api, audience, actor, workers=args.workers,
            transcripts_granted=(os.environ if environ is None else environ).get(
                TRANSCRIPTS_GRANTED) == "1",
            directory_api=directory_api)
        runs, blocked = [], []
        for number in range(1, args.runs + 1):
            run = capture.run(scope, out_of_time=out_of_time)
            runs.append({"run": number, "status": run["status"], "reasons": run["reasons"],
                         "counts": dict(run["counts"]),
                         "mailboxes": [{"status": m["status"], "reasons": m["reasons"],
                                        "resumed_from": m["resumed_from"]}
                                       for m in run["mailboxes"]],
                         "plans": [{"status": p["status"], "reasons": p["reasons"]}
                                   for p in run["plans"]],
                         "transcripts": run["transcripts"]})
            blocked += [p for p in run["blocked_upstream"] if p not in blocked]
            if out_of_time() or run["status"] == "blocked":
                break
        visible, handles = _read_back(store, actor)
        hidden, _ = _read_back(store, withheld)
        resolved = sum(1 for h in handles[:500] if store.resolve(h, actor).get("ok"))
        leaked = sum(1 for h in handles[:500] if store.resolve(h, withheld).get("ok"))
        last = runs[-1]
        transports = [t for t in (transport, directory_transport) if t is not None]
        report = {
            "check": "graph.meetings_capture",
            "evidence_label": "injected-transport" if transport.sender_injected
            else "live-read",
            "read_only_guard": "GET-only; token POST only",
            "credentials": credential_prefix(os.environ if environ is None else environ),
            "graph_permissions_required": list(GRAPH_PERMISSIONS),
            "storage": {"outside_git_tree": True},
            "status": last["status"], "blocked_upstream": blocked, "runs": runs,
            # what the store holds now (writer side; a Planner task is visible only to a
            # reader its group or people grant, so the capture actor reads none back)
            "relations": {RELATIONS[kind.name]: held for kind in KINDS if (held := sum(
                len(store.current_versions(kind.name, unit))
                for unit in store.units(kind.name)))},
            "capture_actor_rows": {RELATIONS[k]: n for k, n in sorted(visible.items()) if n},
            "resolve_check": {"handles_checked": min(len(handles), 500),
                              "resolved_offline": resolved},
            "leak_check": {"withheld_actor_rows": sum(hidden.values()),
                           "withheld_actor_resolved": leaked},
            "requests_sent": sum(t.requests_sent for t in transports),
            "gets_sent": sum(t.gets_sent for t in transports),
            "operations_read": sorted(set().union(*(t.operations_read for t in transports)))}
        report["ok"] = bool(resolved == min(len(handles), 500) and leaked == 0
                            and not sum(hidden.values())
                            and not any(r.startswith("source_error:")
                                        for r in last["reasons"]))
        return report
    finally:
        store.close()


__all__ = ["CAPTURE_PRINCIPAL", "CREDENTIALS_ENV", "DB_FILE", "EVENT_KIND", "KINDS",
           "MeetingsCapture", "MeetingsCaptureError", "MeetingsScope", "RELATIONS",
           "SCOPE_SCHEMA", "STORE_DIR", "TASK_KIND", "TRANSCRIPT_KIND", "UNIT_PREFIX",
           "add_cli_arguments", "build_api", "credential_prefix", "event_row", "open_store",
           "parse_vtt", "run_cli", "task_row", "transcript_row"]
