"""The case host's process: serving lines, host arguments and sessions."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from ontology.core import Actor
from ontology.store_ddl import FORMAT as STORE_FORMAT

from . import (
    agent_definitions, case_tools_host, confirmation_forms, kind_confirmation, ontology_tools,
    playbook_tools, session_identity
)
from .ontology_host import check_marking_grants
from .case_runtime import CaseRuntime, LIVE_READER_SOURCES, live_source_names
from .session_identity import SessionIdentityRefused


ACTION_PATH_DIR = "action-path"


ACTION_PATH_FILE = "actions.db"


def add_host_arguments(parser: argparse.ArgumentParser) -> None:
    """The state, source and store options of an MCP host over one state directory (the
    stdio host here, the remote host ``operating.mcp_http_host``)."""
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--enable-local-pdf-ocr", action="store_true")
    parser.add_argument("--enable-workbench-read-api", action="store_true")
    parser.add_argument("--workbench-read-api-base-url")
    parser.add_argument("--workbench-read-api-key-env", default="PLX_WORKBENCH_READ_API_KEY")
    parser.add_argument("--source-mirror-audience", type=Path, required=True,
                        help="plx-source-mirror-audience/v1 JSON granting the case runtime "
                             "tools per principal (default deny for anything it omits)")
    parser.add_argument("--live-source-audience", type=Path,
                        help="plx-live-source-audience/v1 JSON enabling graph-mail://, "
                             "sharepoint:// and bc:// citations in cases through the "
                             "GET-only readers, built from MICROSOFT_GRAPH_*/SHAREPOINT_*/BC_* "
                             "environment variables (a source without them stays "
                             "source_not_configured)")
    parser.add_argument("--live-sources", type=live_source_names,
                        help="comma-separated host sources (" + ", ".join(
                            sorted(LIVE_READER_SOURCES)) + "; empty: none) whose live "
                             "readers --live-source-audience builds: only their variables "
                             "are read (operating.host passes its config's; default: every "
                             "reader the audience grants)")
    parser.add_argument("--fixture-capture-audience", type=Path,
                        help="fixture sessions only: plx-live-source-audience/v1 JSON gating "
                             "the host captures under --state-dir (its bc-capture) as a live "
                             "audience gates them; builds no reader and reads no credential "
                             "(refused with --live-source-audience and on a non-fixture "
                             "session)")
    parser.add_argument("--sharepoint-drive", action="append", default=[],
                        help="extra SharePoint drive id readable in cases (besides "
                             "SHAREPOINT_DRIVE_ID), e.g. the QA/QC site's Documents drive")
    parser.add_argument("--ingest-sources", type=Path,
                        help="plx-ingest-sources/v1 JSON of the operating.ingestion runs "
                             "writing under --state-dir; every answer then carries their "
                             "freshness in _meta (trimmed per session actor)")
    parser.add_argument("--ontology-store", type=Path,
                        help=f"{STORE_FORMAT} SQLite file built by the projector "
                             "(ontology.project) that the ontology tools read (default: an "
                             "empty one; plx.find then answers from the live readers). A "
                             "unit is readable when the reader's audience lets the session "
                             "actor read it now; a marking is held only as "
                             "--marking-grant grants it")
    parser.add_argument("--marking-grant", action="append", default=[], type=_marking_grant,
                        metavar="MARKING=PRINCIPAL",
                        help="the principal holds the marking (the host config's "
                             "marking_grants; repeatable); without one nobody holds it")
    parser.add_argument("--knowledge-namespaces", type=Path,
                        help="plx-knowledge-namespaces/v1 JSON (integrations.knowledge_source) "
                             "granting readers the brain:<namespaceKey> units of the ontology "
                             "store, re-read every call (personal namespaces never; without "
                             "it, or when it does not load, no brain item is served)")
    parser.add_argument("--mail-capture-dir", type=Path,
                        help="the versioned mail capture (integrations.capture_mail) whose "
                             "projected mail plx.documents and plx.read serve: mail:<mailbox> "
                             "units are granted per reader from the capture's audience check "
                             "and every message is re-gated per item (needs "
                             "--live-source-audience)")
    parser.add_argument("--sharepoint-capture-dir", type=Path,
                        help="the state directory of the versioned SharePoint capture "
                             "(integrations.capture_sharepoint) whose projected files the "
                             "ontology tools serve, labelled as of capture: "
                             "sharepoint:<drive> units are granted per reader from the "
                             "capture's audience check and every file is re-gated per item "
                             "(re-read live where a --sharepoint-drive reader exists; needs "
                             "--live-source-audience)")
    parser.add_argument("--sharepoint-capture-max-age-hours", type=float,
                        help="capture-time permissions of a captured SharePoint file decide "
                             "alone only for a capture younger than this; an older one is "
                             "served only after a live re-read succeeds (default "
                             f"{CAPTURE_AGE_HOURS_DEFAULT:g}, at most "
                             f"{CAPTURE_AGE_HOURS_MAX:g}; needs --sharepoint-capture-dir)")
    parser.add_argument("--sharepoint-live-rereads", type=int,
                        help="live SharePoint re-reads one tool call may make (default "
                             f"{case_tools_host.MAX_LIVE_REREADS}, at most "
                             f"{LIVE_REREADS_MAX}; needs --sharepoint-capture-dir)")
    parser.add_argument("--sharepoint-live-gets", type=int,
                        help="Graph requests one tool call's live SharePoint reads may send "
                             f"(default {case_tools_host.MAX_LIVE_GETS}, at most "
                             f"{LIVE_GETS_MAX}; needs --sharepoint-capture-dir)")
    definitions = agent_definitions.load()
    parser.add_argument("--agent", choices=sorted(definitions.agents),
                        default=definitions.default,
                        help="the agent definition this session runs "
                             "(operating/agent_definitions.json; default: its default_agent)")
    parser.add_argument("--host-captures-portal", action="store_true",
                        help="the host serving --state-dir captures the Portal into its "
                             "mirror (operating.host config 'portal'): answers report the "
                             "Portal's capture state, never that it is not configured")
    parser.add_argument("--freshness-bound", action="append", default=[],
                        metavar="SOURCE=SECONDS",
                        help="how old the host's own capture of a source family (portal_api, "
                             "bc, graph_mail, sharepoint, teams, mission_control) may be "
                             "before answers state it stale (operating.host "
                             "freshness_bounds, from the configured ingest cadence)")
    parser.add_argument("--enable-action-path", action="store_true",
                        help="serve plx.act over a goal-2 action store under "
                             "--state-dir (operating.action_path); no call approvers, MC "
                             "operator, roster or verify readers are configured, so nothing "
                             "can be approved, requested or dispatched")
    parser.add_argument("--playbook-proposals", type=Path,
                        help=f"{playbook_tools.CONFIG_SCHEMA} JSON naming the playbook "
                             "stewards (employee principals) and the frozen case ids: serves "
                             "plx.playbook_propose/_evaluate/_adopt (--agent plx.agent.playbook) "
                             "over DIR/playbook-reports.sqlite, adopting into DIR/playbooks "
                             "(never the shipped operating/playbooks); without it they refuse")


def check_host_arguments(parser: argparse.ArgumentParser,
                         args: argparse.Namespace) -> dict[str, Any]:
    """Usage errors of ``add_host_arguments`` options; returns the capture bounds."""
    if args.live_sources is not None and args.live_source_audience is None:
        parser.error("--live-sources needs --live-source-audience")
    if args.sharepoint_drive and args.live_source_audience is None:
        parser.error("--sharepoint-drive needs --live-source-audience")
    if args.mail_capture_dir is not None and args.live_source_audience is None:
        parser.error("--mail-capture-dir needs --live-source-audience")
    if args.sharepoint_capture_dir is not None and args.live_source_audience is None:
        parser.error("--sharepoint-capture-dir needs --live-source-audience")
    if args.fixture_capture_audience is not None and not getattr(args, "fixture", False):
        parser.error("--fixture-capture-audience needs a --fixture session")
    if args.fixture_capture_audience is not None and args.live_source_audience is not None:
        parser.error("--fixture-capture-audience is refused with --live-source-audience")
    try:
        freshness_bounds(getattr(args, "freshness_bound", None) or ())
    except ValueError as error:
        parser.error(str(error))
    return _sharepoint_capture_bounds(parser, args)


# Source families a capture bound may name (the contract's ``$defs/freshness`` sources a
# host captures).
BOUND_SOURCES = frozenset({"portal_api", "bc", "graph_mail", "sharepoint", "teams",
                           "mission_control", "meetings"})


def freshness_bounds(given: Iterable[str]) -> dict[str, float]:
    """``--freshness-bound SOURCE=SECONDS`` values as source family -> seconds; an unknown
    family, a repeated one or a bound outside (0, ten years] is a ``ValueError``."""
    out: dict[str, float] = {}
    for item in given:
        source, _, seconds = str(item).partition("=")
        try:
            value = float(seconds)
        except ValueError:
            value = float("nan")
        if source not in BOUND_SOURCES or source in out or not 0 < value <= 315360000:
            raise ValueError(f"--freshness-bound {item!r}: SOURCE=SECONDS with SOURCE one of "
                             f"{', '.join(sorted(BOUND_SOURCES))}, once each, and SECONDS "
                             "in (0, 315360000]")
        out[source] = value
    return out


def real_source_flags(args: argparse.Namespace) -> list[str]:
    """The given options that read a real source (never with a fixture or dev session)."""
    return [flag for flag, given in (
        ("--enable-workbench-read-api", args.enable_workbench_read_api),
        ("--live-source-audience", args.live_source_audience is not None),
        ("--sharepoint-drive", bool(args.sharepoint_drive)),
        ("--ingest-sources", args.ingest_sources is not None),
        ("--mail-capture-dir", args.mail_capture_dir is not None),
        ("--sharepoint-capture-dir", args.sharepoint_capture_dir is not None)) if given]


def _marking_grant(value: str) -> tuple[str, str]:
    """``--marking-grant MARKING=PRINCIPAL``, checked as the host config's grants are."""
    marking, _, principal = value.partition("=")
    try:
        check_marking_grants({marking: [principal]})
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid marking grant: {value!r}") from None
    return marking, principal


