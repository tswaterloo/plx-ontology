"""One host over one state directory: capture cycles, the projection, MCP and the console.

Capture cycles write into a persistent state directory, the projection is rebuilt from
them, and MCP plus the console are served from that directory. ``--state-dir`` defaults to
``PLX_WORKBENCH_STATE_DIR`` (``operating.warm_store.state_dir``); with neither the host
refuses, naming the setting (``{"error": "state_dir_unset", "setting":
"PLX_WORKBENCH_STATE_DIR"}``, exit 2).

    python3 -m operating.host run    --state-dir DIR --config HOST.json
    python3 -m operating.host status --state-dir DIR
    python3 -m operating.host serve  --state-dir DIR --config HOST.json \\
        [--interval-seconds N] [--no-console] [--stop-file PATH]
    python3 -m operating.host check  --state-dir DIR --config HOST.json [--cycles N] \\
        [--interrupt-after-seconds S]
    python3 -m operating.host mcp    --state-dir DIR --config HOST.json \\
        --session-ticket-env VAR [--eval-principal] [--agent ID]
    python3 -m operating.host mcp-http --state-dir DIR --config HOST.json \\
        --entra-resource ENTRA.json [--port N]

A cycle reads every configured source once, through the existing capture modules and
their GET-only clients (no HTTP code here):

* ``portal``: the Portal Workbench reporting API into the source mirror
  (``integrations.mirror_capture.sync_or_persisted``): a health check, then an
  incremental sync (``since`` after the first capture). A complete sync is persisted;
  when the API does not answer (non-2xx, unreachable) the last complete copy is served
  instead, labelled stale. The working mirror is the case runtime's own file
  (``<DIR>/case-runtime/source-mirror.sqlite``), so the MCP case tools and the console
  read the same captures offline.
* ``bc``: Business Central into ``<DIR>/bc-capture/capture.sqlite``
  (``integrations.bc_read.BcCapture``, mode ``auto``: modified-since reads after the
  first full pass, a key-only pass weekly). A 429/503 that stops the read part-way is
  its ``throttle``: the HTTP status and the entity sets left unread.
* ``mail``: Graph mail delta into ``<DIR>/mail-capture`` (``integrations.capture_mail``;
  later runs resume from each folder's stored deltaLink).
* ``sharepoint``: the bounded drive scope into ``<DIR>/sharepoint-capture``
  (``integrations.capture_sharepoint``; a full walk first, then each complete root follows
  the drive change feed and re-reads only changed items). ``budget_seconds`` bounds a
  cycle's capture: a walk it cuts resumes in the next cycle, so a library larger than one
  budget completes over several; ``check``'s coverage census of the scope has its own
  ``census_budget_seconds`` (default :data:`CENSUS_BUDGET_SECONDS`). A scope with a ``discover``
  rule (``integrations/sharepoint_estate_scope.json``) captures every library the app
  can read on the configured host.
* ``teams``: Teams channel and chat messages into ``<DIR>/teams-capture``
  (``integrations.capture_teams``; channel delta links and chat high-waters resume, a
  capped read keeps its cursor). Each cycle runs the census first and reads content only
  of the channels and chats the owner's ``<DIR>/teams-capture/content-confirmation.json``
  names and that census cleared (the ``teams-capture`` CLI's gate); without a usable one
  no message is read and the source is ``blocked`` on the later ask
  ``teams-content-confirmation``. Chats are read only for a member whose scope entry
  turns them on. Channel and chat messages are projected
  (``ontology.project_graph.TeamsCaptures``) and served only to a reader the live audience
  grants the conversation and its captured membership includes (the item gate,
  ``CaseRuntime.resolve_citation``). A part Graph
  refuses (a missing permission) is listed as the source's outage.

A brain knowledge capture the state directory holds (``<DIR>/knowledge-capture``, written by
``integrations.knowledge_source``; no cycle reads the brain) is projected too: its
``KnowledgeItem`` objects (audience unit ``brain:<namespaceKey>``) are served only to a
reader the config's ``knowledge_namespaces`` document (``plx-knowledge-namespaces/v1``,
re-read on every call) grants the namespace; personal namespaces never, and nobody without
the document or with one that does not load (``operating.ontology_host.case_host_policy``).
A marked value (``finance`` money read from a document, ``pii``, ...) is served only to the
principals the config's ``marking_grants`` (marking -> principals, next to ``live_audience``)
names for every marking it carries; without them nobody holds a marking (default deny).

Then the projection is rebuilt from what the captures hold (:func:`build_projection`,
the one seam to the projection build, with the one ``post_project`` step list of
``operating.build_steps``: document mentions and document-field links, reviewed links
and confirmed document kinds, the review store ``<DIR>/reviews.sqlite`` a build input
read when there, never created)
into ``<DIR>/projection/ontology.sqlite``, and
the host ledger ``<DIR>/host/host.sqlite`` records, per source, its generation (the
cycle count of successful reads), when it was captured, how it is served now and, when
the latest read failed, ``stale_since`` (the time the data served is as of), and the
parts the latest read was refused or not answered on (``source_blocked``,
``blocked_parts``: a mail capture whose every bound mailbox Graph refused, a Teams part
refused, a source that did not answer), which the case opener reads. Output is
counts, states, reasons and times only: never a key, value, address or citation.

Ingest mode (decision 2026-10-21-ingest-one-writer): with ``"ingest": {"mode":
"capture_clis"}`` in the config a cycle reads its sources through every source's own
capture CLI, as child processes (``operating.ingest_forever``: the source sections are
that module's table, ``portal``, ``bc``, ``mail``, ``sharepoint``, ``sharepoint_lists``,
``teams``, ``mc``), into the same stores the readers above write, through the same
capture classes (the SharePoint walk position and change feed, the Teams owner's content
confirmation gate). It is the same cycle: one scheduler (``serve``), one config schema,
one lock, ledger and projection, one ``check``. Before any capture runs the people-
exclusion list must load, else every configured source is blocked on it.
``serve`` in ingest mode refuses a state directory the machine throws away (under the
system temporary directory, ``/tmp``, ``/dev/shm``, ``/run``: ``state_dir_ephemeral``).
A cycle (``run``, ``check``) runs its capture children concurrently
(``ingest.max_parallel``); ``serve`` in ingest mode is the continuous mode
(``Host.ingest_continuous``): it holds the cycle lock for its whole run, re-runs each
source ``min_interval_seconds`` (default 120) after it finished, and each time sources
finish records them as one ledger cycle (``continuous`` true) and builds the next
projection generation at once (readers keep the previous one until it is published). It
stops when ``--stop-file`` (default ``<DIR>/host/ingest.stop``) exists, when the state
directory's file system has less than 4 GiB free (no build then) or on SIGTERM; the
running children are stopped and each capture resumes from its own cursor next time.

One writer per state directory: every cycle holds ``<DIR>/host/cycle.lock`` and is one
ledger row with its ``runner`` (``host``, ``ingest``). ``run`` refuses while another
cycle holds it (``host_cycle_running``); ``serve`` reports that cycle as ``waiting`` and
tries again at its next interval. The lock holder first stops any capture child a killed
host left running (``host/cycle-children.json``: pid, process group and start token; the
group is killed only when the token still matches) and refuses when it cannot
(``orphan_capture_running``, ``cycle_children_unreadable``); ``run``, ``check`` and
``serve --no-console`` turn SIGTERM into an exit that stops the running child first.
``check`` reports ``checks.cycles_overlapping``: ledger cycles that started before an
earlier one finished (0 under the one lock). In ingest mode ``check`` adds the capture
resume counters (``resume_checked``, ``completed_not_resumed``, ``requests_grew``,
``full_reread_sources``, ...), ``duplicate_items`` per projected source
(``duplicates``), ``leaked`` and ``state_persistent``, and is blocked
(``no_completed_source_to_resume``) when no completed cursor source was there to check;
``mail_folders_restarted`` and ``sharepoint_roots_restarted`` are not measured (None).

``check`` (the ``host.cycles`` live check) runs ``--cycles`` cycles, each by a new host
over the same state directory (a restart between cycles), then reports what the cycles
must keep: every configured source ran, the projection chains generation to generation
and a later cycle rebuilds fewer units than the cold first one, no projected value appears
in the output (``row_data_check``), and the projection read as the capture principal
versus a principal without any grant (``trim``, read through the tools ``mcp`` serves
under the policy each call is served with) shows the second nothing. A source that did
not answer (an outage, or the Portal served from its persisted copy) is listed in
``blocked_upstream``. With SharePoint configured, ``sharepoint_coverage`` measures files
seen versus captured across every folder under each scope root (:func:`sharepoint_coverage`),
each cycle says where it started every root (``resumed_from``: ``walk_position``,
``drive_delta``, ``new_walk``, ``deferred``, ``deferred_walk_position``; a resumed walk
reads round ``full`` too) and ``checks.sharepoint_roots_restarted`` counts the cut walks a
later cycle started over instead of resuming (as ``mail_folders_restarted`` for mail).
``check --interrupt-after-seconds S`` adds a restart that kills a cycle: after the first
cycle it opens a probe case through the served tools (an interpretation finding, a fact
citing a readable projected object) and claims it, runs the next cycle as a child
``operating.host run`` process killed with SIGKILL after ``S`` seconds (its process group,
OCR children included) while its cycle is running (``interrupt``: one that finished first
fails the check), runs the remaining cycles by new hosts (the next marks the killed one
interrupted and builds on the last committed generation: ``checks.resumed_after_interrupt``)
and re-reads the case through new served tools (``case_resume``: chain intact, findings
and citations readable and current, no event lost or added, the claim still held, nothing
for the withheld principal). The probe case also carries one ``plx.act`` intent parked
before the kill in test mode only (an isolated owner, never a PLX system; no approval;
never dispatched): after the restart it must still be the case's one ``proposed`` intent
with a verified chain (``intent_parked``, ``intent_chain_ok``), never dispatched
(``intent_dispatched`` false), ``intent_state`` ``proposed`` and unapproved
(``approval_state`` ``none``). With ``case_coworker`` in the config the probe case is
opened for a group shared with one co-worker, who re-reads it, its claim and the intent
after the restart in a served session of their own (``coworker_read``,
``coworker_chain_ok``, ``coworker_intent_state``, ``coworker_approval_state``,
``coworker_withheld_markers``). The probe case stays in the directory, so this check runs only
on a probe directory: one absent or empty, which it marks (``host-check-probe.json``), or
one it marked before; a real state directory is refused (``host_check_probe_on_real_state``)
and ``serve``, ``mcp`` and ``mcp-http`` refuse a probe directory
(``host_probe_state_not_served``): no employee or agent sees a probe case.
``run`` and ``check`` with ``--sources`` read only the named configured sources (one part
of a config, e.g. its SharePoint scope); a name the config does not configure is refused.

``mcp`` starts ``operating.mcp_server`` over this directory with the authenticated
employee identity: the employee resolver (``--employee-policy``/``--portal-access``
from the config) with the Entra directory provider when ``entra_directory`` is set,
and the durable start-up ticket replay guard. The MCP host reads no Portal itself: it
answers from the captures, the last projection generation and the review store
(``plx.find`` answers reviewed aliases); with a ``portal`` source configured an answer
reports the Portal's capture state (``--host-captures-portal``), never that the host
configures no reader for it, and a case finding may cite captured mail and SharePoint
(``case_tools_host.CaseCaptures``). With ``action_path: true`` in the config it
also serves ``plx.act`` over the state directory's action store (register and read only:
no call approver, MC operator or roster is configured, so nothing is approved, requested
or sent); without it ``plx.act`` refuses (default deny). Such a host (``serve``, ``mcp``,
``mcp-http``) first resolves the approver roster's Entra object ids by UPN through the
GET-only Graph client into the state directory (``resolve_approvers``) and says on stderr
how many resolved; an unresolved approver stays refused. ``mcp-http`` serves the
same tools to many employees (``operating.mcp_http_host``, loopback only): each request
authenticates with an Entra access token.

Credentials: the Portal key from ``PLX_WORKBENCH_API_KEY`` or, on macOS, the Keychain
service ``plx-workbench-api-key``; the URL from the config or ``PLX_WORKBENCH_API_URL``;
Graph and BC from their ``MICROSOFT_GRAPH_*`` / ``BC_*`` variables. Never printed.

Evidence: injected-transport tests (``operating/tests/test_host.py``); live-unverified.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from integrations import child_process
from ontology.core import Actor

from . import session_identity

CONFIG_SCHEMA = "plx-host-config/v1"
STATUS_SCHEMA = "plx-host-status/v1"
CYCLE_SCHEMA = "plx-host-cycle/v1"
CHECK_SCHEMA = "plx-host-check/v1"
WITHHELD_PRINCIPAL = "staff:host-check-withheld"
_LOWER_WORD = re.compile(r"[a-z_]+")
_UPSTREAM = {"portal": "portal-workbench-api", "bc": "business-central",
             "mail": "microsoft-graph", "sharepoint": "microsoft-graph",
             "teams": "microsoft-graph", "sharepoint_lists": "microsoft-graph",
             "mc": "mission-control-mcp"}
# The projection source (an ontology binding's ``source``) each host source captures.
PROJECTED_SOURCE = {"portal": "portal_api", "bc": "bc", "mail": "graph_mail",
                    "sharepoint": "sharepoint", "teams": "teams"}
SOURCES = ("portal", "bc", "mail", "sharepoint", "teams")
_TEAMS_COUNTS = ("pages", "messages_read", "replies_read", "versions_new", "unchanged")
HOST_DIR, LEDGER_FILE, LOCK_FILE = "host", "host.sqlite", "cycle.lock"
# The capture child processes of the running cycle (``CycleChildren``): the next holder of
# the cycle lock stops any a killed runner left behind before its own cycle reads anything.
CHILDREN_FILE, CHILDREN_SCHEMA = "cycle-children.json", "plx-host-cycle-children/v1"
ORPHAN_STOP_SECONDS = 10.0
HOST_RUNNER, INGEST_RUNNER = "host", "ingest"   # the ledger cycle's ``runner``
CONTINUOUS_SCHEMA = "plx-host-ingest-continuous/v1"
STOP_FILE = "ingest.stop"   # <DIR>/host/ingest.stop: the continuous ingest mode stops
MIN_FREE_BYTES = 4 * 1024 ** 3   # ... and when its file system has less free than this
# A projection build needs room for what it writes beside the generation served: the
# estimate ``ontology.project_incremental.space_need`` makes from the last build's measured
# sizes (an incremental build: its seed, none where the file system clones it, plus the
# changed units' share; a cold one: the last measured store and working file) plus this
# margin. Once the changed units are known the build takes the mode it prefers (cold when
# most units changed or that needs less), else the other mode where that fits; only when
# neither fits is it skipped (``disk_low``) and the previous generation stays served.
# Checked before the build (the least any mode writes), once the changed units are known
# (``disk.estimate``: the mode checked, ``estimates``: both) and while it runs (free space
# below the margin stops it, its files removed).
BUILD_DISK_MARGIN_BYTES = 1024 ** 3
# How long the host's checkpoint between refreshes waits for each store's other
# connections (a capture child's write, a short reader) before it reports ``busy``.
CHECKPOINT_WAIT_MS = 2000
PROJECTION_DIR, PROJECTION_FILE = "projection", "ontology.sqlite"
BC_DIR, BC_FILE = "bc-capture", "capture.sqlite"
KEY_ENV, URL_ENV = "PLX_WORKBENCH_API_KEY", "PLX_WORKBENCH_API_URL"
KEYCHAIN_SERVICE = "plx-workbench-api-key"
DEFAULT_PRINCIPAL = "svc:workbench-host"
DEFAULT_INTERVAL_SECONDS = 900
# The coverage census's own time bound when the SharePoint source sets no
# ``census_budget_seconds``: never the capture's ``budget_seconds``, which bounds one cycle's
# reads, not the listing of what the library holds.
CENSUS_BUDGET_SECONDS = 900
TRIM_PAGE = 1000   # objects per page the trim counts
PROBE_CANDIDATES = 50   # readable projected objects the resume probe tries to cite
PROBE_STATE_FILE = "host-check-probe.json"   # marks a directory the probe case may live in
PROBE_STATE_SCHEMA = "plx-host-probe-state/v1"
# The probe's parked plx.act intent (test mode only): the isolated owner's state, in the
# action store's directory, and the fixture session's subject (never a person at PLX).
PROBE_OWNER_FILE = "host-check-owner.sqlite"
PROBE_SUBJECT = "host.check@isolated-fixture.example"
# The child ``check --interrupt-after-seconds`` kills: ``python3 -I -m operating.host``,
# with the repository on the path (isolated mode adds neither the working directory nor
# PYTHONPATH).
RUN_CHILD = (sys.executable, "-I", "-c", "import runpy, sys; sys.path.insert(0, {root!r}); "
             "runpy.run_module('operating.host', run_name='__main__', alter_sys=True)"
             .format(root=str(Path(__file__).resolve().parents[1])))
_CONFIG_KEYS = {"schema", "note", "tenant", "capture_principal", "capture_audience",
                "live_audience", "mirror_audience", "employee_policy", "portal_access",
                "entra_directory", "interval_seconds", "agent", "action_path",
                "knowledge_namespaces", "case_coworker", "marking_grants", "ingest"}
_COWORKER_KEYS = {"group", "principal", "member_of"}
_SOURCE_KEYS = {"portal": {"base_url", "relations", "budget_seconds"},
                "bc": {"entity_sets", "budget_seconds", "books"},
                "mail": {"scope", "budget_seconds"},
                "sharepoint": {"scope", "budget_seconds", "census_budget_seconds"},
                "teams": {"scope"}}
_PATHS = {"capture_audience", "live_audience", "mirror_audience", "employee_policy",
          "portal_access", "knowledge_namespaces"}
_LEDGER_DDL = (
    """CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY, configured INTEGER NOT NULL, generation INTEGER NOT NULL,
  captured_at TEXT, served_from TEXT NOT NULL, complete INTEGER, attempted_at TEXT,
  stale_since TEXT, error TEXT, served_path TEXT)""",
    """CREATE TABLE IF NOT EXISTS cycle (
  cycle_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  state TEXT NOT NULL, report TEXT)""",
    """CREATE TABLE IF NOT EXISTS projection (
  generation INTEGER PRIMARY KEY, built_at TEXT NOT NULL, previous_generation INTEGER,
  content_digest TEXT, objects INTEGER, inputs TEXT NOT NULL)""",
    # The parts the source's latest read was refused or not answered on (``blocked_parts``:
    # codes and statuses only); no row while it answered.
    """CREATE TABLE IF NOT EXISTS source_blocked (
  source TEXT PRIMARY KEY, parts TEXT NOT NULL)""",
)


class HostError(Exception):
    """A host refusal with a code (configuration, state directory, a running cycle)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age(now: datetime, stamp: str | None) -> int | None:
    if not stamp:
        return None
    return max(0, int((now - datetime.fromisoformat(stamp.replace("Z", "+00:00")))
                      .total_seconds()))


