"""Versioned Teams capture (L1): channel and chat messages into Workbench state.

Microsoft Teams as a captured
source. Everything is read through the one GET-only Graph client
(``integrations.graph_read.build_graph`` -> ``live_source_guard``; the closed
grammar's ``graph.user_joined_teams``, ``graph.user_chats``, ``graph.team_*``,
``graph.channel_*`` and ``graph.chat_*`` templates). There is no other HTTP path and
no write: nothing is posted, replied to or marked read.

The CLI runs a census first (``TeamsCapture.census``): every team of the tenant, each team's channels and the chats of the
scope's chat accounts, names and 30-day message counts only. Listings select ids and
types; a team's, channel's or chat's name is requested only once its members cleared
the people guard (``integrations.people_exclusion``), and one with an excluded member
(or members that could not be read whole) is refused before its name or channels are
requested. Team counts come from the tenant's 30-day team activity report
(``Reports.Read.All``, one GET, no content); a channel's count would need a content
read, so it stays None. The census never lists, reads or counts a message, never
calls getAllMessages and never subscribes (``CENSUS_OPERATIONS``). Names, ids and the
purchasing / QA / R&D channels it proposes (by the scope's ``census.proposals``
keywords: a proposal, never a grant) go to ``<state-dir>/teams-capture/census.json``
(0600, outside Git) and never to the report.

Content is read of every channel and chat this run's census lists and the people guard
cleared (No human content confirmation; the census runs the
guard first, a blocked guard clears nothing). ``content-confirmation.json`` is neither
required nor consulted; ``read_confirmation`` stays only for old files.

The people guard runs before every request: a member is cleared before the lookup naming
them, a conversation before its name or messages, and a chat whose id embeds an excluded
person's object id (a one-on-one chat) is refused before any request naming it. Once
refused, a team, channel or chat is not requested again in the run. ``RequestLog``
checks each GET the client sends against the guard and the refused set and counts the
ones that name either (``coverage.excluded_requested``: 0, or the run is not ok).

Reads, per ``TeamsCapture.run`` over a capture scope (``plx-teams-capture-scope/v1``; the
CLI passes only the confirmed scope):

* Discovery: explicit teams (all channels, or named ones), explicit chats,
  ``members`` (the teams a member has joined) and, with ``member_chats``
  ``mail_allowlist``, the chats of every account on the canonical mail allowlist
  (``operating/case_mail_scope.json``, the reported Graph allowlist; decision ``2026-10-07-ovs-teams-capture`` item 2). Those
  accounts are derived from the allowlist itself, so the scope keeps no second list of
  whose chats are read: an account added to or removed from the allowlist is added to
  or removed from the chat reads. A member must be on that allowlist, checked on the
  canonical directory identity before anything of theirs is listed; others are set
  aside (``outside_allowlist``). ``discovered_total`` counts the conversations found.
  At most ``max_conversations`` are read per run, channels and chats taken in turn so
  neither kind crowds out the other. Beyond the cap the runs rotate (``_rotate``): each
  kind in unit order from just after the unit that kind last read (round to the
  start), the kinds in turn from the one due; the cursor is a unit, never a position,
  so a conversation added or removed between runs moves no other one, and repeated runs
  read every conversation. The cursor and the units the current pass read complete live
  in the store (``CaptureStore.state``, layout ``ROTATION_VERSION``: any other layout,
  older or unreadable, starts the pass over with nothing visited): a unit counts as
  visited only when its latest read finished complete (one cut short by a cap, refused
  or failed is read again when the rotation comes round to it). Until every conversation
  found is visited, discovery is ``cap:conversation_cap`` (``unvisited`` counts the
  rest), so no run reports the pass complete while a conversation in it is cut off; then
  a new pass starts. A run under the cap reads every conversation and clears the
  rotation.
* Time budget: a run given ``out_of_time`` (the caller's deadline) starts no conversation
  and reads no further page once it says stop. A read stopped there keeps its cursor
  (``cap:time_budget``); the conversations not started stay due (the cursor stays before
  them) and discovery reports ``cap:time_budget`` with them counted ``unvisited``.
* Membership, read before any message: a standard channel's audience is its team's
  members (``/teams/{id}/members``), a private or shared channel's its own
  (``/channels/{id}/members``), a chat's its members. No membership, no capture.
  The team members endpoint is read every run and used whenever it answers. When it
  answers 403 (``TeamMember.Read.All`` missing), a standard channel's audience falls
  back to its team's Microsoft 365 group (a team id is its group id): the captured
  ACL is ``{"status": "derived", "basis": "group_fallback", "identities":
  ["group:<team-id>"]}`` (``acl_source`` ``group_fallback``), which grants a reader
  holding ``group:<team-id>``, the identity the employee permissions give a member
  of that group (``operating.employee_permissions``: transitive membership, or
  derived from the reader's joined Teams when that read is refused). A private or
  shared channel's membership differs from its team's, so it has no fallback: it
  stays blocked, naming the permission it needs (``ChannelMember.Read.All``).
* Channel messages: ``messages/delta`` (root messages), and the ``replies`` of every
  root message the round lists, up to ``max_replies_per_message`` (a root cut there
  stays ``cap:reply_cap`` and is listed again by every later run until it fits). The
  first round reads the channel whole and keeps the ``@odata.deltaLink``; later
  rounds read only the changes since it. An expired delta token or continuation
  (HTTP 410, also the resumed cursor of a capped full round) restarts the round in
  full once.
* Chat messages (Graph has no chat-message delta): a full listing first; once a
  listing finished, later rounds read ``lastModifiedDateTime gt <high-water minus
  five minutes>`` newest first.
* Per run and conversation at most ``max_pages_per_run`` pages and
  ``max_items_per_run`` messages: a read stopped there keeps its cursor (the next
  page) and the next run resumes it (``cap:page_cap`` / ``cap:item_cap``).

Records (``integrations.capture_store.CaptureStore``, the store BC uses, so the
projector can read it the way ``ontology.project_bc.BcCaptures`` reads BC): kinds
``teams_channel`` (``teams-channel://<team-id>/<channel-id>/<message-id>``, audience
unit ``teams:channel:<team-id>/<channel-id>``) and ``teams_chat``
(``teams-chat://<chat-id>/<message-id>``, unit ``teams:chat:<chat-id>``), handles
``...@<generation>-<capture-id>``. ``row`` holds the message as captured, in the
mail capture's shape: ``source``, ``kind``, ids, ``reply_to_id``, times, ``from``,
``subject``, ``body_content`` (as sent: HTML or text) and ``body_text``,
``attachments`` (references only: id, content type, name, ``content_url``; no bytes
are read), ``mentions`` (who or what was mentioned) and ``original_url``; ``meta``
holds ``source_acl`` (the membership: member object ids and addresses) and
``source_api``. Lineage ``mail`` (person-to-person messages).

Permission trimming: a reader sees a captured message only when the live audience
grants ``teams:channel:<team-id>:<channel-id>`` / ``teams:chat:<chat-id>`` *and* one
of the reader's declared identities is in the message's captured membership
(``LiveSourceAudience.item_visible``; for ``teams:`` resources the membership check
is unconditional) *and* in the conversation's latest captured membership (the
kinds' ``unit_acl``: it gates every version, superseded and missing ones included,
so a removed member loses the conversation's history and every handle minted before
the removal). In a chat, that latest membership admits a member to a message only when
his current visible history (Graph ``visibleHistoryStartDateTime``, recorded per identity
in the membership as ``history_start``, kept to the microsecond and rounded up) starts no
later than the message was created (``CHAT_KIND.unit_acl_for``, decided on every read of
every version: superseded, missing and ones saved before starts were recorded alike). A
member without a recorded start (not reported, unreadable, or a membership captured
before starts were) or a message of unknown creation time is admitted only by the whole
history (fails closed): a member added or re-added without history reads none of the
earlier messages, through any handle. A chat message's own membership is trimmed the same
way when it is saved. Channel members see the channel's whole history. When the
membership changes, every current message is also saved again under the new membership,
so an added member sees the current messages his history covers. A channel or chat whose
membership read fails (an HTTP error or a refusal; a capped read keeps what it read, a
standard channel's refused team read falls back to its group) loses its recorded membership
(``CaptureStore.clear_unit_acl``): it is neither projected nor resolved until a read
succeeds (decision ``2026-10-10-followup-decisions`` item 6). A transient failure (429 once
the bounded ``Retry-After`` retries are spent, 5xx, 408, a timeout or connection failure, a
request budget or deadline: ``_transient_members_failure``) keeps the membership recorded
before and its ``recorded_at`` instead:
the store serves a unit's membership only while it is younger than
``capture_store.UNIT_ACL_MAX_AGE`` (the capture-age bound, 48 hours), so a kept one stops
deciding at that bound. Either way it is ``members_unread``
beside the failure's reason and its run stays open (paused: the next read resumes its
cursor, or reads a full round when it had none), so neither the
pass, the store's completeness nor the projection's (``TeamsCaptures.capture_gap``) is
complete while it is excluded.

Completeness per conversation: ``complete`` only when the latest round reached its
end (a full round's end, or a delta/incremental round's end after a full one) with
nothing capped, dropped or withheld; otherwise ``incomplete`` with its reasons
(``cap:*``, ``members_unread``, ``provenance_dropped``, ``not_visible``,
``source_error:<Class>:<detail>``)
or ``blocked`` (``blocked:missing_permission:<permission>``: Graph answered 403, the
application lacks that Graph permission, also named by the ``blocked_upstream`` part;
``blocked:unauthorized`` on 401). A chat read reports its own parts (``teams.chats``,
``teams.chat_members``, ``teams.chat_messages``), never one a channel read shares. Captures are not an
atomic snapshot and never prove a message absent. Unverified live: whether Graph
marks a root message changed in the channel delta when only a reply is added (a
reply to an unchanged root is otherwise captured by the next full round).

Graph application permissions the reads need (``GRAPH_PERMISSIONS``):
ChannelMessage.Read.All and Chat.Read.All (Microsoft "protected APIs": app-only
access needs Microsoft's approval besides admin consent), Channel.ReadBasic.All,
TeamMember.Read.All, ChannelMember.Read.All, ChatMember.Read.All,
Team.ReadBasic.All, Chat.ReadBasic.All, User.Read.All and, for the census's team counts,
Reports.Read.All.

Storage: ``<state-dir>/teams-capture/capture.db`` and ``census.json`` (private modes); the state
directory must lie outside every git work tree (checked before anything is created).
Without ``--state-dir`` the CLI keeps its store in the host's state directory
(``default_state_dir``: ``$PLX_WORKBENCH_STATE_DIR/teams-capture-check``, else
``$XDG_STATE_HOME/plx-workbench/teams-capture-check``, the pattern of ``eval.heldout``;
``storage.state_base`` says which), so the rotation and every resumable read carry
across its daily runs until one full pass completes (followup decision 17). Only a
persistent ``PLX_WORKBENCH_STATE_DIR`` survives between the verifier's dailies
(``storage.carried``).

CLI (live check ``graph.teams_capture``; counts, booleans and enums only, never
ids, names, addresses or text)::

    python3 -m integrations.graph_read teams-capture \\
        --scope examples/teams_capture_scope.live-check.json \\
        --live-audience examples/teams_capture_audience.live-check.json \\
        --runs 2 --max-runs 12 --budget-seconds 6300

It captures up to ``--runs`` times, then on the same state until ``--max-runs`` ran or
``--budget-seconds`` is spent (no run starts after it), and stops as soon as a run is
complete (one full pass read): an invocation never starts a new pass after one
completes, so its runs all belong to one pass (``pass.stop`` says what ended them).
``pass`` reports the runs, each run's ``unvisited``,
whether the last run was complete and whether ``unvisited`` fell, counted over the
conversations the first run found (``unvisited_first_run``: one created mid-pass is read
in the pass but never makes it rise): never rose, and ended below where it started or
at 0.

The all-channels check ``graph.teams_all_channels`` runs the same CLI with
``--scope examples/teams_capture_scope.all_channels.live-check.json`` (``teams``
``all_census_cleared``). ``coverage`` compares the channels the census enumerated with
the channels of the teams the scope's members joined (``census.member_joined``, read only
for that scope), counts the channels and chats captured, and reports the request log.
Chats need ``Chat.Read.All``: without it they stay their own blocked part
(``teams.chat_*``), never hidden, while channels are captured.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from html.parser import HTMLParser
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from ontology.types import Actor
from operating.case_mail_scope import MailScope

from . import live_source_grammar as grammar
from .capture_mail import (assert_outside_git, blocked_reason, check_state_dir,
                           directory_members, graph_block)
from .capture_store import (CaptureError, CaptureStore, CapturedRecord, SourceKind,
                            register_source_kind)
from .live_source_guard import (LiveSourceAudience, ReadOnlyGuardError,
                                RequestBudgetExhausted, SourceUnavailable,
                                is_transport_failure, unavailable_detail)
from .people_exclusion import ExclusionUnavailable, PeopleExcluded
from .people_exclusion import from_api as people_guard

SCOPE_SCHEMA = "plx-teams-capture-scope/v1"
STORE_DIR = "teams-capture"
DB_FILE = "capture.db"
LINEAGE = "mail"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "teams_capture_scope.json"
CAPTURE_PRINCIPAL = "svc:teams-capture"
GRAPH_PERMISSIONS = ("ChannelMessage.Read.All", "Chat.Read.All", "Channel.ReadBasic.All",
                     "TeamMember.Read.All", "ChannelMember.Read.All", "ChatMember.Read.All",
                     "Team.ReadBasic.All", "Chat.ReadBasic.All", "User.Read.All",
                     "Reports.Read.All")
# The Graph application permission each read needs: named when Graph refuses it (403).
_PERMISSION = {"graph.user": "User.Read.All", "graph.users": "User.Read.All",
               "graph.user_joined_teams": "Team.ReadBasic.All",
               "graph.user_chats": "Chat.ReadBasic.All",
               "graph.team_channels": "Channel.ReadBasic.All",
               "graph.team_channel": "Channel.ReadBasic.All",
               "graph.team_members": "TeamMember.Read.All",
               "graph.channel_members": "ChannelMember.Read.All",
               "graph.chat_members": "ChatMember.Read.All",
               "graph.channel_messages_delta": "ChannelMessage.Read.All",
               "graph.channel_message_replies": "ChannelMessage.Read.All",
               "graph.chat_messages": "Chat.Read.All",
               "graph.teams": "Team.ReadBasic.All", "graph.team": "Team.ReadBasic.All",
               "graph.chat": "Chat.ReadBasic.All",
               "graph.teams_activity_report": "Reports.Read.All"}
GROUP_FALLBACK = "group_fallback"
# A chat an excluded person belongs to: withheld every run, so settled like a complete read.
PEOPLE_EXCLUDED = "people_excluded"
# A chat whose messages no app-only token of this tenant can read (``not_readable:<why>``):
# settled for the pass like an excluded one, never a blocked part or a failure, and its
# messages are never claimed captured (the store's run stays open with the reason).
NOT_READABLE = "not_readable"
# Graph answered 403 on the messages of a chat hosted in another tenant (a cross-tenant
# chat a member of this tenant joined): live 2026-10-27, every one of the chats refused
# was hosted elsewhere and every chat hosted here was read. Chat.Read.All is this
# tenant's grant; the host tenant's data is out of its reach, so no consent changes it.
OTHER_TENANT = "not_readable:other_tenant"
SETTLED = frozenset({"complete", PEOPLE_EXCLUDED, NOT_READABLE})
MEMBERS_UNREAD = "members_unread"
# HTTP statuses of a members read that say nothing about the membership (throttled or a
# server fault): the membership recorded before is kept (``_transient_members_failure``).
TRANSIENT_STATUSES = frozenset({408, 429}) | frozenset(range(500, 600))
MAX_BODY_CHARS = 200_000
MAX_TEXT = 1000
MAX_URL = 2048
MAX_CAPPED_ROOTS = 500
CHAT_OVERLAP = timedelta(minutes=5)
# Graph's visibleHistoryStartDateTime of a member who sees the whole history.
WHOLE_HISTORY = datetime(1, 1, 1, tzinfo=timezone.utc)
# Capture kind -> the ontology relation its records project as (source family teams,
# ontology/project_graph.TeamsCaptures; generated types in ontology/schema/generated/
# graph_teams.json).
RELATIONS = {"teams_channel": "channelMessages", "teams_chat": "chatMessages"}
# The conversation's own record: saved under this reserved message-id segment of its unit
# (``teams-chat://<chat>/conversation@...``, ``teams-channel://<team>/<channel>/
# conversation@...``), so the unit's membership gates it exactly as it gates the messages.
# Graph message ids are numeric; a listed message carrying this id is dropped.
CONVERSATION = "conversation"
CONVERSATION_RELATIONS = {"teams_channel": "channels", "teams_chat": "chats"}
TEAMS_RELATION = "teams"
MAX_PARTICIPANTS = 1000
REASON_MAX = 64
ROTATION = "teams_rotation"
# The chat the last census stopped after when ``max_chats`` or the time budget cut it:
# the next census starts past it (round to the start), so every chat is reached.
CENSUS_CHAT_CURSOR = "teams_census_chats"
ROTATION_VERSION = 2  # the layout _advance writes; any other starts the pass over
TIME_BUDGET = "cap:time_budget"
CHECK_STATE = ("plx-workbench", "teams-capture-check")
MAX_BUDGET_SECONDS = 86_400
MAX_RUNS = 12  # one CLI invocation: --runs, then on until a full pass is read
_ENUM = re.compile(r"[A-Za-z]{1,32}")
_RESYNC_CODES = frozenset({"resync_required", "sync_state_not_found"})
# The census (``TeamsCapture.census``): names and 30-day counts, never content.
CENSUS_FILE = "census.json"
CENSUS_SCHEMA = "plx-teams-census/v1"
CENSUS_AREAS = ("purchasing", "qa", "rnd")
COUNT_DAYS = 30
CHANNEL_COUNT_REASON = "count_requires_content_read"
MESSAGE_COUNTS = ("teams_activity_report", "none")
REFUSALS = ("not_visible", PEOPLE_EXCLUDED, MEMBERS_UNREAD, "member_cap")
_CENSUS_LIMITS = {"max_teams": (500, 1, 5000), "max_chats": (200, 0, 5000)}
# Every Graph read a census may make (with the people guard's directory listing): team,
# channel and chat listings, memberships, one record per cleared team, channel and chat,
# and the tenant's activity report. No message, getAllMessages or subscription.
# An all-channels scope also lists the joined teams of its members (ids only), the
# member-joined baseline its channel count is compared with.
CENSUS_OPERATIONS = frozenset({
    "graph.users", "graph.user", "graph.teams", "graph.team", "graph.team_members",
    "graph.team_channels", "graph.team_channel", "graph.channel_members",
    "graph.user_chats", "graph.chat_members", "graph.chat", "graph.teams_activity_report",
    "graph.user_joined_teams"})
# The confirmation of the channels and chats whose content may be read (a human
# approval: written by a person beside the census, never by code, never defaulted).
CONFIRMATION_FILE = "content-confirmation.json"
CONFIRMATION_SCHEMA = "plx-teams-content-confirmation/v1"
CONFIRMATION_PART = "teams.content_confirmation"
CONFIRMATION_LATER_ASK = "teams-content-confirmation"
MAX_CONFIRMATION_BYTES = 262_144
# The confirmation's forms: the channels and chats it lists, or every conversation the
# run's census cleared (2026-10-21); a scope's
# ``teams`` may name the same set.
LISTED = "listed"
ALL_CENSUS_CLEARED = "all_census_cleared"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DIRECTIVE = re.compile(r"[0-9a-f]{10,32}")
# The signer of an all_census_cleared confirmation is a person named by name: never a
# principal, an address, or the name of code (an agent, a service, a script).
# A full name: at least two words of letters, never a role.
_PERSON = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*\.?(?: [^\W\d_]+(?:['-][^\W\d_]+)*\.?)+")
_CODE_SIGNERS = frozenset({"admin", "agent", "assistant", "automation", "bot", "claude",
                           "codex", "controller", "gpt", "owner", "script", "service", "svc",
                           "system", "workbench"})
# Object ids and addresses inside a request URL (a one-on-one chat's id embeds both
# members' object ids).
_GUID_IN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ADDRESS_IN = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


class TeamsCaptureError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _valid(type_name: str, value: Any) -> bool:
    return grammar.TYPES[type_name].valid(value)


# ------------------------------------------------------------------ source kinds

def _channel_path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 3 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("teams_channel_path_invalid")
    team, channel, message = parts[0].lower(), parts[1], parts[2]
    if not (_valid("guid", team) and _valid("teams_thread", channel)
            and _valid("teams_message_id", message)):
        raise ValueError("teams_channel_path_invalid")
    return team, channel, message


def _chat_path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 2 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("teams_chat_path_invalid")
    if not (_valid("teams_thread", parts[0]) and _valid("teams_message_id", parts[1])):
        raise ValueError("teams_chat_path_invalid")
    return parts[0], parts[1]


CHANNEL_KIND = register_source_kind(SourceKind(
    name="teams_channel", scheme="teams-channel", path_parts=3, canonical_path=_channel_path,
    unit=lambda path: f"teams:channel:{path[0]}/{path[1]}",
    resources=lambda path: (f"teams:channel:{path[0]}:{path[1]}",),
    key=lambda path: {"team_id": path[0], "channel_id": path[1], "id": path[2]},
    unit_acl=True))
CHAT_KIND = register_source_kind(SourceKind(
    name="teams_chat", scheme="teams-chat", path_parts=2, canonical_path=_chat_path,
    unit=lambda path: f"teams:chat:{path[0]}", resources=lambda path: (f"teams:chat:{path[0]}",),
    key=lambda path: {"chat_id": path[0], "id": path[1]}, unit_acl=True,
    unit_acl_for=lambda acl, payload: _history_acl(acl, payload.get("row"))))


@dataclass(frozen=True)
class Conversation:
    """One channel (``team`` set) or chat; ``membership`` is the channel's
    membershipType or the chat's chatType (an enum, ``unknown`` if absent)."""

    kind: str
    thread: str
    team: str | None = None
    membership: str = "unknown"

    @property
    def source_kind(self) -> SourceKind:
        return CHANNEL_KIND if self.kind == "channel" else CHAT_KIND

    def path(self, message_id: str) -> tuple[str, ...]:
        parts = [self.team, self.thread, message_id] if self.kind == "channel" \
            else [self.thread, message_id]
        return self.source_kind.canonical_path(parts)

    @property
    def unit(self) -> str:
        return self.source_kind.unit(self.path("0"))

    @property
    def resource(self) -> str:
        return self.source_kind.resources(self.path("0"))[0]

    def url(self, op: str, query: Mapping[str, str] | None = None, **params: str) -> str:
        where = ({"team": self.team, "channel": self.thread} if self.kind == "channel"
                 else {"chat": self.thread})
        return grammar.url(op, query, **where, **params)