def _marking_grants(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for marking, principal in pairs:
        out.setdefault(marking, []).append(principal)
    return out


def _resolved(given: Path | None) -> Path | None:
    return None if given is None else given.resolve()


def playbook_session_tools(runtime: CaseRuntime, actor: Actor, state_dir: Path,
                           config: dict[str, Any]) -> playbook_tools.PlaybookTools:
    """One session's playbook tools: ``playbook_eval.proposal_store`` over the state
    directory's report store (the case console's: a report is readable iff its case is, and
    the session actor is its one recorder), with the host's stewards and frozen cases.
    Adopted versions go to the state directory's overlay (``playbooks.overlay_dir``), loaded
    after the shipped directory, which is never written."""
    from operator_api.workspace.case_console import REPORTS_FILE, report_store_for
    from .playbook_eval import proposal_store
    from .playbooks import overlay_dir

    reports = report_store_for(runtime, state_dir / REPORTS_FILE, recorders=[actor])
    store = proposal_store(reports, frozen_cases=list(config["frozen_cases"]),
                           stewards=config["stewards"], overlay=overlay_dir(state_dir))
    # The adoption forms live with the host's other confirmation forms (one store).
    forms = confirmation_forms.ConfirmationForms(kind_confirmation.store_path(state_dir))
    return playbook_tools.PlaybookTools(store, ontology_tools.load_contracts(), forms)


def prime_unbound(args: argparse.Namespace) -> dict[str, Any]:
    """What this host's first call would read for every reader alike, read before it has
    a reader: the store and delta-layer facts (``ontology_host.prime_unbound``), the
    SharePoint capture's file names and identifier index (``case_tools_host.
    warm_sharepoint``) and the captured attachment mailboxes (``case_tools_host.
    prime_attachment_mailboxes``), each kept per process (and the facts beside their store
    files for every process). Only the command line's paths: no ticket, identity, grant
    or session state exists yet. Never raises."""
    primed: dict[str, Any] = {}
    try:
        sharepoint = getattr(args, "sharepoint_capture_dir", None)
        mail = getattr(args, "mail_capture_dir", None)
        store = getattr(args, "ontology_store", None)
        if sharepoint is not None:
            case_tools_host.warm_sharepoint(_resolved(sharepoint))
        if mail is not None:
            case_tools_host.prime_attachment_mailboxes(_resolved(mail))
        if store is not None:
            from operating import ontology_host
            primed = ontology_host.prime_unbound(
                store, tenant=getattr(args, "tenant", None) or "unbound")
    except Exception as error:  # noqa: BLE001 - a warm-up never fails a host
        primed["error"] = type(error).__name__
    return primed


CAPTURE_AGE_HOURS_DEFAULT = case_tools_host.MAX_CAPTURE_AGE.total_seconds() / 3600


CAPTURE_AGE_HOURS_MAX = 168.0


LIVE_REREADS_MAX = 500


LIVE_GETS_MAX = 2000


def _sharepoint_capture_bounds(parser: argparse.ArgumentParser,
                               args: argparse.Namespace) -> dict[str, Any]:
    """``CaptureCaseHostTools`` capture-read bounds from the command line (unset: the
    host's defaults); a bound outside its range, or without a SharePoint capture, is a
    usage error."""
    given = {"max_capture_age": args.sharepoint_capture_max_age_hours,
             "max_live_rereads": args.sharepoint_live_rereads,
             "max_live_gets": args.sharepoint_live_gets}
    given = {key: value for key, value in given.items() if value is not None}
    if given and args.sharepoint_capture_dir is None:
        parser.error("--sharepoint-capture-max-age-hours, --sharepoint-live-rereads and "
                     "--sharepoint-live-gets need --sharepoint-capture-dir")
    hours = given.get("max_capture_age")
    if hours is not None:
        if not 0 < hours <= CAPTURE_AGE_HOURS_MAX:  # also refuses nan
            parser.error(f"--sharepoint-capture-max-age-hours must be in "
                         f"(0, {CAPTURE_AGE_HOURS_MAX:g}]")
        given["max_capture_age"] = timedelta(hours=hours)
    for key, option, top in (("max_live_rereads", "--sharepoint-live-rereads",
                              LIVE_REREADS_MAX),
                             ("max_live_gets", "--sharepoint-live-gets", LIVE_GETS_MAX)):
        if key in given and not 0 <= given[key] <= top:
            parser.error(f"{option} must be in [0, {top}]")
    return given


def _foreign_store(store_path: Path, state_dir: Path) -> str | None:
    """Why a non-live host must not serve ``store_path`` (projected from another capture
    generation than ``state_dir``'s, or unverifiable), else None."""
    from operator_api.workspace import object_workspace

    try:
        state = object_workspace.StoreStatus(store_path, state_dir).read()
    except (ValueError, OSError, sqlite3.Error) as error:
        return f"unknown: {type(error).__name__}"
    if state.get("state") in ("foreign", "unknown"):
        return f"{state['state']}: {state.get('detail') or ''}"[:300]
    return None


def _session_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace, *,
                       directory: Callable[[Any], Any] | None = None):
    """The host's one session identity and resolver; exits on anything less.

    There is no default principal: a live session is a configured, verified
    employee (``--employee-oid`` or ``--session-ticket-env``, resolved through
    ``--employee-policy`` and ``--portal-access``); ``--principal`` is accepted
    only with ``--fixture`` and no real source reader. ``--employee-oid`` names the
    employee without authenticating anyone: it is a development session behind
    ``--dev-configured-employee``, refused with every real source reader and capture
    (and, in ``main``, on live state and with a foreign ontology store). A start-up
    ticket is single use: its jti is consumed in the durable replay guard under
    ``--state-dir`` (``session_identity.replay_guard_for``), so a replayed ticket is
    refused across restarts within its TTL.
    """
    from .employee_permissions import (EmployeePermissionResolver, PolicyFile,
                                       PortalAccessSnapshot)

    employee_flags = [flag for flag, given in (
        ("--employee-policy", args.employee_policy is not None),
        ("--portal-access", args.portal_access is not None),
        ("--portal-access-max-age-hours", args.portal_access_max_age_hours is not None),
        ("--employee-oid", args.employee_oid is not None),
        ("--session-ticket-env", args.session_ticket_env is not None)) if given]
    real = real_source_flags(args)
    if getattr(args, "eval_principal", False):
        return _eval_session_from_args(parser, args)
    if args.dev_configured_employee:
        if args.fixture or args.session_ticket_env is not None or args.employee_oid is None:
            parser.error("--dev-configured-employee takes --employee-oid only (not "
                         "--fixture or --session-ticket-env)")
        if real:
            parser.error(f"--dev-configured-employee cannot be combined with "
                         f"{', '.join(real)}: an unauthenticated session never reads a "
                         "real source (use --session-ticket-env)")
    elif args.employee_oid is not None:
        parser.error("--employee-oid is development only (--dev-configured-employee): a "
                     "live session authenticates with --session-ticket-env")
    if args.fixture:
        if real or employee_flags:
            parser.error(f"--fixture cannot be combined with {', '.join(real + employee_flags)}"
                         ": a fixture session never reads a real source")
        try:
            session = session_identity.fixture_session(
                args.tenant or "plx", args.principal, channel=session_identity.CHANNEL_MCP,
                clearance=args.clearance or "internal")
        except SessionIdentityRefused as error:
            parser.error("--fixture needs --principal: " + error.reason)
        return session, None
    if args.principal or args.clearance is not None:
        parser.error("--principal and --clearance name a fixture identity (--fixture only); "
                     "a live session is one employee: --session-ticket-env")
    if args.employee_policy is None or (args.employee_oid is None) == (
            args.session_ticket_env is None):
        parser.error("a session needs --employee-policy and exactly one of "
                     "--session-ticket-env or --dev-configured-employee --employee-oid "
                     "(or --fixture for a demo)")
    try:
        # re-read when the file changes: an edit applies on the next call without a
        # restart; an edit that does not load keeps the last good policy and raises the
        # host alarm (SEC-005)
        policy_file = PolicyFile(args.employee_policy)
        policy = policy_file.policy
    except (ValueError, OSError, UnicodeDecodeError) as error:
        parser.error("employee policy is invalid: " +
                     (str(error) if isinstance(error, ValueError) else type(error).__name__))
    if args.tenant is not None and args.tenant != policy.tenant:
        parser.error("--tenant must equal the employee policy's tenant")
    portal_access: Callable[[Any], Any] = lambda principal: None  # noqa: E731
    if args.portal_access is not None:
        try:
            portal_access = PortalAccessSnapshot(
                args.portal_access, max_age_hours=args.portal_access_max_age_hours)
        except (ValueError, OSError, UnicodeDecodeError) as error:
            parser.error("portal access snapshot is invalid: " +
                         (str(error) if isinstance(error, ValueError) else type(error).__name__))
    # The people exclusion gates the job-title path of a principal (entra_job_title_keywords);
    # unset or invalid, no job title grants anything.
    # Re-read when the list file changes or every few minutes (``ReloadingExclusion``).
    from integrations.people_exclusion import ReloadingExclusion
    resolver = EmployeePermissionResolver(policy_file, portal_access, directory=directory,
                                          people_exclusion=ReloadingExclusion.from_env(
                                              os.environ, state_dir=args.state_dir))
    ticket, replay_guard = None, None
    if args.session_ticket_env is not None:
        ticket = os.environ.get(args.session_ticket_env) or ""
        replay_guard = session_identity.replay_guard_for(args.state_dir.resolve())
    try:
        session = session_identity.employee_session(
            resolver, channel=session_identity.CHANNEL_MCP, object_id=args.employee_oid,
            ticket=ticket, secret=session_identity.session_secret(os.environ),
            replay_guard=replay_guard, single_use=ticket is not None)
    except SessionIdentityRefused as error:
        parser.error("session identity refused: " + error.reason)
    return session, resolver