# ------------------------------------------------------------------ configuration

class HostConfig:
    """``plx-host-config/v1``: which sources the host reads and with which audiences.
    Relative paths resolve against the config file's directory. With ``ingest`` (ingest
    mode) the source sections are ``capture_sources``' (default
    ``operating.ingest_forever.SOURCES``; tests inject fake capture CLIs), each checked
    against its CLI's fields."""

    def __init__(self, document: Mapping[str, Any], base: Path, *,
                 capture_sources: Mapping[str, Any] | None = None) -> None:
        from . import ingest_forever

        if not isinstance(document, Mapping) or document.get("schema") != CONFIG_SCHEMA:
            raise HostError("host_config_invalid")
        self.ingest = None
        self.capture_sources: Mapping[str, Any] = {}
        if "ingest" in document:
            try:
                self.ingest = ingest_forever.ingest_settings(document["ingest"])
            except ingest_forever.IngestError:
                raise HostError("host_config_invalid:ingest") from None
            self.capture_sources = (ingest_forever.SOURCES if capture_sources is None
                                    else capture_sources)
        names = tuple(self.capture_sources) if self.ingest is not None else SOURCES
        if set(document) - _CONFIG_KEYS - set(names):
            raise HostError("host_config_invalid")
        self.tenant = document.get("tenant", "plx")
        self.principal = document.get("capture_principal", DEFAULT_PRINCIPAL)
        if not isinstance(self.tenant, str) or not isinstance(self.principal, str):
            raise HostError("host_config_invalid")
        paths = {}
        for key in _PATHS:
            value = document.get(key)
            if value is not None and not isinstance(value, str):
                raise HostError(f"host_config_invalid:{key}")
            paths[key] = None if value is None else (base / value).resolve()
        if paths["mirror_audience"] is None:
            raise HostError("host_config_invalid:mirror_audience")
        self.mirror_audience = paths["mirror_audience"]
        self.live_audience = paths["live_audience"]
        self.capture_audience = paths["capture_audience"] or paths["live_audience"]
        self.employee_policy = paths["employee_policy"]
        self.portal_access = paths["portal_access"]
        # The brain namespace authority: refused here when it does not load or names
        # another tenant; served re-read per call, so a later break grants nobody.
        self.knowledge_namespaces = paths["knowledge_namespaces"]
        if self.knowledge_namespaces is not None:
            from integrations.knowledge_source import NamespaceAuthority
            try:
                authority = NamespaceAuthority.from_file(self.knowledge_namespaces)
            except (OSError, ValueError):
                authority = None
            if authority is None or authority.tenant != self.tenant:
                raise HostError("host_config_invalid:knowledge_namespaces")
        # Who holds each marking (``case_host_policy``); absent: nobody (default deny).
        from .ontology_host import check_marking_grants
        try:
            self.marking_grants = check_marking_grants(document.get("marking_grants"))
        except ValueError:
            raise HostError("host_config_invalid:marking_grants") from None
        self.entra_directory = document.get("entra_directory", False)
        self.action_path = document.get("action_path", False)
        interval = document.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
        if type(self.entra_directory) is not bool or type(self.action_path) is not bool \
                or isinstance(interval, bool) \
                or not isinstance(interval, int) or not 60 <= interval <= 86400:
            raise HostError("host_config_invalid")
        self.interval_seconds = interval
        self.case_coworker = self._case_coworker(document.get("case_coworker"))
        # The agent definition the MCP session runs (operating/agent_definitions.json);
        # None: the definitions' default_agent.
        self.agent = document.get("agent")
        if self.agent is not None:
            from .agent_definitions import load as load_agents
            if not isinstance(self.agent, str) or self.agent not in load_agents().agents:
                raise HostError("host_config_invalid:agent")
        self.sources: dict[str, dict[str, Any]] = {}
        if self.ingest is not None:
            self._ingest_sections(document, base)
            return
        for name in SOURCES:
            entry = document.get(name)
            if entry is None:
                continue
            # A capture budget may be absent (unbounded); the census's never is.
            budgets = () if not isinstance(entry, Mapping) else (
                *(() if entry.get("budget_seconds") is None else (entry["budget_seconds"],)),
                entry.get("census_budget_seconds", CENSUS_BUDGET_SECONDS))
            if not isinstance(entry, Mapping) or set(entry) - _SOURCE_KEYS[name] or any(
                    isinstance(budget, bool) or not isinstance(budget, (int, float))
                    or not 0 < budget < float("inf")   # 0 never reads; NaN, inf never end
                    for budget in budgets):
                raise HostError(f"host_config_invalid:{name}")
            entry = dict(entry)
            for key in ("scope", "books"):
                if key in entry:
                    entry[key] = (base / entry[key]).resolve()
            self.sources[name] = entry
        portal = self.sources.get("portal")
        if portal is not None and (not isinstance(portal.get("relations"), list)
                                   or not portal["relations"]):
            raise HostError("host_config_invalid:portal.relations")
        if {"bc", "mail", "sharepoint", "teams"} & set(self.sources) \
                and self.capture_audience is None:
            raise HostError("host_config_invalid:capture_audience")

    def _ingest_sections(self, document: Mapping[str, Any], base: Path) -> None:
        """Ingest mode's source sections, each one its capture CLI's
        (``ingest_forever.section_options``): the host's audiences, tenant and principal
        fill the fields a section leaves out. The capture audience is required as soon as
        a configured CLI reads with a live audience (the check's trim and census read with
        it)."""
        from . import ingest_forever

        defaults = {"live_audience": self.capture_audience,
                    "audience": self.mirror_audience, "tenant": self.tenant,
                    "principal": self.principal}
        for name, spec in self.capture_sources.items():
            if name not in document:
                continue
            try:
                self.sources[name] = ingest_forever.section_options(
                    spec, document[name], base, defaults)
            except ingest_forever.IngestError:
                raise HostError(f"host_config_invalid:{name}") from None
        if not self.sources:
            raise HostError("host_config_invalid:ingest")
        if self.capture_audience is None and any(
                "live_audience" in self.capture_sources[name].fields for name in self.sources):
            raise HostError("host_config_invalid:capture_audience")

    def _case_coworker(self, value: Any) -> dict[str, Any] | None:
        """``case_coworker`` (absent: off): the group the ``check`` probe case is opened
        for (with the capture principal) and the one co-worker in it who re-reads the case
        after the restart: a ``staff:`` principal and the principals it holds
        (``member_of``, as the employee gate would resolve them), which must hold the
        group. Refused when they do not, on a wildcard, or when the host, the check's
        withheld principal or a non-staff principal stands for the co-worker."""
        if value is None:
            return None
        invalid = HostError("host_config_invalid:case_coworker")
        if not isinstance(value, Mapping) or set(value) != _COWORKER_KEYS:
            raise invalid
        group, principal, member_of = value["group"], value["principal"], value["member_of"]
        if not isinstance(member_of, list) or not all(
                isinstance(item, str) and item.strip() == item and item
                for item in (group, principal, *member_of)):
            raise invalid
        held = {principal, *member_of}
        if group not in member_of or not principal.startswith("staff:") \
                or "*" in held | {group} or group == principal \
                or {self.principal, WITHHELD_PRINCIPAL} & (held | {group}):
            raise invalid
        return {"group": group, "principals": frozenset(held)}

    def only(self, names: Sequence[str]) -> "HostConfig":
        """This config reading only ``names`` of its sources; a name it does not
        configure (or none at all) is refused."""
        if not names or set(names) - set(self.sources):
            raise HostError("host_sources_invalid")
        narrowed = copy.copy(self)
        narrowed.sources = {name: entry for name, entry in self.sources.items()
                            if name in names}
        return narrowed

    @classmethod
    def from_file(cls, path: str | Path, *,
                  capture_sources: Mapping[str, Any] | None = None) -> "HostConfig":
        path = Path(path).resolve()
        try:
            document = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise HostError("host_config_unreadable") from None
        return cls(document, path.parent, capture_sources=capture_sources)


def workbench_key(environ: Mapping[str, str]) -> str | None:
    """The Portal Workbench key: the environment, else (macOS) the Keychain. Never logged."""
    key = environ.get(KEY_ENV)
    if key or sys.platform != "darwin":
        return key or None
    try:
        found = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                                "-w"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (found.stdout.strip() or None) if found.returncode == 0 else None


# ------------------------------------------------------------------ projection seam

def projection_path(state_dir: Path) -> Path:
    return state_dir / PROJECTION_DIR / PROJECTION_FILE


class DiskLow(Exception):
    """The disk guard stopped a projection build (``disk``: free, needed, estimate)."""

    def __init__(self, disk: Mapping[str, Any]) -> None:
        super().__init__("disk_low")
        self.disk = dict(disk)


def build_projection(state_dir: Path, sources: Mapping[str, Any], *, tenant: str = "plx",
                     space: Callable[[Mapping[str, Any]], None] | None = None,
                     checkpoint: Callable[[], None] | None = None) -> dict[str, Any]:
    """The host's one call into the projection build: the next generation by
    ``ontology.project_incremental.build_generation`` in ``<DIR>/projection`` (only the
    units whose captures changed are projected again; the rest are reused from the
    committed generation), then published at :func:`projection_path`. An interrupted
    build leaves the committed generation, and the file served, as they were. The
    ``post_project`` steps are the one list every build runs (``operating.build_steps``:
    document mentions and document-field links resolved through the identifier index and
    the reviewed aliases, reviewed links, confirmed document kinds), over ``tenant``'s
    review and confirmation stores in the state directory, read as they are now.
    Returns the build report plus ``build``: mode, units, units rebuilt and each step's
    counts."""
    from ontology.project_incremental import build_generation
    from ontology.schema import load_schema
    from operating import build_steps

    path = projection_path(state_dir)
    generation = build_generation(path.parent, load_schema(), sources,
                                  post_project=build_steps.post_project(state_dir, tenant),
                                  space=space, checkpoint=checkpoint)
    _publish(generation.store, path)
    return {**generation.report, "build": {
        "mode": generation.mode, "units": generation.units,
        "units_rebuilt": generation.units_rebuilt, "seconds": generation.seconds,
        "seed": generation.seed, "need": generation.need,
        "steps": {name: dict(step.get("counts") or {}) for name, step in
                  (generation.report.get("post_project") or {}).items()}}}


def _publish(store: Path, path: Path) -> None:
    """Serve a committed generation at ``path``: a hard link to it (a committed
    generation is never written again) swapped in by rename, so a reader opening
    ``path`` gets a whole generation, the new one or the previous one."""
    temp = path.with_name(f".{path.name}.publish")
    temp.unlink(missing_ok=True)
    try:
        os.link(store, temp)
    except OSError:  # a file system without hard links: a copy, same bytes
        shutil.copyfile(store, temp)
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def state_capture_paths(state_dir: str | Path) -> dict[str, Path]:
    """source family -> the file of each BC, mail, SharePoint, Teams, Mission Control and
    brain knowledge capture store a host state directory may hold, held or not (the Portal
    mirror is the caller's)."""
    from integrations import (capture_mail, capture_sharepoint, capture_teams,
                              knowledge_source, mc_sources)

    state_dir = Path(state_dir).expanduser()
    return {"bc": state_dir / BC_DIR / BC_FILE,
            "graph_mail": state_dir / capture_mail.STORE_DIR / capture_mail.DB_FILE,
            "sharepoint": state_dir / capture_sharepoint.STORE_DIR / capture_sharepoint.DB_FILE,
            "teams": state_dir / capture_teams.STORE_DIR / capture_teams.DB_FILE,
            "mission_control": mc_sources.store_path(state_dir),
            "brain": knowledge_source.store_path(state_dir)}


def open_state_capture(state_dir: str | Path, source: str, *,
                       internal_domains: Sequence[str] = ()) -> Any:
    """One capture source of ``state_capture_paths``, opened read-only (a context
    manager); a missing or unreadable store raises as its reader does."""
    from ontology import project

    if source == "graph_mail":
        return project.MailCaptures(state_dir, internal_domains=internal_domains)
    if source == "sharepoint":
        return project.SharePointCaptures(state_dir)
    return project.capture_source(source, state_capture_paths(state_dir)[source])


def state_captures(state_dir: Path, stack: ExitStack, *,
                   internal_domains: Sequence[str] = ()) -> dict[str, Any]:
    """The BC, mail, SharePoint, Teams, Mission Control and brain capture sources a host state
    directory holds, opened read-only into ``stack``. The Portal mirror is the caller's:
    the host serves its live or persisted mirror, the unfamiliar-sample eval syncs its
    own. SharePoint is held when its drive or its list-item capture store is
    (``ontology.project.SharePointCaptures.stores``)."""
    return {source: stack.enter_context(open_state_capture(
                state_dir, source, internal_domains=internal_domains))
            for source in state_capture_paths(state_dir) if capture_held(state_dir, source)}


def capture_held(state_dir: str | Path, source: str) -> bool:
    """The state directory holds ``source``'s capture store (``state_capture_paths``;
    SharePoint: its drive or its list-item store)."""
    from ontology import project

    if source == "sharepoint":
        return any(p.is_file() for p in project.SharePointCaptures.stores(Path(state_dir)))
    path = state_capture_paths(state_dir).get(source)
    return path is not None and path.is_file()


# ------------------------------------------------------------------ the cycle's children

_PROC = Path("/proc")