def _distinct(conversations: Iterable[Conversation]) -> list[Conversation]:
    unique: dict[str, Conversation] = {}
    for conversation in conversations:
        unique.setdefault(conversation.unit, conversation)
    return list(unique.values())


def _in_turn(first: Sequence[Conversation], second: Sequence[Conversation]
             ) -> list[Conversation]:
    return [c for pair in zip_longest(first, second) for c in pair if c is not None]


def _after(group: Iterable[Conversation], unit: str | None) -> list[Conversation]:
    """``group`` in unit order from the first unit after ``unit``, round to the start."""
    ordered = sorted(group, key=lambda c: c.unit)
    start = next((i for i, c in enumerate(ordered) if unit is not None and c.unit > unit), 0)
    return ordered[start:] + ordered[:start]


def _rotation(store: CaptureStore) -> dict[str, Any]:
    """The stored rotation: each kind's last unit, the kind due (``next``) and the units
    read in the current pass. An unreadable one, or one of another layout than
    ``ROTATION_VERSION`` (its ``visited`` may mean something else), starts over with
    nothing read."""
    try:
        state = json.loads(store.state(ROTATION) or "{}")
    except ValueError:
        state = None
    if not isinstance(state, dict) or state.get("version") != ROTATION_VERSION:
        state = {}
    found = {k: state[k] for k in ("channel", "chat", "next") if isinstance(state.get(k), str)}
    visited = state.get("visited") if isinstance(state.get("visited"), list) else []
    return {**found, "visited": [unit for unit in visited if isinstance(unit, str)]}


def _status(reasons: Iterable[str]) -> str:
    reasons = list(reasons)
    return ("blocked" if any(r.startswith("blocked:") for r in reasons)
            else "incomplete" if reasons else "complete")


def _enum(value: Any) -> str:
    return value if isinstance(value, str) and _ENUM.fullmatch(value) else "unknown"


# ------------------------------------------------------------------ scope

MEMBER_CHATS = ("none", "mail_allowlist")
_LIMITS = {"page_size": (50, 1, 50), "max_pages_per_run": (20, 1, 500),
           "max_items_per_run": (1000, 1, 20_000), "max_replies_per_message": (500, 1, 5000),
           "max_conversations": (100, 1, 1000), "max_members": (5000, 1, 5000)}


class TeamsScope:
    """Which teams, channels and chats are captured, and the per-run caps.

    ``plx-teams-capture-scope/v1``: ``members`` (``{address, joined_teams}``: the teams
    an allowlisted member joined, default on), ``member_chats`` (``none``, the default,
    or ``mail_allowlist``: the chats of every in-scope account of ``allowlist``),
    ``teams`` (``{team_id, channels: "all" | [channel ids]}``, or ``"all_census_cleared"``:
    every channel of every team the CLI's census enumerates and clears, which only the
    human confirmation lets it read, its members' joined teams then being the baseline
    it is compared with; ``TeamsCapture.run`` has no census and reads no team from it),
    ``chats`` (chat ids) and the caps of ``_LIMITS``. ``allowlist`` (default: the built-in ``MailScope``): a
    member outside it is set aside and never read; an unrestricted allowlist names no
    account, so it cannot bound ``mail_allowlist`` and is refused. ``members`` holds
    ``(address, joined_teams, chats)``. Configuration, never a grant: the capture actor
    still needs the live audience."""

    def __init__(self, document: Mapping[str, Any], *, allowlist: MailScope | None = None
                 ) -> None:
        allowed = {"schema", "note", "members", "member_chats", "teams", "chats", "census",
                   *_LIMITS}
        if (not isinstance(document, Mapping) or document.get("schema") != SCOPE_SCHEMA
                or set(document) - allowed):
            raise TeamsCaptureError("scope_document_invalid")
        self.allowlist = MailScope.builtin() if allowlist is None else allowlist
        for name, (default, low, high) in _LIMITS.items():
            value = document.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise TeamsCaptureError(f"scope_{name}_invalid")
            setattr(self, name, value)
        member_chats = document.get("member_chats", "none")
        if member_chats not in MEMBER_CHATS:
            raise TeamsCaptureError("scope_member_chats_invalid")
        if member_chats == "mail_allowlist" and self.allowlist.is_unrestricted:
            raise TeamsCaptureError("scope_member_chats_unbounded")
        # With an all_directory allowlist the chat accounts are every directory user the
        # people guard leaves in, listed at run time (``TeamsCapture._members``).
        self.directory_chats = member_chats == "mail_allowlist" and self.allowlist.all_directory
        chat_accounts = set(self.allowlist.addresses) if member_chats == "mail_allowlist" \
            else set()
        if not all(_valid("user_ref", address) for address in chat_accounts):
            raise TeamsCaptureError("scope_member_invalid")
        listed: dict[str, tuple[str, bool]] = {}
        outside = 0
        for entry in self._list(document, "members"):
            if not isinstance(entry, Mapping) or set(entry) - {"address", "joined_teams",
                                                               "note"} \
                    or not _valid("user_ref", entry.get("address")) \
                    or not isinstance(entry.get("joined_teams", True), bool):
                raise TeamsCaptureError("scope_member_invalid")
            if self.allowlist.allows([entry["address"]]):
                listed.setdefault(entry["address"].lower(),
                                  (entry["address"], entry.get("joined_teams", True)))
            else:
                outside += 1
        members = [(address, joined, key in chat_accounts)
                   for key, (address, joined) in listed.items()]
        members += [(address, False, True) for address in sorted(chat_accounts - set(listed))]
        teams = []
        self.all_census_cleared = document.get("teams") == ALL_CENSUS_CLEARED
        for entry in [] if self.all_census_cleared else self._list(document, "teams"):
            channels = entry.get("channels", "all") if isinstance(entry, Mapping) else None
            if not isinstance(entry, Mapping) or set(entry) - {"team_id", "channels", "note"} \
                    or not _valid("guid", entry.get("team_id")) \
                    or not (channels == "all" or (isinstance(channels, list) and channels and all(
                        _valid("teams_thread", c) for c in channels))):
                raise TeamsCaptureError("scope_team_invalid")
            teams.append((entry["team_id"].lower(),
                          "all" if channels == "all" else tuple(channels)))
        chats = self._list(document, "chats")
        if not all(_valid("teams_thread", c) for c in chats):
            raise TeamsCaptureError("scope_chat_invalid")
        self.members, self.teams, self.chats = tuple(members), tuple(teams), tuple(chats)
        self.outside_allowlist = outside
        self.census = _census_config(document.get("census", {}))
        if not (members or teams or chats or outside or self.all_census_cleared):
            raise TeamsCaptureError("scope_empty")

    @staticmethod
    def _list(document: Mapping[str, Any], name: str) -> list:
        # Up to the census's largest cap: a confirmed scope lists what a census cleared.
        value = document.get(name, [])
        if not isinstance(value, list) or len(value) > 5000:
            raise TeamsCaptureError(f"scope_{name}_invalid")
        return value

    def limits(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _LIMITS}

    @classmethod
    def from_file(cls, path: str | Path, *, allowlist: MailScope | None = None
                  ) -> "TeamsScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise TeamsCaptureError("scope_unreadable") from None
        return cls(document, allowlist=allowlist)


