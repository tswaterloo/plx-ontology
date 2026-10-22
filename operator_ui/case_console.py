"""Case workspace console: the operator console over a case runtime, read-only.

    python3 -m operator_ui.case_console --state-dir DIR --source-mirror-audience FILE \\
        --employee-policy POLICY --portal-access PORTAL_SNAPSHOT
    python3 -m operator_ui.tests.case_fixtures --fixture --seed-fixtures --state-dir NEW_DIR \\
        --principal staff:operations

Serves the console on a loopback port. Session identity (``operating.session_identity``):
a live console has no host actor and no default principal; it takes the
identity from each authenticated request, whose bearer credential is a signed session
ticket (author ``human``) naming one employee, verified with ``PLX_WORKBENCH_SESSION_SECRET``
and resolved per request through ``--employee-policy`` and ``--portal-access`` (Portal role
∩ Entra groups ∩ SharePoint item ACL). With ``--entra-resource`` (the console's Entra app
registration, ``operating.oidc_auth``) an employee signs in with their PLX Microsoft account
instead: the bearer is their Entra access token, verified and resolved per request the same
way, and the session journal records that proof. Its case runtime refuses any actor without an
employee. ``--principal``/``--clearance`` name a fixture identity and need ``--fixture``,
which is refused with any real source reader and on state a live session or ingestion
wrote. The mirror is offline: it answers only from captures already held in
``DIR/case-runtime`` and contacts no source system. No case or business write
route exists. ``--report-store`` adds the latest stored playbook evaluation per
case (``operating.playbook_reports``), readable only for cases the actor can read.

``--seed-fixtures`` (with ``--fixture``, from the fixture world's entry point
``python3 -m operator_ui.tests.case_fixtures``, which passes itself as ``main``'s
``fixtures``) first writes the fixture cases into a new DIR (the assembly-order cases with their
synthetic SharePoint packets, a purchase-order case with a
synthetic supplier confirmation mail, a proposal registered on the goal-2 action path and
a stored evaluation; evidence label ``isolated-fixture / injected-transport``: no real
PLX data, no network) and serves them with the fixture's in-process readers;
``--source-mirror-audience`` then defaults to the seed's fixture audience. A directory a
seed wrote is labelled fixture on every later start too (its marker file). The fixture
world is evaluation data and lives with the tests; this module holds only the marker and
the label and never imports it: without ``fixtures`` the console refuses ``--seed-fixtures``
and a directory a full seed wrote.

Without a seed the console reads real case state: ``--live-source-audience`` (with
``--sharepoint-drive``) configures the same read-only Graph/BC readers as the MCP case
host (credentials from the environment; live-unverified here), so SharePoint originals
show and their eTag is rechecked; a case's mail is what it cites (``operating.case_mail``
records it as findings) and a message's stored links to a purchase order are the object
workspace's (``plx.around``, ``plx.review``); ``--action-store`` (default
DIR/actions.sqlite when present) shows proposals' goal-2 action-path state and serves the
propose-follow-up control (``--mc-operator-email`` and ``--mc-follow-up-bucket`` let it
prepare the exact Mission Control call a person then approves; nothing is sent here);
``--ingest-sources`` adds the ingestion coverage (``case_context_tools.ingest_status``).

Live mirror: when the ingestion runner (``python3 -m operating.ingestion run``) fills DIR,
``--open KIND=IDENTIFIER`` (live: with ``--session-ticket-env``, the employee who opens;
``ao=``, ``petra_po=``, ``sales_order=``, ``customer_po=``, ``find=<identifier>``,
``pick=TYPE[+LINKED_TYPE]`` or ``pick=TYPE>LINKED_TYPE``) first opens that case for the console's actor from the
ingested captures through ``operating.case_workspace_live``: ``plx.find``, ``plx.object``
and ``plx.investigate`` over their projection, recorded on the case (offline: no
Workbench API client exists in the console; a request already opened with the same
options after the same ingestion run is reused unless ``--reinvestigate``), so an
employee sees a real case's timeline, originals, relationship map, gaps and actions
together, every part resolved for them. A state directory the ingestion runner filled is
labelled ``source-mirror captures`` (``case_workspace_live.MIRROR_EVIDENCE``), never live.

Object workspace: ``--ontology-store FILE`` serves the employee object
workspace (``operator_ui.object_workspace``) over a projection store: find any served
object with ``plx.find`` and open its card, relationship map, timeline, process, gaps,
conflicts and registered intents, every value cited and every citation opened through
this console's case runtime for the same reader. ``--project-ontology`` first builds
``DIR/ontology.sqlite`` from the captures DIR's source mirror already holds (offline);
``--seed-fixtures`` does so for the seeded fixture mirror. The tools, grants and
per-item gates are the MCP case host's (``CaptureCaseHostTools``), decided at the start
of every request (default deny); ``--mail-capture-dir`` and ``--sharepoint-capture-dir``
add the mail and SharePoint records those captures hold, each re-gated per item for the
reader (a case citation of them resolves through the same gates,
``operating.case_tools_host.CaseCaptures``), and a question family (``plx.investigate``) runs from a found object. The reader is the
session identity: in a live console each request's verified employee gets its own object
workspace (``TicketSessions.object_workspace``) whose grant the runtime's employee gate
resolves again per request; with ``--fixture`` it is the fixture session, and
``--marking`` names a marking that fixture reader holds.

A live console journals every answered request in ``DIR/console-journal.sqlite``
(``operator_ui.session_journal``: counts, routes and enums per session, for a recorded
employee session); ``python3 -m operator_ui.console_ticket mint`` gives a named employee
the ticket URL to open it with, and ``revoke`` ends that session. A journal that cannot
be created (a full or read-only volume) is named on stderr in one line and the console
serves without one.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from integrations.source_mirror import AudienceMap
from ontology.core import Actor
from ontology.reviews import STORE_FILE, ReviewStore
from operating.build_steps import ReviewsIfPresent
from operating.case_runtime import CaseRuntime, live_source_names
from operating.cases import CaseNotFound, actor_label
from operating import session_identity
from operating.session_identity import SessionIdentity, SessionIdentityRefused

from .case_workspace import CaseWorkspace
from . import object_workspace as objects
from .server import serve
from .session_journal import SessionJournal

# Fixture state: a seed (operator_ui.tests.case_fixtures) writes FIXTURE_MARKER first, so a
# directory it wrote is labelled fixture on every later start (operating.session_identity
# reads the same file name).
FIXTURE_MARKER = "fixture-seed.json"
MARKER_SCHEMA = "plx-case-console-fixture/v1"
EVIDENCE_LABEL = "isolated-fixture / injected-transport"
EVIDENCE_NOTE = ("FIXTURE: synthetic rows, packets and mail read through the real read-only "
                 "clients over in-process fakes. Not real PLX data; nothing here was read "
                 "from Portal, SharePoint, Outlook or Business Central.")
MIRROR_AUDIENCE_FILE = "fixture-mirror-audience.json"
LIVE_AUDIENCE_FILE = "fixture-live-audience.json"
ACTIONS_FILE = "actions.sqlite"
REPORTS_FILE = "playbook-reports.sqlite"


def fixture_marker(state_dir: str | Path) -> dict[str, Any] | None:
    """The fixture marker of a state directory, or None (no seed wrote it)."""
    path = Path(state_dir) / FIXTURE_MARKER
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        # An unreadable marker still marks the directory as fixture state.
        return {"schema": MARKER_SCHEMA, "unreadable": True}
    return data if isinstance(data, dict) else {"schema": MARKER_SCHEMA, "unreadable": True}


def report_store_for(runtime: CaseRuntime, path: Path, recorders: Sequence[Actor] = ()):
    """A report store whose audience is the case's: readable iff the case is.

    Readers are ``Actor``s; a recorder is named by its audit label
    (``operating.cases.actor_label``) and must be one of ``recorders``.
    """
    from operating.playbook_reports import PlaybookReportStore

    labels = {actor_label(actor): actor for actor in recorders}

    def can_read(actor, case_id):
        if isinstance(actor, str):
            actor = labels.get(actor)
        if not isinstance(actor, Actor):
            return False
        try:
            runtime.cases.verify_chain(actor, case_id)
        except CaseNotFound:
            return False
        return True

    return PlaybookReportStore(path, can_read=can_read)


ONTOLOGY_FILE = "ontology.sqlite"


def checked_store_status(store_path: Path, state_dir: Path) -> "objects.StoreStatus":
    """The store's status against DIR's captures (``objects.StoreStatus``); a foreign or
    unverifiable store is refused (ValueError) before anything is served."""
    status = objects.StoreStatus(store_path, state_dir)
    state = status.read()
    if state.get("state") in ("foreign", "unknown"):
        raise ValueError(f"ontology store refused ({state.get('state')}): "
                         f"{state.get('detail') or ''}"[:300])
    return status


def object_workspace_for(runtime: CaseRuntime, store_path: Path, actor: Actor, *,
                         state_dir: Path, markings: Sequence[str] = (), action_path=None,
                         evidence: dict | None = None, intents_from=None,
                         mail_capture_dir: Path | None = None,
                         sharepoint_capture_dir: Path | None = None,
                         session: SessionIdentity | None = None,
                         planner: Any = None, ask_bounds: Any = None,
                         capture_bounds: Mapping[str, Any] | None = None, clock: Any = None,
                         portal_captured: bool = False,
                         wrap_access: Callable[[Any], Any] | None = None,
                         report_store: Any = None) -> "objects.ObjectWorkspace":
    """The object workspace for one reader: the case host's tools over ``store_path``
    (``objects.host_access``: the grants, item gates and ``plx.read`` the MCP case host
    applies, decided per request), Portal rows and attached originals opened through
    ``CaseWorkspace.citation`` and (with an action store) the intents that cite the
    object's records. ``intents_from``: the case store the action store belongs to
    (default the runtime's). Capture directories add the mail and SharePoint records
    their stores hold for the reader. A store projected from another capture generation
    than DIR's, or whose provenance cannot be checked, is refused (ValueError). ``actor`` is the
    session's reader (a fixture identity, or one verified employee). With its
    ``session`` the workspace also serves the person's controls: confirm / reject a
    link (``plx.review`` into ``DIR/reviews.sqlite``, the Workbench's review store) and,
    with an action store, propose a follow-up (``objects.FollowUps``: opened and recorded
    through the action path, so the session's subject is the recorded author). Without a
    ``session`` no person can record a review: the review store is read as it is, never
    created or written (``build_steps.ReviewsIfPresent``, as the MCP host reads it). With a
    ``planner`` (``operating.agent_loop``) it serves Ask: the agent loop for this reader,
    within ``ask_bounds``. ``capture_bounds``: the case host's SharePoint capture-read
    bounds (``operating.mcp_server.check_host_arguments``; unset: the host's defaults);
    ``clock``: the host's capture clock (default the real now). ``portal_captured``: the
    host captures the Portal (``--host-captures-portal``), so an answer reports its
    capture state, never that no Portal reader is configured. ``wrap_access``: wraps the
    reader's ``HostAccess`` and the workspace uses the wrapper (the Ask eval's recorder of
    the loop's tool calls). ``report_store``: the host's playbook report store
    (``report_store_for``); each case of an object shows its evaluation from it, as the
    case workspace does (None: ``not_configured``). The one factory of the console's
    workspaces and the Ask eval's (``scripts.eval_real_agent.ask_workspace``): they differ
    only in planner and wrapper."""
    status = checked_store_status(store_path, state_dir)
    if session is not None and session.actor != actor:
        raise ValueError("object_workspace_session_must_be_the_reader")
    reviews = (ReviewsIfPresent if session is None else ReviewStore)(
        Path(state_dir) / STORE_FILE)
    access = objects.host_access(runtime, store_path, actor, markings=markings,
                                 mail_capture_dir=mail_capture_dir,
                                 sharepoint_capture_dir=sharepoint_capture_dir,
                                 session=session, reviews=reviews,
                                 portal_captured=portal_captured, **(capture_bounds or {}),
                                 **({} if clock is None else {"clock": clock}))
    if wrap_access is not None:
        access = wrap_access(access)
    intents = (objects.CaseIntents(intents_from or runtime.cases, action_path)
               if action_path is not None else None)
    cases = CaseWorkspace(runtime, actor, report_store=report_store)
    return objects.ObjectWorkspace(access.tools(), actor, sources=cases,
                                   intents=intents, evidence=evidence, store_status=status,
                                   access=access,
                                   follow_ups=None if session is None or action_path is None
                                   else objects.FollowUps(action_path, session,
                                                          resolves=_resolver_of(runtime)),
                                   planner=planner, ask_bounds=ask_bounds)


def _resolver_of(runtime: CaseRuntime) -> Callable[[str, Actor], bool]:
    """Whether the runtime's case store resolves a citation for the reader now."""
    def resolves(handle: str, actor: Actor) -> bool:
        try:
            runtime.resolve_citation(handle, actor)
        except Exception:  # noqa: BLE001 - unresolvable for this reader: not cited
            return False
        return True
    return resolves


FIXTURE_SESSION_LABEL = {"label": "isolated-fixture", "fixture": True,
                         "note": ("FIXTURE: a --fixture console session over a directory no "
                                  "live session or ingestion wrote. Not real PLX data.")}


class TicketSessions:
    """The live console's identities: one per authenticated request.

    ``authenticate(ticket)`` verifies the request's signed session ticket
    (author ``human``) and resolves its employee now
    (``session_identity.employee_session``); a ticket that is invalid, expired,
    for another tenant or for an employee the Portal does not grant raises
    ``SessionIdentityRefused`` (a ``PermissionError``). Each employee gets its
    own ``CaseWorkspace`` whose actor is exactly ``employee:<oid>``; the runtime's
    gate decides every read for that employee on every call. With
    ``build_objects``, each employee also gets its own object workspace
    (``object_workspace(session)``): same actor, its grant resolved per request.

    With ``entra_resource`` (``operating.oidc_auth.EntraResource``) the bearer may
    instead be the employee's Entra access token (signed in with their PLX Microsoft
    account): verified per request (signature, tenant, audience, scope, lifetime) and
    resolved through the same resolver (``oidc_auth.entra_session``, channel
    ``console``); the state directory's replay guard must be given. A ticket is told
    from a token by its ``v1.`` prefix; without ``entra_resource`` a token is refused
    like any invalid ticket.
    """

    MAX_WORKSPACES = 64

    def __init__(self, resolver: Any, secret: str | None,
                 build_workspace: Callable[[SessionIdentity], CaseWorkspace], *,
                 clock: Callable[[], float] = time.time,
                 build_objects: Callable[[SessionIdentity], Any] | None = None,
                 replay_guard: session_identity.TicketReplayGuard | None = None,
                 entra_resource: Any = None) -> None:
        if entra_resource is not None:
            from operating.oidc_auth import EntraResource

            if not isinstance(entra_resource, EntraResource):
                raise ValueError("console_entra_resource_invalid")
            if replay_guard is None:
                raise ValueError("console_entra_sign_in_requires_replay_guard")
            if entra_resource.tenant_id != resolver.policy.entra_tenant_id:
                raise ValueError("entra_resource_tenant_must_equal_the_policy_tenant")
        self._entra = entra_resource
        self.resolver = resolver
        self._secret = secret
        self._build = build_workspace
        self._clock = clock
        self._workspaces: dict[str, CaseWorkspace] = {}
        self._build_objects = build_objects
        # The state directory's durable guard: a revoked ticket is refused on every
        # request, also after a restart (session_identity.replay_guard_for).
        self._replay_guard = replay_guard
        self._objects: dict[str, Any] = {}

    def authenticate(self, ticket: str) -> tuple[SessionIdentity, CaseWorkspace]:
        now = int(self._clock())
        if self._entra is not None and not ticket.startswith(
                session_identity.SESSION_TICKET_VERSION + "."):
            from operating import oidc_auth

            claims = oidc_auth.verify_access_token(ticket, self._entra, now=now)
            session = oidc_auth.entra_session(
                self.resolver, claims, replay_guard=self._replay_guard, now=now,
                channel=session_identity.CHANNEL_CONSOLE)
        elif self._secret is None:
            raise SessionIdentityRefused("session_secret_unavailable")
        else:
            session = session_identity.employee_session(
                self.resolver, channel=session_identity.CHANNEL_CONSOLE, ticket=ticket,
                secret=self._secret, now=now, replay_guard=self._replay_guard)
        key = session.employee.principal
        workspace = self._workspaces.get(key)
        if workspace is None:
            if len(self._workspaces) >= self.MAX_WORKSPACES:
                self._workspaces.pop(next(iter(self._workspaces)))
            workspace = self._workspaces[key] = self._build(session)
        return session, workspace

    def object_workspace(self, session: SessionIdentity) -> Any:
        """The authenticated employee's object workspace (None: not configured).
        Call only with the session ``authenticate`` returned for this request."""
        if self._build_objects is None:
            return None
        key = session.employee.principal
        objects_view = self._objects.get(key)
        if objects_view is None:
            if len(self._objects) >= self.MAX_WORKSPACES:
                self._objects.pop(next(iter(self._objects))).close()
            objects_view = self._objects[key] = self._build_objects(session)
        objects_view.bind(session)   # this request's session authors its reviews
        return objects_view

    def close(self) -> None:
        """Release every employee's object workspace (its host's store and work files)."""
        while self._objects:
            self._objects.pop(next(iter(self._objects))).close()


def evidence_for(marker: dict | None, state_dir: Path | None = None) -> dict | None:
    """What the UI says about the data: fixture when a seed marked the directory or a
    fixture session labelled it."""
    if marker is None:
        if state_dir is not None and session_identity.state_label(state_dir) == "fixture":
            return dict(FIXTURE_SESSION_LABEL)
        return None
    return {"label": EVIDENCE_LABEL, "fixture": True, "note": EVIDENCE_NOTE}


def main(argv: Sequence[str] | None = None, *, fixtures: Any = None,
         planner: Any = None) -> int:
    """``fixtures``: the fixture world (``seed``, ``build_world``, ``live_readers``), passed
    only by its own entry point; product code never supplies one. ``planner``: the fixture
    world's scripted Ask planner in place of Claude Code (with ``fixtures`` only)."""
    parser = argparse.ArgumentParser(prog="python3 -m operator_ui.case_console",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--source-mirror-audience", type=Path, default=None,
                        help="required unless DIR holds seeded fixtures")
    parser.add_argument("--employee-policy", type=Path, default=None,
                        help="plx-employee-permission-policy/v1 JSON (required live)")
    parser.add_argument("--portal-access", type=Path, default=None,
                        help="plx-portal-access-snapshot/v1 JSON of Portal user rows, re-read "
                             "per request; without it every employee is refused")
    parser.add_argument("--session-ticket-env", default=None,
                        help="live --open: environment variable holding the signed session "
                             "ticket (author 'human') of the employee who opens the cases")
    parser.add_argument("--entra-resource", type=Path, default=None,
                        help="live: plx-entra-resource/v1 JSON (operating.oidc_auth) of the "
                             "console's Entra app registration; employees then sign in with "
                             "their own Entra access token as the bearer")
    parser.add_argument("--fixture", action="store_true",
                        help="fixture/demo console: --principal names a free-text identity; "
                             "refused with any real source reader and on live state")
    parser.add_argument("--tenant", default=None,
                        help="live: must equal the employee policy's; fixture: default plx")
    parser.add_argument("--principal", action="append", default=[],
                        help="fixture principal (--fixture only)")
    parser.add_argument("--clearance", default=None,
                        help="fixture clearance (--fixture only; default internal)")
    parser.add_argument("--report-store", type=Path, default=None,
                        help="playbook report store (default: DIR/playbook-reports.sqlite "
                             "when it exists)")
    parser.add_argument("--action-store", type=Path, default=None,
                        help="goal-2 action-path store (default: DIR/actions.sqlite when it "
                             "exists): intents, and the follow-ups people propose")
    parser.add_argument("--mc-operator-email", default=None,
                        help="the MC operator (X-MC-Operator-Email) whose follow-up calls "
                             "are prepared for approval; never an approver")
    parser.add_argument("--mc-follow-up-bucket", default=None,
                        help="the MC bucket (BKT-...) a person chose for follow-up tasks")
    parser.add_argument("--live-source-audience", type=Path, default=None,
                        help="plx-live-source-audience/v1 file: enables the read-only "
                             "Graph/BC readers from the environment (live-unverified)")
    parser.add_argument("--live-sources", type=live_source_names, default=None,
                        help="comma-separated host sources whose live readers "
                             "--live-source-audience builds (operating.host passes its "
                             "config's; default: every reader the audience grants)")
    parser.add_argument("--sharepoint-drive", action="append", default=[])
    parser.add_argument("--ingest-sources", type=Path, default=None,
                        help="plx-ingest-sources/v1 file: adds the ingestion coverage")
    parser.add_argument("--open", action="append", default=[], metavar="KIND=IDENTIFIER",
                        help="open a case from the ingested captures for this actor before "
                             "serving: ao=, petra_po=, sales_order=, customer_po=, find="
                             "<identifier>, pick=TYPE[+LINKED_TYPE] or pick=TYPE>"
                             "LINKED_TYPE (operating.case_workspace_live: the generic "
                             "tools, offline)")
    parser.add_argument("--reinvestigate", action="store_true",
                        help="--open: investigate again even when the same request was "
                             "opened before with the same options and ingestion run")
    parser.add_argument("--sharepoint-original", action="store_true",
                        help="--open: attach the requested record's original from the "
                             "--originals-expected folder through the GET-only Graph reader "
                             "(needs --live-source-audience)")
    parser.add_argument("--originals-expected", type=Path, default=None,
                        help="plx-sharepoint-originals-expected/v1 file naming the originals "
                             "folder for --sharepoint-original")
    parser.add_argument("--ontology-store", type=Path, default=None,
                        help="projection store for the object workspace (plx.find/object/"
                             "around/timeline/process); default DIR/ontology.sqlite when "
                             "it exists")
    parser.add_argument("--project-ontology", action="store_true",
                        help="build DIR/ontology.sqlite from DIR's source mirror first "
                             "(offline: only captures already held)")
    parser.add_argument("--mail-capture-dir", type=Path, default=None,
                        help="object workspace: the versioned mail capture (integrations/"
                             "capture_mail.py); its records show per item for the reader")
    parser.add_argument("--sharepoint-capture-dir", type=Path, default=None,
                        help="object workspace: the versioned SharePoint capture; each file "
                             "re-gated per item for the reader")
    parser.add_argument("--host-captures-portal", action="store_true",
                        help="the host serving DIR captures the Portal into its mirror "
                             "(operating.host config 'portal'): answers report the Portal's "
                             "capture state, never that it is not configured")
    parser.add_argument("--marking", action="append", default=[],
                        help="fixture only: a marking the fixture reader holds (finance, "
                             "pii, ...); default none. A live employee holds no marking here")
    parser.add_argument("--ask", action="store_true",
                        help="object workspace: serve Ask, the employee agent loop with a "
                             "headless Claude Code planner (the tool results it reads for "
                             "the reader leave this host for the model provider)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--seed-fixtures", action="store_true",
                        help="write the fixture cases first (AO packets, a packaging "
                             "purchase order; isolated-fixture / injected-transport, not "
                             "real data)")
    args = parser.parse_args(argv)
    state_dir = args.state_dir.resolve()
    if args.marking and not args.fixture:
        parser.error("--marking names a fixture reader's marking (--fixture only)")
    if args.seed_fixtures and not args.fixture:
        parser.error("--seed-fixtures needs --fixture")
    if args.seed_fixtures and fixtures is None:
        parser.error("--seed-fixtures runs only from the fixture world's own entry point")
    session, resolver = _identity_from_args(parser, args)
    args.tenant = session.tenant if session is not None else resolver.policy.tenant
    if args.seed_fixtures:
        # Fixture rows carry real relation names: never mix them into existing state.
        if (state_dir / "case-runtime").exists() or fixture_marker(state_dir):
            parser.error("seeding needs a new state directory: "
                         f"{state_dir} already holds case state")
    real_readers = [flag for flag, given in (
        ("--live-source-audience", args.live_source_audience is not None),
        ("--sharepoint-drive", bool(args.sharepoint_drive)),
        ("--ingest-sources", args.ingest_sources is not None),
        ("--mail-capture-dir", args.mail_capture_dir is not None),
        ("--sharepoint-capture-dir", args.sharepoint_capture_dir is not None),
        ("--open", bool(args.open))) if given]
    from operating import case_workspace_live
    try:
        open_requests = [case_workspace_live.parse_open(text) for text in args.open]
    except case_workspace_live.OpenError as error:
        parser.error(str(error))
    if args.sharepoint_original and args.live_source_audience is None:
        parser.error("--sharepoint-original needs --live-source-audience")
    if args.sharepoint_original != (args.originals_expected is not None):
        parser.error("--sharepoint-original and --originals-expected go together")
    if real_readers and (args.seed_fixtures or fixture_marker(state_dir) is not None):
        # Fixture rows carry real relation names: never read real sources beside them.
        parser.error(f"{', '.join(real_readers)} cannot be used on a fixture state "
                     f"directory ({state_dir} was written by a seed)")
    if real_readers and args.fixture:
        parser.error(f"--fixture cannot be combined with {', '.join(real_readers)}: a "
                     "fixture session never reads a real source")
    entra_resource = None
    if args.entra_resource is not None:
        if resolver is None:
            parser.error("--entra-resource is a live console's sign-in (not with --fixture)")
        from operating.oidc_auth import EntraResource
        try:
            entra_resource = EntraResource.from_file(args.entra_resource)
        except (OSError, ValueError) as error:
            parser.error(f"--entra-resource is unusable: {str(error)[:200]}")
        if entra_resource.tenant_id != resolver.policy.entra_tenant_id:
            parser.error("--entra-resource names another tenant than the employee policy")
    if args.open and session is None:
        parser.error("a live --open needs --session-ticket-env: the employee who opens")
    try:
        # A fixture identity never touches live state, nor a live one fixture state.
        session_identity.bind_state_dir(state_dir, fixture=resolver is None)
    except SessionIdentityRefused as error:
        parser.error("session identity refused: " + error.reason)
    if (args.sharepoint_drive or args.live_sources is not None) \
            and args.live_source_audience is None:
        parser.error("--sharepoint-drive and --live-sources need --live-source-audience")
    if args.seed_fixtures:
        written = fixtures.seed(state_dir, args.tenant)
        print(f"Seeded {len(written['aos']) + 1} fixture cases "
              f"({EVIDENCE_LABEL}, not real PLX data)")
    marker = fixture_marker(state_dir)
    # A seed's in-process readers serve only a directory that seed wrote (its live audience).
    full_fixture = marker is not None and (state_dir / LIVE_AUDIENCE_FILE).is_file()
    if full_fixture and fixtures is None:
        parser.error(f"{state_dir} was written by a full fixture seed: serve it from the "
                     "fixture world's own entry point")
    audience_path = args.source_mirror_audience
    if audience_path is None and full_fixture:
        audience_path = state_dir / MIRROR_AUDIENCE_FILE
    if audience_path is None:
        parser.error("--source-mirror-audience is required")
    try:
        audience = AudienceMap.from_file(audience_path)
    except (ValueError, OSError) as error:
        parser.error(f"source mirror audience is invalid: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("source mirror audience tenant must equal --tenant")
    live = None
    originals = None
    if full_fixture:
        # The seed's in-process Graph fake: SharePoint packets and the mailbox.
        world = fixtures.build_world()
        live = fixtures.live_readers(world, state_dir / LIVE_AUDIENCE_FILE)
    elif args.live_source_audience is not None:
        from operating.case_runtime import build_live_readers
        drives = list(args.sharepoint_drive)
        if args.sharepoint_original:
            from operating.sharepoint_originals import folder_source
            originals = folder_source(args.originals_expected)
            drives.append(originals["drive_id"])
        try:
            live = build_live_readers(args.live_source_audience, sharepoint_drives=drives,
                                      sources=args.live_sources)
        except (ValueError, OSError) as error:
            parser.error(f"live source audience is invalid: {type(error).__name__}")
    ingestion = None
    if args.ingest_sources is not None:
        from operating.case_context_tools import IngestionStatusSource
        try:
            ingestion = IngestionStatusSource.from_file(state_dir, args.ingest_sources,
                                                        args.tenant)
        except (ValueError, OSError) as error:
            parser.error("ingest sources are invalid: " + str(error)[:200])
    runtime = CaseRuntime(state_dir, client=None, audience=audience_path, tenant=args.tenant,
                          live=live, employee_permissions=resolver,
                          require_employee=resolver is not None,
                          mail_capture_dir=args.mail_capture_dir)
    report_path = args.report_store or state_dir / "playbook-reports.sqlite"
    store = (report_store_for(runtime, report_path)
             if args.report_store or report_path.exists() else None)
    action_path = None
    action_file = args.action_store or state_dir / ACTIONS_FILE
    if args.action_store or action_file.exists():
        from operating.action_path import ActionPath
        from operating.goal2_mc_approval import load_approver_roster, load_bucket_audiences
        try:
            # Call approvers and the MC roster: the committed approver roster, with the
            # oids the host resolved at its start (an approver without one is refused,
            # call_approver_oid_unknown). Who reads the follow-up bucket: the committed
            # bucket audiences, each member through this console's employee gate.
            action_path = ActionPath(action_file, runtime.cases,
                                     **load_approver_roster(state_dir=state_dir)
                                     .path_options(),
                                     mc_operator_email=args.mc_operator_email,
                                     mc_follow_up_bucket=args.mc_follow_up_bucket,
                                     mc_follow_up_audience=load_bucket_audiences(
                                         tenant=args.tenant))
        except (ValueError, OSError) as error:
            parser.error(str(error)[:200])
    evidence = evidence_for(marker, state_dir)
    if evidence is None and case_workspace_live.ingested(state_dir):
        evidence = dict(case_workspace_live.MIRROR_EVIDENCE)
    ontology_path = args.ontology_store
    if args.project_ontology or args.seed_fixtures:
        ontology_path = ontology_path or state_dir / ONTOLOGY_FILE
        report = objects.project_mirror(state_dir, ontology_path)
        print(f"Projected {report['counts'].get('objects', 0)} objects into {ontology_path}")
        from datetime import datetime, timezone
        from operating import proactive_engine
        found = proactive_engine.after_build(ontology_path,
                                             as_of=datetime.now(timezone.utc).date())
        print(f"Proactive engine: {found['state']}, {found.get('gaps_found', 0)} gaps")
    elif ontology_path is None and (state_dir / ONTOLOGY_FILE).is_file():
        ontology_path = state_dir / ONTOLOGY_FILE
    if args.mail_capture_dir is not None or args.sharepoint_capture_dir is not None:
        # Case citations of captured mail and SharePoint resolve through the gates
        # plx.read reads the captures with, over the store it reads (as the MCP host).
        from operating.case_tools_host import CaseCaptures
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=args.mail_capture_dir,
                                        sharepoint_capture_dir=args.sharepoint_capture_dir,
                                        store_path=ontology_path)
    fixture_objects = None
    if planner is not None and (fixtures is None or not args.ask):
        parser.error("a scripted planner is the fixture console's, with --ask")
    if args.ask and planner is None:
        # PLX_ASK_PLANNER: claude-code (default) or messages; a Messages planner without
        # its key or model falls back to Claude Code (setting names only on stderr).
        from operating.messages_planner import ask_planner
        try:
            planner, selection = ask_planner(os.environ)
        except ValueError as error:
            raise SystemExit(f"ask planner: {error}") from None
        print(json.dumps({"ask_planner": selection}, sort_keys=True), file=sys.stderr)
    try:
        if open_requests:
            opener = case_workspace_live.CaseOpener(runtime, session.actor, state_dir,
                                                    audience=audience, originals=originals,
                                                    author_kind=session.author_kind)
            for kind, identifier in open_requests:
                result = opener.open_case(kind, identifier, reinvestigate=args.reinvestigate)
                # Counts and enums only: the case itself is on the console.
                print(json.dumps({key: result.get(key) for key in (
                    "request", "status", "missing") if result.get(key)}))
        def objects_for(identity: SessionIdentity) -> Any:
            return object_workspace_for(runtime, ontology_path, identity.actor,
                                        state_dir=state_dir,
                                        markings=args.marking if resolver is None else (),
                                        action_path=action_path, evidence=evidence,
                                        mail_capture_dir=args.mail_capture_dir,
                                        sharepoint_capture_dir=args.sharepoint_capture_dir,
                                        session=identity, planner=planner,
                                        report_store=store,
                                        portal_captured=args.host_captures_portal)

        def workspace_for(identity: SessionIdentity) -> CaseWorkspace:
            return CaseWorkspace(runtime, identity.actor, report_store=store,
                                 evidence=evidence, action_path=action_path,
                                 ingestion=ingestion)

        if ontology_path is not None:
            try:
                checked_store_status(ontology_path, state_dir)
                fixture_objects = objects_for(session) if resolver is None else None
            except (ValueError, OSError, sqlite3.Error) as error:
                parser.error(f"ontology store is unusable: {str(error)[:200]}")
        if resolver is None:
            serve(port=args.port, case_workspace=workspace_for(session),
                  session=session, object_workspace=fixture_objects)
        else:
            tickets = TicketSessions(
                resolver, session_identity.session_secret(os.environ), workspace_for,
                build_objects=objects_for if ontology_path is not None else None,
                replay_guard=session_identity.replay_guard_for(state_dir),
                entra_resource=entra_resource)
            try:
                journal: SessionJournal | None = SessionJournal.for_state_dir(state_dir)
            except (OSError, sqlite3.Error) as error:
                # A full or read-only volume costs the recorded session's journal, never
                # the console: one line naming the failure's class, no path or value.
                print(f"console journal: unavailable ({type(error).__name__}); "
                      "serving without a journal", file=sys.stderr)
                journal = None
            try:
                serve(port=args.port, ticket_sessions=tickets, journal=journal)
            finally:
                tickets.close()
    finally:
        if fixture_objects is not None:
            fixture_objects.close()
        runtime.close()
    return 0


def _identity_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace
                        ) -> tuple[SessionIdentity | None, Any]:
    """``(session, resolver)``: a fixture session and no resolver, or a resolver and the
    startup employee session (only for a live ``--open``; else None: every request
    brings its own identity). Exits on anything less; there is no default principal."""
    employee_flags = [flag for flag, given in (
        ("--employee-policy", args.employee_policy is not None),
        ("--portal-access", args.portal_access is not None),
        ("--session-ticket-env", args.session_ticket_env is not None)) if given]
    if args.fixture:
        if employee_flags:
            parser.error(f"--fixture cannot be combined with {', '.join(employee_flags)}")
        try:
            return session_identity.fixture_session(
                args.tenant or "plx", args.principal, channel=session_identity.CHANNEL_CONSOLE,
                clearance=args.clearance or "internal"), None
        except SessionIdentityRefused as error:
            parser.error("--fixture needs --principal: " + error.reason)
        except ValueError:
            parser.error("--clearance is invalid")
    if args.principal or args.clearance is not None:
        parser.error("--principal and --clearance name a fixture identity (--fixture only); "
                     "a live console takes each request's employee from its session ticket")
    if args.employee_policy is None:
        parser.error("a live console needs --employee-policy (or --fixture for a demo)")
    try:
        resolver = employee_resolver(args.employee_policy, args.portal_access)
    except ValueError as error:
        parser.error(str(error))
    if args.tenant is not None and args.tenant != resolver.policy.tenant:
        parser.error("--tenant must equal the employee policy's tenant")
    if args.session_ticket_env is None:
        return None, resolver
    try:
        return session_identity.employee_session(
            resolver, channel=session_identity.CHANNEL_CONSOLE,
            ticket=os.environ.get(args.session_ticket_env) or "",
            secret=session_identity.session_secret(os.environ),
            replay_guard=session_identity.replay_guard_for(args.state_dir.resolve())), resolver
    except SessionIdentityRefused as error:
        parser.error("session identity refused: " + error.reason)


def employee_resolver(employee_policy: Path, portal_access: Path | None) -> Any:
    """The live console's ``EmployeePermissionResolver``: the employee policy and the Portal
    access snapshot (re-read per request; without one every employee is refused).
    ``ValueError`` names what is unusable."""
    from operating.employee_permissions import (EmployeePermissionPolicy,
                                                EmployeePermissionResolver,
                                                PortalAccessSnapshot)

    try:
        policy = EmployeePermissionPolicy.from_file(employee_policy)
    except (ValueError, OSError, UnicodeDecodeError) as error:
        raise ValueError("employee policy is invalid: " + (
            str(error) if isinstance(error, ValueError) else type(error).__name__)) from None
    access: Callable[[Any], Any] = lambda principal: None  # noqa: E731
    if portal_access is not None:
        try:
            access = PortalAccessSnapshot(portal_access)
        except (ValueError, OSError, UnicodeDecodeError) as error:
            raise ValueError("portal access snapshot is invalid: " + (
                str(error) if isinstance(error, ValueError) else type(error).__name__)) from None
    return EmployeePermissionResolver(policy, access)


if __name__ == "__main__":
    # The URL line is how a person (and tests/test_case_console_smoke.py) finds the
    # console: on a pipe, block buffering would hold it until the server exits.
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