def _eval_session_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace):
    """``--eval-principal``: the declared eval principal's session from a start-up ticket
    (``operating.eval_principal``), resolved by its own resolver (no Portal row, no
    directory) and refused with the action path or a marking it does not declare."""
    from . import eval_principal
    from .employee_permissions import EmployeePermissionPolicy

    if args.fixture or args.dev_configured_employee or args.employee_oid is not None \
            or args.portal_access is not None or args.principal \
            or args.clearance is not None:
        parser.error("--eval-principal takes --session-ticket-env and --employee-policy "
                     "only (not --fixture, --dev-configured-employee, --employee-oid, "
                     "--portal-access, --principal or --clearance)")
    if args.session_ticket_env is None or args.employee_policy is None:
        parser.error("--eval-principal needs --session-ticket-env and --employee-policy")
    try:
        principal = eval_principal.load()
        policy = EmployeePermissionPolicy.from_file(args.employee_policy)
    except (ValueError, OSError, UnicodeDecodeError) as error:
        parser.error("session identity refused: " +
                     (str(error) if isinstance(error, ValueError) else type(error).__name__))
    if args.tenant is not None and args.tenant != policy.tenant:
        parser.error("--tenant must equal the employee policy's tenant")
    try:
        eval_principal.check_host(principal, action_path=args.enable_action_path,
                                  marking_grants=_marking_grants(args.marking_grant))
        resolver = eval_principal.EvalPrincipalResolver(policy, principal)
        session = eval_principal.eval_session(
            resolver, ticket=os.environ.get(args.session_ticket_env) or "",
            secret=session_identity.session_secret(os.environ),
            replay_guard=session_identity.replay_guard_for(args.state_dir.resolve()))
    except SessionIdentityRefused as error:
        parser.error("session identity refused: " + error.reason)
    return session, resolver