def _census_config(value: Any) -> dict[str, Any]:
    """The scope's ``census``: ``max_teams`` and ``max_chats`` (looked at per census),
    ``message_counts`` (``teams_activity_report``, the default, or ``none``) and
    ``proposals`` (per area of ``CENSUS_AREAS``, the keywords that propose a channel or
    chat to the owner by its name: data, never a decision)."""
    if not isinstance(value, Mapping) or set(value) - {"note", "message_counts", "proposals",
                                                       *_CENSUS_LIMITS}:
        raise TeamsCaptureError("scope_census_invalid")
    config: dict[str, Any] = {}
    for name, (default, low, high) in _CENSUS_LIMITS.items():
        found = value.get(name, default)
        if isinstance(found, bool) or not isinstance(found, int) or not low <= found <= high:
            raise TeamsCaptureError("scope_census_invalid")
        config[name] = found
    config["message_counts"] = value.get("message_counts", MESSAGE_COUNTS[0])
    proposals = value.get("proposals", {})
    if config["message_counts"] not in MESSAGE_COUNTS or not isinstance(proposals, Mapping) \
            or set(proposals) - set(CENSUS_AREAS):
        raise TeamsCaptureError("scope_census_invalid")
    config["proposals"] = {}
    for area in CENSUS_AREAS:
        words = proposals.get(area, [])
        if not isinstance(words, list) or len(words) > 50 or not all(
                isinstance(w, str) and 1 <= len(w.strip()) <= 60 for w in words):
            raise TeamsCaptureError("scope_census_invalid")
        config["proposals"][area] = tuple(re.compile(r"(?<![a-z0-9])" + re.escape(w))
                                          for w in dict.fromkeys(w.strip().lower()
                                                                 for w in words))
    return config


# ------------------------------------------------------------------ records

class _Text(HTMLParser):
    _BLOCK = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(content: str) -> str:
    """Plain text of a Teams HTML body (tags dropped, entities decoded)."""
    parser = _Text()
    parser.feed(content)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _party(value: Any, kinds: Sequence[str] = ("user", "application", "device")
           ) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    for kind in kinds:
        entry = value.get(kind)
        if isinstance(entry, Mapping):
            return {"kind": kind, "id": _text(entry.get("id"), 300),
                    "display_name": _text(entry.get("displayName"))}
    return None