def _proc_stat(pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` from its state field on (Linux); None when there is none."""
    try:
        text = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text[text.rindex(")") + 2:].split()


def process_token(pid: int) -> str | None:
    """When process ``pid`` started, as a token two different processes never share (the
    boot id and the start tick on Linux, ``ps``'s start time elsewhere); None when no
    running process has that pid (a zombie is not running)."""
    if (_PROC / "self" / "stat").is_file():
        fields = _proc_stat(pid)
        if fields is None or fields[0] in ("Z", "X"):
            return None
        try:
            boot = (_PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
        except OSError:
            boot = ""
        return f"{boot}:{fields[19]}"
    try:
        found = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    line = found.stdout.strip()
    if found.returncode != 0 or not line or line.startswith("Z"):
        return None
    return line.split(None, 1)[1] if " " in line else None


def _group_running(pgid: int) -> bool:
    """A process of group ``pgid`` still runs (zombies left out on Linux)."""
    if (_PROC / "self" / "stat").is_file():
        for entry in _PROC.iterdir():
            if entry.name.isdigit():
                fields = _proc_stat(int(entry.name))
                if fields is not None and fields[2] == str(pgid) \
                        and fields[0] not in ("Z", "X"):
                    return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CycleChildren:
    """The capture child processes of the running cycle, in ``<DIR>/host/
    cycle-children.json`` (0600, replaced whole): each one's pid, process group (a child
    leads its own session), start token and source. Only the holder of the cycle lock
    writes it. A runner SIGKILLed mid-cycle cannot stop its children; the next holder of
    the lock does (``recover``) before its own cycle reads anything."""

    # Concurrent capture children (ingest mode) add and remove their entries from worker
    # threads: every read-modify-write of the file holds this lock.
    _guard = threading.RLock()

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[dict[str, Any]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise HostError("cycle_children_unreadable") from None
        entries = document.get("children") if isinstance(document, dict) \
            and document.get("schema") == CHILDREN_SCHEMA else None
        if not isinstance(entries, list) or not all(
                isinstance(e, dict) and type(e.get("pid")) is int and e["pid"] > 1
                and type(e.get("pgid")) is int and e["pgid"] > 1
                and isinstance(e.get("token"), str) for e in entries):
            raise HostError("cycle_children_unreadable")
        return entries

    def _write(self, entries: Sequence[Mapping[str, Any]]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}."
                                        f"{threading.get_ident()}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": CHILDREN_SCHEMA, "children": list(entries)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def add(self, pid: int, source: str) -> None:
        """Record child ``pid`` (started in its own session) right after it starts."""
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        token = process_token(pid)
        if token is None:
            return   # already gone: nothing to stop later
        with self._guard:
            self._write([*self._read(), {"pid": pid, "pgid": pgid, "token": token,
                                         "source": source}])

    def remove(self, pid: int) -> None:
        """Forget child ``pid`` once it is stopped and reaped (``child_process.stop``),
        unless a process of its group outlives it: that stays recorded for ``recover``."""
        with self._guard:
            self._write([e for e in self._read()
                         if e["pid"] != pid or _group_running(e["pgid"])])

    def recover(self, *, monotonic: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep,
                wait_seconds: float = ORPHAN_STOP_SECONDS) -> dict[str, int]:
        """Stop the process group of every recorded child still running: SIGKILL when
        its leader is the process recorded (same start token), or when the leader is gone
        but the group is not (a pid is not reused while its group exists); a leader pid
        now another process is left alone (``not_ours``). Refused
        (``orphan_capture_running``) when a group outlives ``wait_seconds``; the record
        is cleared only once every recorded group is gone."""
        entries = self._read()
        counts = {"recorded": len(entries), "stopped": 0, "gone": 0, "not_ours": 0}
        killed = []
        for entry in entries:
            token = process_token(entry["pid"])
            if token is not None and token != entry["token"]:
                counts["not_ours"] += 1
                continue
            if token is None and not _group_running(entry["pgid"]):
                counts["gone"] += 1
                continue
            try:
                os.killpg(entry["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                counts["gone"] += 1
                continue
            except PermissionError:
                raise HostError("orphan_capture_running") from None
            killed.append(entry["pgid"])
            counts["stopped"] += 1
        deadline = monotonic() + wait_seconds
        while any(_group_running(pgid) for pgid in killed):
            if monotonic() >= deadline:
                raise HostError("orphan_capture_running")
            sleep(0.05)
        if entries:
            self._write([])
        return counts


def cycle_overlaps(host: "Host") -> dict[str, Any]:
    """Every finished cycle of ``host``'s ledger, whichever runner ran it: how many
    started before an earlier one finished (two writers at once; 0 under one lock)."""
    with host._ledger() as db:
        rows = db.execute("SELECT started_at, finished_at, report FROM cycle "
                          "ORDER BY started_at, cycle_id").fetchall()
    overlapping, latest = 0, None
    runners: dict[str, int] = {}
    for row in rows:
        try:
            runner = json.loads(row["report"] or "{}").get("runner") or HOST_RUNNER
        except ValueError:
            runner = "unreadable"
        runners[runner] = runners.get(runner, 0) + 1
        if row["finished_at"] is None:
            continue   # killed while running: it never finished
        if latest is not None and row["started_at"] < latest:
            overlapping += 1
        latest = max(latest or row["finished_at"], row["finished_at"])
    return {"cycles_recorded": len(rows), "cycles_overlapping": overlapping,
            "cycles_by_runner": dict(sorted(runners.items()))}


# ------------------------------------------------------------------ the host

class Host:
    """Capture cycles over one state directory; ``cycle``, ``status`` and ``serve``.

    ``sender`` (tests): one injected Graph/BC sender behind the real
    ``GuardedTransport``; ``workbench_fetch`` (tests): the Workbench client's fetch;
    ``portal_options``: extra ``SourceMirror`` options."""

    def __init__(self, state_dir: str | Path, config: HostConfig | None = None, *,
                 environ: Mapping[str, str] | None = None, sender: Any = None,
                 workbench_fetch: Callable[..., Any] | None = None,
                 portal_options: Mapping[str, Any] | None = None,
                 clock: Callable[[], datetime] = _utc,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 disk_free: Callable[[Path], int] | None = None) -> None:
        from integrations.capture_mail import assert_outside_git

        self.state_dir = assert_outside_git(Path(state_dir).expanduser().resolve())
        self.config = config
        self.environ = os.environ if environ is None else environ
        self.sender = sender
        self.workbench_fetch = workbench_fetch
        self.portal_options = dict(portal_options or {})
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic   # the capture budgets' clock
        self.stop = threading.Event()
        # The continuous ingest mode's disk guard (``ingest_continuous``): free bytes of
        # the state directory's file system, stopped below ``min_free_bytes``; the build's
        # guard too. ``disk_free`` (tests): the measure, never the machine's real disk.
        self.disk_free: Callable[[Path], int] = (
            disk_free or (lambda path: shutil.disk_usage(path).free))
        self.min_free_bytes = MIN_FREE_BYTES
        (self.state_dir / HOST_DIR).mkdir(parents=True, exist_ok=True)
        with self._ledger() as db:
            for statement in _LEDGER_DDL:
                db.execute(statement)

    @contextmanager
    def _ledger(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.state_dir / HOST_DIR / LEDGER_FILE, timeout=30,
                             isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def _cycle_lock(self) -> Iterator[dict[str, int]]:
        """The state directory's one cycle lock, whatever the host's mode: a second cycle
        is refused (``host_cycle_running``). Taken, it first stops the capture children a
        killed holder left running (``CycleChildren.recover``) and yields those counts."""
        with open(self.state_dir / HOST_DIR / LOCK_FILE, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostError("host_cycle_running") from None
            try:
                yield self.children().recover(monotonic=self.monotonic, sleep=time.sleep)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def children(self) -> "CycleChildren":
        """The record of the running cycle's capture child processes (lock holder only)."""
        return CycleChildren(self.state_dir / HOST_DIR / CHILDREN_FILE)

    def _actor(self) -> Actor:
        return Actor(self.config.tenant, frozenset({self.config.principal}), "restricted")

    def _coworker(self) -> Actor | None:
        """The configured co-worker (``case_coworker``) as a reader the employee gate
        resolved, at the host's clearance; None when not configured."""
        coworker = self.config.case_coworker
        return None if coworker is None else Actor(
            self.config.tenant, coworker["principals"], "restricted")

    def _capture_audience(self) -> Any:
        from integrations.live_source_guard import LiveSourceAudience
        return LiveSourceAudience.from_file(self.config.capture_audience)

    def _graph(self) -> tuple[Any, Any]:
        from integrations.graph_read import build_graph
        from integrations.live_source_guard import BoundedBackoff
        return build_graph(self.environ, sender=self.sender,
                           retry=BoundedBackoff(sleep=self.sleep, budget_seconds=60))

    # -- sources ------------------------------------------------------------------

    def _portal(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import mirror_capture
        from integrations.source_mirror import MirrorError, load_ingest_config, mirror_options
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from .case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

        base_url = entry.get("base_url") or self.environ.get(URL_ENV)
        key = workbench_key(self.environ)
        if not base_url or not key:
            return {"state": "not_configured", "reasons": ["portal_url_or_key_unavailable"]}
        options = {} if self.workbench_fetch is None else {"fetch": self.workbench_fetch}
        client = WorkbenchReadApiClient(base_url, key, **options)
        working = self.state_dir / CASE_RUNTIME_DIR / MIRROR_FILE
        working.parent.mkdir(parents=True, exist_ok=True)
        fallback = self.state_dir / mirror_capture.STORE_DIR / "work" / "served.persisted.sqlite"
        store = mirror_capture.MirrorCaptureStore(self.state_dir)
        try:
            mirror, report = mirror_capture.sync_or_persisted(
                store, client, self.config.mirror_audience, self._actor(),
                relations=list(entry["relations"]), tenant=self.config.tenant,
                mirror_path=working, fallback_path=fallback,
                budget_seconds=entry.get("budget_seconds"),
                **{**mirror_options(load_ingest_config()), **self.portal_options})
        except MirrorError as exc:
            if exc.code not in ("missing_mirror", "persisted_incomplete"):
                raise
            # The copy is read only after a live outage, and there is none to serve.
            return {"state": "failed", "complete": False,
                    "outage": {"http_status": None, "error_code": exc.code},
                    "reasons": [f"live_source_unavailable:{exc.code}"]}
        mirror.close()
        counts = {"relations": report["relation_count"], "captured": report["captured_count"]}
        if report["served_from"] == "persisted_capture":
            fresh = report["source_freshness"]
            return {"state": "persisted_capture", "complete": True, "counts": counts,
                    "served_path": str(fallback), "stale_since": fresh["captures_as_of"],
                    "outage": {"http_status": fresh["http_status"],
                               "error_code": fresh["error_code"]},
                    "reasons": [f"live_source_unavailable:{fresh['http_status']}:"
                                f"{fresh['error_code']}"]}
        return {"state": "captured" if report["complete"] else "partial",
                "complete": report["complete"], "counts": counts, "served_path": str(working),
                "persisted": bool(report["persisted"].get("persisted"))}

    def _bc(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import bc_read
        from integrations.capture_store import CaptureStore
        from integrations.live_source_guard import BoundedBackoff

        api, transport, tenant_id, environment = bc_read.build_bc(
            self.environ, sender=self.sender, retry=BoundedBackoff(
                sleep=self.sleep, budget_seconds=bc_read.CAPTURE_RETRY_BUDGET_SECONDS))
        audience = self._capture_audience()
        reader = bc_read.BusinessCentralReader(api, audience, tenant_id=tenant_id,
                                               environment=environment)
        (self.state_dir / BC_DIR).mkdir(parents=True, exist_ok=True)
        store = CaptureStore(self.state_dir / BC_DIR / BC_FILE, audience)
        try:
            # no books file: the shipped map, which also names My Company, so its Page 50
            # purchase-order references (Your_Reference) are read; an injected map names none
            books = (bc_read.load_company_books(entry["books"]) if entry.get("books")
                     else None)
            report = bc_read.BcCapture(reader, store, books=books).capture(
                self._actor(), entity_sets=tuple(entry.get("entity_sets")
                                                 or bc_read.CAPTURE_ENTITY_SETS),
                budget_seconds=entry.get("budget_seconds"), mode="auto")
        finally:
            store.close()
        units = [u for c in report["companies"] for u in c["units"]]
        modes: dict[str, int] = {}
        for unit in units:
            if unit.get("mode"):
                modes[unit["mode"]] = modes.get(unit["mode"], 0) + 1
        outcome = {"state": "captured" if report["all_complete"] else "partial",
                   "complete": report["all_complete"], "modes": modes,
                   "reasons": [report["stopped_reason"]] if report["stopped_reason"] else [],
                   "counts": {"units": report["units_total"],
                              "units_reached": report["units_reached"],
                              "units_complete": report["units_complete"],
                              "units_not_exposed": report["units_not_exposed"],
                              "unpinned_companies": report["unpinned_companies"],
                              "pinned_companies_missing": report["pinned_companies_missing"],
                              "new_versions": sum(u.get("new_versions") or 0 for u in units),
                              "requests": transport.requests_sent}}
        if report["throttle"] is not None:  # a 429/503 that stopped the read part-way
            outcome["throttle"] = report["throttle"]
        return outcome

    def _mail(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import capture_mail
        from integrations.graph_read import GraphMailReader

        scope = capture_mail.CaptureScope.from_file(entry.get("scope")
                                                    or capture_mail.DEFAULT_SCOPE)
        api, _transport = self._graph()
        store = capture_mail.MailCaptureStore(self.state_dir)
        try:
            capture = capture_mail.MailCapture(
                store, GraphMailReader(api, self._capture_audience()), self._actor(),
                sleep=self.sleep, budget_seconds=entry.get("budget_seconds"),
                **capture_mail.PRODUCTION)
            run = capture.run(scope, directory=lambda: capture_mail.directory_mailboxes(
                api, scope.directory_allows))
        finally:
            store.close()
        folders = [f for m in run["mailboxes"] for f in m["folders"]]
        resumed: dict[str, int] = {}
        for folder in folders:
            how = folder.get("resumed_from") or "new_round"
            resumed[how] = resumed.get(how, 0) + 1
        reasons = sorted({f["reason"] for f in folders if f["reason"]}
                         | {m["status"] for m in run["mailboxes"]
                            if not m["folders"] and m["status"] != "complete"})
        outcome = {"state": "captured" if run["complete"] else "partial",
                   "complete": run["complete"], "resumed_from": resumed, "reasons": reasons,
                   "counts": {key: sum(f["counts"][key] for f in folders) for key in (
                       "pages", "messages_seen", "versions_new", "unchanged", "removed")}
                   | {"folders": len(folders),
                      "folders_complete": sum(1 for f in folders if f["complete"])}}
        # Every bound mailbox refused (a missing grant): the source's outage, as Teams'.
        return _with_refusals(outcome, capture_mail.refused_parts(run))

    def _sharepoint(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import capture_sharepoint as cs
        from integrations.graph_read import SharePointReader, SharePointSites, resolve_site_drive
        from integrations.live_source_guard import sharepoint_host_from_env

        scope = cs.CaptureScope.from_file(entry.get("scope") or cs.DEFAULT_SCOPE)
        audience = self._capture_audience()
        api, transport = self._graph()
        host = sharepoint_host_from_env(self.environ)
        readers: dict[str, Any] = {}

        def reader_for(drive_id: str) -> Any:
            if drive_id not in readers:
                readers[drive_id] = SharePointReader(api, audience, drive_id,
                                                     transport=transport,
                                                     read_permissions=True,
                                                     max_acl_reads=cs.MAX_ACL_READS)
            return readers[drive_id]

        def site_drive(name: str) -> str:
            if host is None:
                raise ValueError("site_host_unconfigured")
            return resolve_site_drive(api, host, name)

        store = cs.SharePointCaptureStore(self.state_dir)
        try:
            run = cs.SharePointCapture(
                store, reader_for, self._actor(), site_drive=site_drive,
                estate=None if host is None else SharePointSites(api, host),
                max_content_bytes=scope.max_content_bytes,
                budget_seconds=entry.get("budget_seconds"),
                monotonic=self.monotonic).run(scope)
        finally:
            store.close()
        roots = [r for t in run["targets"] for r in t["roots"]]
        counts: dict[str, int] = {"targets": len(run["targets"]), "roots": len(roots)}
        rounds: dict[str, int] = {}   # roots by round: full (walked) or incremental (feed)
        # Roots by where this cycle started them (the mail source's resumed_from):
        # walk_position (a cut walk resumed), drive_delta (the change feed followed),
        # new_walk, deferred (nothing read) or deferred_walk_position (nothing read, its
        # cut walk kept for a later cycle).
        resumed: dict[str, int] = {}
        for root in roots:
            rounds[root["round"]] = rounds.get(root["round"], 0) + 1
            how = ("deferred_walk_position" if root["round"] is None
                   and root.get("walk_position") is not None
                   else "deferred" if root["round"] is None
                   else "drive_delta" if root["round"] == "incremental"
                   else "walk_position" if root.get("full_walk_reason") == "resumed"
                   else "new_walk")
            resumed[how] = resumed.get(how, 0) + 1
            for key, value in root["counts"].items():
                if isinstance(value, int) and not isinstance(value, bool):
                    counts[key] = counts.get(key, 0) + value
        reasons = sorted({r["reason"] for r in roots if r.get("reason")}
                         | {t["status"] for t in run["targets"]
                            if not t["roots"] and t["status"] != "complete"}
                         | set((run.get("discovery") or {}).get("gaps", [])))
        return {"state": "captured" if run["complete"] else "partial",
                "complete": run["complete"], "reasons": reasons, "rounds": rounds,
                "resumed_from": resumed,
                # Roots whose cut walk the next cycle resumes (``cursor`` walk_position).
                "walk_positions_held": sum(1 for r in roots
                                           if r.get("cursor") == "walk_position"),
                "counts": counts}

    def _teams(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import capture_teams

        scope = capture_teams.TeamsScope.from_file(entry.get("scope")
                                                   or capture_teams.DEFAULT_SCOPE)
        audience = self._capture_audience()
        api, transport = self._graph()
        store = capture_teams.open_store(self.state_dir, audience)
        blocked: list[dict[str, Any]] = []
        try:
            capture = capture_teams.TeamsCapture(store, api, audience, self._actor(),
                                                 workers=capture_teams.WORKERS)
            # The CLI's gate (decision 2026-10-21-teams-host-confirmation-gate): the census
            # first (names and counts, no content), then content only of what the owner's
            # confirmation names and this census cleared; without one no message is read
            # and the source is blocked on that later ask, never defaulted.
            census, private = capture.census(scope, blocked)
            capture_teams.write_census(self.state_dir, private)
            confirmed, code = capture_teams.read_confirmation(self.state_dir)
            content_scope = (capture_teams.confirmed_scope(scope, private, confirmed)[0]
                             if confirmed is not None else None)
            if content_scope is None:
                # Everything confirmed is refused or gone now: the owner confirms again.
                blocked.append(capture_teams.confirmation_block(
                    code if confirmed is None else "content_confirmation_none_cleared"))
                run = None
            else:
                run = capture.run(content_scope)
        finally:
            store.close()
        cleared = {"channels_cleared": census["channels_cleared"],
                   "chats_cleared": census["chats_cleared"]}
        if run is None:
            outcome = {"state": "blocked", "complete": False,
                       "reasons": sorted({*census["reasons"], "later_ask:"
                                          + capture_teams.CONFIRMATION_LATER_ASK}),
                       "counts": dict.fromkeys(_TEAMS_COUNTS + ("conversations",
                                                               "conversations_complete"), 0)
                       | cleared | {"requests": transport.requests_sent}}
            return _with_refusals(outcome, blocked)
        conversations = run["conversations"]
        counts = {key: sum(c["counts"][key] for c in conversations) for key in _TEAMS_COUNTS}
        outcome = {"state": "captured" if run["complete"] else "partial",
                   "complete": run["complete"],
                   "reasons": sorted(set(run["discovery"]["reasons"])
                                     | {r for c in conversations for r in c["reasons"]}),
                   "counts": counts | {
                       "conversations": len(conversations),
                       "conversations_complete": run["complete_bound"],
                       "requests": transport.requests_sent} | cleared}
        return _with_refusals(outcome, blocked + [p for p in run["blocked_upstream"]
                                                  if p not in blocked])

    def _read(self, name: str) -> dict[str, Any]:
        entry = self.config.sources.get(name)
        if entry is None:
            return {"state": "not_configured", "reasons": []}
        try:
            return getattr(self, "_" + name)(entry)
        except Exception as exc:  # noqa: BLE001 - one source never stops the others
            failed = {"state": "failed", "complete": False, "reasons": [_reason(exc)]}
            outage = _outage(exc)
            return failed if outage is None else {**failed, "outage": outage}

    # -- one cycle ----------------------------------------------------------------

    def cycle(self) -> dict[str, Any]:
        """Read every configured source once, record each outcome and rebuild the
        projection. Counts only. Under the cycle lock (a killed holder's capture children
        stopped first), a ``running`` ledger row (an earlier one left running was killed:
        ``interrupted``), each source's outcome recorded (``_record``), the projection
        rebuilt, the row finished with the report. The sources are read by the host's own
        readers, or in ingest mode by their capture CLIs (``ingest_forever.read_sources``,
        each child recorded in ``children()`` while it runs)."""
        if self.config is None:
            raise HostError("host_config_required")
        if self.config.ingest is None:
            return self._cycle(lambda: {name: self._read(name) for name in SOURCES},
                               HOST_RUNNER)
        from . import ingest_forever

        return self._cycle(lambda: ingest_forever.read_sources(
            self.config.capture_sources, self.config.sources, self.state_dir, self.environ,
            grace=self.config.ingest["grace_seconds"], children=self.children(),
            parallel=ingest_forever.max_parallel(self.config.ingest,
                                                 len(self.config.sources))),
            INGEST_RUNNER)

    def _cycle(self, read: Callable[[], Mapping[str, Mapping[str, Any]]], runner: str
               ) -> dict[str, Any]:
        try:
            session_identity.bind_state_dir(self.state_dir, fixture=False)
        except session_identity.SessionIdentityRefused as error:
            raise HostError(error.reason) from None
        with self._cycle_lock() as recovered:
            cycle_id, started = self._open_cycle(runner)
            return self._finish(cycle_id, started, dict(read()), runner, recovered)

    def _open_cycle(self, runner: str) -> tuple[int, datetime]:
        """A ``running`` ledger row (one left running by a killed holder: ``interrupted``)."""
        started = self.clock()
        with self._ledger() as db:
            db.execute("UPDATE cycle SET state='interrupted' WHERE state='running'")
            cycle_id = db.execute(
                "INSERT INTO cycle(started_at, state, report) VALUES (?, 'running', ?)",
                (_iso(started), json.dumps({"runner": runner}))).lastrowid
        return cycle_id, started

    def _finish(self, cycle_id: int, started: datetime,
                outcomes: Mapping[str, Mapping[str, Any]], runner: str,
                recovered: Mapping[str, int], *, skip_build: str | None = None,
                extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Record each outcome (``_record``), build the next projection generation
        (``skip_build``: a reason not to, the previous generation stays served) and finish
        the cycle row with its report."""
        now = self.clock()
        with self._ledger() as db:
            for name, outcome in outcomes.items():
                self._record(db, name, outcome, now)
        if skip_build is not None:
            with self._ledger() as db:
                previous = db.execute("SELECT MAX(generation) FROM projection").fetchone()[0]
            projection = {"state": "skipped", "generation": previous,
                          "reasons": [skip_build]}
        else:
            projection = self._project(cycle_id)
        # Between refreshes, while no build runs (its snapshot is closed): every store's
        # WAL moves into its file and is cut back (``checkpoint_stores``).
        if projection["state"] == "built":
            # The scheduled proactive engine over the new generation (its gap inbox
            # beside the store, read by the console Home), every detector within its
            # time budget; never fails the cycle.
            projection["proactive"] = self._proactive()
        wal = self.checkpoint_stores()
        report = {"schema": CYCLE_SCHEMA, "cycle": cycle_id, "runner": runner,
                  "started_at": _iso(started),
                  "sources": {name: {k: v for k, v in outcome.items()
                                     if k != "served_path"}
                              for name, outcome in outcomes.items()},
                  "projection": projection, "wal": wal, **(extra or {})}
        if recovered.get("recorded"):
            report["recovered_children"] = dict(recovered)
        state = ("failed" if any(o["state"] == "failed" for o in outcomes.values())
                 or projection["state"] == "failed" else "complete")
        with self._ledger() as db:
            db.execute("UPDATE cycle SET finished_at=?, state=?, report=? WHERE "
                       "cycle_id=?", (_iso(self.clock()), state,
                                      json.dumps(report, sort_keys=True), cycle_id))
        return {**report, "state": state}

    def _proactive(self) -> dict[str, Any]:
        """The full scheduled engine run (``proactive_engine.refresh``) over the built
        projection, as the capture principal (``_actor``), inside the cycle lock after the
        build, with the live readers the host's sessions build (``_live_readers``), within
        ``proactive_engine.REFRESH_BUDGET_SECONDS``: counts and enums only, never raising."""
        from integrations.source_mirror import AudienceMap
        from . import case_workspace_live as live
        from . import proactive_engine

        store = projection_path(self.state_dir)

        def scheduled(as_of: Any, budget: Any) -> Mapping[str, Any]:
            runtime = live.offline_runtime(self.state_dir, self.config.mirror_audience,
                                           self.config.tenant, live=self._live_readers(),
                                           environ=self.environ)
            try:
                return proactive_engine.refresh(
                    runtime, self._actor(), AudienceMap.from_file(self.config.mirror_audience),
                    store, as_of, captures=self.state_dir,
                    scan_coverage="portal" in self.config.sources, budget=budget,
                    clock=self.clock)
            finally:
                runtime.close()

        return proactive_engine.after_build(store, clock=self.clock, scheduled=scheduled,
                                            monotonic=self.monotonic)

    def stop_reason(self, stop_file: Path) -> str | None:
        """Why the continuous ingest mode stops now: ``stopped`` (``stop`` set: a signal,
        the console closing), ``stop_file`` (the file exists) or ``low_disk`` (the state
        directory's file system has less than ``min_free_bytes`` free, or cannot say);
        None to go on."""
        if self.stop.is_set():
            return "stopped"
        if stop_file.exists():
            return "stop_file"
        try:
            free = self.disk_free(self.state_dir)
        except OSError:
            return "low_disk"   # unmeasured is never enough
        return "low_disk" if free < self.min_free_bytes else None

    def ingest_continuous(self, *, stop_file: Path, max_refreshes: int = 0,
                          on_refresh: Callable[[dict[str, Any]], None] | None = None
                          ) -> dict[str, Any]:
        """Ingest mode, continuously (``operating.ingest_forever.Continuous``): every
        configured source on its own cadence, ``ingest.max_parallel`` children at once,
        under the one cycle lock held for the whole run (a ``run``, ``check`` or other
        ``serve`` is refused meanwhile). Each time sources finish they are one ledger
        cycle (runner ``ingest``, ``continuous`` true): recorded, then the next
        projection generation built from every capture store as it is now (incremental:
        only the units whose captures changed) and published; readers keep the previous
        generation until it is. Stops on ``stop_file``, low disk (no build then) or
        ``stop``; returns the run's summary (counts and codes)."""
        if self.config is None or self.config.ingest is None:
            raise HostError("host_ingest_mode_required")
        from . import ingest_forever

        try:
            session_identity.bind_state_dir(self.state_dir, fixture=False)
        except session_identity.SessionIdentityRefused as error:
            raise HostError(error.reason) from None
        with self._cycle_lock() as recovered:
            first = [dict(recovered)]

            def refresh(outcomes: dict[str, dict[str, Any]], reason: str | None,
                        exclusion: dict[str, Any]) -> None:
                # No capture of a store this build reads runs now (``Continuous``): the
                # WAL each one left moves into its file before the build's snapshot.
                exclusion = {**exclusion, "checkpoint": self.checkpoint_written(
                    exclusion["exclusive"])}
                cycle_id, started = self._open_cycle(INGEST_RUNNER)
                report = self._finish(
                    cycle_id, started, outcomes, INGEST_RUNNER,
                    first.pop() if first else {},
                    skip_build="low_disk" if reason == "low_disk" else None,
                    extra={"continuous": True, "build_exclusion": exclusion})
                if on_refresh is not None:
                    on_refresh(report)

            summary = ingest_forever.Continuous(
                self.config.capture_sources, self.config.sources, self.state_dir,
                self.environ, grace=self.config.ingest["grace_seconds"],
                parallel=ingest_forever.max_parallel(self.config.ingest,
                                                     len(self.config.sources)),
                refresh=refresh, should_stop=lambda: self.stop_reason(stop_file),
                children=self.children(), max_refreshes=max_refreshes,
                wait_idle=self.stop.wait, build_reads=self.build_reads).run()
        return {"schema": CONTINUOUS_SCHEMA, **summary}

    def _record(self, db: sqlite3.Connection, name: str, outcome: Mapping[str, Any],
                now: datetime) -> None:
        row = db.execute("SELECT * FROM source_state WHERE source=?", (name,)).fetchone()
        generation = row["generation"] if row else 0
        captured_at = row["captured_at"] if row else None
        served_path = row["served_path"] if row else None
        state = outcome["state"]
        if state == "not_configured":
            values = (0, generation, captured_at, "none" if not generation else "capture",
                      None, row["attempted_at"] if row else None, None,
                      ",".join(outcome["reasons"]) or None, served_path)
        elif state in ("captured", "partial"):
            values = (1, generation + 1, _iso(now), "live", int(outcome["complete"]),
                      _iso(now), None, None, outcome.get("served_path", served_path))
        elif state == "persisted_capture":
            values = (1, generation, captured_at, "persisted_capture", 1, _iso(now),
                      outcome["stale_since"], ",".join(outcome["reasons"]),
                      outcome["served_path"])
        else:   # failed: the last generation stays served, stale since it was captured
            values = (1, generation, captured_at, "capture" if generation else "none",
                      row["complete"] if row else None, _iso(now), captured_at,
                      ",".join(outcome["reasons"]), served_path)
        db.execute("INSERT OR REPLACE INTO source_state VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (name, *values))
        db.execute("DELETE FROM source_blocked WHERE source=?", (name,))
        parts = blocked_parts(name, outcome)
        if parts:
            db.execute("INSERT INTO source_blocked VALUES (?,?)",
                       (name, json.dumps(parts, sort_keys=True)))

    def _project(self, cycle_id: int) -> dict[str, Any]:
        from ontology import project

        with self._ledger() as db:
            served = {r["source"]: r for r in db.execute("SELECT * FROM source_state")}
            previous = db.execute("SELECT MAX(generation) FROM projection").fetchone()[0]
        portal = served.get("portal")
        disk = self.build_disk()
        if disk["free_bytes"] is None or disk["free_bytes"] < disk["needed_bytes"]:
            return {"state": "skipped", "generation": previous, "reasons": ["disk_low"],
                    "disk": disk}
        with ExitStack() as stack:
            sources: dict[str, Any] = {}
            try:
                if portal is not None and portal["served_path"] \
                        and Path(portal["served_path"]).is_file():
                    sources["portal_api"] = stack.enter_context(
                        project.MirrorCaptures(portal["served_path"]))
                sources.update(state_captures(self.state_dir, stack,
                                              internal_domains=self._internal_domains()))
                if not sources:
                    return {"state": "no_captures", "generation": previous}
                built = build_projection(self.state_dir, sources, tenant=self.config.tenant,
                                         space=self._space_guard, checkpoint=self._disk_floor)
            except DiskLow as low:   # the guard stopped it: nothing of it is left on disk
                return {"state": "skipped", "generation": previous, "reasons": ["disk_low"],
                        "disk": low.disk}
            except Exception as exc:  # noqa: BLE001 - the previous generation stays served
                return {"state": "failed", "generation": previous, "reasons": [_reason(exc)]}
        inputs = {name: served[name]["generation"] for name in served}
        objects = built.get("counts", {}).get("objects", 0)
        with self._ledger() as db:
            db.execute("INSERT INTO projection VALUES (?,?,?,?,?,?)",
                       (cycle_id, _iso(self.clock()), previous, built["content_digest"],
                        objects, json.dumps(inputs, sort_keys=True)))
        return {"state": "built", "generation": cycle_id, "previous_generation": previous,
                "objects": objects, "sources": sorted(sources), **built["build"]}

    def build_disk(self, need: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The free bytes of the state directory's file system (None: it cannot say,
        never enough) and the bytes a build needs: ``need`` (a ``space_need`` estimate; by
        default the least the next build writes: an incremental one's seed, a cold one's
        measured size, nothing known for a store of a new format) plus
        ``BUILD_DISK_MARGIN_BYTES``."""
        from ontology.project_incremental import space_need

        try:
            served = projection_path(self.state_dir).stat().st_size
        except OSError:
            served = 0
        if need is None:
            try:
                need = space_need(self.state_dir / PROJECTION_DIR, dirty=())
            except (OSError, ValueError, sqlite3.Error):
                need = {"mode": "unknown", "needed_bytes": served}
        try:
            free: int | None = int(self.disk_free(self.state_dir))
        except OSError:
            free = None
        return {"free_bytes": free,
                "needed_bytes": int(need.get("needed_bytes") or 0) + BUILD_DISK_MARGIN_BYTES,
                "generation_bytes": served, "estimate": dict(need)}

    def _space_guard(self, need: Mapping[str, Any]) -> None:
        """``build_generation``'s ``space``: refuse a build its estimate does not fit."""
        disk = self.build_disk(need)
        if disk["free_bytes"] is None or disk["free_bytes"] < disk["needed_bytes"]:
            raise DiskLow(disk)

    def _disk_floor(self) -> None:
        """Between build batches: stop when free space falls below the margin."""
        try:
            free: int | None = int(self.disk_free(self.state_dir))
        except OSError:
            free = None
        if free is None or free < BUILD_DISK_MARGIN_BYTES:
            raise DiskLow({"free_bytes": free, "needed_bytes": BUILD_DISK_MARGIN_BYTES,
                           "estimate": {"mode": "running_floor"}})

    def store_paths(self) -> dict[str, Path]:
        """name -> every SQLite capture store file this state directory holds: the
        served Portal mirror (live or persisted copy), the case runtime's mirror and
        every ``state_capture_paths`` store (SharePoint: each of its stores)."""
        from ontology import project

        from .case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

        found: dict[str, Path] = {}
        with self._ledger() as db:
            row = db.execute("SELECT served_path FROM source_state WHERE source='portal'"
                             ).fetchone()
        if row is not None and row["served_path"]:
            found["portal"] = Path(row["served_path"])
        found["portal_mirror"] = self.state_dir / CASE_RUNTIME_DIR / MIRROR_FILE
        for name, path in state_capture_paths(self.state_dir).items():
            if name == "sharepoint":
                for index, store in enumerate(project.SharePointCaptures.stores(
                        self.state_dir)):
                    found[f"sharepoint{index or ''}"] = store
            else:
                found[name] = path
        unique: dict[str, Path] = {}
        seen: set[Path] = set()
        for name, path in found.items():
            if path.is_file() and path.resolve() not in seen:
                seen.add(path.resolve())
                unique[name] = path
        return unique

    def build_reads(self) -> list[Path]:
        """Every store file the next build may open, held or not: the Portal mirror the
        ledger serves, the host's working mirror (always: a Portal capture writes it and
        its next record serves it), and every ``state_capture_paths`` store (SharePoint:
        its drive and its list-item store). ``_project`` opens each one held, each with
        one read snapshot until the build ends."""
        from ontology import project

        from .case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

        paths = [self.state_dir / CASE_RUNTIME_DIR / MIRROR_FILE]
        with self._ledger() as db:
            row = db.execute("SELECT served_path FROM source_state WHERE source='portal'"
                             ).fetchone()
        if row is not None and row["served_path"]:
            paths.append(Path(row["served_path"]))
        for name, path in state_capture_paths(self.state_dir).items():
            paths.extend(project.SharePointCaptures.stores(self.state_dir)
                         if name == "sharepoint" else [path])
        return paths

    def checkpoint_written(self, names: Sequence[str]) -> dict[str, Any]:
        """``wal_checkpoint(TRUNCATE)`` of the stores the named capture sources write
        (``SourceSpec.writes``), before a build opens them: per source its state and WAL
        bytes before and after (names, never paths). Never raises."""
        from integrations.capture_store import checkpoint_file, sqlite_code

        out: dict[str, Any] = {}
        sources = self.config.capture_sources if self.config else {}
        for name in names:
            spec = sources.get(name)
            if spec is None or spec.writes is None:
                continue
            for index, path in enumerate(spec.writes(self.state_dir)):
                try:
                    result = checkpoint_file(path, wait_ms=CHECKPOINT_WAIT_MS)
                except (OSError, sqlite3.Error) as exc:
                    result = {"state": "failed", "error": (sqlite_code(exc) if isinstance(
                        exc, sqlite3.Error) else type(exc).__name__)}
                if result is not None:
                    out[f"{name}{index or ''}"] = {
                        k: result[k] for k in ("state", "error", "wal_bytes_before",
                                               "wal_bytes") if k in result}
        return out

    def checkpoint_stores(self) -> dict[str, Any]:
        """``wal_checkpoint(TRUNCATE)`` of every store (``store_paths``), each waiting at
        most ``CHECKPOINT_WAIT_MS``: counts by outcome and the WAL bytes before and after,
        in total and per store (names, never paths). Never raises: a store it cannot
        checkpoint is counted ``failed`` with its code."""
        from integrations.capture_store import checkpoint_file, sqlite_code

        out: dict[str, Any] = {"stores": 0, "truncated": 0, "busy": 0, "failed": 0,
                               "wal_bytes_before": 0, "wal_bytes": 0, "by_store": {}}
        try:
            paths = self.store_paths()
        except (OSError, sqlite3.Error) as exc:
            return {**out, "failed": 1, "error": type(exc).__name__}
        for name, path in paths.items():
            try:
                result = checkpoint_file(path, wait_ms=CHECKPOINT_WAIT_MS)
            except (OSError, sqlite3.Error) as exc:
                result = {"state": "failed", "error": (sqlite_code(exc) if isinstance(
                    exc, sqlite3.Error) else type(exc).__name__), "wal_bytes_before": 0,
                    "wal_bytes": 0}
            if result is None:
                continue
            state = result["state"] if result["state"] in ("truncated", "busy") \
                else "failed"
            out["stores"] += 1
            out[state] += 1
            out["wal_bytes_before"] += result["wal_bytes_before"]
            out["wal_bytes"] += result["wal_bytes"]
            out["by_store"][name] = {k: result[k] for k in
                                     ("state", "error", "wal_bytes_before", "wal_bytes")
                                     if k in result}
        return out

    def _internal_domains(self) -> Sequence[str]:
        from integrations import capture_mail
        entry = (self.config.sources.get("mail") if self.config else None) or {}
        try:
            return capture_mail.CaptureScope.from_file(
                entry.get("scope") or capture_mail.DEFAULT_SCOPE).internal_domains
        except (OSError, ValueError):
            return ()

    @contextmanager
    def served_tools(self, actor: Actor) -> Iterator[Any]:
        """The ontology host ``mcp`` serves over this state directory
        (``case_tools_host.serving_tools`` over the projection, the review store, the
        source-mirror audience, the live audience and the captures ``_live_arguments``
        passes), over a
        case runtime without the employee resolver: ``actor`` stands for a reader the
        employee gate already resolved, which is what ``policy_now`` receives when
        serving. Its live readers are those of the sources the config names
        (``live_sources``), as the MCP host and the console build them from
        ``--live-sources``: a BC-only host never reads a Graph credential."""
        from ontology.reviews import store_path
        from .build_steps import ReviewsIfPresent
        from integrations.people_exclusion import PeopleExclusion
        from .case_runtime import CaseRuntime
        from .case_tools_host import serving_tools

        _live_audience, mail, sharepoint = _served_captures(self)
        runtime = CaseRuntime(self.state_dir, client=None,
                              audience=self.config.mirror_audience,
                              tenant=self.config.tenant, live=self._live_readers(),
                              people_exclusion=PeopleExclusion.from_env(self.environ))
        try:
            served = serving_tools(runtime, actor,
                                   store_path=projection_path(self.state_dir),
                                   mail_capture_dir=mail, sharepoint_capture_dir=sharepoint,
                                   reviews=ReviewsIfPresent(store_path(self.state_dir)),
                                   portal_captured="portal" in self.config.sources,
                                   knowledge_namespaces=self.config.knowledge_namespaces,
                                   marking_grants=self.config.marking_grants,
                                   freshness_bounds=freshness_bounds(self.config))
            try:
                yield served
            finally:
                served.close()
        finally:
            runtime.close()

    def _live_readers(self) -> Any:
        """The live readers of the sources the config names (``live_sources``), from the
        live audience; None without one."""
        from .case_runtime import build_live_readers

        live_audience, _mail, _sharepoint = _served_captures(self)
        return None if live_audience is None else build_live_readers(
            live_audience, self.environ, graph_sender=self.sender, bc_sender=self.sender,
            sources=live_sources(self))

    def trim_check(self, withheld_principal: str = WITHHELD_PRINCIPAL, *,
                   sample: int = 200) -> dict[str, Any]:
        """The served projection read through ``served_tools``, under the policy a
        session's call is served with (``policy_now``, decided per reader now), as the
        capture principal and as ``withheld_principal``: the store's own read scope
        (granted audience units, held markings), so the trim sees what serving shows.
        Counts only: objects visible to each and, for the withheld reader, everything a
        sample of objects and identifiers leaks; and for the capture principal, everything
        a sample of the objects whose audience units its own policy withholds leaks
        (``capture_principal_denied_probed``: 0 when it is granted every unit)."""
        path = projection_path(self.state_dir)
        if not path.is_file():
            return {"state": "no_projection"}
        granted = self._actor()
        withheld = Actor(self.config.tenant, frozenset({withheld_principal}), "restricted")
        with self.served_tools(granted) as served:
            tools = served.tools
            reader = tools.store
            policy = served.policy_now(granted)
            with reader.lock:
                conn = reader.conn
                types = [r[0] for r in conn.execute("SELECT DISTINCT object_type FROM objects")]
                link_types = [r[0] for r in conn.execute(
                    "SELECT DISTINCT link_type FROM links")]
                objects = [r[0] for r in conn.execute(
                    "SELECT object_id FROM objects ORDER BY object_id LIMIT ?", (sample,))]
                identifiers = [r[0] for r in conn.execute(
                    "SELECT DISTINCT normalized FROM identifiers ORDER BY normalized LIMIT ?",
                    (sample,))]
                denied_keys = [key for (key,) in conn.execute(
                    "SELECT DISTINCT audience_key FROM objects")
                    if not policy.may_read(granted, key)]
                denied = [r[0] for r in conn.execute(
                    "SELECT object_id FROM objects WHERE audience_key IN (SELECT value FROM "
                    "json_each(?)) ORDER BY object_id LIMIT ?", (json.dumps(denied_keys), sample))]
                denied_identifiers = [r[0] for r in conn.execute(
                    "SELECT DISTINCT normalized FROM identifiers WHERE object_id IN (SELECT "
                    "value FROM json_each(?)) ORDER BY normalized LIMIT ?",
                    (json.dumps(denied), sample))]

            def scope(actor: Actor) -> Any:
                tools.policy = served.policy_now(actor)
                return tools._scope(actor, tools._as_known_at({}))

            def count(actor: Actor) -> int:
                seen, read = 0, scope(actor)
                with reader.lock:
                    for object_type in types:
                        after = ""
                        while True:
                            rows = reader.objects_of_type(read, object_type, after,
                                                          TRIM_PAGE)
                            seen += len(rows)
                            if len(rows) < TRIM_PAGE:
                                break
                            after = rows[-1]["object_id"]
                return seen

            def leaks(actor: Actor, ids: Sequence[str], values: Sequence[str], *,
                      among: Sequence[str] | None = None) -> int:
                """What ``actor`` gets back of ``ids`` and of the identifier ``values``
                (the rows of objects in ``among``; None: every row)."""
                read, inside = scope(actor), None if among is None else set(among)
                with reader.lock:
                    shown = sorted({r["object_id"] for r in reader.visible_versions(read, ids)})
                    found = reader.identifiers_by_value(read, values)
                    return (len(shown) + len(reader.readable_values(read, shown))
                            + len(reader.identifiers_of(read, shown))
                            + len(reader.links_touching(read, shown, link_types))
                            + reader.conn.execute(
                                "SELECT COUNT(DISTINCT e.event_id) FROM events e JOIN "
                                "event_objects eo ON eo.event_id = e.event_id WHERE "
                                "eo.object_id IN (SELECT value FROM json_each(:ids)) AND "
                                "e.markings IN (SELECT value FROM json_each(:marks))",
                                read.params(ids=json.dumps(shown))).fetchone()[0]
                            + sum(1 for r in found
                                  if inside is None or r["object_id"] in inside))

            visible, hidden = count(granted), count(withheld)
            withheld_leaks = leaks(withheld, objects, identifiers)
            granted_leaks = leaks(granted, denied, denied_identifiers, among=denied)
        return {"state": "checked", "object_types": len(types),
                "capture_principal_objects": visible, "withheld_principal_objects": hidden,
                "withheld_leaks": withheld_leaks, "objects_probed": len(objects),
                "identifiers_probed": len(identifiers),
                "capture_principal_denied_probed": len(denied),
                "capture_principal_leaks": granted_leaks}

    # -- status and serving -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Per configured source: generation, capture time and age, how it is served now
        and since when it is stale; the projection generation; the last cycle. Only the
        configured sources are listed (the held config's, else those the last cycle
        recorded as configured, one not run yet at generation 0); a known source no
        configuration reads is named in ``not_configured``, never a status entry. With no
        config held the list is the last cycle's view: a config edit shows from the next
        cycle, and a source that last ran without credentials reads not configured."""
        now = self.clock()
        with self._ledger() as db:
            rows = {r["source"]: r for r in db.execute("SELECT * FROM source_state")}
            built = db.execute("SELECT * FROM projection ORDER BY generation DESC "
                               "LIMIT 1").fetchone()
            last = db.execute("SELECT cycle_id, started_at, finished_at, state FROM cycle "
                              "ORDER BY cycle_id DESC LIMIT 1").fetchone()
        configured = (set(self.config.sources) if self.config is not None else
                      {name for name, row in rows.items() if row["configured"]})
        sources = []
        # The host's sources, then any other ingest mode configures (SharePoint lists,
        # Mission Control).
        for name in (*SOURCES, *sorted(configured - set(SOURCES))):
            row = rows.get(name)
            if name not in configured:
                continue
            if row is None:
                sources.append({"source": name, "configured": True, "generation": 0,
                                "served_from": "none", "complete": None})
                continue
            sources.append({"source": name, "configured": True,
                            "generation": row["generation"], "captured_at": row["captured_at"],
                            "age_seconds": _age(now, row["captured_at"]),
                            "served_from": row["served_from"],
                            "complete": None if row["complete"] is None else bool(row["complete"]),
                            "stale_since": row["stale_since"],
                            "last_attempt_at": row["attempted_at"], "last_error": row["error"]})
        return {"schema": STATUS_SCHEMA, "now": _iso(now), "sources": sources,
                "not_configured": [name for name in SOURCES if name not in configured],
                "projection": None if built is None else {
                    "generation": built["generation"], "built_at": built["built_at"],
                    "age_seconds": _age(now, built["built_at"]), "objects": built["objects"],
                    "inputs": json.loads(built["inputs"])},
                "last_cycle": None if last is None else dict(last)}

    def serve_cycle(self) -> dict[str, Any]:
        """``cycle``, or, while another process holds this directory's cycle (a ``run``,
        another ``serve``, whatever its mode), a typed ``waiting`` report: the next
        interval tries again, the serving host never stops for it."""
        try:
            return self.cycle()
        except HostError as error:
            if error.code != "host_cycle_running":
                raise
            return {"schema": CYCLE_SCHEMA, "cycle": None,
                    "state": "waiting", "reason": error.code,
                    "started_at": _iso(self.clock())}

    def serve(self, *, interval_seconds: float, max_cycles: int = 0,
              on_cycle: Callable[[dict[str, Any]], None] | None = None,
              stop_file: Path | None = None) -> int:
        """Cycles every ``interval_seconds`` until ``stop`` is set (or ``max_cycles``
        attempts: a ``waiting`` one counts). In ingest mode: the continuous mode
        (``ingest_continuous``, ``max_cycles`` its refreshes) until ``stop_file``
        (default ``<DIR>/host/ingest.stop``), low disk or ``stop``; while another process
        holds the cycle lock it reports ``waiting`` and tries again each interval."""
        if self.config is not None and self.config.ingest is not None:
            stop_file = stop_file or self.state_dir / HOST_DIR / STOP_FILE
            while True:
                try:
                    summary = self.ingest_continuous(stop_file=stop_file,
                                                     max_refreshes=max_cycles,
                                                     on_refresh=on_cycle)
                except HostError as error:
                    if error.code != "host_cycle_running":
                        raise
                    if on_cycle is not None:
                        on_cycle({"schema": CYCLE_SCHEMA, "cycle": None, "state": "waiting",
                                  "reason": error.code, "started_at": _iso(self.clock())})
                    if max_cycles == 1 or self.stop.wait(interval_seconds) \
                            or self.stop_reason(stop_file):
                        return 0
                    continue
                if on_cycle is not None:
                    on_cycle(summary)
                return summary["refreshes"]
        done = 0
        while not self.stop.is_set():
            report = self.serve_cycle()
            done += 1
            if on_cycle is not None:
                on_cycle(report)
            if max_cycles and done >= max_cycles:
                break
            self.stop.wait(interval_seconds)
        return done


def _with_refusals(outcome: dict[str, Any], parts: Sequence[Mapping[str, Any]]
                   ) -> dict[str, Any]:
    """``outcome`` with the parts its source refused (``capture_mail.graph_block``
    shapes) and, when there are any, the first as the source's ``outage``."""
    if not parts:
        return outcome
    first = parts[0]
    return {**outcome, "blocked_upstream": [dict(p) for p in parts],
            "outage": {"http_status": first.get("http_status"),
                       "error_code": first.get("error_code")}}


def blocked_parts(name: str, outcome: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The live verifier's ``blocked_upstream`` parts of source ``name``'s ``outcome``:
    the parts its capture names as refused, else one for the source when it did not
    answer (``outage``), else none. The one rule the host ledger records and the case
    opener reads (``case_workspace_live.host_refusals``)."""
    if outcome.get("blocked_upstream"):
        return [dict(p) for p in outcome["blocked_upstream"]]
    if "outage" in outcome:
        return [{"part": name, "source": outcome.get("upstream") or _UPSTREAM[name],
                 **outcome["outage"]}]
    return []


def _outage(exc: BaseException) -> dict[str, Any] | None:
    """A source that did not answer (unreachable, 5xx, 429, a refused credential): the
    HTTP status when there was one and a code; None for any other failure."""
    from integrations.live_source_guard import (RequestBudgetExhausted, SourceNotFound,
                                                SourceUnavailable, unavailable_detail)
    from integrations.mirror_capture import is_outage
    from integrations.source_mirror import MirrorError

    if isinstance(exc, MirrorError):
        return ({"http_status": exc.status, "error_code": exc.upstream_code or exc.code}
                if is_outage(exc.code, exc.status) else None)
    if not isinstance(exc, SourceUnavailable) \
            or isinstance(exc, (SourceNotFound, RequestBudgetExhausted)):
        return None
    detail = unavailable_detail(exc) or "unavailable"
    status = detail.rpartition("http_")[2]
    return {"http_status": int(status) if status.isdigit() else None, "error_code": detail}


def projection_row_check(path: Path, text: str, *, limit: int = 200_000) -> dict[str, Any]:
    """Self-check that ``text`` (a serialized check output) holds no projected row data:
    no object id or native key, L1 handle, identifier or text/identifier value of at
    least 8 characters (dates, numbers and enums are not row data on their own, nor is one
    lowercase word such as a status, which the output's own states share)."""
    if not path.is_file():
        return {"values_checked": 0, "leaked": 0}
    values: set[str] = set()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for object_id, native_key in conn.execute(
                "SELECT object_id, native_key FROM objects LIMIT ?", (limit,)):
            key = json.loads(native_key)
            values.add(object_id)
            values.update(v for v in (key if isinstance(key, list) else [key])
                          if isinstance(v, str))
        for query in ("SELECT l1_handle FROM object_versions LIMIT ?",
                      "SELECT raw FROM identifiers LIMIT ?",
                      "SELECT normalized FROM identifiers LIMIT ?",
                      "SELECT value FROM object_values WHERE value_type IN ('text', "
                      "'identifier') AND value IS NOT NULL LIMIT ?"):
            values.update(row[0] for row in conn.execute(query, (limit,)))
    finally:
        conn.close()
    checked = [value for value in values if isinstance(value, str) and len(value) >= 8
               and _LOWER_WORD.fullmatch(value) is None]
    return {"values_checked": len(checked),
            "leaked": sum(1 for value in checked if value in text)}


def cycles_check(make_host: Callable[[], "Host"], *, cycles: int = 2,
                 withheld_principal: str = WITHHELD_PRINCIPAL,
                 interrupt_after: float | None = None,
                 spawn: Callable[[], subprocess.Popen] | None = None) -> dict[str, Any]:
    """``cycles`` capture cycles, each by a new host over the same state directory, and
    what they must keep (module docstring, ``check``). With ``interrupt_after``, a cycle
    run by ``spawn`` (a child ``run`` process) is killed between the first and the second
    and a probe case is opened before it and re-read after the last. Counts, states and
    codes only."""
    if not 2 <= cycles <= 10:
        raise HostError("host_check_cycles_invalid")
    if interrupt_after is not None and (interrupt_after <= 0 or spawn is None):
        raise HostError("host_check_interrupt_invalid")
    reports: list[dict[str, Any]] = []
    committed: list[int | None] = []   # the generation committed before each cycle ran
    interrupt = probe = None
    carried = False   # observed: a capture store this check found, not one it made
    for index in range(cycles):
        if index == 1 and interrupt_after is not None:
            probe = _probe_case(make_host())
            interrupt = _killed_cycle(make_host(), spawn, interrupt_after)
        host = make_host()
        if index == 0:
            carried = _store_carried(host.state_dir)
        committed.append(_committed_generation(host))
        reports.append(host.cycle())
    host = make_host()
    status = host.status()
    with host._ledger() as db:
        interrupted = db.execute("SELECT COUNT(*) FROM cycle WHERE state='interrupted'"
                                 ).fetchone()[0]
    first, last = reports[0]["projection"], reports[-1]["projection"]
    configured = set(host.config.sources)
    checks = {
        "cycles_run": len(reports),
        "cycles_interrupted": interrupted,
        "sources_configured": len(configured),
        "sources_not_run": sum(1 for r in reports for name in configured
                               if r["sources"][name]["state"] == "not_configured"),
        # Ingest mode keeps no folder or walk positions in its reports: not measured.
        "mail_folders_restarted": None if host.config.ingest else
        sum(_mail_restarted(r) for r in reports[1:]),
        "sharepoint_roots_restarted": None if host.config.ingest else
        sum(_sharepoint_restarted(earlier, later)
            for earlier, later in zip(reports, reports[1:])),
        # Each later cycle builds on the generation committed when it started, never
        # older than the cycle before it built (a killed cycle may commit one between).
        "projection_chained": all(
            earlier["projection"].get("generation") is not None and before is not None
            and later["projection"].get("previous_generation") == before
            and before >= earlier["projection"]["generation"]
            for earlier, later, before in zip(reports, reports[1:], committed[1:])),
        "later_rebuilds_fewer": first.get("state") == last.get("state") == "built"
        and last.get("mode") == "incremental"
        and last.get("units_rebuilt", 0) < first.get("units_rebuilt", 0),
        # Every cycle of the ledger, by either runner (the host's readers or its ingest
        # mode): none started before another finished.
        **cycle_overlaps(host),
    }
    document = {"schema": CHECK_SCHEMA, "cycles": reports, "status": status,
                "checks": checks, "trim": host.trim_check(withheld_principal)}
    ingest_blocked: list[dict[str, Any]] = []
    if host.config.ingest is not None:
        from . import ingest_forever

        added = ingest_forever.cycle_checks(
            reports, sorted(configured), host.config.capture_sources, host.state_dir,
            host.environ, internal_domains=host._internal_domains())
        checks.update({key: value for key, value in added["checks"].items()
                       if key not in checks})
        checks["state_persistent"] = not ingest_forever.ephemeral(host.state_dir)
        document["duplicates"] = added["duplicates"]
        ingest_blocked = added["blocked"]
    if interrupt is not None:
        resumed = reports[1]
        checks["resumed_after_interrupt"] = (
            interrupt["state"] == "killed_running" and resumed["state"] == "complete"
            and committed[1] is not None
            and resumed["projection"].get("previous_generation") == committed[1]
            and _mail_restarted(resumed) == 0)
        document["interrupt"] = {key: interrupt[key] for key in
                                 ("signal", "after_seconds", "killed_running", "state")}
        document["case_resume"] = _resumed_case(make_host(), probe, withheld_principal)
    blocked: list[dict[str, Any]] = []
    for report in reports:
        for name, outcome in report["sources"].items():
            if host.config.ingest is not None and outcome.get("blocked_upstream"):
                parts = outcome["blocked_upstream"]   # its capture's typed parts
            else:
                parts = [{"part": name, "source": outcome.get("upstream") or _UPSTREAM[name],
                          **outcome["outage"]}] if "outage" in outcome else []
            blocked.extend(part for part in parts if part not in blocked)
    blocked.extend(part for part in ingest_blocked if part not in blocked)
    if blocked:
        document["blocked_upstream"] = blocked
    if "sharepoint" in configured:
        try:
            document["sharepoint_coverage"] = sharepoint_coverage(host)
        except Exception as exc:  # noqa: BLE001 - not measured is never measured complete
            document["sharepoint_coverage"] = {"census_complete": False, "complete": False,
                                               "reasons": [_reason(exc)]}
        # Ingest mode types its own parts (ingest_forever.cycle_checks): unchanged.
        cut = [] if host.config.ingest is not None else coverage_cut_block(
            document, carried=carried or bool(
                (document["sharepoint_coverage"].get("census") or {}).get("carried")))
        if cut:
            document["blocked_upstream"] = [*document.get("blocked_upstream", []), *cut]
    document["row_data_check"] = projection_row_check(
        projection_path(host.state_dir), json.dumps(document, sort_keys=True))
    return document


def _store_carried(state_dir: Path) -> bool:
    """A capture store (SharePoint or mail) in ``state_dir`` before the check ran: the
    observed fact that this state is kept between runs."""
    from integrations import capture_mail, capture_sharepoint

    return capture_sharepoint.store_exists(state_dir) or capture_mail.store_exists(state_dir)


def coverage_cut_block(document: Mapping[str, Any], *, carried: bool) -> list[dict[str, Any]]:
    """The ``blocked_upstream`` parts (cause ``later_ask`` live-host-persistent-state-dir)
    of a SharePoint check whose only shortfall is a cut by the time budget, of the census
    or of the capture still under way, over a state this check did not find kept
    (``carried`` false): one rule for every capture check,
    ``capture_mail.budget_cut_block``. Any other shortfall (a scope bound, a gap, a
    missed or unseen file, a failed cycle, coverage not measured), or a cut over a
    carried state that resumes by itself, yields none: the check fails, never blocked.
    Part names and enums only."""
    from integrations.capture_mail import budget_cut_block

    coverage = document.get("sharepoint_coverage") or {}
    units: list[tuple[str, list[str]]] = []
    for index, report in enumerate(document.get("cycles") or []):
        state = (report.get("sources", {}).get("sharepoint") or {}).get("state")
        if state not in ("captured", "partial"):
            units.append((f"sharepoint.cycle{index}", [f"cycle_{state}"]))
    if "census" not in coverage:
        units.append(("sharepoint.coverage", ["not_measured"]))
    units += [("sharepoint.census", [reason]) for reason in coverage.get("reasons") or []]
    units += [("sharepoint.capture", [reason])
              for reason in coverage.get("capture_incomplete") or []]
    if coverage.get("capture_pending"):
        units.append(("sharepoint.capture", list(coverage["capture_pending"])))
    for key in ("files_missed", "files_current_not_seen"):
        if coverage.get(key):
            units.append((f"sharepoint.{key}", [key]))
    if coverage.get("files_not_captured") and not (
            coverage.get("capture_pending") or coverage.get("capture_incomplete")
            or coverage.get("files_missed")):
        units.append(("sharepoint.capture", ["files_not_captured"]))
    return budget_cut_block(units, carried=carried)


def _mail_restarted(report: Mapping[str, Any]) -> int:
    return report["sources"].get("mail", {}).get("resumed_from", {}).get("new_round", 0)


def _sharepoint_restarted(earlier: Mapping[str, Any], later: Mapping[str, Any]) -> int:
    """Roots whose walk ``earlier`` cut (a stored walk position) that ``later`` neither
    resumed nor kept deferred: a walk that started over instead of resuming."""
    before = earlier["sources"].get("sharepoint", {})
    after = later["sources"].get("sharepoint", {}).get("resumed_from", {})
    if "walk_positions_held" not in before:
        return 0   # not run, or failed: no walk position to resume
    return max(0, before["walk_positions_held"] - after.get("walk_position", 0)
               - after.get("deferred_walk_position", 0))


def _committed_generation(host: Host) -> int | None:
    with host._ledger() as db:
        return db.execute("SELECT MAX(generation) FROM projection").fetchone()[0]


def _killed_cycle(host: Host, spawn: Callable[[], subprocess.Popen],
                  after: float) -> dict[str, Any]:
    """Start ``spawn`` (a ``run`` child over ``host``'s state directory, leading its own
    process group) and SIGKILL it after ``after`` seconds: the whole group, so no OCR child
    it started outlives it, and on any other exit too (``child_process.stop``). ``state``: ``killed_running`` (its cycle was running when it
    died), ``killed_before_cycle`` (killed before it started one) or
    ``finished_before_kill`` (it exited first: nothing was interrupted)."""
    with host._ledger() as db:
        before = db.execute("SELECT COALESCE(MAX(cycle_id), 0) FROM cycle").fetchone()[0]
    child = spawn()
    sent = None
    try:
        child.wait(timeout=after)
    except subprocess.TimeoutExpired:
        sent = "SIGKILL"
    finally:
        child_process.stop(child)
    with host._ledger() as db:
        row = db.execute("SELECT state FROM cycle WHERE cycle_id > ? ORDER BY cycle_id DESC "
                         "LIMIT 1", (before,)).fetchone()
    state = ("finished_before_kill" if sent is None else
             "killed_running" if row is not None and row["state"] == "running" else
             "killed_before_cycle")
    return {"signal": sent, "after_seconds": after, "state": state,
            "killed_running": state == "killed_running"}


def probe_state(state_dir: Path) -> bool:
    """Whether ``check --interrupt-after-seconds`` marked ``state_dir`` as its own."""
    try:
        marker = json.loads((state_dir / PROBE_STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get("schema") == PROBE_STATE_SCHEMA


def claim_probe_state(state_dir: Path) -> None:
    """Mark ``state_dir`` as a probe directory before the check writes into it: one absent
    or empty, or one already marked. Anything else is real state a probe case would be
    left in, visible to whoever is granted that state: refused."""
    if probe_state(state_dir):
        return
    if state_dir.exists() and (not state_dir.is_dir() or any(state_dir.iterdir())):
        raise HostError("host_check_probe_on_real_state")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / PROBE_STATE_FILE).write_text(
        json.dumps({"schema": PROBE_STATE_SCHEMA}), "utf-8")


@contextmanager
def _probe_action_path(host: Host, runtime: Any) -> Iterator[tuple[Any, Any, Any]]:
    """``plx.act`` for the probe case in test mode only: the state directory's action store
    (the one ``mcp`` serves with ``action_path``), with no call approver, MC operator or
    roster, so nothing is approved, requested or dispatched; a fixture session (catalog
    ``hosts: ["test"]`` capabilities only); and the isolated owner
    (``goal2_exec_owner.IsolatedOwnerSystem``, never a PLX system, its SQLite state in the
    action store's directory, read by the capture principal and the ``case_coworker``
    group) that resolves its ``fixture://`` citations on this runtime alone while the
    probe reads. Yields ``(owner, path, act)``."""
    from .action_path import ActionPath
    from .action_tools import ActTool
    from .goal2_exec_owner import IsolatedOwnerSystem
    from .mcp_server import ACTION_PATH_DIR, ACTION_PATH_FILE

    store = host.state_dir / ACTION_PATH_DIR
    coworker = host.config.case_coworker
    owner = IsolatedOwnerSystem(store / PROBE_OWNER_FILE, readers=frozenset(
        {host.config.principal, *(() if coworker is None else (coworker["group"],))}))
    served = runtime.resolve_citation
    runtime.resolve_citation = lambda handle, actor: (
        owner.resolve(handle, actor) if handle.startswith("fixture://")
        else served(handle, actor))
    try:
        path = ActionPath(store / ACTION_PATH_FILE, runtime.cases)
        actor = host._actor()
        session = session_identity.fixture_session(
            actor.tenant, actor.principals, channel=session_identity.CHANNEL_MCP,
            clearance=actor.clearance, subject_email=PROBE_SUBJECT)
        yield owner, path, ActTool(runtime, path, test_capabilities=True, session=session)
    finally:
        del runtime.resolve_citation


def _park_intent(host: Host, runtime: Any, actor: Actor, case_id: str) -> dict[str, Any]:
    """One ``plx.act`` intent parked on the probe case: an isolated-owner order record and
    the inbound record whose cited field supplies the value (``goal2_exec``'s typed owner),
    a ``fixture.record.set_cited_value`` proposal with compare-and-set preconditions,
    registered through ``plx.act`` (mode propose). It stays ``proposed``: no approval,
    never dispatched."""
    from .goal2_exec import (CITED_VALUE_CAPABILITY, SOURCE_FIELDS, SOURCE_RELATION,
                             TYPED_FIELDS, TYPED_RELATION, VERSION_FIELD)

    with _probe_action_path(host, runtime) as (owner, path, act):
        owner.define(TYPED_RELATION, TYPED_FIELDS, version_field=VERSION_FIELD)
        owner.define(SOURCE_RELATION, SOURCE_FIELDS, version_field=VERSION_FIELD)
        key = {"id": uuid4().int % 1_000_000_000}
        target = owner.seed(TYPED_RELATION, key, {**dict.fromkeys(TYPED_FIELDS), **key})
        source = owner.seed(SOURCE_RELATION, key, {**key, "customer_po": "PO-HOST-CHECK",
                                                   "lines_stated": "1"})
        value = owner.resolve(source, actor)["row"]["customer_po"]
        row = owner.resolve(target, actor)["row"]
        proposal = path.record_proposal(
            actor, case_id, CITED_VALUE_CAPABILITY,
            {"field": "po_reference", "value": value, "evidence_citation": source,
             "evidence_field": "customer_po"},
            [{"check": "field_empty", "citation": target, "field": "po_reference"},
             {"check": "field_equals", "citation": target, "field": VERSION_FIELD,
              "value": row[VERSION_FIELD]}],
            citations=[source, target], session=act.session,
            expected_effect={"relation": TYPED_RELATION, "key": key,
                             "values": {"po_reference": value}})
        intent = act({"mode": "propose", "case_id": case_id,
                      "proposal_id": proposal["proposal_id"]}, actor)
    if intent["dispatched"] or intent["intent"]["state"] != "proposed":
        raise HostError("host_check_intent_not_parked")
    return {"intent_id": intent["intent"]["intent_id"], "target": target, "row": row}


def _probe_case(host: Host) -> dict[str, Any]:
    """Open a probe case as the capture principal through the served tools: one
    interpretation finding, one fact citing the first readable projected object whose
    citation resolves, one ``plx.act`` intent parked on it (``_park_intent``) and a claim
    for a probe holder. With ``case_coworker`` the case's audience is the capture
    principal and the group (opened as the capture principal holding the group: the case
    store keeps only requested principals the opener holds). What ``_resumed_case``
    compares."""
    from .cases import MAX_LEASE_SECONDS, actor_label

    granted = host._actor()
    try:
        with host.served_tools(granted) as served:
            cases, tools = served.runtime.cases, served.tools
            tools.policy = served.policy_now(granted)
            read, reader = tools._scope(granted, tools._as_known_at({})), tools.store
            with reader.lock:
                types = [r[0] for r in reader.conn.execute(
                    "SELECT DISTINCT object_type FROM objects ORDER BY object_type")]
                ids = [row["object_id"] for object_type in types
                       for row in reader.objects_of_type(read, object_type, "",
                                                         PROBE_CANDIDATES)]
                handles = [row["l1_handle"] for row in
                           reader.visible_versions(read, ids[:PROBE_CANDIDATES])]
            coworker = host.config.case_coworker
            audience = None if coworker is None else (host.config.principal,
                                                      coworker["group"])
            opener = granted if coworker is None else Actor(
                granted.tenant, frozenset(audience), granted.clearance)
            case_id = cases.open(opener, "host check: does this case survive a killed "
                                         "capture cycle and a restart?", audience=audience)
            cases.finding(granted, case_id, "Probe: the host keeps a case across a restart.",
                          "interpretation", "unknown")
            for handle in handles:
                try:
                    cases.finding(granted, case_id, "Probe: a readable projected object.",
                                  "fact", "supported", [handle])
                    break
                except Exception:  # noqa: BLE001 - this citation does not resolve: the next
                    continue
            else:
                return {"reasons": ["no_readable_citation"]}
            parked = _park_intent(host, served.runtime, granted, case_id)
            holder = {"session": hashlib.sha256(
                f"host-check:{uuid4().hex}".encode()).hexdigest(),
                "subject": actor_label(granted)}
            cases.claim(granted, case_id, holder, MAX_LEASE_SECONDS)
            events = cases.get(granted, case_id)["integrity"]["events"]
    except Exception as exc:  # noqa: BLE001 - a probe that fails is reported, never passed
        return {"reasons": [_reason(exc)]}
    return {"case_id": case_id, "holder": holder, "events": events, "intent": parked}


def _resumed_case(host: Host, probe: Mapping[str, Any], withheld_principal: str
                  ) -> dict[str, Any]:
    """The probe case re-read through a new host's served tools, and its parked intent
    through ``plx.act`` (mode status) and the isolated owner's record: still ``proposed``
    and the case's only intent (``intent_parked``), its event chain verified, never
    dispatched (no dispatch recorded and the owner record unchanged), its state
    (``intent_state``) and its approval state (``approval_state``; ``none``: no per-call
    approval, gate or decision). With ``case_coworker``, the same case, its claim and
    the intent re-read through a new served session as the co-worker
    (``_coworker_read``). Booleans, counts and states."""
    from .action_path import IntentNotFound
    from .cases import FINDING_STATUSES, CaseNotFound
    from .mcp_server import InputError

    result: dict[str, Any] = {"opened_before_restart": "case_id" in probe, "chain_ok": False,
                              "findings_readable": 0, "citations_current": False,
                              "seq_unchanged": False, "claim_visible": False,
                              "withheld_visible": None, "intent_parked": False,
                              "intent_chain_ok": False, "intent_dispatched": None,
                              "intent_state": None, "approval_state": None}
    if "case_id" not in probe:
        return {**result, "reasons": list(probe["reasons"])}
    granted = host._actor()
    withheld = Actor(host.config.tenant, frozenset({withheld_principal}), "restricted")
    parked = probe["intent"]
    try:
        with host.served_tools(granted) as served:
            cases = served.runtime.cases
            view = cases.get(granted, probe["case_id"])
            try:
                cases.get(withheld, probe["case_id"])
                result["withheld_visible"] = 1
            except CaseNotFound:
                result["withheld_visible"] = 0
            with _probe_action_path(host, served.runtime) as (owner, path, act):
                listed = act({"mode": "status", "case_id": probe["case_id"]}, granted)
                try:
                    path.view(withheld, parked["intent_id"])
                    result["withheld_visible"] += 1
                except IntentNotFound:
                    pass
                try:
                    intent = act({"mode": "status", "intent_id": parked["intent_id"]},
                                 granted)["intent"]
                except InputError:   # lost (intent_not_found) or its chain broken
                    intent = None
                row = owner.resolve(parked["target"], granted)["row"]
    except Exception as exc:  # noqa: BLE001 - not re-read is never resumed
        return {**result, "reasons": [_reason(exc)]}
    if host.config.case_coworker is not None:
        result.update(_coworker_read(host, probe))
    readable = [item for status in FINDING_STATUSES for item in view["findings"][status]]
    claim = view.get("claim") or {}
    result.update({
        "chain_ok": bool(view["integrity"]["chain_ok"]),
        "findings_readable": len(readable),
        "citations_current": any(item["citations"] for item in readable) and all(
            cite["status"] == "resolved" for item in readable for cite in item["citations"]),
        "seq_unchanged": view["integrity"]["events"] == probe["events"],
        "claim_visible": bool(claim.get("active")) and claim.get("holder") == probe["holder"]})
    if intent is not None and not intent.get("withheld"):
        result.update({
            "intent_parked": intent["state"] == "proposed" and listed["total"] == 1
            and [item.get("intent_id") for item in listed["intents"]]
            == [parked["intent_id"]],
            "intent_chain_ok": bool(intent["chain_ok"]),
            "intent_dispatched": bool(intent["dispatches"]) or intent["outcome"]
            != "not_dispatched" or row != parked["row"],
            "intent_state": intent["state"], "approval_state": approval_state(intent)})
    return result


def _coworker_read(host: Host, probe: Mapping[str, Any]) -> dict[str, Any]:
    """The probe case resumed by the ``case_coworker`` co-worker in a served session of
    their own: ``coworker_read`` (the case, its claim and the parked intent read, the
    intent not withheld and the case's only one), ``coworker_chain_ok`` (the case's and
    the intent's event chains verified), the intent's state and approval state as they
    see it, and ``coworker_withheld_markers`` (the case items shown to them as withheld
    markers). States and counts only."""
    from .cases import WITHHELD
    from .mcp_server import InputError

    result: dict[str, Any] = {"coworker_read": False, "coworker_chain_ok": False,
                              "coworker_intent_state": None, "coworker_approval_state": None,
                              "coworker_withheld_markers": None}
    coworker, intent_id = host._coworker(), probe["intent"]["intent_id"]
    try:
        with host.served_tools(coworker) as served:
            with _probe_action_path(host, served.runtime) as (_owner, _path, act):
                # Inside: the proposal's fixture:// citations resolve on the owner.
                view = served.runtime.cases.get(coworker, probe["case_id"])
                listed = act({"mode": "status", "case_id": probe["case_id"]}, coworker)
                try:
                    intent = act({"mode": "status", "intent_id": intent_id},
                                 coworker)["intent"]
                except InputError:   # lost (intent_not_found) or its chain broken
                    intent = None
    except Exception as exc:  # noqa: BLE001 - not re-read is never resumed
        return {**result, "coworker_reasons": [_reason(exc)]}
    claim = view.get("claim") or {}
    # An intent is withheld exactly when its proposal is: counted once, as the proposal.
    result["coworker_withheld_markers"] = len(view["findings"]["withheld"]) + sum(
        1 for name in ("calculations", "gaps", "proposals", "verifications", "links")
        for item in view.get(name) or () if item.get("content") == WITHHELD)
    if intent is None or intent.get("withheld"):
        return result
    result.update({
        "coworker_read": bool(claim.get("active")) and claim.get("holder") == probe["holder"]
        and listed["total"] == 1
        and [item.get("intent_id") for item in listed["intents"]] == [intent_id],
        "coworker_chain_ok": bool(view["integrity"]["chain_ok"]) and bool(intent["chain_ok"]),
        "coworker_intent_state": intent["state"],
        "coworker_approval_state": approval_state(intent)})
    return result


def approval_state(intent: Mapping[str, Any]) -> str:
    """An intent's approval as ``plx.act`` (mode status) shows it: ``decided`` (an MC
    gate decision recorded), ``gate_requested``, ``call_approved`` (a human's per-call
    approval) or ``none``."""
    return ("decided" if intent["decisions"] else
            "gate_requested" if intent["gate"] else
            "call_approved" if intent["call_approved"] else "none")


COVERAGE_COUNTS = ("roots", "roots_listed", "folders_listed", "items_withheld", "files_seen",
                   "files_current", "files_not_captured", "files_missed",
                   "files_current_not_seen")


def sharepoint_coverage(host: "Host", *,
                        monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Files seen versus captured across every folder under each root of the host's
    SharePoint scope: a GET-only listing of every folder under the root (by folder id,
    breadth first, within the target's depth bound and the census's own time budget, the
    source's ``census_budget_seconds`` or :data:`CENSUS_BUDGET_SECONDS`, never the
    capture's ``budget_seconds``; no content and no item permission the audience does not
    require) against the current versions the capture store holds under that root for the
    capture principal. The folder list is the library's own listing.

    The census is resumable across calls (decision 2026-10-21-sharepoint-census-across-
    runs): a call the time budget or the folder bound (``max_folders``, folders listed per
    call and root) cuts keeps its folder frontier and the listing cursor of the folder it
    was in, with the files it saw, in ``host/`` of the state directory
    (:data:`CENSUS_FILE`), and the next call carries on from there, so no folder is listed
    twice and none is skipped, and coverage is counted over the carried census. A census
    that ended is consumed: the next call lists afresh. One older than the scope's
    ``census_max_age_hours`` (default ``full_walk_hours``), or kept for another scope,
    restarts. A census that ended complete leaves its verdict (``ended``: when it started,
    complete) beside the fresh one: while that verdict is inside the age window, a call
    whose fresh census is cut only by its own budget or folder bound, with nothing missed
    or unseen in what it listed, keeps it (``census.verdict`` ``ended_kept``,
    ``census.refresh_reasons`` the fresh census's cut): every folder was listed inside the
    window, by the fresh census where it reached and by the ended one elsewhere, the
    staleness bound a carried census already has. Any gap, anything missed or unseen, or
    the window passing, drops it (``census.verdict`` ``this_census``).
    ``census``: ``carried`` (this call resumed one), ``calls`` (this census so far),
    ``folders_listed_before`` (listed by the earlier calls), ``folders_pending`` (queued),
    ``restarted_stale``, ``verdict``, ``refresh_reasons``.

    ``files_not_captured``: files listed whose listed version (eTag) is not current under
    the root, ``files_missed`` those of a root whose capture last ended complete (a
    capture that lost a file, not a part still pending); ``files_current_not_seen``:
    files the store serves as current that the listing did not show, counted only where
    the listing could show them: under a root listed to the end, or in a folder (the
    store's path) listed whole, every page and nothing withheld. A root with a gap
    (``depth_bound``, ``time_budget``, ...) still reports a file gone from a folder it
    listed whole (moved from there to a folder it did not reach too: the store serves it
    where it no longer is), never one the store holds in a folder it did not reach.
    ``census_complete``: every root listed to the end (else ``reasons``: ``time_budget``
    or ``folder_bound`` when this call's budget or bound cut it); ``complete``: that, and
    nothing listed left uncaptured or served unseen, so ``files_seen == files_current``.
    A cut census is ``complete`` false with ``files_seen`` the files listed so far, never
    the library's count. A ``discover`` scope is not measured. Counts and codes only."""
    from integrations import capture_sharepoint as cs
    from integrations.graph_read import SharePointReader, resolve_site_drive
    from integrations.live_source_guard import SourceUnavailable, sharepoint_host_from_env

    entry = host.config.sources["sharepoint"]
    scope = cs.CaptureScope.from_file(entry.get("scope") or cs.DEFAULT_SCOPE)
    if scope.discover is not None:
        return {"census_complete": False, "complete": False, "reasons": ["discover_scope"]}
    budget_end = monotonic() + entry.get("census_budget_seconds", CENSUS_BUDGET_SECONDS)
    actor, audience = host._actor(), host._capture_audience()
    api, transport = host._graph()
    site_host = sharepoint_host_from_env(host.environ)
    counts = dict.fromkeys(COVERAGE_COUNTS, 0)
    reasons: set[str] = set()
    capture_cut: set[str] = set()   # why the capture (not the census) left files unread
    pending: set[str] = set()       # why a capture still under way left files unread
    kept, stale = _load_census(host, scope)
    census = {"carried": bool(kept["roots"]), "calls": kept["calls"] + 1,
              "folders_listed_before": sum(r["folders_listed"] for r in kept["roots"].values()),
              "folders_pending": 0, "restarted_stale": stale,
              "verdict": "this_census", "refresh_reasons": []}
    kept["calls"] += 1
    ended, consumed = True, False
    # (drive, root) -> item id: (eTag, the folder the store holds it in)
    current: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    store = (cs.SharePointCaptureStore.open_read_only(host.state_dir)
             if cs.store_exists(host.state_dir) else None)
    try:
        if store is not None:
            index = store.index(actor, audience, limit=cs.INDEX_LIMIT)
            if index["truncated"]:
                reasons.add("index_truncated")
            for item in index["items"]:
                row = store.current(item["drive_id"], item["item_id"])
                current.setdefault((row["drive_id"], row["root_path"]), {})[
                    row["item_id"]] = (row["etag"], row["path"].rsplit("/", 1)[0] or "/")
        for number, target in enumerate(scope.targets):
            counts["roots"] += len(target.folders)
            drive_id = target.drive_id
            if drive_id is None and site_host is not None:
                try:
                    drive_id = resolve_site_drive(api, site_host, target.site)
                except (SourceUnavailable, ValueError):
                    drive_id = None
            if drive_id is None:
                reasons.add("site_unresolved")
                ended = False
                continue
            reader = SharePointReader(api, audience, drive_id, transport=transport)
            for root in target.folders:
                record = kept["roots"].setdefault(f"{number}:{root}", _new_census_root(root))
                files, listed, gaps, whole = _listed_files(
                    reader, actor, root, target, budget_end, monotonic, record,
                    save=lambda: _save_census(host, kept))
                ended = ended and record["done"]
                census["folders_pending"] += len(record["queue"]) + bool(record["cursor"])
                held = current.get((drive_id, root), {})
                uncaptured = sum(1 for item_id, etag in files.items()
                                 if held.get(item_id, (None,))[0] != etag)
                state = (store.root_state(drive_id, root) if store is not None else None) or {}
                if uncaptured and state.get("status") != "complete" and (
                        "folder_bound" in (state.get("reason") or "")):
                    # The capture's own walk bound (cumulative over its folders, scope
                    # max_folders) stopped before the library's end: named, never raised.
                    capture_cut.add("capture_folder_bound")
                elif uncaptured and state.get("status") != "complete":
                    # Still pending in the capture: by the reasons its root stopped with
                    # (time_budget: it resumes in a later cycle), else capture_pending.
                    pending |= set(filter(None, (state.get("reason") or "").split(","))) \
                        or {"capture_pending"}
                counts["roots_listed"] += not gaps
                counts["folders_listed"] += listed["folders_listed"]
                counts["items_withheld"] += listed["items_withheld"]
                counts["files_seen"] += len(files)
                counts["files_current"] += len(held)
                counts["files_not_captured"] += uncaptured
                counts["files_missed"] += uncaptured if state.get("status") == "complete" else 0
                counts["files_current_not_seen"] += sum(
                    1 for item_id, (_etag, folder) in held.items()
                    if item_id not in files and (not gaps or folder in whole))
                reasons |= gaps
        consumed = ended and bool(kept["roots"])
    finally:
        if store is not None:
            store.close()
        # A census that ended is consumed (its verdict kept when complete, below); a cut
        # one (or one the run broke off) is kept.
        if not consumed:
            _save_census(host, kept)
    complete = (not reasons and not counts["files_not_captured"]
                and not counts["files_current_not_seen"])
    previous = kept.get("ended")
    if consumed:
        _save_census(host, {**_fresh_census(host, scope), **({"ended": {
            "started_at": kept["started_at"], "complete": True}} if complete else {})})
    elif (isinstance(previous, dict) and previous.get("complete") is True
          and _within_window(host, scope, previous.get("started_at"))
          and reasons and reasons <= {cs.TIME_BUDGET, "folder_bound"}
          and not counts["files_missed"] and not counts["files_current_not_seen"]
          and not capture_cut):
        census.update(verdict="ended_kept", refresh_reasons=sorted(reasons))
        reasons = set()
    return {**counts, "census_complete": not reasons,
            "complete": not reasons and not counts["files_not_captured"]
            and not counts["files_current_not_seen"],
            "reasons": sorted(reasons), "capture_incomplete": sorted(capture_cut),
            "capture_pending": sorted(pending), "census": census}


STATE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")   # ``check --state-name``
CENSUS_FILE = "sharepoint-census.json"   # in HOST_DIR: the resumable coverage census
CENSUS_SCHEMA = "plx-sharepoint-census/v1"
CENSUS_SAVE_EVERY = 100   # folders listed between two checkpoints of the kept census


def _new_census_root(root: str) -> dict[str, Any]:
    return {"files": {}, "whole": [], "queue": [[None, root, 0]], "cursor": None,
            "folders_listed": 0, "items_withheld": 0, "gaps": [], "done": False}


def _census_key(scope: Any) -> str:
    """The scope a kept census belongs to: its targets and bounds."""
    return hashlib.sha256(json.dumps(
        [[t.id, t.drive_id, t.site, list(t.folders), t.max_depth, t.max_folders]
         for t in scope.targets], sort_keys=True).encode("utf-8")).hexdigest()


def _fresh_census(host: "Host", scope: Any) -> dict[str, Any]:
    return {"schema": CENSUS_SCHEMA, "scope": _census_key(scope),
            "started_at": _iso(host.clock()), "calls": 0, "roots": {}}


def _within_window(host: "Host", scope: Any, started_at: Any) -> bool:
    """``started_at`` (a census's first call) is inside the scope's census age window."""
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age = (host.clock() - started).total_seconds()
    return 0 <= age <= scope.census_max_age_hours * 3600


def _load_census(host: "Host", scope: Any) -> tuple[dict[str, Any], bool]:
    """(the kept census, whether a stale one was dropped). A census that is unreadable,
    malformed, of another scope or older than the scope's ``census_max_age_hours`` is dropped:
    the listing starts over, never carries a position nobody can vouch for."""
    key = _census_key(scope)
    fresh = _fresh_census(host, scope)
    path = host.state_dir / HOST_DIR / CENSUS_FILE
    try:
        kept = json.loads(path.read_text("utf-8"))
        started = datetime.strptime(kept["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        valid = (kept["schema"] == CENSUS_SCHEMA and kept["scope"] == key
                 and isinstance(kept["calls"], int) and isinstance(kept["roots"], dict)
                 and all(isinstance(r, dict) and isinstance(r["files"], dict)
                         and isinstance(r["whole"], list) and isinstance(r["queue"], list)
                         and isinstance(r["gaps"], list) and isinstance(r["done"], bool)
                         and isinstance(r["folders_listed"], int)
                         and isinstance(r["items_withheld"], int)
                         and (r["cursor"] is None or isinstance(r["cursor"], dict))
                         for r in kept["roots"].values()))
    except (OSError, ValueError, KeyError, TypeError):
        return fresh, False
    if not valid:
        return fresh, False
    age = host.clock() - started
    if age.total_seconds() > scope.census_max_age_hours * 3600:
        return fresh, bool(kept["roots"])   # an empty one (a verdict only) is not restarted
    return kept, False


def _save_census(host: "Host", kept: Mapping[str, Any], *, drop: bool = False) -> None:
    path = host.state_dir / HOST_DIR / CENSUS_FILE
    if drop:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(kept, handle, sort_keys=True)
    os.replace(tmp, path)


def _listed_files(reader: Any, actor: Actor, root: str, target: Any, deadline: float,
                  monotonic: Callable[[], float], record: dict[str, Any],
                  save: Callable[[], None] = lambda: None
                  ) -> tuple[dict[str, str], dict[str, int], set[str], set[str]]:
    """(file item id -> listed eTag, counts, gaps, the folders listed whole) of every
    folder under ``root``, listed as the capture walks it
    (``capture_sharepoint.SharePointCapture._walk``'s bounds), carried in ``record`` (the
    kept census of this root: its folder frontier, the cursor of the folder it was in, the
    files and the permanent gaps). A call the time budget or ``max_folders`` (folders
    listed this call) cuts leaves ``record`` resumable and reports that reason; a folder
    is listed whole (its canonical path) when every page was read and nothing was
    withheld or dropped."""
    from integrations import capture_sharepoint as cs
    from integrations.live_source_guard import NotVisible, SourceUnavailable, unavailable_detail

    files: dict[str, str] = record["files"]
    whole: set[str] = set(record["whole"])
    permanent: set[str] = set(record["gaps"])
    queue: list[list[Any]] = record["queue"]
    cut: set[str] = set()
    this_call = 0
    since_save = 0
    while queue or record["cursor"]:
        if monotonic() >= deadline:
            cut.add(cs.TIME_BUDGET)
            break
        cursor = record["cursor"]
        if cursor is None and this_call >= target.max_folders:
            cut.add("folder_bound")
            break
        # Checkpoint to return to if the listing is broken off by anything the census does
        # not handle: the folder (or its cursor) goes back, so a resumed census lists it.
        before = (len(queue) - (cursor is None), record["folders_listed"],
                  record["items_withheld"])
        if cursor is None:
            popped = queue.pop(0)
            folder_id, path, depth = popped
            start, listed_whole, seen = None, True, set()
        else:
            popped = None
            folder_id, path, depth = cursor["id"], cursor["path"], cursor["depth"]
            start, listed_whole, seen = cursor["start"], cursor["whole"], set(cursor["seen"])
            record["cursor"] = None
        # One folder's listing, continued from the start of its next page (``list_folder``
        # ``start``, skipping the children already listed) until it reads to the end: a
        # folder of more children than one listing holds is listed whole, never cut at
        # ``LIST_MAX_ITEMS``, and a child deleted meanwhile shifts none past the cut.
        listing = None
        try:
            while True:
                try:
                    listing = reader.list_folder(actor, path, page_size=cs.LIST_PAGE_SIZE,
                                                 max_pages=cs.LIST_MAX_PAGES,
                                                 max_items=cs.LIST_MAX_ITEMS, folder_id=folder_id,
                                                 start=start, skip=frozenset(seen))
                except NotVisible:
                    permanent.add("not_visible")
                    listed_whole = False
                    break
                except SourceUnavailable as exc:
                    permanent.add("source_error:" + (unavailable_detail(exc) or "unclassified"))
                    listed_whole = False
                    break
                if start is None:
                    record["folders_listed"] += 1
                    this_call += 1
                done = listing["completeness"]
                resume = done.get("next")
                if not (resume or done.get("complete", False)):
                    permanent.add("listing_truncated")
                    listed_whole = False
                if done.get("withheld_count", 0):
                    record["items_withheld"] += done["withheld_count"]
                    permanent.add("withheld_from_capture")
                    listed_whole = False
                for item in listing["items"]:
                    seen.add(item["id"])
                    if item.get("kind") == "folder":
                        if depth < target.max_depth:
                            queue.append([item["id"], item["path"], depth + 1])
                        else:
                            permanent.add("depth_bound")
                    elif item.get("kind") == "file":
                        files[item["id"]] = item["etag"]
                if not resume:
                    break
                folder_id, start = listing["folder_id"], {"url": resume["url"], "offset": 0}
                path = listing["path"]
                if monotonic() >= deadline:
                    # Cut inside the folder: it carries on at its next page, listed once.
                    cut.add(cs.TIME_BUDGET)
                    record["cursor"] = {"id": folder_id, "path": path, "depth": depth,
                                        "start": start, "seen": sorted(seen),
                                        "whole": listed_whole}
                    break
        except BaseException:
            del queue[before[0]:]
            if popped is not None:
                queue.insert(0, popped)
            else:
                record["cursor"] = cursor
            record["folders_listed"], record["items_withheld"] = before[1], before[2]
            record["whole"], record["gaps"] = sorted(whole), sorted(permanent)
            raise
        if record["cursor"] is not None:
            break
        if listed_whole and listing is not None:
            whole.add(listing["path"])
        since_save += 1
        if since_save >= CENSUS_SAVE_EVERY:
            since_save = 0
            record["whole"], record["gaps"] = sorted(whole), sorted(permanent)
            save()
    record["whole"], record["gaps"] = sorted(whole), sorted(permanent)
    record["done"] = not queue and record["cursor"] is None
    counts = {"folders_listed": record["folders_listed"],
              "items_withheld": record["items_withheld"]}
    return files, counts, permanent | cut, whole


def _reason(exc: BaseException) -> str:
    """A failure as a code: the exception type and, for a source failure, its sanitized
    detail (``live_source_guard.unavailable_detail``) or the mirror's code; for an
    unreadable review store its code and path."""
    from integrations.live_source_guard import SourceUnavailable, unavailable_detail
    from integrations.source_mirror import MirrorError
    from .build_steps import ReviewStoreUnreadable

    name = type(exc).__name__
    if isinstance(exc, ReviewStoreUnreadable):   # which store blocks every build
        return f"{name}:{exc.code}:{exc.path}"
    if isinstance(exc, MirrorError):
        return f"{name}:{exc.code}" + (f":{exc.status}" if exc.status is not None else "")
    if isinstance(exc, SourceUnavailable):
        return f"{name}:{unavailable_detail(exc) or 'unavailable'}"
    if isinstance(exc, HostError):
        return f"{name}:{exc.code}"
    if isinstance(exc, sqlite3.Error):   # the cause (disk_full, locked, ...), not the text
        from integrations.capture_store import sqlite_code
        return f"{name}:{sqlite_code(exc)}"
    return name


# ------------------------------------------------------------------ serving

def mcp_arguments(host: Host, *, session_ticket_env: str | None,
                  eval_principal: bool = False, agent: str | None = None) -> list[str]:
    """``operating.mcp_server`` arguments for a live session over the host's state: the
    employee comes from a signed start-up ticket only (a command-line employee is a
    development session, which never opens live state). ``None``: the remote host's
    arguments (``operating.mcp_http_host``), whose employees come from each request.
    ``eval_principal``: the ticket names the declared eval principal
    (``operating.eval_principal``): read only (no action path) and only the marking
    grants it declares; no Portal snapshot (it has no Portal row). ``agent``: the agent
    definition this session runs instead of the config's (``operating/agent_definitions.json``;
    an unknown one is refused, never the default)."""
    config = host.config
    if agent is not None:
        from .agent_definitions import load as load_agents
        if not isinstance(agent, str) or agent not in load_agents().agents:
            raise HostError("agent_unknown")
    declared = None
    if eval_principal:
        if session_ticket_env is None:
            raise HostError("eval_principal_needs_session_ticket")
        from . import eval_principal as eval_module
        try:
            declared = eval_module.load()
        except ValueError as error:
            raise HostError(str(error)) from None
    if config.employee_policy is None:
        raise HostError("host_config_invalid:employee_policy")
    argv = ["--state-dir", str(host.state_dir),
            "--source-mirror-audience", str(config.mirror_audience),
            "--employee-policy", str(config.employee_policy)]
    if config.portal_access is not None and declared is None:
        argv += ["--portal-access", str(config.portal_access)]
    if session_ticket_env is not None:
        argv += ["--session-ticket-env", session_ticket_env]
    if declared is not None:
        argv += ["--eval-principal"]
    if (agent or config.agent) is not None:
        argv += ["--agent", agent or config.agent]
    if "portal" in config.sources:
        argv += ["--host-captures-portal"]
    argv += _freshness_arguments(config)
    if config.action_path and declared is None:
        argv += ["--enable-action-path"]
    if config.knowledge_namespaces is not None:
        argv += ["--knowledge-namespaces", str(config.knowledge_namespaces)]
    grants = config.marking_grants if declared is None else \
        eval_module.session_grants(declared, config.marking_grants)
    for marking, principals in sorted(grants.items()):
        argv += [arg for principal in sorted(principals)
                 for arg in ("--marking-grant", f"{marking}={principal}")]
    if projection_path(host.state_dir).is_file():
        argv += ["--ontology-store", str(projection_path(host.state_dir))]
    return argv + _live_arguments(host)


def _served_captures(host: Host) -> tuple[Path | None, Path | None, Path | None]:
    """The live audience and the mail and SharePoint capture directories the host
    serves (None each when not served; no capture is served without the live audience)."""
    from integrations import capture_mail, capture_sharepoint

    if host.config.live_audience is None:
        return None, None, None
    return (host.config.live_audience,
            host.state_dir if capture_mail.store_exists(host.state_dir) else None,
            host.state_dir if capture_sharepoint.store_exists(host.state_dir) else None)


def live_sources(host: Host) -> list[str]:
    """The configured sources a live reader reads (``case_runtime.LIVE_READER_SOURCES``):
    the only live readers the host's sessions build."""
    from .case_runtime import LIVE_READER_SOURCES

    return sorted(set(host.config.sources) & set(LIVE_READER_SOURCES))


def _live_arguments(host: Host) -> list[str]:
    """The live audience with the sources whose readers are built from it, and the mail
    and SharePoint captures the host holds: the MCP host and the console re-gate each
    captured record per item for the reader."""
    live_audience, mail, sharepoint = _served_captures(host)
    if live_audience is None:
        return []
    argv = ["--live-source-audience", str(live_audience),
            "--live-sources", ",".join(live_sources(host))]
    if mail is not None:
        argv += ["--mail-capture-dir", str(mail)]
    if sharepoint is not None:
        argv += ["--sharepoint-capture-dir", str(sharepoint)]
    return argv


def directory_provider(host: Host, *, monotonic: Callable[[], float] = time.monotonic
                       ) -> Any:
    """The Entra directory provider the employee resolver requires when the config sets
    ``entra_directory`` (None otherwise); its Graph credentials missing is a refusal,
    never a resolver without the directory."""
    if not host.config.entra_directory:
        return None
    from integrations.graph_directory import GraphDirectoryReader
    from integrations.graph_read import build_graph
    from integrations.live_source_guard import SourceUnavailable
    from .employee_permissions_entra import EntraDirectoryProvider

    try:
        api, _transport = build_graph(host.environ, sender=host.sender)
    except (SourceUnavailable, ValueError):
        raise HostError("entra_directory_credentials_unavailable") from None
    return EntraDirectoryProvider(GraphDirectoryReader(api), clock=monotonic)


def resolve_approvers(host: Host) -> dict[str, Any] | None:
    """At the start of a host that serves actions: the approver roster's oids by UPN
    into the state directory (``integrations.graph_directory.resolve_roster``; Graph
    credentials missing resolve nobody). Counts and status only, also on stderr."""
    if not host.config.action_path:
        return None
    from integrations.graph_directory import GraphDirectoryReader, resolve_roster
    from integrations.graph_read import build_graph
    from integrations.live_source_guard import SourceUnavailable

    try:
        api, _transport = build_graph(host.environ, sender=host.sender)
        reader = GraphDirectoryReader(api)
    except (SourceUnavailable, ValueError):
        reader = None
    from . import goal2_mc_approval  # the roster the host's action path loads, read now
    summary = resolve_roster(reader, host.state_dir,
                             roster=goal2_mc_approval.DEFAULT_APPROVER_ROSTER)
    print(json.dumps({"approver_roster": summary}, sort_keys=True), file=sys.stderr,
          flush=True)
    return summary


def serve_mcp(host: Host, *, session_ticket_env: str,
              input_stream: Any = None, output_stream: Any = None,
              monotonic: Callable[[], float] = time.monotonic,
              eval_principal: bool = False, agent: str | None = None,
              clock: Callable[[], datetime] | None = None) -> None:
    """The MCP start-up path: identity resolution (with the directory when configured)
    and the durable replay guard happen in ``mcp_server.main``'s session start.
    ``clock``: the "now" the session judges its captures' age at (the host's)."""
    from . import mcp_server

    mcp_server.main(mcp_arguments(host, session_ticket_env=session_ticket_env,
                                  eval_principal=eval_principal, agent=agent),
                    # The eval principal is resolved from its declaration, never Entra.
                    directory=None if eval_principal
                    else directory_provider(host, monotonic=monotonic),
                    input_stream=input_stream, output_stream=output_stream, clock=clock)


def serve_mcp_http(host: Host, *, entra_resource: Path, bind: str, port: int,
                   monotonic: Callable[[], float] = time.monotonic, jwks: Any = None,
                   ready: Callable[[Any], None] | None = None) -> int:
    """The remote MCP host over this state for every signed-in employee: each request's
    Entra access token is verified and its employee resolved (with the directory when
    configured) in ``operating.mcp_http_host``. ``jwks``/``ready``: tests."""
    from . import mcp_http_host

    return mcp_http_host.main(
        mcp_arguments(host, session_ticket_env=None)
        + ["--entra-resource", str(entra_resource), "--bind", bind, "--port", str(port)],
        directory=directory_provider(host, monotonic=monotonic), jwks=jwks, ready=ready)


def console_arguments(host: Host, port: int) -> list[str]:
    config = host.config
    if config.employee_policy is None:
        raise HostError("host_config_invalid:employee_policy")
    argv = ["--state-dir", str(host.state_dir), "--source-mirror-audience",
            str(config.mirror_audience), "--employee-policy", str(config.employee_policy),
            "--port", str(port)]
    if config.portal_access is not None:
        argv += ["--portal-access", str(config.portal_access)]
    if "portal" in config.sources:
        argv += ["--host-captures-portal"]
    argv += _freshness_arguments(config)
    if projection_path(host.state_dir).is_file():
        argv += ["--ontology-store", str(projection_path(host.state_dir))]
    return argv + _live_arguments(host)


def freshness_bounds(config: "HostConfig") -> dict[str, float]:
    """Source family -> seconds: how old this host's own capture of a source may be
    before answers state it stale (supervisor decision 2026-10-27, captured-Portal
    staleness). Twice one capture turn of the source: its re-run interval (ingest mode:
    the section's ``min_interval_seconds``, default 120 s; cycle mode: the host's
    ``interval_seconds``) plus the time one capture may take (its ``budget_seconds``, else
    ``timeout_seconds``, else the ingest default timeout, plus the ingest grace). A
    family two sources write (SharePoint and its lists) takes the larger bound."""
    from . import ingest_forever as ingest
    out: dict[str, float] = {}
    for name, section in sorted(config.sources.items()):
        if config.ingest is not None:
            spec = config.capture_sources.get(name)
            family = getattr(spec, "projected", None)
            interval = section.get("min_interval_seconds", ingest.DEFAULT_MIN_INTERVAL_SECONDS)
            grace = config.ingest["grace_seconds"]
        else:
            family, interval, grace = PROJECTED_SOURCE.get(name), config.interval_seconds, 0
        if family is None:
            continue
        run = (section.get("budget_seconds") or section.get("timeout_seconds")
               or ingest.DEFAULT_TIMEOUT_SECONDS)
        out[family] = max(out.get(family, 0.0), 2.0 * (float(interval) + float(run) + grace))
    return out


def _freshness_arguments(config: "HostConfig") -> list[str]:
    return [arg for family, seconds in sorted(freshness_bounds(config).items())
            for arg in ("--freshness-bound", f"{family}={seconds:g}")]


@contextmanager
def _terminated_exits() -> Iterator[None]:
    """SIGTERM as ``SystemExit(143)`` while a cycle may run on the main thread, so the
    cycle's capture children are stopped and unrecorded on the way out (a SIGKILL leaves
    them to the next start's recovery, ``CycleChildren``). Off the main thread: as is."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminated(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=1, sort_keys=True), flush=True)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, workbench_fetch: Callable[..., Any] | None = None,
         portal_options: Mapping[str, Any] | None = None,
         clock: Callable[[], datetime] = _utc, sleep: Callable[[float], None] = time.sleep,
         monotonic: Callable[[], float] = time.monotonic,
         input_stream: Any = None, output_stream: Any = None, jwks: Any = None,
         ready: Callable[[Any], None] | None = None,
         child: Sequence[str] | None = None,
         capture_sources: Mapping[str, Any] | None = None,
         disk_free: Callable[[Path], int] | None = None) -> int:
    """``child`` (tests): the command ``check --interrupt-after-seconds`` runs ``run`` with,
    in place of ``RUN_CHILD``. ``capture_sources`` (tests): ingest mode's capture CLIs, in
    place of ``operating.ingest_forever.SOURCES``. ``disk_free`` (tests): the free-space
    measure of every host this builds (``Host.disk_free``)."""
    parser = argparse.ArgumentParser(prog="python3 -m operating.host",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("run", "one capture cycle and projection rebuild (counts only)"),
                       ("status", "per source generation, age and staleness"),
                       ("serve", "capture cycles on an interval and the console"),
                       ("check", "N cycles over one state directory and what they keep"),
                       ("mcp", "the MCP stdio host over this state for one employee"),
                       ("mcp-http", "the remote MCP host (Streamable HTTP, loopback) for "
                                    "Entra-signed-in employees")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--state-dir", type=Path, default=None,
                         help="default: $PLX_WORKBENCH_STATE_DIR (unset: refused, naming it)")
        if name != "status":
            sub.add_argument("--config", required=True, type=Path)
    serve = commands.choices["serve"]
    serve.add_argument("--interval-seconds", type=int, default=None)
    serve.add_argument("--max-cycles", type=int, default=0)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-console", action="store_true",
                       help="cycles only (for a scheduler or a separate console)")
    serve.add_argument("--stop-file", type=Path, default=None,
                       help="ingest mode: stop when this file exists (default: "
                            "<DIR>/host/ingest.stop)")
    mcp = commands.choices["mcp"]
    mcp.add_argument("--session-ticket-env", required=True)
    mcp.add_argument("--eval-principal", action="store_true",
                     help="the ticket names the declared eval principal "
                          "(operating.eval_principal): read only, no action path")
    mcp.add_argument("--agent", default=None,
                     help="the agent definition this session runs instead of the config's "
                          "(operating/agent_definitions.json; unknown: refused)")
    check = commands.choices["check"]
    check.add_argument("--cycles", type=int, default=2)
    check.add_argument("--state-name", default=None, metavar="NAME",
                       help="keep the check's state as $PLX_WORKBENCH_STATE_DIR/host-check-"
                            "NAME/live (or the XDG/HOME state base), across runs, so a "
                            "capture and its coverage census carry on in the next run "
                            "(instead of --state-dir)")
    check.add_argument("--withheld-principal", default=WITHHELD_PRINCIPAL)
    check.add_argument("--interrupt-after-seconds", type=float, default=None,
                       help="kill the second cycle (a child run) after S seconds")
    for name in ("run", "check"):
        commands.choices[name].add_argument(
            "--sources", default=None,
            help="comma-separated configured sources to read (default: every one)")
    remote = commands.choices["mcp-http"]
    remote.add_argument("--entra-resource", required=True, type=Path,
                        help="plx-entra-resource/v1 JSON (operating.oidc_auth)")
    remote.add_argument("--bind", default="127.0.0.1")
    remote.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    state_name = getattr(args, "state_name", None)
    if state_name is not None:
        from integrations.capture_mail import CaptureError, assert_outside_git, check_state_dir
        try:
            if args.state_dir is not None or STATE_NAME.fullmatch(state_name) is None:
                raise ValueError("state_name_invalid")
            args.state_dir = check_state_dir(
                os.environ if environ is None else environ, f"host-check-{state_name}",
                injected=False)[0]
            assert_outside_git(args.state_dir)
        except (ValueError, CaptureError) as exc:
            print(json.dumps({"error": str(exc).split(":")[0]}), file=sys.stderr)
            return 2
        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.state_dir is None:   # the one durable directory the verifier's warm store uses
        from operating.warm_store import WarmStoreError, state_dir as durable_state_dir
        try:
            args.state_dir = durable_state_dir(os.environ if environ is None else environ)
        except WarmStoreError as exc:
            print(json.dumps({"error": exc.code, "setting": exc.setting}), file=sys.stderr)
            return 2
    try:
        config = None if args.command == "status" else HostConfig.from_file(
            args.config, capture_sources=capture_sources)
        from integrations.capture_mail import assert_outside_git
        state_dir = assert_outside_git(args.state_dir)   # as Host() does, before any write
        if args.command == "check" and args.interrupt_after_seconds is not None:
            claim_probe_state(state_dir)   # before Host() writes into it
        if args.command in ("serve", "mcp", "mcp-http") and probe_state(state_dir):
            raise HostError("host_probe_state_not_served")
        if args.command == "serve" and config.ingest is not None:
            from .ingest_forever import ephemeral
            if ephemeral(state_dir):   # cursors a reboot clears re-read every source
                raise HostError("state_dir_ephemeral")
        if getattr(args, "sources", None) is not None:
            config = config.only(args.sources.split(","))
        host = Host(args.state_dir, config, environ=environ, sender=sender,
                    workbench_fetch=workbench_fetch, portal_options=portal_options,
                    clock=clock, sleep=sleep, monotonic=monotonic, disk_free=disk_free)
        if args.command in ("serve", "mcp", "mcp-http") \
                and not getattr(args, "eval_principal", False):
            resolve_approvers(host)   # an eval session is never served the action path
        if args.command == "status":
            _print(host.status())
        elif args.command == "run":
            with _terminated_exits():
                _print(host.cycle())
        elif args.command == "check":
            run = ["run", "--state-dir", str(args.state_dir), "--config", str(args.config),
                   *(["--sources", args.sources] if args.sources is not None else [])]

            def spawn() -> subprocess.Popen:
                return subprocess.Popen([*(child or RUN_CHILD), *run], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, start_new_session=True,
                                        env=None if environ is None else dict(environ))

            with _terminated_exits():
                _print(cycles_check(lambda: Host(
                    args.state_dir, config, environ=environ, sender=sender,
                    workbench_fetch=workbench_fetch, portal_options=portal_options,
                    clock=clock, sleep=sleep, monotonic=monotonic, disk_free=disk_free),
                    cycles=args.cycles,
                    withheld_principal=args.withheld_principal,
                    interrupt_after=args.interrupt_after_seconds, spawn=spawn))
        elif args.command == "mcp":
            serve_mcp(host, session_ticket_env=args.session_ticket_env,
                      input_stream=input_stream, output_stream=output_stream,
                      monotonic=monotonic, eval_principal=args.eval_principal,
                      agent=args.agent, clock=clock)
        elif args.command == "mcp-http":
            return serve_mcp_http(host, entra_resource=args.entra_resource, bind=args.bind,
                                  port=args.port, monotonic=monotonic, jwks=jwks, ready=ready)
        else:
            interval = args.interval_seconds or config.interval_seconds
            if args.no_console:
                with _terminated_exits():
                    host.serve(interval_seconds=interval, max_cycles=args.max_cycles,
                               on_cycle=_print, stop_file=args.stop_file)
                return 0
            continuous = config.ingest is not None
            if not continuous:
                _print(host.serve_cycle())   # the console opens over a built projection

            def later() -> None:
                # Ingest mode starts at once: its sources refresh the projection as each
                # finishes, the console serves the generation committed meanwhile.
                if continuous or not host.stop.wait(interval):
                    host.serve(interval_seconds=interval, max_cycles=args.max_cycles,
                               on_cycle=_print, stop_file=args.stop_file)

            threading.Thread(target=later, daemon=True).start()
            from operator_ui import case_console
            try:
                return case_console.main(console_arguments(host, args.port))
            finally:
                host.stop.set()
    except HostError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    return 0


__all__ = ["CONFIG_SCHEMA", "Host", "HostConfig", "HostError", "build_projection",
           "cycles_check", "directory_provider", "main", "mcp_arguments", "open_state_capture",
           "projection_path", "projection_row_check", "resolve_approvers", "serve_mcp",
           "serve_mcp_http", "sharepoint_coverage", "state_capture_paths", "state_captures",
           "workbench_key"]


if __name__ == "__main__":
    raise SystemExit(main())