def _attachment(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    url = raw.get("contentUrl")
    return {"id": _text(raw.get("id"), 300), "content_type": _text(raw.get("contentType"), 200),
            "name": _text(raw.get("name")),
            "content_url": url if isinstance(url, str) and url.startswith("https://")
            and len(url) <= MAX_URL else None,
            "captured": "reference_only"}


def _mention(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    target = _party(raw.get("mentioned"), ("user", "application", "conversation", "tag"))
    mention_id = raw.get("id")
    return {"id": mention_id if isinstance(mention_id, int) and not isinstance(mention_id, bool)
            else None, "text": _text(raw.get("mentionText")),
            "kind": target["kind"] if target else None,
            "target_id": target["id"] if target else None,
            "display_name": target["display_name"] if target else None}


def message_row(conversation: Conversation, raw: Mapping[str, Any]) -> dict[str, Any]:
    """The captured row of one Graph chatMessage (JSON values only)."""
    body = raw.get("body") if isinstance(raw.get("body"), Mapping) else {}
    content = body.get("content") if isinstance(body.get("content"), str) else ""
    content_type = "html" if str(body.get("contentType") or "").lower() == "html" else "text"
    text = html_text(content) if content_type == "html" else content
    where = ({"team_id": conversation.team, "channel_id": conversation.thread}
             if conversation.kind == "channel" else {"chat_id": conversation.thread})
    return {
        "source": "graph-teams", "kind": f"{conversation.kind}_message", **where,
        "id": raw["id"], "reply_to_id": _text(raw.get("replyToId"), 40),
        "message_type": _text(raw.get("messageType"), 60),
        "created_at": _text(raw.get("createdDateTime"), 40),
        "last_modified": _text(raw.get("lastModifiedDateTime"), 40),
        "last_edited": _text(raw.get("lastEditedDateTime"), 40),
        "deleted_at": _text(raw.get("deletedDateTime"), 40),
        "subject": _text(raw.get("subject")), "importance": _text(raw.get("importance"), 40),
        "from": _party(raw.get("from")), "body_content_type": content_type,
        "body_content": content[:MAX_BODY_CHARS], "body_text": text[:MAX_BODY_CHARS],
        "body_truncated": len(content) > MAX_BODY_CHARS or len(text) > MAX_BODY_CHARS,
        "attachments": [a for a in map(_attachment, raw.get("attachments") or []) if a],
        "mentions": [m for m in map(_mention, raw.get("mentions") or []) if m],
        "original_url": _text(raw.get("webUrl"), MAX_URL)}


def _participant(member: Mapping[str, Any]) -> dict[str, Any] | None:
    """One member as a participant: the Entra object id (the shared key a Person is
    resolved by), the display name and the work address; None without a valid id."""
    user = member.get("userId")
    if not isinstance(user, str) or not _valid("guid", user.lower()):
        return None
    email = member.get("email")
    return {"id": user.lower(), "display_name": _text(member.get("displayName"), 200),
            "email": email.strip().lower() if isinstance(email, str) and "@" in email
            and len(email) <= 320 else None}


def conversation_row(conversation: Conversation, details: Mapping[str, Any] | None,
                     participants: Sequence[Mapping[str, Any]] | None, *,
                     basis: str, last_activity_at: str | None,
                     team: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The captured row of one conversation (JSON values only): its Graph chat or channel
    (``details``, None when that read failed: ``details`` ``unread``), its participants as
    read with the membership (None: the membership names nobody, a group fallback), the
    latest captured message time and, for a channel, its team (``team``)."""
    got = details if isinstance(details, Mapping) else {}
    row: dict[str, Any] = {
        "source": "graph-teams", "kind": conversation.kind,
        "details": "read" if isinstance(details, Mapping) else "unread",
        "created_at": _text(got.get("createdDateTime"), 40),
        "last_activity_at": last_activity_at,
        "participants": sorted((dict(p) for p in participants or ()),
                               key=lambda p: p["id"])[:MAX_PARTICIPANTS],
        "participants_basis": basis,
        "participants_truncated": len(participants or ()) > MAX_PARTICIPANTS,
        "original_url": _text(got.get("webUrl"), MAX_URL)}
    if conversation.kind == "chat":
        row.update({"chat_id": conversation.thread, "topic": _text(got.get("topic")),
                    "chat_type": _enum(got.get("chatType") or conversation.membership),
                    "last_updated": _text(got.get("lastUpdatedDateTime"), 40)})
        return row
    tgot = team if isinstance(team, Mapping) else {}
    row.update({"team_id": conversation.team, "channel_id": conversation.thread,
                "display_name": _text(got.get("displayName")),
                "description": _text(got.get("description")),
                "membership_type": _enum(got.get("membershipType") or conversation.membership),
                "team": {"details": "read" if isinstance(team, Mapping) else "unread",
                         "display_name": _text(tgot.get("displayName")),
                         "description": _text(tgot.get("description")),
                         "created_at": _text(tgot.get("createdDateTime"), 40)}})
    return row


def _history_acl(acl: Mapping[str, Any], row: Any) -> dict[str, Any]:
    """A chat's membership ``acl`` as it applies to one captured message (``row``): the
    members whose recorded visible history (``history_start``) starts no later than the
    message was created. A member without a recorded start (an ACL captured before starts
    were recorded has none), or a message of unknown creation time, is admitted only by
    the whole history (fails closed)."""
    starts = acl.get("history_start")
    starts = starts if isinstance(starts, Mapping) else {}
    created = _instant(_covered_at(row)) if isinstance(row, Mapping) else None

    def sees(identity: Any) -> bool:
        value = starts.get(identity) if isinstance(identity, str) else None
        start = _history_start(value) if isinstance(value, str) else None
        return start is not None and (start == WHOLE_HISTORY
                                      or (created is not None and start <= created))

    identities = acl.get("identities")
    shown = {k: v for k, v in acl.items() if k != "history_start"}
    shown["identities"] = [i for i in identities if sees(i)] \
        if isinstance(identities, list) else []
    return shown


def _covered_at(row: Mapping[str, Any]) -> Any:
    """The time a member's visible history must cover to see a captured row: a message's
    creation; the conversation record's latest message (else its own creation), so a
    member whose history starts after the last message sees no conversation record."""
    if row.get("kind") in ("chat", "channel"):
        return row.get("last_activity_at") or row.get("created_at")
    return row.get("created_at")


@lru_cache(maxsize=1024)
def _history_start(value: str) -> datetime | None:
    """A recorded history start as an instant, rounded up (``_instant``); cached, since every
    read of every chat version checks each member's and a chat's members share few."""
    return _instant(value, ceil=True)


def _message_acl(conversation: Conversation, acl: Mapping[str, Any],
                 row: Mapping[str, Any]) -> dict[str, Any]:
    """The source ACL a captured message (``row``) is saved with: a chat's members whose
    visible history covers it (``_history_acl``), a channel's membership as read."""
    return _history_acl(acl, row) if conversation.kind == "chat" else dict(acl)


def _error_reason(exc: BaseException) -> str:
    """``source_error:<Class>[:<detail>]`` within ``REASON_MAX`` (fixed vocabulary)."""
    reason = f"source_error:{type(exc).__name__}"
    detail = unavailable_detail(exc)
    if detail:
        if len(f"{reason}:{detail}") > REASON_MAX:
            reason = "source_error:SourceUnavailable"
        reason += ":" + detail
    return reason


def _transient_members_failure(exc: BaseException) -> bool:
    """Whether a failed members read is transient: throttled (429, once the client's
    bounded ``Retry-After`` retries are spent), a server error (5xx), a request timeout
    (408), a transport failure before any status (timeout, connection reset or refused,
    TLS) or a request budget or deadline that refused it before any I/O. Such a failure
    says nothing about who is a member, so the membership recorded before is kept. Every
    other failure is definitive or unknown and clears it (fail closed): 401, 403, 404 (not
    a member any more, the conversation gone), a malformed answer, anything unclassified."""
    if isinstance(exc, RequestBudgetExhausted) or is_transport_failure(exc):
        return True
    status = getattr(exc, "http_status", None)
    return type(status) is int and status in TRANSIENT_STATUSES


def _failure(part: str, op: str, exc: BaseException, blocked: list[dict[str, Any]]) -> str:
    """The reason of a failed read: ``blocked:<code>[:<permission>]`` where the one Graph
    refusal rule (``capture_mail.graph_block``) blocks, its part (a 403 names the
    permission ``op`` needs) added to ``blocked``; else ``source_error:...``."""
    found = graph_block(part, op, _PERMISSION, getattr(exc, "http_status", None))
    if found is None:
        return _error_reason(exc)
    blocked.append(found)
    return blocked_reason(found)


def _new_counts() -> dict[str, int]:
    return {"pages": 0, "messages_read": 0, "replies_read": 0, "versions_new": 0,
            "unchanged": 0, "provenance_dropped": 0, "replies_capped": 0,
            "acl_refreshed": 0, "members": 0, "round_restarts": 0, "people_dropped": 0}


def _instant(value: Any, *, ceil: bool = False) -> datetime | None:
    """The UTC instant of a Graph ``...Z`` time, its fraction kept to the microsecond
    (digits beyond dropped, or with ``ceil`` rounded up: a history start is never read as
    earlier than Graph states it); None when ``value`` is no such time."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    head, dot, fraction = value[:-1].partition(".")
    if dot and not (fraction.isascii() and fraction.isdigit()):
        return None
    try:
        moment = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        moment += timedelta(microseconds=int(fraction[:6].ljust(6, "0")))
        if ceil and fraction[6:].strip("0"):
            moment += timedelta(microseconds=1)
    except (ValueError, OverflowError):
        return None
    return moment


# ------------------------------------------------------------------ capture

def open_store(state_dir: str | Path, audience: Any) -> CaptureStore:
    """The Teams capture store under ``state_dir`` (outside every git work tree)."""
    root = assert_outside_git(state_dir) / STORE_DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = CaptureStore(root / DB_FILE, audience)
    os.chmod(root / DB_FILE, 0o600)
    return store


# What the host and the CLI capture with (``TeamsCapture`` workers): conversations, and the
# census's per-team, per-account and per-chat reads, eight at a time.
WORKERS = 8
MAX_WORKERS = 16


class TeamsCapture:
    """One capture pass over a ``TeamsScope`` into a ``CaptureStore``, as ``actor``
    (checked against the live audience per conversation before any of its I/O).

    ``workers``: how many conversations a run captures at once, and how many teams,
    accounts and chats the census reads at once. Each conversation's reads keep their
    order (members before messages, a page before the next); a conversation thread writes
    through its own connection to the same store (``CaptureStore`` serialises writes),
    and results are reported in the run's conversation order. Graph throttling (429
    ``Retry-After``) holds every thread sharing the client (``BoundedBackoff.hold``)."""

    def __init__(self, store: CaptureStore, api: Any, audience: LiveSourceAudience,
                 actor: Actor, *, workers: int = 1, home_tenant: str | None = None) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) \
                or not 1 <= workers <= MAX_WORKERS:
            raise ValueError(f"workers must be 1..{MAX_WORKERS}")
        self.workers = workers
        # The tenant the application's token belongs to (a GUID, lower case): a chat
        # hosted in any other one is ``not_readable`` once Graph refuses its messages.
        # None (unknown): every refusal stays a blocked part (fail closed).
        self.home_tenant = (home_tenant.lower() if isinstance(home_tenant, str)
                            and _valid("guid", home_tenant.lower()) else None)
        self.store, self.api, self.audience, self.actor = store, api, audience, actor
        # The people guard build_graph attached (none: every read fails closed).
        self.people = people_guard(api)
        # The teams, channels and chats (ids, lower case) the guard refused this process:
        # nothing more of them is requested (``names_excluded`` counts any request that is).
        self.refused: set[str] = set()
        # Teams read this capture (``_team``), shared by the conversation threads.
        self._teams: dict[str, Mapping[str, Any] | None] = {}
        self._team_lock = threading.Lock()

    def close(self) -> None:
        self.store.close()

    def _ordered(self, read: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        """``read`` of every item, in item order: on ``workers`` threads (GETs and the
        guard only: no store write), else one at a time."""
        if self.workers == 1 or len(items) < 2:
            return [read(item) for item in items]
        with ThreadPoolExecutor(min(self.workers, len(items)),
                                thread_name_prefix="teams-census") as pool:
            return list(pool.map(read, items))

    def _id_excluded(self, thread: str) -> bool:
        """Whether a conversation id embeds an excluded person's object id (a one-on-one
        chat's does, both members'): refused before any request naming it."""
        return self.people.excluded(_GUID_IN.findall(thread.lower()))

    def names_excluded(self, url: str) -> bool:
        """Whether a request URL names an excluded person (an object id or address in its
        path or query) or a team, channel or chat the guard refused before it. An address
        the guard cannot decide yet counts (fail closed)."""
        parts = urlsplit(url)
        path, query = unquote(parts.path).lower(), unquote(parts.query).lower()
        if self.refused.intersection(path.split("/")):
            return True
        text = f"{path} {query}"
        try:
            return self.people.excluded([*_GUID_IN.findall(text), *_ADDRESS_IN.findall(text)])
        except ExclusionUnavailable:
            return True

    # -- discovery ------------------------------------------------------------

    def _listing(self, op: str, query: Mapping[str, str], reasons: set[str],
                 **params: str) -> list[Mapping[str, Any]]:
        items, completeness = self.api.get_pages(grammar.url(op, query, **params),
                                                 max_pages=50, max_items=5000)
        if completeness["truncated"]:
            reasons.add(f"cap:{completeness['truncation_reason']}")
        return items

    def _members(self, scope: TeamsScope, reasons: set[str]
                 ) -> list[tuple[str, bool, bool]]:
        """``scope.members`` plus, for an ``all_directory`` allowlist, the chats of every
        directory member user the people guard leaves in.
        The exclusion filter runs on the listing before any member is looked up; a guard
        that is unusable or a listing that fails adds none (fail closed)."""
        members = list(scope.members)
        if not scope.directory_chats:
            return members
        scope.allowlist.attach_people(self.people)
        try:
            self.people.require()
            self.people.bind(self.api)
            found, status, _dropped = directory_members(
                self.api, lambda ids: not self.people.excluded(ids))
        except (ExclusionUnavailable, SourceUnavailable):
            reasons.add("blocked:directory_listing")
            return members
        if status != "complete":
            reasons.add(f"cap:directory_{status}")
        have = {address.lower() for address, _j, _c in members}
        return members + [(mail, False, True) for _i, mail in found if mail not in have]

    def _member(self, address: str, scope: TeamsScope, reasons: set[str]) -> str | None:
        # An excluded person is refused before the lookup naming them, and again by the
        # object ID and names the lookup returns (``PeopleExcluded``).
        self.people.refuse([address], "chat_member")
        user = self.api.get_json(grammar.url(
            "graph.user", {"$select": "id,userPrincipalName,mail"}, user=address))
        user_id = str(user.get("id") or "").lower() if isinstance(user, Mapping) else ""
        names = [user.get(k) for k in ("userPrincipalName", "mail")] if user_id else []
        self.people.refuse([user_id, *[n for n in names if isinstance(n, str)]], "chat_member")
        if not _valid("user_id", user_id) or not scope.allowlist.allows(
                [user_id, *[n for n in names if isinstance(n, str)]]):
            reasons.add("outside_allowlist")
            return None
        return user_id

    def discover(self, scope: TeamsScope, blocked: list[dict[str, Any]]
                 ) -> tuple[list[Conversation], list[Conversation], dict[str, Any]]:
        """(the conversations due this run, every conversation found, the discovery
        report); ``unvisited`` is settled by ``_advance`` once the reads are done."""
        reasons: set[str] = set()
        info: dict[str, Any] = {"members": len(scope.members),
                                "members_outside_allowlist": scope.outside_allowlist,
                                "people_excluded": 0}
        if not self.audience.may_read_any(self.actor, "teams:"):
            return [], [], {**info, "status": "not_visible", "reasons": ["not_visible"],
                            "conversations": 0, "discovered_total": 0, "unvisited": 0}

        def attempt(part: str, op: str, read: Callable[[], Any]) -> Any:
            try:
                return read()
            except SourceUnavailable as exc:
                reasons.add(_failure(part, op, exc, blocked))
                return None

        teams = list(scope.teams)
        chats = [Conversation("chat", chat) for chat in scope.chats]
        for address, joined, with_chats in self._members(scope, reasons):
            try:
                user = attempt("teams.member", "graph.user",
                               lambda: self._member(address, scope, reasons))
            except PeopleExcluded:
                # Neither their teams nor their chats are listed (counted, never named).
                info["people_excluded"] += 1
                continue
            if user is None:
                continue
            if joined:
                for item in attempt("teams.joined_teams", "graph.user_joined_teams",
                                    lambda: self._listing("graph.user_joined_teams",
                                                          {"$select": "id"}, reasons,
                                                          user=user)) or []:
                    team = str(item.get("id") or "").lower()
                    if _valid("guid", team):
                        teams.append((team, "all"))
                    else:
                        reasons.add("provenance_dropped")
            if with_chats:
                for item in attempt("teams.chats", "graph.user_chats", lambda: self._listing(
                        "graph.user_chats", {"$select": "id,chatType"}, reasons,
                        user=user)) or []:
                    if _valid("teams_thread", item.get("id")):
                        chats.append(Conversation("chat", item["id"],
                                                  membership=_enum(item.get("chatType"))))
                    else:
                        reasons.add("provenance_dropped")
        channels: list[Conversation] = []
        for team, wanted in dict.fromkeys(teams):
            select = {"$select": "id,membershipType"}
            if wanted == "all":
                found = attempt("teams.channels", "graph.team_channels", lambda: self._listing(
                    "graph.team_channels", select, reasons, team=team)) or []
            else:
                found = [item for item in (attempt(
                    "teams.channel", "graph.team_channel", lambda c=c: self.api.get_json(
                        grammar.url("graph.team_channel", select, team=team, channel=c)))
                    for c in wanted) if isinstance(item, Mapping)]
            for item in found:
                if _valid("teams_thread", item.get("id")):
                    channels.append(Conversation("channel", item["id"], team,
                                                 _enum(item.get("membershipType"))))
                else:
                    reasons.add("provenance_dropped")
        channels, chats = _distinct(channels), _distinct(chats)
        found = _in_turn(channels, chats)
        conversations = (self._rotate(channels, chats, scope.max_conversations)
                         if len(found) > scope.max_conversations else found)
        return conversations, found, {**info, "status": _status(reasons),
                                      "reasons": sorted(reasons),
                                      "conversations": len(conversations),
                                      "discovered_total": len(found), "unvisited": 0}

    def _rotate(self, channels: list[Conversation], chats: list[Conversation], cap: int
                ) -> list[Conversation]:
        """The ``cap`` conversations due."""
        rotation = _rotation(self.store)
        ordered = {"channel": _after(channels, rotation.get("channel")),
                   "chat": _after(chats, rotation.get("chat"))}
        first, second = (("chat", "channel") if rotation.get("next") == "chat"
                         else ("channel", "chat"))
        return _in_turn(ordered[first], ordered[second])[:cap]

    def _advance(self, found: list[Conversation], taken: list[Conversation],
                 results: Sequence[Mapping[str, Any]], rotating: bool) -> set[str]:
        """Store the rotation after ``taken`` were read (the conversations due but not
        taken, and a run that stops first, read them again) and return the units found
        the pass leaves unvisited: each kind past its last one read, the other kind due
        next, the units of the pass whose latest read finished complete (none once every
        one found did). A read that ended otherwise unvisits its unit. A run under the cap
        that read every conversation due clears the rotation."""
        state: dict[str, Any] = {"version": ROTATION_VERSION}
        unvisited: set[str] = set()
        if rotating:
            rotation = _rotation(self.store)
            read = {c.unit for c in taken}
            visited = ({*rotation["visited"]} - read) | {
                c.unit for c, r in zip(taken, results) if r["status"] in SETTLED}
            visited &= {c.unit for c in found}
            unvisited = {c.unit for c in found} - visited
            state.update({**rotation, **{c.kind: c.unit for c in taken},
                          "visited": sorted(visited) if unvisited else []})
            if taken:
                state["next"] = "chat" if taken[-1].kind == "channel" else "channel"
        self.store.set_state(ROTATION, json.dumps(state, sort_keys=True))
        return unvisited

    # -- one conversation -------------------------------------------------------

    @staticmethod
    def _members_op(conversation: Conversation) -> str:
        if conversation.kind == "chat":
            return "graph.chat_members"
        # Only a standard channel is its team's audience; private, shared or an
        # unknown membershipType reads the channel's own members (fail closed).
        return ("graph.team_members" if conversation.membership == "standard"
                else "graph.channel_members")

    def _membership(self, conversation: Conversation, op: str, scope: TeamsScope
                    ) -> tuple[dict[str, Any], int, str | None, list[dict[str, Any]]]:
        url = (grammar.url(op, team=conversation.team) if op == "graph.team_members"
               else conversation.url(op))
        items, completeness = self.api.get_pages(url, max_pages=50,
                                                 max_items=scope.max_members)
        identities: set[str] = set()
        starts: dict[str, str | None] = {}
        participants = {p["id"]: p for p in map(_participant, items) if p}
        for member in items:
            user, email = member.get("userId"), member.get("email")
            found = []
            if isinstance(user, str) and _valid("guid", user.lower()):
                found.append(user.lower())
            if isinstance(email, str) and "@" in email and len(email) <= 320:
                found.append(email.strip().lower())
            identities.update(found)
            start = member.get("visibleHistoryStartDateTime")
            start = start if _instant(start, ceil=True) is not None else None
            for identity in found:
                # One identity listed twice keeps the latest start (an unknown one wins).
                known = starts.get(identity, start)
                starts[identity] = (None if known is None or start is None
                                    else max(known, start,
                                             key=lambda t: _instant(t, ceil=True)))
        acl = {"status": "read", "basis": op.split(".", 1)[1],
               "identities": sorted(identities)}
        if conversation.kind == "chat":
            acl["history_start"] = dict(sorted(starts.items()))
        capped = (f"cap:member_{completeness['truncation_reason']}"
                  if completeness["truncated"] else None)
        return acl, len(items), capped, list(participants.values())

    def _record(self, conversation: Conversation, raw: Any, acl: Mapping[str, Any], op: str,
                *, reply_to: str | None = None) -> CapturedRecord | None:
        """A captured version, or None when the item does not belong here (dropped)."""
        if not isinstance(raw, Mapping) or not _valid("teams_message_id", raw.get("id")) \
                or raw.get("id") == CONVERSATION:
            return None
        if conversation.kind == "channel":
            where = raw.get("channelIdentity")
            if isinstance(where, Mapping) and (
                    str(where.get("teamId") or "").lower() != conversation.team
                    or where.get("channelId") != conversation.thread):
                return None
        elif raw.get("chatId") is not None and raw.get("chatId") != conversation.thread:
            return None
        if reply_to is not None and raw.get("replyToId") != reply_to:
            return None
        version = raw.get("etag") or raw.get("lastModifiedDateTime") or raw.get("createdDateTime")
        if not isinstance(version, str) or not version:
            return None
        row = message_row(conversation, raw)
        return CapturedRecord(conversation.path(raw["id"]),
                              {"row": row, "meta": {
                                  "source_acl": _message_acl(conversation, acl, row),
                                  "source_api": op}},
                              version[:200], LINEAGE)

    def _details(self, conversation: Conversation) -> Mapping[str, Any] | None:
        """The conversation's own Graph chat or channel (name or topic, type, times), or
        None when that read fails (the record then says ``details`` ``unread``)."""
        if conversation.kind == "chat":
            op, select = ("graph.chat",
                          "id,topic,chatType,createdDateTime,lastUpdatedDateTime,webUrl,tenantId")
        else:
            op, select = ("graph.team_channel",
                          "id,displayName,description,membershipType,createdDateTime,webUrl")
        try:
            found = self.api.get_json(conversation.url(op, {"$select": select}))
        except SourceUnavailable:
            return None
        return found if isinstance(found, Mapping) and found.get("id") == conversation.thread \
            else None

    def _team(self, team: str | None) -> Mapping[str, Any] | None:
        """A channel's team (name, description, creation), read once per capture; None
        when that read fails."""
        if team is None:
            return None
        with self._team_lock:
            if team in self._teams:
                return self._teams[team]
        try:
            found = self.api.get_json(grammar.url(
                "graph.team", {"$select": "id,displayName,description,createdDateTime"},
                team=team))
        except SourceUnavailable:
            found = None
        found = found if isinstance(found, Mapping) \
            and str(found.get("id") or "").lower() == team else None
        with self._team_lock:
            self._teams[team] = found
        return found

    def _save_conversation(self, conversation: Conversation, run_id: int, cursor: str | None,
                           about: Mapping[str, Any]) -> None:
        """Save the conversation's own record (``CONVERSATION``) in this run, with the
        latest activity of the messages the store holds now, under the membership its
        messages are saved with."""
        latest = None
        for r in self.store.current_records(conversation.source_kind.name, conversation.unit):
            if r.path[-1] == CONVERSATION:
                continue
            row = r.payload.get("row") or {}
            for value in (row.get("created_at"), row.get("last_modified")):
                moment = _instant(value)
                if moment is not None and (latest is None or moment > latest[0]):
                    latest = (moment, value)
        row = conversation_row(conversation, about["details"], about["participants"],
                               basis=about["basis"],
                               last_activity_at=latest[1] if latest else None,
                               team=about["team"])
        version = sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()[:40]
        self.store.save_page(run_id, [CapturedRecord(
            conversation.path(CONVERSATION),
            {"row": row, "meta": {"source_acl": _message_acl(conversation, about["acl"], row),
                                  "source_api": "graph.chat" if conversation.kind == "chat"
                                  else "graph.team_channel"}},
            version, LINEAGE)], cursor)

    def _sent_by_excluded(self, raw: Any) -> bool:
        """Whether a listed message was sent by an excluded person (``from.user.id``)."""
        sender = raw.get("from") if isinstance(raw, Mapping) else None
        user = sender.get("user") if isinstance(sender, Mapping) else None
        return isinstance(user, Mapping) and self.people.excluded([user.get("id")])

    def _replies(self, conversation: Conversation, root: str, acl: Mapping[str, Any],
                 scope: TeamsScope, counts: dict[str, int]
                 ) -> tuple[list[CapturedRecord], int, bool]:
        op = "graph.channel_message_replies"
        items, completeness = self.api.get_pages(
            conversation.url(op, {"$top": "50"}, message=root), max_pages=50,
            max_items=min(5000, scope.max_replies_per_message))
        kept = [raw for raw in items if not self._sent_by_excluded(raw)]
        counts["people_dropped"] += len(items) - len(kept)
        records = [self._record(conversation, raw, acl, op, reply_to=root) for raw in kept]
        counts["replies_read"] += len(items)
        return ([r for r in records if r is not None], records.count(None),
                bool(completeness["truncated"]))

    def _start(self, conversation: Conversation, scope: TeamsScope,
               previous: Mapping[str, Any] | None) -> tuple[str, str]:
        """(first URL, round) of a new run from the last finished one's detail."""
        detail = json.loads(previous["detail"]) if previous and previous.get("detail") else {}
        top = {"$top": str(scope.page_size)}
        if conversation.kind == "channel":
            delta = detail.get("delta_url")
            if isinstance(delta, str) and delta:
                return delta, "incremental"
            return conversation.url("graph.channel_messages_delta", top), "full"
        since = _instant(detail.get("high_water"))
        if since is None:
            # Newest first explicitly: a full listing paused across runs must not leave
            # older messages behind the high-water taken at its end.
            return conversation.url("graph.chat_messages", {
                **top, "$orderby": "lastModifiedDateTime desc"}), "full"
        moment = (since - CHAT_OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
        return conversation.url("graph.chat_messages", {
            **top, "$orderby": "lastModifiedDateTime desc",
            "$filter": f"lastModifiedDateTime gt {moment}"}), "incremental"

    def _refresh_acl(self, conversation: Conversation, run_id: int, cursor: str | None,
                     acl: Mapping[str, Any]) -> int:
        """Save every current message held under another membership again under this
        one (so a removed member stops seeing it); the count saved."""
        kind = conversation.source_kind
        stale = []
        for r in self.store.current_records(kind.name, conversation.unit):
            if r.path[-1] == CONVERSATION:
                continue   # saved again under this membership as the read ends
            wanted = _message_acl(conversation, acl, r.payload.get("row") or {})
            if (r.payload.get("meta") or {}).get("source_acl") != wanted:
                stale.append((r, wanted))
        for start in range(0, len(stale), 500):
            batch = [CapturedRecord(r.path, {**r.payload, "meta": {
                **(r.payload.get("meta") or {}), "source_acl": wanted}},
                r.source_version, r.lineage) for r, wanted in stale[start:start + 500]]
            self.store.save_page(run_id, batch, cursor, marks=["acl_refreshed"])
        return len(stale)

    def capture(self, conversation: Conversation, scope: TeamsScope,
                blocked: list[dict[str, Any]], out_of_time: Callable[[], bool] = lambda: False
                ) -> dict[str, Any]:
        counts = _new_counts()
        result: dict[str, Any] = {"kind": conversation.kind,
                                  "membership": conversation.membership,
                                  "unit": conversation.unit, "status": "incomplete",
                                  "reasons": [], "round": None, "resumed_from": None,
                                  "acl_source": None, "counts": counts}
        if not self.audience.allows(self.actor, conversation.resource):
            return {**result, "reasons": ["not_visible"]}  # no I/O for a withheld conversation
        members_op = self._members_op(conversation)
        if (self.refused & {conversation.thread.lower(), str(conversation.team).lower()}
                or self._id_excluded(conversation.thread)):
            # Refused already (its team this run, or its id names an excluded person):
            # not requested again, its members included.
            self.store.clear_unit_acl(conversation.source_kind.name, conversation.unit)
            return {**result, "status": PEOPLE_EXCLUDED}
        # A refused chat read names its own part, never one a channel read shares.
        part = "teams.chat_" if conversation.kind == "chat" else "teams."
        try:
            acl, counts["members"], member_cap, participants = self._membership(
                conversation, members_op, scope)
        except SourceUnavailable as exc:
            if members_op != "graph.team_members" or getattr(exc, "http_status", None) != 403:
                # The run stays open (members_unread) until a members read succeeds. A
                # transient failure (429, 5xx, timeout, budget) keeps the membership read
                # before, with its recorded_at: the store serves it only while it is younger
                # than ``capture_store.UNIT_ACL_MAX_AGE``. Any other failure (403, 404, ...)
                # drops it: no membership, no conversation.
                kind = conversation.source_kind
                if not _transient_members_failure(exc):
                    self.store.clear_unit_acl(kind.name, conversation.unit)
                run = self.store.begin_run(kind.name, conversation.unit,
                                           [conversation.resource])
                return self._paused(result, run["run_id"], f"{part}members", members_op, exc,
                                    blocked, {MEMBERS_UNREAD})
            # A team id is its Microsoft 365 group's id: the standard channel's audience
            # is that group's members, decided per reader by its group identity.
            acl, member_cap = {"status": "derived", "basis": GROUP_FALLBACK,
                               "identities": [f"group:{conversation.team}"]}, None
            participants = None   # the group names nobody here
        if self.people.excluded(acl["identities"]):
            # A chat or channel an excluded person belongs to (The
            # census refuses it too): no message is read, and what an earlier capture
            # holds of it loses its membership (neither projected nor resolved). Settled,
            # not a gap: it is withheld by policy, every run. A group_fallback audience
            # names no person, so only the census (which refuses an unread team) bounds it.
            self.store.clear_unit_acl(conversation.source_kind.name, conversation.unit)
            # Refused for the rest of the run (its own id: a standard channel's team still
            # holds private channels cleared on their own members).
            self.refused.add(conversation.thread.lower())
            return {**result, "status": PEOPLE_EXCLUDED, "acl_source": None}
        if conversation.kind == "chat" and member_cap:
            # A cut member list may hide an excluded person, so the guard cannot clear
            # the chat: no message is read and the earlier membership is dropped (a gap).
            self.store.clear_unit_acl(conversation.source_kind.name, conversation.unit)
            return {**result, "reasons": [member_cap]}
        result["acl_source"] = acl["basis"]
        # Its name and times, requested only now the members cleared the people guard.
        about = {"details": self._details(conversation), "participants": participants,
                 "basis": acl["basis"], "acl": acl,
                 "team": self._team(conversation.team) if conversation.kind == "channel"
                 else None}
        kind = conversation.source_kind
        previous = self.store.run(kind.name, conversation.unit)
        run = self.store.begin_run(kind.name, conversation.unit, [conversation.resource])
        run_id, reasons = run["run_id"], set([member_cap] if member_cap else [])
        if run["resumed"] and run["cursor"]:
            url = run["cursor"]
            marks = self.store.run_marks(run_id)
            round_ = "full" if "round:full" in marks else "incremental"
            result["resumed_from"] = "cursor"
        else:
            url, round_ = self._start(conversation, scope,
                                      None if run["resumed"] else previous)
            result["resumed_from"] = (None if round_ == "full" else "delta_url"
                                      if conversation.kind == "channel" else "high_water")
            self.store.save_page(run_id, [], url, marks=[f"round:{round_}"])
        result["round"] = round_
        self.store.set_unit_acl(run_id, acl)
        counts["acl_refreshed"] = self._refresh_acl(conversation, run_id, url, acl)
        if previous is not None and not run["resumed"] and previous.get("detail"):
            # Roots whose replies a cap cut: listed again until they fit.
            capped = json.loads(previous["detail"]).get("replies_capped") or []
            try:
                self._relist(conversation, capped, acl, scope, run_id, url, counts)
            except SourceUnavailable as exc:
                return self._paused(result, run_id, "teams.replies",
                                    "graph.channel_message_replies", exc, blocked, reasons)
        op = ("graph.channel_messages_delta" if conversation.kind == "channel"
              else "graph.chat_messages")
        pages = items = 0
        restarted = False
        while True:
            if pages >= scope.max_pages_per_run or items >= scope.max_items_per_run \
                    or out_of_time():
                cap = ("cap:page_cap" if pages >= scope.max_pages_per_run
                       else "cap:item_cap" if items >= scope.max_items_per_run
                       else TIME_BUDGET)
                self._save_conversation(conversation, run_id, url, about)
                self.store.pause_run(run_id, reason=cap)
                reasons.add(cap)
                return self._result(result, reasons)
            try:
                page = self.api.get_json(url)
                if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
                    raise SourceUnavailable("graph listing lacks value list")
                records, dropped, marks = [], 0, []
                for raw in page["value"]:
                    if self._sent_by_excluded(raw):
                        counts["people_dropped"] += 1  # never stored; its replies unread
                        continue
                    record = self._record(conversation, raw, acl, op)
                    if record is None:
                        dropped += 1
                        continue
                    records.append(record)
                    counts["messages_read"] += 1
                    if conversation.kind == "channel" and not raw.get("replyToId") \
                            and not raw.get("deletedDateTime"):
                        replies, lost, cut = self._replies(conversation, raw["id"], acl, scope,
                                                           counts)
                        records += replies
                        dropped += lost
                        if cut:
                            marks.append(f"capped:{raw['id']}")
                next_url = self._next(url, page.get("@odata.nextLink"))
                end = (self._next(url, page.get("@odata.deltaLink"), delta=True)
                       if next_url is None and conversation.kind == "channel" else None)
            except SourceUnavailable as exc:
                expired = (getattr(exc, "http_status", None) == 410
                           or getattr(exc, "error_code", None) in _RESYNC_CODES)
                if expired and not restarted:
                    # An expired delta or continuation token (a capped full round's
                    # resumed cursor too): the round starts over in full, once per run.
                    restarted = True
                    counts["round_restarts"] += 1
                    self.store.restart_run(run_id, "cursor_expired")
                    url, round_ = self._start(conversation, scope, None)
                    result["round"] = round_
                    self.store.save_page(run_id, [], url, marks=[f"round:{round_}"])
                    continue
                if self._hosted_elsewhere(conversation, about["details"], exc):
                    # Not this tenant's to read: settled, no blocked part, nothing claimed.
                    self.store.pause_run(run_id, reason=OTHER_TENANT)
                    return {**self._result(result, reasons | {OTHER_TENANT}),
                            "status": NOT_READABLE}
                if self._empty_channel(conversation, exc):
                    # Graph's delta of a channel with no message answers an empty page
                    # and a nextLink it then refuses (HTTP 400): the round ends here,
                    # complete and empty, and the next run reads a full round again.
                    self.store.save_page(run_id, [], None)
                    self._save_conversation(conversation, run_id, None, about)
                    return self._finish(result, conversation, run_id, round_, None, reasons,
                                        empty=True)
                return self._paused(result, run_id, f"{part}messages", op, exc, blocked,
                                    reasons)
            saved = self.store.save_page(run_id, records, next_url, dropped=dropped,
                                         marks=marks)
            counts["pages"] += 1
            counts["versions_new"] += saved["new_versions"]
            counts["unchanged"] += saved["unchanged"]
            counts["provenance_dropped"] += dropped
            pages += 1
            items += len(records)
            if next_url is None:
                self._save_conversation(conversation, run_id, None, about)
                return self._finish(result, conversation, run_id, round_, end, reasons)
            url = next_url

    def _hosted_elsewhere(self, conversation: Conversation, details: Any,
                          exc: SourceUnavailable) -> bool:
        """Whether a refused chat read is a chat hosted in another tenant: Graph answered
        403 and the chat's own record (read this run) names a tenant other than the
        token's. An unknown home tenant or an unread chat record never is (fail closed)."""
        if conversation.kind != "chat" or getattr(exc, "http_status", None) != 403 \
                or self.home_tenant is None or not isinstance(details, Mapping):
            return False
        host = str(details.get("tenantId") or "").lower()
        return _valid("guid", host) and host != self.home_tenant

    def _empty_channel(self, conversation: Conversation, exc: SourceUnavailable) -> bool:
        """Whether a channel delta page Graph refused with HTTP 400 belongs to a channel
        that holds no message at all: one page of its plain message listing (``$top=1``)
        is empty and has no next page. Any other answer, or a failure, is not (the read
        stays a source error)."""
        if conversation.kind != "channel" or getattr(exc, "http_status", None) != 400:
            return False
        try:
            page = self.api.get_json(conversation.url("graph.channel_messages", {"$top": "1"}))
        except SourceUnavailable:
            return False
        return (isinstance(page, Mapping) and page.get("value") == []
                and page.get("@odata.nextLink") is None)

    def _relist(self, conversation: Conversation, roots: Iterable[str], acl: Mapping[str, Any],
                scope: TeamsScope, run_id: int, cursor: str, counts: dict[str, int]) -> None:
        for root in roots:
            if conversation.kind != "channel" or not _valid("teams_message_id", root):
                continue
            records, dropped, cut = self._replies(conversation, root, acl, scope, counts)
            saved = self.store.save_page(run_id, records, cursor, dropped=dropped,
                                         marks=[f"capped:{root}"] if cut else [])
            counts["versions_new"] += saved["new_versions"]
            counts["unchanged"] += saved["unchanged"]
            counts["provenance_dropped"] += dropped

    def _next(self, url: str, link: Any, *, delta: bool = False) -> str | None:
        """The checked continuation (same collection, the request's own path) of a
        nextLink, or of a deltaLink (which may equal the request: no change)."""
        if link is None:
            return None
        found = self.api.continuation_url(url, link)
        if found is None or (found == url and not delta):
            raise SourceUnavailable("graph nextLink escaped the collection")
        return found

    def _finish(self, result: dict[str, Any], conversation: Conversation, run_id: int,
                round_: str, end: str | None, reasons: set[str], *, empty: bool = False
                ) -> dict[str, Any]:
        marks = self.store.run_marks(run_id)
        capped = sorted(m.split(":", 1)[1] for m in marks if m.startswith("capped:"))
        detail: dict[str, Any] = {"round": round_, "replies_capped": capped[:MAX_CAPPED_ROOTS]}
        if conversation.kind == "channel":
            detail["delta_url"] = end
            if end is None and not empty:
                reasons.add("delta_link_missing")
        else:
            seen = [_instant(r.payload["row"].get("last_modified")) for r in
                    self.store.current_records(CHAT_KIND.name, conversation.unit)
                    if r.path[-1] != CONVERSATION]
            newest = max((m for m in seen if m is not None), default=None)
            detail["high_water"] = newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None
        result["counts"]["replies_capped"] = len(capped)
        if capped:
            reasons.add("cap:reply_cap")
        state, reason = "complete", None
        if capped:
            state, reason = "partial", "reply_cap"
        elif round_ == "incremental":
            state, reason = "partial", "incremental"
        elif "acl_refreshed" in marks:
            state, reason = "partial", "acl_refreshed"
        if (self.store.run(conversation.source_kind.name, conversation.unit) or {}).get("dropped"):
            reasons.add("provenance_dropped")  # items of another conversation: never complete
        self.store.finish_run(run_id, state, reason=reason, detail=detail)
        return self._result(result, reasons)

    @staticmethod
    def _result(result: dict[str, Any], reasons: set[str]) -> dict[str, Any]:
        result["reasons"] = sorted(reasons)
        result["status"] = _status(reasons)
        return result

    def _failed(self, result: dict[str, Any], part: str, op: str, exc: SourceUnavailable,
                blocked: list[dict[str, Any]]) -> dict[str, Any]:
        return self._result(result, {_failure(part, op, exc, blocked)})

    def _paused(self, result: dict[str, Any], run_id: int, part: str, op: str,
                exc: SourceUnavailable, blocked: list[dict[str, Any]], reasons: set[str]
                ) -> dict[str, Any]:
        """A read that failed keeps its run resumable (cursor kept) with the reason."""
        failed = self._failed(result, part, op, exc, blocked)
        self.store.pause_run(run_id, reason=failed["reasons"][0])
        return self._result(result, reasons | set(failed["reasons"]))

    # -- pass -----------------------------------------------------------------

    def run(self, scope: TeamsScope, out_of_time: Callable[[], bool] = lambda: False
            ) -> dict[str, Any]:
        """Discover, then capture every conversation due once, none started once
        ``out_of_time()`` (the caller's time budget); ``blocked_upstream`` lists the parts
        Graph refused (missing permission). ``found_units`` and ``unvisited_units`` are
        for the caller's pass accounting, never printed."""
        blocked: list[dict[str, Any]] = []
        refused = self._people_refused(scope, blocked)
        if refused is not None:
            return refused
        conversations, found, discovery = self.discover(scope, blocked)
        results = self._capture_all(conversations, scope, blocked, out_of_time)
        # Cut by time: a conversation not taken, or one the deadline paused (concurrent
        # conversations may all have started before it, the deadline pausing them).
        capped = len(found) > scope.max_conversations
        cut = len(results) < len(conversations) or any(
            TIME_BUDGET in r["reasons"] for r in results)
        unvisited = self._advance(found, conversations[:len(results)], results, capped or cut)
        reasons = {*discovery["reasons"], *(["cap:conversation_cap"] if capped and unvisited
                                            else []), *([TIME_BUDGET] if cut else [])}
        discovery = {**discovery, "status": _status(reasons), "reasons": sorted(reasons),
                     "unvisited": len(unvisited)}
        complete = discovery["status"] == "complete" and all(
            r["status"] in SETTLED for r in results)
        unique = list({json.dumps(p, sort_keys=True): p for p in blocked}.values())
        return {"complete": complete, "discovery": discovery, "conversations": results,
                "blocked_upstream": unique,
                "complete_bound": sum(r["status"] in SETTLED for r in results),
                "bound_total": len(results), "found_units": {c.unit for c in found},
                "unvisited_units": unvisited,
                "people_excluded": discovery["people_excluded"] + sum(
                    r["status"] == PEOPLE_EXCLUDED for r in results),
                "people_dropped": sum(r["counts"]["people_dropped"] for r in results)}

    def _capture_all(self, conversations: Sequence[Conversation], scope: TeamsScope,
                     blocked: list[dict[str, Any]], out_of_time: Callable[[], bool]
                     ) -> list[dict[str, Any]]:
        """Capture each conversation due, none started once ``out_of_time()``; the
        results of the conversations taken, a prefix of ``conversations`` in their order
        (one started after a later one was refused for time is read again next run).
        With ``workers`` > 1 the conversations run on that many threads, each one
        writing through its own connection to the store."""
        if self.workers == 1 or len(conversations) < 2:
            results = []
            for conversation in conversations:
                if out_of_time():
                    break
                results.append(self.capture(conversation, scope, blocked, out_of_time))
            return results
        def one(conversation: Conversation) -> dict[str, Any] | None:
            if out_of_time():
                return None
            lane = copy.copy(self)   # shares the client, the guard and the refused set
            lane.workers = 1
            lane.store = CaptureStore(self.store.path, self.audience)   # this thread's own
            try:
                return lane.capture(conversation, scope, blocked, out_of_time)
            finally:
                lane.store.close()
        with ThreadPoolExecutor(min(self.workers, len(conversations)),
                                thread_name_prefix="teams-capture") as pool:
            done = list(pool.map(one, conversations))
        results = []
        for result in done:
            if result is None:
                break
            results.append(result)
        return results

    def _people_refused(self, scope: TeamsScope, blocked: list[dict[str, Any]]
                        ) -> dict[str, Any] | None:
        """The run the people guard refuses, or None once it is usable: without its ID
        list, or the directory listing that finds the excluded people's addresses, no
        member, team, channel or chat is looked up, listed or read (fail closed). A
        listing Graph refused reports that refusal; otherwise the guard's own part."""
        if not self.audience.may_read_any(self.actor, "teams:"):
            return None  # discovery withholds every conversation without I/O
        try:
            self.people.require()
            self.people.bind(self.api)
            return None
        except ExclusionUnavailable as exc:
            cause = exc.__cause__
            reason = (_failure("teams.people_exclusion", "graph.users", cause, blocked)
                      if isinstance(cause, SourceUnavailable) else None)
            if reason is None:
                part = self.people.block_part("teams.people_exclusion")
                blocked.append(part)
                reason = f"blocked:people_exclusion:{part['error_code']}"
        discovery = {"members": len(scope.members),
                     "members_outside_allowlist": scope.outside_allowlist,
                     "people_excluded": 0, "status": _status([reason]), "reasons": [reason],
                     "conversations": 0, "discovered_total": 0, "unvisited": 0}
        return {"complete": False, "discovery": discovery, "conversations": [],
                "blocked_upstream": blocked, "complete_bound": 0, "bound_total": 0,
                "found_units": set(), "unvisited_units": set(), "people_excluded": 0,
                "people_dropped": 0}

    # -- census (names and counts, no content) ----------------------------------

    def census(self, scope: TeamsScope, blocked: list[dict[str, Any]],
               out_of_time: Callable[[], bool] = lambda: False
               ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """(the census counts, its private listing or None): every team of the tenant
        (``graph.teams``, ids only), each team's members read and cleared by the people
        guard before its name or channels are requested, a private or shared channel's
        own members before its name, the chats of the scope's chat accounts (ids and
        types) and each chat's members before its topic, then the tenant's 30-day team
        activity report (one GET, counts only). A team, channel or chat whose members
        hold an excluded person, or could not be read whole, is refused: nothing more of
        it is requested, and it is counted, never listed. Channel counts need a content
        read (``CHANNEL_COUNT_REASON``), so a channel's ``messages_30d`` stays None. The
        private listing (names, ids, proposals; never a refused item) goes to the census
        file only, never to the report. Chats past ``max_chats`` or the time budget are
        reached in turn: a cut census stores the last chat it reached
        (``CENSUS_CHAT_CURSOR``) and the next one starts after it, round to the start."""
        config = scope.census
        reasons: set[str] = set()
        refused = {kind: dict.fromkeys(REFUSALS, 0) for kind in ("teams", "channels", "chats")}
        n = dict.fromkeys(("teams", "teams_unlisted", "channels", "chats", "chats_unlisted",
                           "accounts", "accounts_excluded"), 0)
        chat_types: dict[str, int] = {}
        teams: list[dict[str, Any]] = []
        chats: list[dict[str, Any]] = []
        enumerated: dict[str, int] = {}  # team -> channels the census enumerated in it
        baseline: dict[str, Any] = {"value": None}
        counts = {"source": config["message_counts"], "period_days": COUNT_DAYS,
                  "status": "not_requested", "reasons": [], "teams_counted": 0,
                  "teams_uncounted": 0, "channels_counted": 0,
                  "channel_count_reason": CHANNEL_COUNT_REASON, "chats_counted": 0}

        def report(private: dict[str, Any] | None) -> tuple[dict[str, Any],
                                                            dict[str, Any] | None]:
            channels = [c for t in teams for c in t["channels"]]
            proposals = (private or {}).get("proposals", {})
            return {"status": _status(reasons), "reasons": sorted(reasons), **n,
                    "teams_cleared": len(teams),
                    "teams_named": sum(t["name"] is not None for t in teams),
                    "teams_refused": refused["teams"],
                    "channels_cleared": len(channels),
                    "channels_named": sum(c["name"] is not None for c in channels),
                    "channels_refused": refused["channels"],
                    "chats_by_type": dict(sorted(chat_types.items())),
                    "chats_cleared": len(chats),
                    "chats_named": sum(c["topic"] is not None for c in chats),
                    "chats_refused": refused["chats"], "message_counts": counts,
                    "member_joined": baseline["value"],
                    "proposals": {area: len(proposals.get(area, []))
                                  for area in CENSUS_AREAS}}, private

        if not self.audience.may_read_any(self.actor, "teams:"):
            reasons.add("not_visible")  # no I/O at all
            return report(None)
        refusal = self._people_refused(scope, blocked)
        if refusal is not None:
            reasons.update(refusal["discovery"]["reasons"])  # nothing listed or read
            return report(None)

        def attempt(part: str, op: str, read: Callable[[], Any]) -> Any:
            try:
                return read()
            except SourceUnavailable as exc:
                reasons.add(_failure(part, op, exc, blocked))
                return None

        def withheld(conversation: Conversation, op: str, part: str) -> str | None:
            """Why the guard cannot clear ``conversation`` (None: cleared)."""
            try:
                acl, _count, capped, _people = self._membership(conversation, op, scope)
            except SourceUnavailable as exc:
                reasons.add(_failure(part, op, exc, blocked))
                return MEMBERS_UNREAD
            if self.people.excluded(acl["identities"]):
                # Refused for the rest of the run: no later request names it.
                self.refused.add((conversation.thread or str(conversation.team)).lower())
                return PEOPLE_EXCLUDED
            if capped:
                reasons.add(capped)  # a cut list may hide an excluded person
                return "member_cap"
            return None

        listed = attempt("teams.teams", "graph.teams", lambda: self._listing(
            "graph.teams", {"$select": "id"}, reasons)) or []
        found = set()
        for item in listed:
            team = str(item.get("id") or "").lower() if isinstance(item, Mapping) else ""
            if _valid("guid", team):
                found.add(team)
            else:
                reasons.add("provenance_dropped")
        ordered = sorted(found)
        n["teams"] = len(ordered)
        if len(ordered) > config["max_teams"]:
            reasons.add("cap:team_cap")
        late = "time"   # a read the budget kept from starting

        def read_team(team: str) -> tuple:
            """One team, its channels and their names (a thread: GETs and the guard only)."""
            if out_of_time():
                return (late,)
            if not self.audience.may_read_any(self.actor, f"teams:channel:{team}:"):
                return ("refused", "not_visible")
            why = withheld(Conversation("channel", "", team, "standard"),
                           "graph.team_members", "teams.members")
            if why is not None:
                return ("refused", why, team)
            record = attempt("teams.team", "graph.team", lambda: self.api.get_json(
                grammar.url("graph.team", {"$select": "id,displayName"}, team=team)))
            entry = {"team_id": team, "name": _named(record, team, "displayName"),
                     "messages_30d": None, "count_reason": "not_requested", "channels": []}
            seen: set[str] = set()
            channel_refusals: list[str] = []
            for item in attempt("teams.channels", "graph.team_channels", lambda: self._listing(
                    "graph.team_channels", {"$select": "id,membershipType"}, reasons,
                    team=team)) or []:
                channel = item.get("id") if isinstance(item, Mapping) else None
                if not _valid("teams_thread", channel):
                    reasons.add("provenance_dropped")
                    continue
                if channel in seen:
                    continue
                seen.add(channel)
                conversation = Conversation("channel", channel, team,
                                            _enum(item.get("membershipType")))
                if not self.audience.allows(self.actor, conversation.resource):
                    channel_refusals.append("not_visible")
                    continue
                # A standard channel's audience is its team's, cleared above; any other
                # (private, shared, unknown) is cleared on its own members.
                why = (None if conversation.membership == "standard" else withheld(
                    conversation, "graph.channel_members", "teams.members"))
                if why is not None:
                    channel_refusals.append(why)
                    continue
                record = attempt("teams.channel", "graph.team_channel",
                                 lambda c=conversation: self.api.get_json(c.url(
                                     "graph.team_channel", {"$select": "id,displayName"})))
                entry["channels"].append({
                    "channel_id": channel, "name": _named(record, channel, "displayName"),
                    "membership": conversation.membership, "messages_30d": None,
                    "count_reason": CHANNEL_COUNT_REASON})
            return ("ok", entry, len(seen), channel_refusals)

        for team, read in zip(ordered, self._ordered(read_team,
                                                     ordered[:config["max_teams"]])):
            if read[0] == late:
                reasons.add(TIME_BUDGET)
                break
            if read[0] == "refused":
                refused["teams"][read[1]] += 1
                if len(read) > 2:
                    enumerated[team] = 0
                continue
            _ok, entry, listed_channels, channel_refusals = read
            enumerated[team] = listed_channels
            n["channels"] += listed_channels
            for why in channel_refusals:
                refused["channels"][why] += 1
            teams.append(entry)
        n["teams_unlisted"] = n["teams"] - len(teams) - sum(refused["teams"].values())
        if scope.all_census_cleared:
            baseline["value"] = self._member_joined(scope, enumerated, attempt, reasons,
                                                    out_of_time)

        accounts = [address for address, _joined, with_chats in self._members(scope, reasons)
                    if with_chats]

        def read_account(address: str) -> tuple:
            """One chat account's chats (ids and types; a thread: GETs and the guard)."""
            if out_of_time():
                return (late,)
            try:
                user = attempt("teams.member", "graph.user",
                               lambda: self._member(address, scope, reasons))
            except PeopleExcluded:
                return ("excluded",)   # none of their chats is listed
            listed = (attempt("teams.chats", "graph.user_chats", lambda: self._listing(
                "graph.user_chats", {"$select": "id,chatType"}, reasons, user=user))
                if user is not None else None) or []
            return ("ok", listed)

        types: dict[str, str] = {}
        for read in self._ordered(read_account, accounts if config["max_chats"] else []):
            if read[0] == late:
                reasons.add(TIME_BUDGET)
                break
            n["accounts"] += 1
            if read[0] == "excluded":
                n["accounts_excluded"] += 1
                continue
            for item in read[1]:
                chat = item.get("id") if isinstance(item, Mapping) else None
                if _valid("teams_thread", chat):
                    types.setdefault(chat, _enum(item.get("chatType")))
                else:
                    reasons.add("provenance_dropped")
        n["chats"] = len(types)
        for kind in types.values():
            chat_types[kind] = chat_types.get(kind, 0) + 1
        if len(types) > config["max_chats"]:
            reasons.add("cap:chat_cap")

        def read_chat(chat: str) -> tuple:
            """One chat's members, then its topic once they cleared (a thread)."""
            if out_of_time():
                return (late,)
            conversation = Conversation("chat", chat, membership=types[chat])
            if not self.audience.allows(self.actor, conversation.resource):
                return ("refused", "not_visible")
            if self._id_excluded(chat):
                # A one-on-one chat's id names both its members: one is an excluded
                # person, so not even its members are requested (counted, never named).
                self.refused.add(chat.lower())
                return ("refused", PEOPLE_EXCLUDED)
            why = withheld(conversation, "graph.chat_members", "teams.chat_members")
            if why is not None:
                return ("refused", why)
            record = attempt("teams.chat", "graph.chat", lambda c=conversation: self.api.get_json(
                c.url("graph.chat", {"$select": "id,topic,chatType"})))
            return ("ok", {"chat_id": chat, "topic": _named(record, chat, "topic"),
                           "chat_type": types[chat]})

        # Past the cap or the time budget the census rotates: it starts after the chat
        # the last cut census stopped at, never always at the same first ids.
        cursor = self.store.state(CENSUS_CHAT_CURSOR) or None
        ordered_chats = sorted(types)
        start = next((i for i, chat in enumerate(ordered_chats)
                      if cursor is not None and chat > cursor), 0)
        window = (ordered_chats[start:] + ordered_chats[:start])[:config["max_chats"]]
        reached = 0
        for read in self._ordered(read_chat, window):
            if read[0] == late:
                reasons.add(TIME_BUDGET)
                break
            reached += 1
            if read[0] == "refused":
                refused["chats"][read[1]] += 1
                continue
            chats.append(read[1])
        if reached:   # a census that reached no chat keeps the cursor it started from
            self.store.set_state(CENSUS_CHAT_CURSOR,
                                 window[reached - 1] if reached < len(types) else "")
        n["chats_unlisted"] = n["chats"] - len(chats) - sum(refused["chats"].values())

        self._count(config, teams, counts, reasons, blocked)
        proposals: dict[str, list[dict[str, Any]]] = {area: [] for area in CENSUS_AREAS}
        for team in teams:
            for channel in team["channels"]:
                channel["proposed"] = _propose(config, team["name"], channel["name"])
                for area in channel["proposed"]:
                    proposals[area].append({"kind": "channel", "team_id": team["team_id"],
                                            "channel_id": channel["channel_id"]})
        for chat in chats:
            chat["proposed"] = _propose(config, chat["topic"])
            for area in chat["proposed"]:
                proposals[area].append({"kind": "chat", "chat_id": chat["chat_id"]})
        return report({"schema": CENSUS_SCHEMA, "period_days": COUNT_DAYS,
                       "status": _status(reasons), "teams": teams, "chats": chats,
                       "proposals": proposals})

    def _member_joined(self, scope: TeamsScope, enumerated: Mapping[str, int],
                       attempt: Callable[[str, str, Callable[[], Any]], Any],
                       reasons: set[str], out_of_time: Callable[[], bool]
                       ) -> dict[str, Any]:
        """The member-joined baseline an all-channels scope is measured against: the
        teams the scope's ``joined_teams`` members belong to (each member cleared by the
        people guard before the lookup naming them) and the channels the census
        enumerated in those teams. ``channels`` is None when a member or listing could
        not be read whole, or a joined team was outside what the census enumerated:
        the baseline is then unknown, never a smaller number."""
        joined: set[str] = set()
        out = {"accounts": 0, "accounts_excluded": 0, "teams": 0, "channels": None}
        known = True
        for address, wanted, _chats in scope.members:
            if not wanted:
                continue
            if out_of_time():
                reasons.add(TIME_BUDGET)
                return out
            out["accounts"] += 1
            try:
                # Boxed, so a failed read (None) differs from a member outside the
                # allowlist (whose teams discovery does not list either).
                found = attempt("teams.member", "graph.user",
                                lambda a=address: (self._member(a, scope, set()),))
            except PeopleExcluded:
                out["accounts_excluded"] += 1  # their teams are not listed
                continue
            if found is None or found[0] is None:
                known = known and found is not None
                continue
            cut: set[str] = set()
            items = attempt("teams.joined_teams", "graph.user_joined_teams",
                            lambda u=found[0]: self._listing(
                                "graph.user_joined_teams", {"$select": "id"}, cut, user=u))
            reasons.update(cut)
            if items is None or cut:
                known = False  # unread or cut: the baseline is unknown
                continue
            for item in items:
                team = str(item.get("id") or "").lower() if isinstance(item, Mapping) else ""
                if _valid("guid", team):
                    joined.add(team)
                else:
                    known = False
        out["teams"] = len(joined)
        if known and joined <= set(enumerated):
            out["channels"] = sum(enumerated[team] for team in joined)
        return out

    def _count(self, config: Mapping[str, Any], teams: list[dict[str, Any]],
               counts: dict[str, Any], reasons: set[str], blocked: list[dict[str, Any]]
               ) -> None:
        """Each cleared team's 30-day channel message count from the tenant's team
        activity report (one GET, JSON). Only cleared teams' rows are used; a row of any
        other team is never kept. A team the report does not match stays None with its
        ``count_reason``."""
        if config["message_counts"] == "none" or not teams:
            for team in teams:
                team["count_reason"] = "not_requested"
            return
        found: dict[str, Any] = {}
        why = None
        try:
            page = self.api.get_json(grammar.url("graph.teams_activity_report",
                                                 {"$format": "application/json"}))
        except SourceUnavailable as exc:
            why = _failure("teams.activity_report", "graph.teams_activity_report", exc,
                           blocked)
        except ReadOnlyGuardError as exc:
            if "redirect" not in str(exc):
                raise
            why = "report_redirect"  # a CSV download link: never followed (no host for it)
        else:
            rows = page.get("value") if isinstance(page, Mapping) else None
            if not isinstance(rows, list):
                why = "report_shape_unknown"
            else:
                if page.get("@odata.nextLink") is not None:
                    why = "report_truncated"  # not followed: the rest stays uncounted
                wanted = {t["team_id"] for t in teams}
                for row in rows:
                    team = str(row.get("teamId") or "").lower() \
                        if isinstance(row, Mapping) else ""
                    if team in wanted:
                        value = _period_count(row)
                        found[team] = (value if team not in found or found[team] == value
                                       else None)  # two rows that disagree: neither
        for team in teams:
            value = found.get(team["team_id"])
            team["messages_30d"] = value
            team["count_reason"] = (None if value is not None else why
                                    or ("report_shape_unknown" if team["team_id"] in found
                                        else "report_unmatched"))
        counted = sum(t["messages_30d"] is not None for t in teams)
        uncounted = sorted({t["count_reason"] for t in teams if t["count_reason"]})
        counts.update(teams_counted=counted, teams_uncounted=len(teams) - counted,
                      reasons=uncounted, status="counted" if counted == len(teams)
                      else "partial" if counted else "unavailable")
        # Only a refused report (a typed consent block) holds the census back; any other
        # missing count (the v1.0 report's 302 to a CSV, never followed; a row not
        # matched; an outage) leaves those counts None, named in message_counts, and the
        # listing of names stands complete.
        reasons.update(r for r in uncounted if r.startswith("blocked:"))


# ------------------------------------------------------------------ census file and human confirmation

def _named(record: Any, expected: str, key: str) -> str | None:
    """The name ``key`` of a record Graph returned for ``expected`` (None when it is
    another record, or has no such name)."""
    if not isinstance(record, Mapping) or str(record.get("id") or "").lower() != expected.lower():
        return None
    name = record.get(key)
    return name[:256] if isinstance(name, str) and name.strip() else None


def _period_count(row: Mapping[str, Any]) -> int | None:
    """A report row's channel messages over ``COUNT_DAYS`` (one detail of that period
    with a whole count, else None)."""
    details = row.get("details")
    found = [d.get("channelMessages") for d in details if isinstance(d, Mapping)
             and d.get("reportPeriod") in (COUNT_DAYS, str(COUNT_DAYS))] \
        if isinstance(details, list) else []
    if len(found) != 1 or isinstance(found[0], bool) or not isinstance(found[0], int) \
            or found[0] < 0:
        return None
    return found[0]


def _propose(config: Mapping[str, Any], *names: str | None) -> list[str]:
    """The areas whose keywords a name matches (a proposal to the owner, never a grant)."""
    text = " ".join(n.lower() for n in names if n)
    return [area for area in CENSUS_AREAS
            if text and any(p.search(text) for p in config["proposals"][area])]


def census_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / STORE_DIR / CENSUS_FILE


def confirmation_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / STORE_DIR / CONFIRMATION_FILE


def write_census(state_dir: str | Path, private: Mapping[str, Any] | None) -> bool:
    """Write the census listing to ``census_path`` (0600, atomically, outside every git
    work tree); with no listing (the census never passed the people guard), remove an
    earlier one, so a stale listing is never read as this run's. Whether one was written."""
    root = assert_outside_git(state_dir) / STORE_DIR
    path = root / CENSUS_FILE
    if private is None:
        path.unlink(missing_ok=True)
        return False
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = root / (CENSUS_FILE + ".tmp")
    staging.unlink(missing_ok=True)
    handle = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(private, out, indent=1, sort_keys=True)
    os.replace(staging, path)
    return True


def confirmation_block(code: str) -> dict[str, Any]:
    """The typed block of content waiting on the human confirmation (``code``: why
    none is usable). A later ask, never cleared by code."""
    return {"part": CONFIRMATION_PART, "source": "owner-confirmation", "http_status": None,
            "error_code": code, "cause": "later_ask", "later_ask": CONFIRMATION_LATER_ASK}


def read_confirmation(state_dir: str | Path) -> tuple[dict[str, Any] | None, str]:
    """(the confirmation, ``valid``) or (None, why not): the human
    ``plx-teams-content-confirmation/v1`` at ``confirmation_path``, in one of two forms.

    The listed form: ``confirmed_by``, ``confirmed_at`` (a date), ``channels``
    (``{team_id, channel_id}``) and ``chats`` (chat ids), at most 200 each, naming at
    least one. The all form (2026-10-21): ``scope``
    ``"all_census_cleared"``, ``confirmed_by`` (the confirming person's full name, never a role,
    address, principal or the name of code: ``content_confirmation_not_owner``),
    ``confirmed_at`` (a calendar date) and ``directive`` (the directive id); no
    ``channels`` or ``chats``. It confirms exactly what the run's census clears.

    Absent, unreadable, inside a git work tree, malformed, of another ``scope`` or naming
    nothing: no content is read (fails closed). Code never writes or defaults one. The
    result holds ``form`` (``LISTED`` or ``ALL_CENSUS_CLEARED``), ``channels`` and
    ``chats`` (empty for the all form)."""
    path = confirmation_path(state_dir)
    if not path.exists() and not path.is_symlink():
        return None, "content_confirmation_absent"
    try:
        real = assert_outside_git(path)
    except ValueError:  # capture_mail's CaptureError: it resolves into a git work tree
        return None, "content_confirmation_in_git"
    except (OSError, RuntimeError):  # a symlink loop
        return None, "content_confirmation_unreadable"
    try:
        if not real.is_file() or real.stat().st_size > MAX_CONFIRMATION_BYTES:
            return None, "content_confirmation_unreadable"
        document = json.loads(real.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "content_confirmation_unreadable"
    if not isinstance(document, Mapping) or document.get("schema") != CONFIRMATION_SCHEMA:
        return None, "content_confirmation_invalid"
    if "scope" in document:
        return _all_form(document)
    keys = {"schema", "confirmed_by", "confirmed_at", "note", "channels", "chats"}
    if set(document) - keys:
        return None, "content_confirmation_invalid"
    by, at = document.get("confirmed_by"), document.get("confirmed_at")
    channels, chats = document.get("channels", []), document.get("chats", [])
    if not (isinstance(by, str) and 0 < len(by.strip()) <= 200 and isinstance(at, str)
            and _DATE.match(at) and len(at) <= 40
            and isinstance(channels, list) and len(channels) <= 200
            and isinstance(chats, list) and len(chats) <= 200
            and all(isinstance(c, Mapping) and not set(c) - {"team_id", "channel_id", "note"}
                    and _valid("guid", c.get("team_id"))
                    and _valid("teams_thread", c.get("channel_id")) for c in channels)
            and all(_valid("teams_thread", c) for c in chats)):
        return None, "content_confirmation_invalid"
    if not channels and not chats:
        return None, "content_confirmation_empty"
    return {"form": LISTED,
            "channels": {(c["team_id"].lower(), c["channel_id"]) for c in channels},
            "chats": set(chats)}, "valid"


def _all_form(document: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The all_census_cleared form of :func:`read_confirmation` (``document`` has a
    ``scope``). Any other scope, an extra key, a bad date or directive id is invalid; a
    signer that is not a person's full name, or names code, is not the owner."""
    keys = {"schema", "scope", "confirmed_by", "confirmed_at", "directive", "note"}
    at, directive = document.get("confirmed_at"), document.get("directive")
    note = document.get("note", "")
    if document.get("scope") != ALL_CENSUS_CLEARED or set(document) - keys \
            or not isinstance(note, str) or len(note) > 2000:
        return None, "content_confirmation_invalid"
    try:
        dated = isinstance(at, str) and len(at) == 10 and bool(date.fromisoformat(at))
    except ValueError:
        dated = False
    if not dated or not isinstance(directive, str) or not _DIRECTIVE.fullmatch(directive):
        return None, "content_confirmation_invalid"
    by = document.get("confirmed_by")
    words = set(re.split(r"[^a-z]+", by.lower())) if isinstance(by, str) else set()
    if not isinstance(by, str) or len(by) > 120 or not _PERSON.fullmatch(by) \
            or words & _CODE_SIGNERS:
        return None, "content_confirmation_not_owner"
    return {"form": ALL_CENSUS_CLEARED, "channels": set(), "chats": set()}, "valid"


def confirmed_scope(scope: TeamsScope, private: Mapping[str, Any] | None,
                    confirmed: Mapping[str, Any]) -> tuple[TeamsScope | None, int]:
    """(the content scope, confirmed items this census did not clear): the channels and
    chats both confirmed by the owner and cleared by this run's census, with ``scope``'s
    caps and allowlist and nothing discovered from members. A confirmed item the census
    refused (an excluded member now, a members read failed) or no longer found is not
    read. The all form confirms exactly the cleared set (none left out). None when
    nothing is left."""
    cleared_channels = {(t["team_id"], c["channel_id"]) for t in (private or {}).get(
        "teams", []) for c in t["channels"]}
    cleared_chats = {c["chat_id"] for c in (private or {}).get("chats", [])}
    if confirmed.get("form") == ALL_CENSUS_CLEARED:
        confirmed = {"channels": cleared_channels, "chats": cleared_chats}
    channels = sorted(confirmed["channels"] & cleared_channels)
    chats = sorted(confirmed["chats"] & cleared_chats)
    left_out = (len(confirmed["channels"]) - len(channels)
                + len(confirmed["chats"]) - len(chats))
    if not channels and not chats:
        return None, left_out
    by_team: dict[str, list[str]] = {}
    for team, channel in channels:
        by_team.setdefault(team, []).append(channel)
    return TeamsScope({"schema": SCOPE_SCHEMA, **scope.limits(),
                       "teams": [{"team_id": t, "channels": c} for t, c in by_team.items()],
                       "chats": chats}, allowlist=scope.allowlist), left_out


# ------------------------------------------------------------------ CLI (counts only)

def default_state_dir(env: Mapping[str, str], *, injected: bool) -> Path:
    """The host's state directory of the capture check, kept across its runs
    (:func:`capture_mail.check_state_dir`): ``$PLX_WORKBENCH_STATE_DIR/teams-capture-check``,
    else ``$XDG_STATE_HOME/plx-workbench/teams-capture-check``, else under
    ``$HOME/.local/state``, never in Git. An injected transport gets its own
    subdirectory, so a fixture run never shares the live store. None set to an absolute
    path: refused, never a store in a place nobody chose."""
    return check_state_dir(env, CHECK_STATE[-1], injected=injected)[0]


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=None,
                        help="Workbench state directory (outside every git work tree); "
                             "default the host's, kept across runs (default_state_dir)")
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE),
                        help="plx-teams-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--member-principal", default="staff:teams-member",
                        help="a reader whose audience identities are members of the captured "
                             "conversations: what it sees must resolve offline")
    parser.add_argument("--outsider-principal", default="staff:teams-outsider",
                        help="a reader granted the conversations but a member of none: it "
                             "must see nothing")
    parser.add_argument("--withheld-principal", default="staff:teams-withheld",
                        help="a principal the audience does not grant")
    parser.add_argument("--runs", type=int, default=2,
                        help="capture runs unless one completes a pass first (no new pass "
                             "starts after it); run 2+ resumes from the stored cursor or "
                             "delta link")
    parser.add_argument("--max-runs", type=int, default=None,
                        help="keep capturing after --runs until a run is complete (a full "
                             "pass read), at most this many runs in all (default --runs)")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help="conversations captured at once (and census reads at once)")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="time budget of the capture runs: none starts, and no read goes "
                             "on to its next page, once it is spent (default none)")


_COUNT_KEYS = tuple(_new_counts())


def _read_back(store: CaptureStore, units: Sequence[tuple[str, str]], actor: Actor
               ) -> tuple[list[str], int]:
    """(citations ``actor`` may see, items withheld from it) over ``units``."""
    seen, withheld = [], 0
    for kind_name, unit in units:
        after = None
        while True:
            try:
                page = store.records(actor, kind_name, unit, after_path=after, limit=1000)
            except CaptureError:
                break  # the unit is not visible to this actor at all
            seen += [item["citation"] for item in page["items"]]
            withheld += page["withheld_count"]
            after = page["next_after_path"]
            if after is None:
                break
    return seen, withheld


def _report_run(number: int, run: Mapping[str, Any]) -> dict[str, Any]:
    return {"run": number, "complete": run["complete"],
            "discovery": {k: run["discovery"][k] for k in (
                "status", "reasons", "conversations", "discovered_total", "unvisited",
                "members_outside_allowlist")},
            "complete_bound": run["complete_bound"], "bound_total": run["bound_total"],
            # Counts only: who was excluded is never reported.
            "people_excluded": run["people_excluded"], "people_dropped": run["people_dropped"],
            "conversations": [{"index": i, "kind": c["kind"], "membership": c["membership"],
                               "status": c["status"], "reasons": c["reasons"],
                               "round": c["round"], "resumed_from": c["resumed_from"],
                               "acl_source": c["acl_source"],
                               "counts": {k: c["counts"][k] for k in _COUNT_KEYS}}
                              for i, c in enumerate(run["conversations"])]}


def _member_groups(api: Any, identities: Iterable[str], allowlist: MailScope) -> set[str]:
    """``group:<id>`` of the groups of a reader's allowlisted addresses, read the way the
    employee permissions read them (``integrations.graph_directory``: the transitive
    membership, or derived from the joined Teams when that read is refused): what a
    real member of a ``group_fallback`` channel holds. An unread user adds none."""
    from .graph_directory import GraphDirectoryReader

    reader, groups = GraphDirectoryReader(api), set()
    for identity in sorted(identities):
        if "@" in identity and allowlist.allows([identity]):
            read = reader.read(identity)
            if read.status == "read":
                groups |= {f"group:{g}" for g in read.group_ids}
    return groups


class RequestLog:
    """The request log of one run's Graph client: every GET it sends, and how many of
    them named an excluded person or a team, channel or chat the people guard refused
    (``TeamsCapture.names_excluded``, decided before the request goes out). It wraps the
    client's single send path on that instance, keeps no URL and changes nothing it
    sends, so the live-read or injected-transport label stays the client's own."""

    def __init__(self, transport: Any, capture: "TeamsCapture") -> None:
        self.gets = 0
        self.excluded = 0
        send = transport._send  # the one path every request takes (GuardedTransport)
        lock = threading.Lock()   # concurrent readers (TeamsCapture workers) count exactly

        def logged(method: str, url: str, *rest: Any, **kwargs: Any) -> Any:
            if method == "GET":
                named = capture.names_excluded(url)
                with lock:
                    self.gets += 1
                    self.excluded += named
            return send(method, url, *rest, **kwargs)

        transport._send = logged


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, sleep: Callable[[float], None] | None = None,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Take the census (``TeamsCapture.census``; its listing to ``census_path``), then,
    only for what the human confirmation names and the census cleared, capture
    ``--runs`` times, then until ``--max-runs`` ran or ``--budget-seconds`` is
    spent, stopping at the first run that completes a pass (never a new pass after it:
    ``pass`` is about one pass), then read the store back as a member, an outsider
    (granted, not a member) and a withheld actor. Counts, booleans and enums only.
    When a channel's ACL fell back to its group, the member reader also holds the
    groups of its addresses (``_member_groups``); the outsider and withheld never do."""
    from .graph_read import build_graph
    from .live_source_guard import BoundedBackoff

    if isinstance(args.runs, bool) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    max_runs = args.runs if getattr(args, "max_runs", None) is None else args.max_runs
    if (not isinstance(max_runs, int) or isinstance(max_runs, bool)
            or not args.runs <= max_runs <= MAX_RUNS):
        raise ValueError(f"max-runs must be runs..{MAX_RUNS}")
    budget = getattr(args, "budget_seconds", None)
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                               or not 0 < budget <= MAX_BUDGET_SECONDS):
        raise ValueError(f"budget-seconds must be in (0, {MAX_BUDGET_SECONDS}]")
    scope = TeamsScope.from_file(args.scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    state_base = None
    if args.state_dir is None:
        state_dir, base, state_base = check_state_dir(env, CHECK_STATE[-1],
                                                      injected=sender is not None)
        assert_outside_git(state_dir)
        # The directories it makes below the host's state base: 0700.
        for n in range(1, len(state_dir.relative_to(base).parts) + 1):
            base.joinpath(*state_dir.relative_to(base).parts[:n]).mkdir(
                mode=0o700, parents=True, exist_ok=True)
    else:
        state_dir = Path(args.state_dir).expanduser()
    carried = (state_dir / STORE_DIR / DB_FILE).is_file()
    started = clock()

    def expired() -> bool:
        return budget is not None and clock() - started >= budget
    actors = {name: Actor(audience.tenant, frozenset({principal}), "restricted")
              for name, principal in (("capture", args.principal),
                                      ("member", args.member_principal),
                                      ("outsider", args.outsider_principal),
                                      ("withheld", args.withheld_principal))}
    store = open_store(state_dir, audience)
    try:
        retry = BoundedBackoff(budget_seconds=60, **({"sleep": sleep} if sleep else {}))
        api, transport = build_graph(environ, sender=sender, retry=retry)
        env_tenant = (environ if environ is not None else os.environ).get(
            "MICROSOFT_GRAPH_TENANT_ID")
        capture = TeamsCapture(store, api, audience, actors["capture"],
                               workers=getattr(args, "workers", None) or 1,
                               home_tenant=env_tenant)
        log = RequestLog(transport, capture)
        runs, blocked, units, unvisited_first = [], [], {}, []
        captured: dict[str, str] = {}  # unit -> kind, of those with a page of content read
        # The census first (names and counts, no content), then content of what it cleared.
        census, private = capture.census(scope, blocked, expired)
        census["file"] = {"written": write_census(state_dir, private),
                          "outside_git_tree": True, "name": f"{STORE_DIR}/{CENSUS_FILE}"}
        # No human content confirmation. Content is read of
        # everything this census lists that the people guard cleared (the census ran the
        # guard first; a blocked guard clears nothing). A content-confirmation.json is
        # neither required nor consulted.
        confirmed = {"form": ALL_CENSUS_CLEARED, "channels": set(), "chats": set()}
        code = "not_required"
        content_scope, left_out = confirmed_scope(scope, private, confirmed)
        everything = (census["channels_cleared"], census["chats_cleared"])
        content = {"form": ALL_CENSUS_CLEARED,
                   "status": "census_cleared" if content_scope is not None else "none_cleared",
                   "confirmation": code,
                   "confirmed_channels": everything[0],
                   "confirmed_chats": everything[1],
                   "confirmed_not_cleared": left_out,
                   "channels_due": sum(len(channels) for _t, channels in content_scope.teams)
                   if content_scope else 0,
                   "chats_due": len(content_scope.chats) if content_scope else 0}
        first_found: set[str] | None = None
        for number in range(1, max_runs + 1 if content_scope is not None else 1):
            if number > 1 and expired():
                break
            run = capture.run(content_scope, expired)
            runs.append(_report_run(number, run))
            first_found = run["found_units"] if first_found is None else first_found
            unvisited_first.append(len(run["unvisited_units"] & first_found))
            blocked += [p for p in run["blocked_upstream"] if p not in blocked]
            for conversation in run["conversations"]:
                if conversation["counts"]["pages"] > 0:
                    captured[conversation["unit"]] = conversation["kind"]
                if conversation["round"] is not None:
                    units[conversation["unit"]] = ("teams_channel"
                                                   if conversation["kind"] == "channel"
                                                   else "teams_chat")
            if run["complete"] or (number >= args.runs and run["blocked_upstream"]):
                break
        exhausted = expired()
        pairs = [(kind, unit) for unit, kind in sorted(units.items())]
        operator = [store.operator_completeness(actors["capture"], k, u) for k, u in pairs]
        # Each conversation's own record (CONVERSATION) is no message: counted apart.
        own = [sum(r.path[-1] == CONVERSATION for r in store.current_records(k, u))
               for k, u in pairs]
        current = sum(o["records_current"] for o in operator) - sum(own)
        by_kind = {kind: sum(o["records_current"] - n for (k, _), o, n
                             in zip(pairs, operator, own) if k == kind) for kind in RELATIONS}
        # A group_fallback channel grants group:<team-id>: the member reader then holds
        # its groups too, read live (counted, never printed).
        member_groups: set[str] = set()
        if any(c["acl_source"] == GROUP_FALLBACK for r in runs for c in r["conversations"]):
            member_groups = _member_groups(api, audience.identities_for(actors["member"]),
                                           scope.allowlist)
        document = json.loads(Path(args.live_audience).read_text(encoding="utf-8"))
        identities = dict(document.get("identities") or {})
        identities[args.member_principal] = sorted(
            {*identities.get(args.member_principal, []), *member_groups})
        with CaptureStore(store.path, LiveSourceAudience.from_json(
                {**document, "identities": identities})) as view:
            member, member_withheld = _read_back(view, pairs, actors["member"])
            resolved = sum(1 for h in member if view.resolve(h, actors["member"]).get("ok"))
        outsider, outsider_withheld = _read_back(store, pairs, actors["outsider"])
        hidden, _ = _read_back(store, pairs, actors["withheld"])
        last = runs[-1] if runs else None
        conversations = last["conversations"] if last else []
        unvisited = [r["discovery"]["unvisited"] for r in runs]
        errors = sorted({r for c in conversations for r in c["reasons"]
                         if r.startswith("source_error:")}
                        | {r for r in (last["discovery"]["reasons"] if last else [])
                           if r.startswith("source_error:")}
                        | {r for r in census["reasons"] if r.startswith("source_error:")})
        blocked = list({json.dumps(p, sort_keys=True): p for p in blocked}.values())
        complete = bool(last and last["complete"]) and census["status"] == "complete"
        joined = census.get("member_joined") or {}
        coverage = {
            # Channels the census enumerated in every team it cleared, against the
            # channels of the teams the scope's members joined (None: unknown, or not an
            # all-channels scope), and the channels and chats with content read.
            "channels_enumerated": census["channels"],
            "channels_member_joined": joined.get("channels"),
            "wider_than_member_joined": (None if joined.get("channels") is None
                                         else census["channels"] > joined["channels"]),
            "channels_captured": sum(k == "channel" for k in captured.values()),
            "chats_captured": sum(k == "chat" for k in captured.values()),
            # From the client's request log: GETs naming an excluded person or a refused
            # conversation (0 is the guard's proof), and every GET it saw.
            "excluded_requested": log.excluded, "requests_logged": log.gets,
            # Blocked parts that name neither the Graph permission they need nor the
            # human confirmation (the one later ask a run may wait on): 0, or the
            # block is not one the all-channels check accepts.
            "blocked_unnamed": sum(1 for p in blocked if not p.get("permission")
                                   and p.get("part") != CONFIRMATION_PART)}
        report = {
            "check": "graph.teams_capture",
            "evidence_label": "injected-transport" if transport.sender_injected
            else "live-read",
            "read_only_guard": "GET-only; token POST only",
            "graph_permissions_required": list(GRAPH_PERMISSIONS),
            "storage": {"outside_git_tree": True,
                        "under_state_dir": store.path.parent.parent == state_dir.resolve(),
                        "host_state": args.state_dir is None, "carried": carried,
                        # Which host state base (capture_mail.check_state_dir): only
                        # workbench_state_dir is kept between the verifier's daily runs.
                        "state_base": state_base},
            "budget": {"seconds": budget, "exhausted": exhausted},
            "status": "blocked" if blocked else "complete" if complete else "incomplete",
            "blocked_upstream": blocked,
            # Names and 30-day counts of every team, channel and chat the people guard
            # cleared (counts here; names only in the census file), and what content the
            # human confirmation lets the runs read.
            "census": census,
            "content": content,
            "coverage": coverage,
            "runs": runs,
            # Falling is counted over the first run's conversations: one created mid-pass
            # is read in the pass (unvisited counts it) but never makes the pass rise.
            "pass": {"runs": len(runs), "last_run_complete": bool(last and last["complete"]),
                     "stop": ("blocked" if blocked else "no_content" if not runs
                              else "complete" if last["complete"]
                              else "budget" if exhausted else "max_runs"),
                     "unvisited": unvisited, "unvisited_first_run": unvisited_first,
                     "unvisited_falling": bool(unvisited_first) and all(
                         b <= a for a, b in zip(unvisited_first, unvisited_first[1:]))
                     and (unvisited_first[-1] == 0
                          or unvisited_first[-1] < unvisited_first[0])},
            "summary": {
                "conversations": last["bound_total"] if last else 0,
                "channels": sum(c["kind"] == "channel" for c in conversations),
                "chats": sum(c["kind"] == "chat" for c in conversations),
                "conversations_complete": last["complete_bound"] if last else 0,
                "conversations_blocked": sum(c["status"] == "blocked" for c in conversations),
                "conversations_not_readable": sum(c["status"] == NOT_READABLE
                                                  for c in conversations),
                "conversations_group_fallback": sum(c["acl_source"] == GROUP_FALLBACK
                                                    for c in conversations),
                "units_captured": len(pairs),
                "messages_current": current,
                "conversation_records": sum(own),
                "versions": sum(o["versions_total"] for o in operator),
                "replies_read": sum(c["counts"]["replies_read"] for r in runs
                                    for c in r["conversations"]),
                "delta_resumes": sum(1 for r in runs[1:] for c in r["conversations"]
                                     if c["resumed_from"] == "delta_url"
                                     and c["status"] == "complete"),
                "source_errors": len(errors)},
            "resolve_check": {"handles_checked": len(member), "resolved_offline": resolved,
                              "member_withheld": member_withheld,
                              "member_group_identities": len(member_groups)},
            "leak_check": {"outsider_actor_rows": len(outsider),
                           "outsider_actor_resolved": sum(
                               1 for h in member if store.resolve(h, actors["outsider"])
                               .get("ok")),
                           "outsider_actor_withheld": outsider_withheld,
                           "withheld_actor_rows": len(hidden),
                           "withheld_actor_resolved": sum(
                               1 for h in member if store.resolve(h, actors["withheld"])
                               .get("ok"))},
            # The ontology relations (source family teams) this run holds records of.
            "relations": {RELATIONS[kind]: n for kind, n in sorted(by_kind.items()) if n},
            "requests_sent": transport.requests_sent,
            "gets_sent": transport.gets_sent,
            # The Graph reads it made (grammar operations): the endpoints a pass evaluates.
            "operations_read": sorted(transport.operations_read),
        }
        leak = report["leak_check"]
        report["ok"] = bool(not blocked and not errors and resolved == len(member)
                            and coverage["excluded_requested"] == 0
                            and leak["outsider_actor_rows"] == 0
                            and leak["outsider_actor_resolved"] == 0
                            and leak["withheld_actor_rows"] == 0
                            and leak["withheld_actor_resolved"] == 0)
        return report
    finally:
        store.close()


__all__ = ["ALL_CENSUS_CLEARED", "CAPTURE_PRINCIPAL", "CENSUS_OPERATIONS", "CENSUS_SCHEMA", "CHANNEL_KIND",
           "CHAT_KIND", "CONFIRMATION_LATER_ASK", "CONFIRMATION_SCHEMA", "Conversation",
           "GRAPH_PERMISSIONS", "RELATIONS", "RequestLog", "SCOPE_SCHEMA", "TeamsCapture",
           "TeamsCaptureError", "TeamsScope", "add_cli_arguments", "census_path",
           "confirmation_block", "confirmation_path", "confirmed_scope", "default_state_dir",
           "html_text", "message_row", "open_store", "read_confirmation", "run_cli",
           "write_census"]
