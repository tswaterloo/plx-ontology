"""Case runtime: the source mirror and the case record joined for MCP clients.

Implements docs/reference/case-runtime.md section 3. The investigating agent is the MCP
client (Claude); this module only binds the two deterministic parts together:

* ``integrations.source_mirror.SourceMirror`` answers catalog/rows/resolve for
  the reading actor under a default-deny relation-family audience, and
* ``operating.cases.CaseStore`` records findings, calculations, gaps and
  proposals, re-resolving every citation for the reading actor on every read.

The runtime resolves these citation schemes, all for the calling actor and
after an audience check: ``api://`` rows through the mirror,
``original://`` originals through ``operating.case_originals.OriginalStore``
(bytes re-hashed on every resolve), and the live sources through the read-only
readers the host configured (``LiveReaders``, docs/reference/case-runtime.md section 7):
``graph-mail://`` (``GraphMailReader``), ``sharepoint://`` (one
``SharePointReader`` per configured drive) and ``bc://``
(``BusinessCentralReader``). A live scheme whose reader is not configured
refuses with ``source_not_configured``; every other scheme fails closed (the
case store records nothing on write and withholds on read). Live citations are
re-read from the source on every resolve (nothing is cached here): ``current``
yields a content-stable view of the cited version, ``changed_since`` a view
without the record, so its digest never matches what was cited and a finding
on it is never presented as supported. Nothing here dispatches a proposal or
writes to a business system.

Mail in cases (``operating.case_mail``): a ``graph-mail://`` citation is either
a message (bare, or body-pinned ``#body_sha256=..&shown_sha256=..``) or a file
attachment pinned to its bytes (``#attachment=<id>&sha256=<hex>``,
``integrations.graph_evidence``), re-read and re-hashed on every resolve;
``read_mail_original`` returns the body or bytes of a pinned citation through
the same GET-only reader, to a person's principals only. A PDF attachment can
be stored as an ``original://`` (``attach_mail_attachment_original``) whose
every read re-decides the message's readability for the reader. The mail scope
(``operating.case_mail_scope``; default: the built-in allowlist, the only
opt-out being the explicitly named ``MailScope.unrestricted()``) refuses every
mail read of a mailbox outside it before any message is requested, and every
record read is checked against it again. Case views of a cited attachment
re-read the message metadata for the reader but download the bytes again only
when the message's changeKey moved; ``read_mail_original`` always downloads.

BC capture handles (``bc://…@<generation>-<capture-id>``, the citations of a projection
built from the host's BC capture, ``ontology.project_bc``) are not live citations: the
GET-only BC reader refuses them. They resolve here, for every consumer (cases, ``plx.read``,
the ``plx.investigate`` claim gate), to exactly the revision they name in the host's BC
capture store (``operating.host`` ``<state>/bc-capture/capture.sqlite``), read on the one
read-only connection the runtime keeps while the file is the same (``CaptureStore.
open_read_only``: nothing written, never waits on a capture's write lock) under the live
audience for the employee-gated actor, with the store's per-record visibility (a hidden
record and an uncaptured one read alike). The values are BC as captured at the revision's
``recorded_at`` (in every view), never a live read: a revision a later capture superseded
reads as ``changed_since`` and is never support, and a change in BC after the last capture
is not seen until the next capture.

Teams channel and chat message handles (``teams-channel://<team>/<channel>/<message>@
<generation>-<capture-id>``, ``teams-chat://<chat>/<message>@...``, the citations
``ontology.project_graph.TeamsCaptures`` projects) resolve the same way from the host's Teams
capture store (``<state>/teams-capture/capture.db``, ``integrations.capture_teams``): visible
only when the live audience grants the channel or chat and the reader's identities are in the
message's and the conversation's latest captured membership (a reader outside it, a withheld
one and an uncaptured handle read alike). For an
employee those identities are the employee's own, decided by the one item ACL check mail and
SharePoint use (``EmployeeEntitlement.item_acl_allows``: object id, addresses and the
``group:<id>`` of a live membership; a channel's ``read`` or ``derived`` membership, a chat's
``read`` one), never the audience file's static ``identities`` map, so a role principal mapped
to a member's address grants nothing and a member removed from a team or chat upstream loses
it on the next read. A message an excluded person sent, or of a chat one is in, reads alike
for every reader however long ago it was captured (the people exclusion is re-checked when a
mail or Teams capture is served; an unavailable guard withholds them all). This one
resolver is the host's item gate (``ontology_tools.item_gate_from_case_runtime``) and the
claim gate's row reader (``claim_row``), so both decide a Teams row alike.

SharePoint list item handles (``sharepoint-list://<site>/<list>/<item>@<generation>-
<capture-id>``, the citations of the ``SharePointListItem`` objects ``ontology.project
.SharePointCaptures`` projects) resolve the same way from the host's list-item capture store
(``<state>/sharepoint-list-capture/capture.db``, ``integrations.capture_sharepoint_lists``):
visible only when the live audience grants the list (``sharepoint:list:<site>:<list>``) and,
where it requires a source ACL, the reader's identities are in the item's captured
permissions and its current version's; for an employee always, decided by the same item ACL
check (a completely ``read`` permission list), never the audience file's static identities.

Mission Control handles (``mc://<scope>/<collection>/<id>@<generation>-<capture-id>``, the
citations ``ontology.project_mc`` projects: tasks, approval gates, task events, buckets,
knowledge and brain items) resolve the same way from the host's MC capture store
(``<state>/mc-capture/capture.sqlite``, ``integrations.mc_sources``), as BC handles do: the
live audience decides the collection (``mc:<scope>:<collection>``) and, for a collection MC
scopes per project, the version's project (``mc:<scope>:project:<project>``) for the
employee-gated actor. MC is never re-read live here (its reads are MCP tool calls, and only
the capture worker makes them): the values are MC as captured, a revision a later capture
superseded is ``changed_since``, and a decision made in MC after the last capture is not seen
until the next one. This is the resolver ``plx.investigate``'s decision evidence (F11) re-reads
an approval's or a task event's time through (``citation_rows``).

Captured mail and SharePoint go through the same capture path (``_from_capture``): their
handles are live-shaped, so a version resolves from a capture where the host holds one and
it holds that version for the reader, through the gate ``plx.read`` reads it with: a
captured version that gate refuses is unresolvable (never read live in its place), a
superseded one is ``changed_since``, and one the capture does not hold for the reader goes
to the live reader as before. A ``graph-mail://`` version is read first from the runtime's
mail capture (``mail_capture_dir``: the host's document captures' when they name one, else
the runtime's own, by default the state directory, as BC's), so the claim gate, a case's
digest and ``plx.read`` read one row of the exact ``@changeKey`` version (decision
2026-10-08-mail-citation-one-resolver); a ``sharepoint://`` version only where the host
serves its document captures (``CaseRuntime.captures``,
``operating.case_tools_host.CaseCaptures``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Collection, Mapping, Sequence

from integrations.capture_store import CaptureError, CaptureStore, is_capture_handle
from integrations.http import SourceUnavailable
from integrations.live_source_guard import (LiveSourceAudience, NotVisible,
                                            ReadOnlyGuardError, parse_handle)
from integrations.mirror_capture import store_from_env
from integrations.source_mirror import AudienceMap, SourceMirror, citation_generation
from ontology.core import Actor

from .capture_age import capture_decides
from .case_links import LinkContracts
from .case_mail_scope import OUTSIDE_SCOPE as MAIL_OUTSIDE_SCOPE, MailScope
from .case_originals import (RECORDED_CAPTURE_SOURCE, SCHEME as ORIGINAL_SCHEME,
                             OriginalError, OriginalStore)
from .cases import CaseStore, CitationUnresolvable
from .employee_permissions import (MAILBOX_ACCESS_UNDETERMINED, EmployeeActorGate,
                                   EmployeePermissionResolver, gate_actor_methods)
from .source_units import SourceUnitContracts

CASE_RUNTIME_DIR = "case-runtime"
MIRROR_FILE = "source-mirror.sqlite"
CASES_FILE = "cases.sqlite"
MAIL_SCHEME = "graph-mail://"
SHAREPOINT_SCHEME = "sharepoint://"
BC_SCHEME = "bc://"
TEAMS_CHANNEL_SCHEME = "teams-channel://"
TEAMS_CHAT_SCHEME = "teams-chat://"
TEAMS_SCHEMES = (TEAMS_CHANNEL_SCHEME, TEAMS_CHAT_SCHEME)
SHAREPOINT_LIST_SCHEME = "sharepoint-list://"
MC_SCHEME = "mc://"
LIVE_SCHEMES = (MAIL_SCHEME, SHAREPOINT_SCHEME, BC_SCHEME)
SUPPORTED_SCHEMES = (("api://", ORIGINAL_SCHEME) + LIVE_SCHEMES + TEAMS_SCHEMES
                     + (SHAREPOINT_LIST_SCHEME, MC_SCHEME))
# Mail body text hashed for a citation: the first N characters of the plain-text
# body (Graph renders it with outlook.body-content-type="text").
MAIL_BODY_CHARS = 20_000
# Record keys that can change without the cited version changing (ACL grants,
# a record hash that covers them, the minted handle); never digested.
_LIVE_VOLATILE = frozenset({"source_acl", "content_sha256", "citation", "body_text"})
# The host-config sources (``operating.host``) a live-reader filter may name. Teams has
# no live reader (its citations resolve from the host's capture).
LIVE_READER_SOURCES = frozenset({"mail", "sharepoint", "teams", "bc"})
# The sources a live reader reads, each the live-audience resource family it reads
# (``mail:``, ``sharepoint:``, ``bc:``): a source no audience pattern grants gets no reader.
_LIVE_READERS = frozenset({"mail", "sharepoint", "bc"})
LIMITATIONS = (
    "Advisory: mirror pages come from a reporting replica, are non-atomic and "
    "do not prove deletions; nothing here certifies current business state.",
    "Proposals are recorded only; they are never dispatched and no business "
    "system is written.",
    "api:// (mirror rows), original://, teams-channel:// and teams-chat:// (Teams "
    "capture), sharepoint-list:// (SharePoint list-item capture), mc:// (Mission "
    "Control capture) and, where the host configured their read-only readers, "
    "graph-mail://, sharepoint:// and bc:// citations resolve; a live scheme without a "
    "reader, or a capture scheme without its host capture, is source_not_configured, any "
    "other is withheld.",
    "mc:// citations are read from the host's Mission Control capture, never from MC live: "
    "a decision recorded in MC after the last capture is not seen until the next one.",
    "Live citations (graph-mail://, sharepoint://, bc://) are re-read from the source "
    "for the reading actor on every resolve: a changed message, item or record reads "
    "as changed_since (citation_content_changed on the case) and is never support; a "
    "source outage withholds. sharepoint:// status is eTag-based (bytes are not "
    "re-downloaded on resolve).",
    "Text read from an original by OCR is machine-read (text_source 'ocr'): evidence of "
    "what the scan appears to say, for a person to confirm, never a verified value.",
    "A citation whose content no longer matches the digest recorded at write "
    "time is reported as citation_content_changed and never counts as support. "
    "Rebuilding the source mirror starts a new generation: citations minted "
    "earlier stop resolving, so findings, calculations, gaps and proposals that "
    "cite them are withheld as unverifiable; the case keeps its recorded digests "
    "and values.",
    "Gaps and proposals are gated only by the citations they carry; text a caller "
    "records without citations is visible to the whole case audience.",
    "A calculation input's unit is checked against a source unit contract only for "
    "the columns operating/source_unit_contracts.json covers (unit_basis "
    "source_contract); every other unit is the caller's declaration "
    "(unit_basis caller_declared). A contract fixes a column's scale, never that "
    "its values are present, current or correct.",
    "Mail attachment citations (graph-mail://...#attachment=<id>&sha256=<hex>) are pinned to "
    "the attachment bytes; a case view re-reads the message for the reader and downloads the "
    "bytes again only when the message's changeKey moved (read_mail_original always does). "
    "Only file attachments carry bytes. Mail of a mailbox outside the case mail scope (the "
    "built-in allowlist by default) is refused for every reader before any message read.",
    "A verified link (plx.case_add_finding link mode) means the two cited rows agree on the stored "
    "value a declared link contract names; it never says the source value is correct. "
    "A text match is only a lead, and an id the source set by a text join is recorded "
    "as stored_by_text_join; neither supports a relationship claim.",
)


class CaseRuntimeUnavailable(ValueError):
    """The case runtime is not configured on this host (default deny)."""


class LiveSourceNotConfigured(CitationUnresolvable):
    """A live citation scheme whose read-only reader this host did not configure."""

    code = "source_not_configured"
    # Host configuration, not an answer about the cited content: the case store
    # lets it through instead of folding it into a generic unresolvable.
    public = True

    def __init__(self, scheme: str = "") -> None:
        super().__init__(f"source_not_configured:{scheme}" if scheme else
                         "source_not_configured")
        self.scheme = scheme


# What a live reader may raise for a citation the actor cannot read now; all
# read the same (not visible, nonexistent, malformed, source down).
_LIVE_ERRORS = (NotVisible, SourceUnavailable, ReadOnlyGuardError, ValueError, LookupError)
_UNBOUND = object()   # ``CaseRuntime.address_exclusion`` not yet attempted


@dataclass
class LiveReaders:
    """Read-only live readers a case host configured, under one audience.

    ``audience`` is the ``plx-live-source-audience/v1`` map every reader was
    built with (default deny); ``sharepoint`` maps a drive id to its reader.
    A reader left ``None`` (or a drive absent) makes its scheme refuse with
    ``source_not_configured``.
    """

    audience: LiveSourceAudience
    mail: Any = None
    sharepoint: Mapping[str, Any] = field(default_factory=dict)
    bc: Any = None
    # The Graph GuardedTransport the mail/SharePoint readers share (request
    # counts for counts-only reports; never used to send).
    graph_transport: Any = None
    # The BC GuardedTransport (request bounds only; never used to send).
    bc_transport: Any = None
    # The GET-only Graph client the mail/SharePoint readers share; the serve-time
    # people guard lists the directory through it once (``CaseRuntime.address_exclusion``).
    graph_api: Any = None
    # The case mail scope these readers ask for; None = the built-in allowlist.
    # Only build_live_readers over an injected (in-process) Graph sender sets the
    # named opt-out MailScope.unrestricted(): such readers never reach Microsoft
    # and serve synthetic mailboxes (tests, the console's fixture world).
    mail_scope: Any = None

    def configured(self) -> dict[str, Any]:
        return {"graph-mail": self.mail is not None,
                "sharepoint_drives": sorted(self.sharepoint),
                "bc": self.bc is not None}


def live_source_names(text: str) -> frozenset[str]:
    """``--live-sources`` of the MCP host and the console: comma-separated host sources
    (``LIVE_READER_SOURCES``; empty: none). Any other name is a ``ValueError``."""
    names = frozenset(name.strip() for name in text.split(",") if name.strip())
    unknown = sorted(names - set(LIVE_READER_SOURCES))
    if unknown:
        raise ValueError("unknown live source: " + ",".join(unknown))
    return names


def build_live_readers(audience: LiveSourceAudience | str | Path,
                       environ: Mapping[str, str] | None = None, *,
                       sharepoint_drives: Sequence[str] = (),
                       graph_sender: Any = None, bc_sender: Any = None,
                       sources: Collection[str] | None = None) -> LiveReaders:
    """Readers from the environment.

    Graph mail + SharePoint need ``MICROSOFT_GRAPH_{TENANT,CLIENT}_ID`` and
    ``MICROSOFT_GRAPH_CLIENT_SECRET``; drives are ``SHAREPOINT_DRIVE_ID`` plus
    ``sharepoint_drives`` (e.g. the QA/QC drive holding '2026 - AOs'); BC needs
    ``BC_{TENANT,CLIENT}_ID``, ``BC_CLIENT_SECRET`` and ``BC_ENVIRONMENT``. A
    source whose variables are missing or invalid is left unconfigured (its
    scheme refuses with ``source_not_configured``); nothing is requested here
    (tokens are fetched on first read). Senders are for injected-transport tests.
    This is the one place that decides which credentials are read: a source's reader
    (mail, the SharePoint drives, BC) is built only when ``audience`` grants a principal
    some resource of that source (``mail:``, ``sharepoint:``, ``bc:``) and, when
    ``sources`` is given, it is one of them (``LIVE_READER_SOURCES``; an unknown name is a
    ``ValueError``); the Graph variables are read only for a mail or SharePoint reader, the
    BC ones only for BC. A BC-only audience or host never reads a Graph credential, and a
    SharePoint-only one builds no mail reader, whatever the environment holds.
    """
    from integrations import bc_read, graph_read  # GET-only clients; lazy import

    unknown = set() if sources is None else set(sources) - set(LIVE_READER_SOURCES)
    if unknown:
        raise ValueError("unknown live source: " + ",".join(sorted(unknown)))
    if not isinstance(audience, LiveSourceAudience):
        audience = LiveSourceAudience.from_file(audience)
    readers_for = audience.granted_sources() & _LIVE_READERS
    if sources is not None:
        readers_for &= set(sources)
    env = os.environ if environ is None else environ
    readers = LiveReaders(audience)
    api = transport = None
    if readers_for & {"mail", "sharepoint"}:
        try:
            api, transport = graph_read.build_graph(env, sender=graph_sender)
        except (SourceUnavailable, ValueError):
            api = transport = None
    if api is not None:
        readers.graph_transport = transport
        readers.graph_api = api
        # The guard's own network sender (or anything else from that module)
        # is never an injected transport.
        if (graph_sender is not None and getattr(graph_sender, "__module__", None)
                != "integrations.live_source_guard"):
            # Injected transport: no request leaves the process, so the
            # mailboxes are synthetic. Named opt-out, never a real host's default.
            readers.mail_scope = MailScope.unrestricted()
        if "mail" in readers_for:
            readers.mail = graph_read.GraphMailReader(api, audience)
        drives = ([env.get("SHAREPOINT_DRIVE_ID") or "", *sharepoint_drives]
                  if "sharepoint" in readers_for else [])
        for drive in dict.fromkeys(d.strip() for d in drives if d and d.strip()):
            try:
                readers.sharepoint[drive] = graph_read.SharePointReader(
                    api, audience, drive, transport=transport)
            except ValueError:
                continue  # an invalid drive id stays unconfigured
    if "bc" in readers_for:
        try:
            bc_api, bc_transport, tenant_id, environment = bc_read.build_bc(
                env, sender=bc_sender)
            readers.bc = bc_read.BusinessCentralReader(bc_api, audience, tenant_id=tenant_id,
                                                       environment=environment)
            readers.bc_transport = bc_transport
        except (SourceUnavailable, ValueError):
            readers.bc = None
    return readers


def _live_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in _LIVE_VOLATILE}


class CaseRuntime:
    """Mirror + case store under one state directory, with one citation resolver.

    ``audience`` is the path of a ``plx-source-mirror-audience/v1`` file (re-read
    on every mirror operation, so a revocation applies immediately) or an
    ``AudienceMap``. ``client`` is a ``WorkbenchReadApiClient`` or ``None`` for
    an offline mirror that answers only from captures it already holds.
    ``unit_contracts`` is a ``SourceUnitContracts`` or the path of a
    ``plx-source-unit-contracts/v1`` file; default: the built-in contracts.

    Through a Portal API outage the mirror fills a relation it never held from the
    last complete capture in the Workbench capture store (``integrations/
    mirror_capture.py``), labelled stale and never live: ``mirror_options
    ["persisted_store"]`` names the store (None: never); without it a runtime with a
    client uses the one an explicitly set ``PLX_WORKBENCH_STATE_DIR`` names (outside
    Git, the credentialed host's capture store), and an offline runtime none.
    """

    def __init__(self, state_dir: str | Path, *, client: Any, audience: Any,
                 tenant: str, mirror_options: Mapping[str, Any] | None = None,
                 case_clock: Any = None,
                 originals_options: Mapping[str, Any] | None = None,
                 unit_contracts: SourceUnitContracts | str | Path | None = None,
                 link_contracts: LinkContracts | str | Path | None = None,
                 live: LiveReaders | None = None,
                 employee_permissions: EmployeePermissionResolver | None = None,
                 mail_scope: MailScope | None = None,
                 require_employee: bool = False,
                 mail_capture_dir: str | Path | None = None,
                 people_exclusion: Any = None) -> None:
        if audience is None:
            raise CaseRuntimeUnavailable("source_mirror_audience_not_configured")
        if isinstance(audience, (str, Path)):
            # Validate once at startup so a broken host config is loud; the
            # mirror still re-reads (and fails closed) on every operation.
            AudienceMap.from_file(audience)
            audience = Path(audience)
        if unit_contracts is None:
            unit_contracts = SourceUnitContracts.builtin()
        elif not isinstance(unit_contracts, SourceUnitContracts):
            unit_contracts = SourceUnitContracts.from_file(unit_contracts)
        self.unit_contracts = unit_contracts
        # Declared row-to-row link contracts (operating/link_contracts.json by default).
        self.link_contracts = (LinkContracts.builtin() if link_contracts is None else
                               link_contracts if isinstance(link_contracts, LinkContracts)
                               else LinkContracts.from_file(link_contracts))
        root = Path(state_dir) / CASE_RUNTIME_DIR
        self.state_dir = Path(state_dir)
        self.client_configured = client is not None
        if live is not None and not isinstance(live, LiveReaders):
            raise CaseRuntimeUnavailable("live_readers_invalid")
        if live is not None and live.audience.tenant != tenant:
            raise CaseRuntimeUnavailable("live_source_audience_tenant_mismatch")
        self.live = live
        # One read-only connection per host capture store (BC, Teams, MC), kept
        # while the file is the same one (``_capture_read``); the lock serialises its use.
        self._capture_reads: dict[Path, tuple[tuple[int, ...], CaptureStore]] = {}
        self._capture_lock = threading.Lock()
        # The host's mail and SharePoint document captures (``_from_capture``)
        # (``operating.case_tools_host.CaseCaptures``); None: live readers only.
        self.captures: Any = None
        # The versioned mail capture a graph-mail:// version is read from first
        # (``mail_capture_dir``); default: the state directory, as BC's.
        self._mail_capture_dir = Path(state_dir if mail_capture_dir is None
                                      else mail_capture_dir)
        if mail_scope is not None and not isinstance(mail_scope, MailScope):
            raise CaseRuntimeUnavailable("mail_scope_invalid")
        # The mailboxes whose mail may enter a case (operating.case_mail_scope):
        # every mail read outside it is refused before any message is requested.
        # Default: the built-in allowlist. The only opt-out is the deliberately
        # named MailScope.unrestricted(), passed here or carried by readers over
        # an injected in-process sender (synthetic mailboxes; see LiveReaders).
        if mail_scope is None and live is not None and isinstance(live.mail_scope, MailScope):
            mail_scope = live.mail_scope
        self.mail_scope = MailScope.builtin() if mail_scope is None else mail_scope
        # The people exclusion every captured mail and Teams record is served under, re-read
        # at serve time (``ontology_tools_evidence.people_excluded``: unavailable withholds
        # them all). Default: the guard ``PLX_EXCLUDED_PEOPLE_OIDS``/``_FILE`` name.
        if people_exclusion is None:
            from integrations.people_exclusion import PeopleExclusion
            people_exclusion = PeopleExclusion.from_env(os.environ)
        self.people_exclusion = people_exclusion
        self._address_exclusion: Any = _UNBOUND
        self._address_lock = threading.Lock()
        # (mailbox, message, attachment, message changeKey) -> attachment metadata
        # whose bytes were downloaded and hashed by this runtime: a case view of an
        # unchanged message (same changeKey) need not download them again.
        self._attachment_verified: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        # Every actor reaching the mirror, originals, case store or a live
        # reader passes this gate: an ``employee:<oid>`` actor is replaced by
        # what operating.employee_permissions resolves for that employee now
        # (refused without a resolver). Any other actor passes unchanged, unless
        # ``require_employee``: every session host serving live data sets it
        # (operating.session_identity), so an actor without an employee identity
        # is refused before any read.
        self.employee_gate = EmployeeActorGate(employee_permissions, tenant,
                                               require_employee=require_employee)
        options = dict(mirror_options or {})
        if "persisted_store" not in options and client is not None:
            options["persisted_store"] = store_from_env(os.environ)
        self.mirror = SourceMirror(root / MIRROR_FILE, client, audience, tenant=tenant,
                                   **options)
        self.originals = OriginalStore(root, **{**dict(originals_options or {}),
                                                "source_gate": self._original_source_gate})
        self.cases = CaseStore(root / CASES_FILE,
                               citation_resolver=lambda handle, actor:
                               self.resolve_citation(handle, actor),
                               clock=case_clock, unit_contracts=unit_contracts,
                               link_contracts=self.link_contracts,
                               header_gate=self._case_header_gate)
        for target in (self.mirror, self.originals, self.cases, self):
            gate_actor_methods(target, self.employee_gate)

    def _case_header_gate(self, relations: Sequence[str], actor: Actor) -> bool:
        """May ``actor`` read at least one of these mirror relations now? The same
        audience decision (re-read per call: a revocation applies at once) the mirror
        makes for rows, applied to a case header's recorded provenance."""
        audience = self.mirror._audience()
        return any(audience.allows(actor, relation) for relation in relations)

    def _employee_acl_denial(self, actor: Actor, scheme: str, record: Any) -> str | None:
        """For an employee actor, the item's own source ACL ∩ the employee's identities.

        ``None`` when allowed, else the denial reason (no identifier in it).
        SharePoint needs a completely read permission list (unknown, partial or
        not requested denies: ``employee_acl_denied``); mail the derived
        mailbox-owner ACL. A mailbox the employee does not own is denied as
        ``MAILBOX_ACCESS_UNDETERMINED``: shared and delegated (FullAccess /
        ReadPermission) mailbox access is an Exchange permission that Graph v1.0
        does not expose, so it can be neither proven nor ruled out
        (docs/reference/permissions.md). BC exposes no record ACL. A SharePoint record
        must also pass the employee's file clearance (its security class:
        Finance, Business Central, Staff Scratch), refused like an ACL denial so
        the class is not revealed. A non-employee actor is unaffected (``None``).
        """
        entitlement = self.employee_gate.entitlement(actor)
        if entitlement is None or scheme == "bc":
            return None
        if scheme == "sharepoint" and not entitlement.file_class_allows(record):
            return EMPLOYEE_ACL_DENIED
        acl = record.get("source_acl") if isinstance(record, Mapping) else None
        if entitlement.item_acl_allows(acl, statuses=_ITEM_ACL_STATUSES.get(scheme, ("read",))):
            return None
        if (scheme == "graph-mail" and isinstance(acl, Mapping)
                and acl.get("status") == "derived" and acl.get("kind") == "mailbox_owner"):
            return MAILBOX_ACCESS_UNDETERMINED
        return EMPLOYEE_ACL_DENIED

    def _employee_acl_allows(self, actor: Actor, scheme: str, record: Any) -> bool:
        return self._employee_acl_denial(actor, scheme, record) is None

    def _employee_mailbox_denial(self, actor: Actor, reader: Any, mailbox: Any) -> str | None:
        """The mailbox decided **before** any message read: mail scope, then owner ACL.

        Uses the reader's directory-only lookup (``mailbox_owner_acl``), so no
        subject, metadata or body of a mailbox outside the case mail scope, or
        one the employee may not read, is fetched. With a ``mail_scope`` the
        mailbox's directory identities must include an in-scope address
        (``MAIL_OUTSIDE_SCOPE`` otherwise, for every actor). For an employee the
        owner ACL is then intersected with the employee's identities. A reader
        without that lookup denies; a mailbox the reader cannot resolve raises
        the reader's error (``NotVisible``), as the message read would. No scope
        and a non-employee actor: ``None`` without any lookup (unchanged).
        """
        employee = self.employee_gate.entitlement(actor) is not None
        if self.mail_scope.is_unrestricted and not employee:
            return None
        lookup = getattr(reader, "mailbox_owner_acl", None)
        if not callable(lookup):
            return EMPLOYEE_ACL_DENIED if employee else MAIL_OUTSIDE_SCOPE
        acl = lookup(actor, mailbox)
        if not self.mail_scope.allows(
                (acl or {}).get("identities") if isinstance(acl, Mapping) else None):
            return MAIL_OUTSIDE_SCOPE
        if not employee:
            return None
        return self._employee_acl_denial(actor, "graph-mail", {"source_acl": acl})

    def _mail_record_denial(self, actor: Actor, record: Any) -> str | None:
        """After a message read: the record's own mailbox identities against the
        scope (the directory may have changed since the lookup), then the
        employee's item ACL."""
        if not self.mail_scope.allows_record(record):
            return MAIL_OUTSIDE_SCOPE
        return self._employee_acl_denial(actor, "graph-mail", record)

    @property
    def mail_capture_dir(self) -> Path:
        """The mail capture every ``graph-mail://`` citation is read from first: the one
        the host's document captures (``captures``) name, else the runtime's own."""
        held = getattr(self.captures, "mail_capture_dir", None)
        return Path(held) if held is not None else self._mail_capture_dir

    def close(self) -> None:
        with self._capture_lock:
            for path in list(self._capture_reads):
                self._drop_capture_read(path)
        self.mirror.close()

    def resolve_citation(self, handle: str, actor: Actor) -> dict[str, Any]:
        """Content-stable view of one citation for ``actor``, or a typed error.

        Only the immutable parts of the cited capture are returned (relation,
        key, capture id and the exact stored row), so the digest a case records
        fingerprints what was cited, not when the mirror last refreshed.
        """
        return self._resolve(handle, actor)[0]

    def hidden_from_every_reader(self, actor: Actor, case_id: str) -> list[str]:
        """Why NO reader of this host can read case ``case_id`` (which ``actor`` reads) in full:
        the sorted reasons some item's citation resolves for nobody, from host facts alone,
        never a reader's permission or the cited content: ``source_not_configured`` (a capture
        or live scheme this host has no store or reader for) or ``mirror_rebuilt`` (an
        ``api://`` citation minted by another mirror generation). Empty when no such fact is
        known: a reader with more access may then read the whole case."""
        reasons = {self._hidden_reason(handle)
                   for handle in self.cases.cited_handles(actor, case_id)}
        return sorted(reasons - {None})

    def _hidden_reason(self, handle: str) -> str | None:
        if handle.startswith("api://"):
            try:
                generation = citation_generation(handle)
            except ValueError:
                return None
            return ("mirror_rebuilt" if generation is not None
                    and generation != self.mirror.generation else None)
        if _captured_handle(handle):
            _source, _kind, path = self._capture_store(handle)
            return "source_not_configured" if self.live is None or not path.is_file() else None
        if handle.startswith(LIVE_SCHEMES) and not (
                self.captures is not None and handle.startswith((MAIL_SCHEME, SHAREPOINT_SCHEME))
                or handle.startswith(MAIL_SCHEME) and self._holds_mail_capture()):
            try:
                self._reader(handle)
            except LiveSourceNotConfigured:
                return "source_not_configured"
            except CitationUnresolvable:
                return None
        return None

    def claim_row(self, handle: str, actor: Actor) -> dict[str, Any]:
        """``resolve_citation``'s view for the claim gate (``check_items``), from the same
        read: a mirror (``api://``) revision a later capture superseded also carries
        ``status``, ``changed_since``, ``superseded_at`` (the superseding capture's
        completion; None while it has not completed) and ``current_citation``, kept out
        of the digested view so a case's recorded digest does not move. Every other
        scheme's view already says whether its revision moved."""
        view, moved = self._resolve(handle, actor)
        return {**view, **moved} if moved else view

    def _resolve(self, handle: str, actor: Actor) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(handle, str) or not handle.startswith(SUPPORTED_SCHEMES):
            raise CitationUnresolvable("citation_scheme_not_supported")
        if handle.startswith(ORIGINAL_SCHEME):
            return self.originals.resolve(handle, actor), {}
        captured = self._from_capture(handle, actor)
        if captured is not None:
            return captured, {}   # a captured view says itself whether it moved
        if handle.startswith(LIVE_SCHEMES):
            return self._live_view(self._live_resolve(handle, actor)), {}
        result = self.mirror.resolve_citation(handle, actor)
        if not result.get("ok"):
            raise CitationUnresolvable(str(result.get("error") or "citation_unresolvable"))
        view: dict[str, Any] = {"citation": handle, "relation": result["relation"],
                                "key": result["key"],
                                "capture_id": result["capture"]["capture_id"]}
        if "row" in result:
            view["row"] = result["row"]
        else:
            view["rows"] = result["rows"]
        if result.get("changed_since") is False:
            return view, {}
        # Superseded, or a status this reader does not know: changed (fails closed).
        return view, {"status": "changed_since", "changed_since": True,
                      "superseded_at": result.get("superseded_at"),
                      "current_citation": result.get("current_citation")}

    def link(self, actor: Actor, case_id: str, from_citation: str, to_citation: str,
             contract_id: str) -> dict[str, Any]:
        """Workbench-checked link between two cited rows (``CaseStore.link``)."""
        return self.cases.link(actor, case_id, from_citation, to_citation, contract_id)

    def read_original(self, handle: str, actor: Actor) -> dict[str, Any]:
        """An ``original://`` citation as its reader sees it now, or a typed error.

        The same audience check and byte re-hash as ``resolve_citation``; a file
        citation also lists its page citations, a page citation carries its whole
        text and ``text_source`` (OCR text is labelled machine-read). Errors are
        returned, not raised, like ``SourceMirror.resolve_citation``: an original
        outside the reader's audience and one that does not exist read the same.
        """
        try:
            view = self.originals.read(handle, actor)
        except CitationUnresolvable as exc:
            return {"ok": False, "error": str(exc), "citation": handle}
        result = {"ok": True, "status": "current", "changed_since": False, **view}
        if view.get("text_source") == "ocr":
            result["note"] = ("machine-read OCR text: what the scan appears to say, "
                              "to be confirmed by a person, never a verified value")
        return result

    def citation_status(self, handle: str, actor: Actor) -> dict[str, Any]:
        """Current mirror status of a citation the actor can already resolve."""
        if not isinstance(handle, str) or not handle.startswith(SUPPORTED_SCHEMES):
            return {"ok": False, "error": "citation_scheme_not_supported"}
        if handle.startswith(LIVE_SCHEMES) or _captured_handle(handle):
            return self.read_live(handle, actor, include_record=False)
        if handle.startswith(ORIGINAL_SCHEME):
            try:
                view = self.originals.resolve(handle, actor)
            except CitationUnresolvable as exc:
                return {"ok": False, "error": str(exc)}
            status = {"ok": True, "status": "current", "changed_since": False,
                      "source": "operator-supplied original (bytes re-hashed on this read)",
                      "extraction_method": view["extraction"]["method"]}
            if "provenance" in view:
                status["source"] = (f"{view['provenance']['source']} original (bytes read at "
                                    "attach, re-hashed on this read)")
                status["source_citation"] = view["provenance"].get("source_citation")
            if "page" in view:
                status["page"] = view["page"]
                status["text_source"] = view["text_source"]
                if view["text_source"] == "ocr":
                    status["note"] = ("machine-read OCR text: what the scan appears to say, "
                                      "to be confirmed by a person, never a verified value")
            return status
        result = self.mirror.resolve_citation(handle, actor)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error"), "reason": result.get("reason")}
        status = {"ok": True, "status": result["status"],
                  "mirror_generation": result.get("mirror_generation"),
                  "changed_since": result["changed_since"],
                  "current_citation": result.get("current_citation"),
                  "upstream_presence": result.get("upstream_presence"),
                  "relation_last_refresh_at": result.get("relation_last_refresh_at"),
                  "atomic_upstream_snapshot": False, "deletions_proven": False}
        if result.get("source_freshness") is not None:
            # A row filled from a persisted capture while the live API was out: stale,
            # never "current"; the mirror's own content status is kept beside it.
            status.update(status="stale", content_status=result["status"],
                          upstream_presence="not_rechecked_live_source_unavailable",
                          source_freshness=result["source_freshness"],
                          freshness_label=result["freshness_label"])
        return status

    # ---- captures (BC, Teams, Mission Control; captured mail and SharePoint) ----------

    def _capture_resolve(self, handle: str, actor: Actor) -> dict[str, Any]:
        """The ``CaptureStore.resolve`` result of a BC, Teams, SharePoint list or MC
        capture handle for ``actor`` (gated by the caller's public method), or
        ``CitationUnresolvable``. The store is the host's, read on the runtime's one
        read-only connection to it (``_capture_read``); the live audience decides, as it
        does for the live BC reader (for MC: the collection and the version's project
        resource, ``integrations.mc_sources``), and for Teams the store's per-record check
        (the message's and the conversation's latest captured membership, against an employee's
        own identities: ``_EmployeeItemAcl``, with the entitlement the employee gate
        resolved for this call). Every refusal (not granted, hidden by the record's own
        check, not captured, another store generation, an unreadable store) reads the same;
        a host without a live audience or that capture store is ``source_not_configured``
        (host configuration, nothing about the record)."""
        source, kind, path = self._capture_store(handle)
        if self.live is None or not path.is_file():
            raise LiveSourceNotConfigured(source)
        audience: Any = self.live.audience
        refusal = (self.exclusion_refusal() if source in ("teams-channel", "teams-chat")
                   else None)
        if refusal:
            # No usable people guard: no captured Teams message is served, whoever asks,
            # and the refusal names why (decided before the record is looked up).
            raise CitationUnresolvable(refusal)
        if source in ("teams-channel", "teams-chat", "sharepoint-list"):
            entitlement = self.employee_gate.entitlement(actor)
            if entitlement is not None:
                audience = _EmployeeItemAcl(audience, entitlement, source)
        with self._capture_lock:
            try:
                result = self._capture_read(path).read_as(audience).resolve(handle, actor)
            except (CaptureError, sqlite3.Error, OSError):
                self._drop_capture_read(path)
                raise CitationUnresolvable("capture_citation_unresolvable") from None
        if not result.get("ok") or result.get("kind") != kind:
            raise CitationUnresolvable("capture_citation_unresolvable")
        if source in ("teams-channel", "teams-chat"):
            from .ontology_tools_evidence import people_excluded
            # A message an excluded person sent, or of a chat they are in, is withheld from
            # every reader now, whenever it was captured (read alike: not visible).
            named = [(result["row"] or {}).get("from")]
            if source == "teams-chat":
                named.append(((result.get("meta") or {}).get("source_acl") or {})
                             .get("identities"))
            if people_excluded(self.people_exclusion, named):
                raise CitationUnresolvable("capture_citation_unresolvable")
        return result

    def _capture_read(self, path: Path) -> CaptureStore:
        """The read-only connection to a capture store (the caller holds the lock): opened
        once and kept while the file is the same (device, inode, size, modification time),
        so a replaced or rewritten store is opened anew; its audience is each read's own
        (``CaptureStore.read_as``)."""
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = self._capture_reads.get(path)
        if cached is not None and cached[0] == identity:
            return cached[1]
        self._drop_capture_read(path)
        store = CaptureStore.open_read_only(path, self.live.audience)
        self._capture_reads[path] = (identity, store)
        return store

    def _drop_capture_read(self, path: Path) -> None:
        cached = self._capture_reads.pop(path, None)
        if cached is not None:
            cached[1].close()

    def _capture_store(self, handle: str) -> tuple[str, str, Path]:
        """(source, capture kind, store path) of a capture handle's scheme."""
        if handle.startswith(TEAMS_SCHEMES):
            from integrations.capture_teams import CHANNEL_KIND, CHAT_KIND, DB_FILE, STORE_DIR
            kind = CHAT_KIND if handle.startswith(TEAMS_CHAT_SCHEME) else CHANNEL_KIND
            return kind.scheme, kind.name, self.state_dir / STORE_DIR / DB_FILE
        if handle.startswith(SHAREPOINT_LIST_SCHEME):
            from integrations import capture_sharepoint_lists as lists
            return ("sharepoint-list", lists.LIST_KIND.name,
                    self.state_dir / lists.STORE_DIR / lists.DB_FILE)
        if handle.startswith(MC_SCHEME):
            from integrations.mc_sources import MC_KIND, store_path  # registers the kind
            return "mc", MC_KIND.name, store_path(self.state_dir)
        from .host import BC_DIR, BC_FILE
        return "bc", "bc", self.state_dir / BC_DIR / BC_FILE

    def _from_capture(self, handle: str, actor: Actor) -> dict[str, Any] | None:
        """The content-stable view of a citation a host capture holds for ``actor``, or
        None for a live citation no capture holds for this reader (the live reader then
        decides). A BC, Teams, SharePoint list or MC capture handle names its capture:
        always read from it (``_capture_resolve``'s refusals). A mail version is read from
        the mail capture (``mail_capture``), a SharePoint version from the host's document
        captures (``captures``), when they hold it; their gate refusing it is
        ``CitationUnresolvable``."""
        if _captured_handle(handle):
            return _capture_view(self._capture_resolve(handle, actor))
        if handle.startswith(MAIL_SCHEME):
            return self.mail_capture(handle, actor)
        if self.captures is not None and handle.startswith(SHAREPOINT_SCHEME):
            return self.captures(handle, actor)
        return None

    def exclusion_refusal(self) -> str | None:
        """The code a captured mail or Teams record is refused with while the people
        guard cannot decide (``integrations.people_exclusion.CODES``: no list, an invalid
        entry, ...), so the refusal names its cause; None when the guard is usable."""
        from integrations.people_exclusion import PeopleExclusion
        guard = self.people_exclusion
        if not isinstance(guard, PeopleExclusion):
            return "excluded_people_oids_absent"
        return guard.unavailable

    def address_exclusion(self) -> Any:
        """The people guard able to say whether an address is an excluded person's: the
        runtime's own when bound, else a copy of it bound once per runtime through the
        host's GET-only Graph client (``LiveReaders.graph_api``: one complete ``GET
        /users`` listing, which names nobody). None when the guard is unusable, the host
        has no Graph client or the listing failed (the caller then withholds what only
        an address could decide)."""
        import copy
        from integrations.people_exclusion import PeopleExclusion
        guard = self.people_exclusion
        if not isinstance(guard, PeopleExclusion) or guard.unavailable:
            return None
        if guard.bound:
            return guard
        with self._address_lock:
            if self._address_exclusion is _UNBOUND:
                api, bound = getattr(self.live, "graph_api", None), None
                if api is not None:
                    bound = copy.copy(guard)
                    try:
                        bound.bind(api)
                    except Exception:  # noqa: BLE001 - no listing: addresses stay undecided
                        bound = None
                self._address_exclusion = (bound if bound is not None and bound.bound
                                           and not bound.unavailable else None)
            return self._address_exclusion

    def _holds_mail_capture(self, directory: Path | None = None) -> bool:
        from integrations.capture_mail import store_exists
        directory = self.mail_capture_dir if directory is None else directory
        return getattr(self.live, "audience", None) is not None and store_exists(directory)

    def mail_capture(self, handle: str, actor: Actor, *,
                     directory: str | Path | None = None) -> dict[str, Any] | None:
        """The content-stable view of a ``graph-mail://`` version the mail capture
        (``directory``; default ``mail_capture_dir``) holds for ``actor``, through the gate
        ``plx.read`` reads it with (``RuntimeCitations.mail_capture``: the mailbox
        identities recorded at capture, the live audience, the case mail scope, the
        employee's item ACL): ``{citation, relation, recorded_at, row}``, digested as
        ``plx.read`` reports it. A version the capture no longer holds as current (a later
        version, or none: removed at the source) is a ``changed_since`` marker (never
        support), except a mail attachment, pinned to its bytes. None when no capture holds the version for this reader (the live reader then
        decides); ``CitationUnresolvable`` when the gate refuses it or the capture cannot be
        read (never read live in its place), and ``page_not_found`` for a cited page past
        the last page of an attachment whose captured text tells (``plx.read``'s answer)."""
        directory = self.mail_capture_dir if directory is None else Path(directory)
        if not self._holds_mail_capture(directory):
            return None
        from .ontology_tools_evidence import RuntimeCitations
        audience = self.live.audience
        refusal = self.exclusion_refusal()
        if refusal:
            # No usable people guard: no captured message is served, and the refusal
            # names why (host configuration, nothing about the record).
            raise CitationUnresolvable(refusal)
        try:
            gated = self.employee_gate(actor)
            if not audience.may_read_any(gated, "mail:"):
                raise CitationUnresolvable("capture_citation_unresolvable")
            reader = RuntimeCitations(self, mail_capture_dir=directory)
            found = reader.mail_capture(handle, gated, audience)
            if reader.missing_page(handle, gated, audience):
                raise CitationUnresolvable("page_not_found")
        except (sqlite3.Error, OSError, ValueError, LookupError):
            # An unreadable capture serves nothing, and is not read live in its place.
            raise CitationUnresolvable("capture_citation_unresolvable") from None
        if found is None:
            return None
        captured, current = found
        # A superseded message is never current support, with or without a current
        # version (one removed at the source has none), as for a superseded mirror row.
        if (captured.get("superseded_at") and captured["relation"] == "graph-mail.message"
                and current != handle):
            return {"citation": handle, "source": "graph-mail", "captured": True,
                    "status": "changed_since", "changed_since": True,
                    "current_citation": current, "recorded_at": captured["recorded_at"],
                    "superseded_at": captured["superseded_at"]}
        return {key: captured[key] for key in ("citation", "relation", "recorded_at", "row")}

    # ---- live sources (docs/reference/case-runtime.md section 7) ------------------

    def _reader(self, handle: str) -> tuple[str, Any]:
        """(scheme, reader) for a live handle, or ``LiveSourceNotConfigured``."""
        live = self.live
        if handle.startswith(MAIL_SCHEME):
            if live is None or live.mail is None:
                raise LiveSourceNotConfigured("graph-mail")
            return "graph-mail", live.mail
        if handle.startswith(BC_SCHEME):
            if live is None or live.bc is None:
                raise LiveSourceNotConfigured("bc")
            return "bc", live.bc
        if live is None or not live.sharepoint:
            raise LiveSourceNotConfigured("sharepoint")
        try:
            (drive_id, _item), _etag, _extra = parse_handle(handle, "sharepoint", 2)
        except ValueError:
            raise CitationUnresolvable("live_citation_unresolvable") from None
        reader = live.sharepoint.get(drive_id)
        if reader is None:
            raise LiveSourceNotConfigured("sharepoint")
        return "sharepoint", reader

    def _live_resolve(self, handle: str, actor: Actor, *,
                      need_bytes: bool = False) -> dict[str, Any]:
        """Re-read a live citation for ``actor``: the reader's resolve result.

        Every failure other than a missing reader reads the same
        (``live_citation_unresolvable``): not visible, nonexistent, malformed
        and source down are not distinguished.
        """
        scheme, reader = self._reader(handle)
        if scheme == "graph-mail":
            try:
                (mailbox, _message), _version, _extra = parse_handle(handle, "graph-mail", 2)
            except ValueError:
                raise CitationUnresolvable("live_citation_unresolvable") from None
            try:
                denial = self._employee_mailbox_denial(actor, reader, mailbox)
            except _LIVE_ERRORS:
                raise CitationUnresolvable("live_citation_unresolvable") from None
            if denial is not None:
                raise CitationUnresolvable(_public_denial(denial))
        try:
            if scheme == "graph-mail":
                resolved = self._mail_resolve(reader, handle, actor, need_bytes=need_bytes)
            else:
                # SharePoint: eTag-based (the eTag covers content and metadata);
                # bytes are not downloaded on every case read.
                resolved = reader.resolve_citation(handle, actor)
        except _LIVE_ERRORS:
            raise CitationUnresolvable("live_citation_unresolvable") from None
        if not isinstance(resolved, Mapping) or resolved.get("status") not in (
                "current", "changed_since"):
            raise CitationUnresolvable("live_citation_unresolvable")
        if scheme == "graph-mail":
            denial = self._mail_record_denial(actor, resolved.get("record"))
        else:
            denial = self._employee_acl_denial(actor, scheme, resolved.get("record"))
        if denial is not None:
            raise CitationUnresolvable(_public_denial(denial))
        return {"scheme": scheme, **resolved}

    def _mail_resolve(self, reader: Any, handle: str, actor: Actor, *,
                      need_bytes: bool = False) -> dict[str, Any]:
        """Mail citation, optionally pinned to its content.

        A pinned citation (``#body_sha256=<hex>&shown_sha256=<hex>``) is
        ``current`` while the bounded body and the fields a finding shows
        (``_shown``: sender, recipients, subject, times, attachments flag) are
        unchanged; a changeKey drift alone (read, flag, category, folder move)
        is reported as ``change_key_changed`` but is not a content change. An
        unpinned citation is judged on the changeKey (the reader's rule).
        """
        (mailbox, message_id), change_key, extra = parse_handle(handle, "graph-mail", 2)
        if "attachment" in extra:
            return self._attachment_resolve(reader, handle, actor, need_bytes=need_bytes)
        if set(extra) - {"body_sha256", "shown_sha256"}:
            raise ValueError("citation_handle_invalid")
        cited_body = extra.get("body_sha256")
        if cited_body is None:
            if "shown_sha256" in extra:
                raise ValueError("citation_handle_invalid")
            return reader.resolve_citation(handle, actor)
        if mailbox != mailbox.lower():
            raise NotVisible()  # minted with the lower-case object id only
        record = reader.get_message(actor, mailbox, message_id, include_body=True,
                                    max_body_chars=MAIL_BODY_CHARS)
        body, shown = _body_digest(record), _shown_digest(record)
        same = body == cited_body and extra.get("shown_sha256") in (None, shown)
        return {"citation": handle, "status": "current" if same else "changed_since",
                "cited_version": change_key, "current_version": record["change_key"],
                "current_citation": _pinned_mail_citation(record),
                "change_key_changed": record["change_key"] != change_key,
                "body_sha256": body, "shown_sha256": shown, "pinned": True,
                "record": record}

    def _attachment_resolve(self, reader: Any, handle: str, actor: Actor, *,
                            need_bytes: bool = False) -> dict[str, Any]:
        """A mail attachment citation, pinned to the SHA-256 of its bytes.

        The attachment is read again through the GET-only reader
        (``GraphMailReader.get_attachment``: mailbox audience, owner ACL and the
        message's folder provenance first) and re-hashed: ``current`` while the
        same bytes are attached to that message. The message's changeKey alone
        (read, flag, move) is reported as ``change_key_changed``, never as a
        content change. The bytes are returned to the caller here and are never
        stored by the case runtime (an attached PDF original is stored apart,
        ``attach_mail_attachment_original``).

        Case views (``need_bytes`` false) do not download again what this
        runtime already hashed while the message's changeKey is unchanged
        (Exchange changes it on every edit of the item, attachments included):
        the message metadata is still re-read for the reader (scope, audience,
        owner ACL, folder provenance) on every resolve. ``read_mail_original``
        always downloads and re-hashes.
        """
        from integrations.graph_evidence import (mail_attachment_citation,
                                                 parse_mail_attachment_citation)

        cited = parse_mail_attachment_citation(handle)
        if not need_bytes:
            message = reader.get_message(actor, cited["mailbox"], cited["message_id"])
            known = self._attachment_verified.get(
                (message["mailbox"], message["id"], cited["attachment_id"],
                 message["change_key"]))
            if known is not None:
                current = mail_attachment_citation(message["citation"],
                                                   cited["attachment_id"],
                                                   known["content_sha256"])
                return {"citation": handle,
                        "status": ("current" if known["content_sha256"] == cited["sha256"]
                                   else "changed_since"),
                        "cited_version": cited["sha256"],
                        "current_version": known["content_sha256"],
                        "current_citation": current,
                        "change_key_changed": message["change_key"] != cited["change_key"],
                        "attachment": dict(known), "content": None, "record": message,
                        "bytes_reread": False}
        read = reader.get_attachment(actor, cited["mailbox"], cited["message_id"],
                                     cited["attachment_id"])
        attachment, message = read["attachment"], read["message"]
        if len(self._attachment_verified) >= _ATTACHMENT_CACHE_MAX:
            self._attachment_verified.pop(next(iter(self._attachment_verified)))
        self._attachment_verified[(message["mailbox"], message["id"], attachment["id"],
                                   message["change_key"])] = dict(attachment)
        return {"citation": handle,
                "status": ("current" if attachment["content_sha256"] == cited["sha256"]
                           else "changed_since"),
                "cited_version": cited["sha256"],
                "current_version": attachment["content_sha256"],
                "current_citation": read["citation"],
                "change_key_changed": message["change_key"] != cited["change_key"],
                "attachment": attachment, "content": read["content"], "record": message,
                "bytes_reread": True}

    @staticmethod
    def _live_view(resolved: Mapping[str, Any]) -> dict[str, Any]:
        """Content-stable view of the cited version, or a changed-since marker.

        A ``changed_since`` view carries no record (it is not the cited content),
        so its digest never equals one recorded while the citation was current.
        """
        view: dict[str, Any] = {"citation": resolved["citation"], "source": resolved["scheme"],
                                "version": resolved["cited_version"]}
        if resolved["status"] != "current":
            view.update(status="changed_since", changed_since=True,
                        current_version=resolved["current_version"],
                        current_citation=resolved["current_citation"])
            return view
        if resolved.get("attachment") is not None:
            # Only what the byte pin covers: the attachment and its message's identity.
            record = resolved["record"]
            view.update(status="current", kind="mail_attachment",
                        attachment=_attachment_shown(resolved["attachment"]),
                        message={key: record.get(key) for key in _ATTACHMENT_MESSAGE_FIELDS})
            view.pop("version")
            view["content_sha256"] = resolved["attachment"]["content_sha256"]
            return view
        if resolved.get("pinned"):
            # Only what the pin covers: a changeKey drift never changes the digest.
            view.update(status="current", record=_shown(resolved["record"]),
                        body_sha256=resolved["body_sha256"],
                        body_truncated=bool(resolved["record"].get("body_truncated")))
            view.pop("version")
            return view
        view.update(status="current", record=_live_record(resolved["record"]))
        return view

    def read_live(self, handle: str, actor: Actor, *,
                  include_record: bool = True) -> dict[str, Any]:
        """Status (and, by default, the record metadata) of a live citation for ``actor``.

        Returned, not raised, like ``SourceMirror.resolve_citation``. Mail body
        text is never returned here (only its hash, for a body-pinned citation). A BC, Teams
        channel or Mission Control capture handle is read from the capture, never live (``captured:
        true``, ``recorded_at``; ``changed_since`` when a later capture superseded it); so
        is a mail version the mail capture holds (``mail_capture``: its captured row as the
        record, without the body text) and the status of a SharePoint version the host's
        document captures hold (``captures``), without its record.
        """
        captured = self._read_capture(handle, actor, include_record)
        if captured is not None:
            return captured
        try:
            resolved = self._live_resolve(handle, actor)
        except LiveSourceNotConfigured as exc:
            return {"ok": False, "error": "source_not_configured", "source": exc.scheme,
                    "citation": handle}
        except CitationUnresolvable as exc:
            refused = {"ok": False, "error": "citation_unresolvable", "citation": handle}
            if str(exc) == MAILBOX_ACCESS_UNDETERMINED:
                refused["reason"] = MAILBOX_ACCESS_UNDETERMINED
            return refused
        result = {"ok": True, "citation": handle, "source": resolved["scheme"],
                  "status": resolved["status"],
                  "changed_since": resolved["status"] != "current",
                  "cited_version": resolved["cited_version"],
                  "current_version": resolved["current_version"],
                  "current_citation": resolved["current_citation"],
                  "read_at": datetime.now(timezone.utc).isoformat(),
                  "cached": False, "atomic_upstream_snapshot": False}
        if "body_sha256" in resolved:
            result.update(body_sha256=resolved["body_sha256"],
                          shown_sha256=resolved["shown_sha256"],
                          change_key_changed=resolved["change_key_changed"])
        if resolved.get("attachment") is not None:
            # Metadata and hash only; the bytes come from read_mail_original.
            result.update(kind="mail_attachment",
                          attachment=_attachment_shown(resolved["attachment"]),
                          change_key_changed=resolved["change_key_changed"])
        if include_record:
            result["record"] = _live_record(resolved["record"])
            acl = (resolved["record"].get("source_acl") or {}).get("status")
            result["record"]["source_acl_status"] = acl
        return result

    def _read_capture(self, handle: str, actor: Actor, include_record: bool
                      ) -> dict[str, Any] | None:
        """``read_live`` of a citation a host capture holds (``_from_capture``), else None."""
        if not _captured_handle(handle):
            try:
                view = self._from_capture(handle, actor)
            except CitationUnresolvable:
                return {"ok": False, "error": "citation_unresolvable", "citation": handle}
            if view is None:
                return None
            changed = view.get("changed_since") is True
            result = {"ok": True, "citation": handle, "source": _capture_source(handle),
                      "status": "changed_since" if changed else "current",
                      "changed_since": changed,
                      "current_citation": view.get("current_citation"), "captured": True,
                      "recorded_at": view.get("recorded_at"), "cached": False,
                      "atomic_upstream_snapshot": False}
            if include_record and isinstance(view.get("row"), Mapping):
                # The captured version's row (mail); body text is read through plx.read.
                result["record"] = {key: value for key, value in view["row"].items()
                                    if key != "body_text"}
            return result
        try:
            found = self._capture_resolve(handle, actor)
        except LiveSourceNotConfigured as exc:
            return {"ok": False, "error": "source_not_configured", "source": exc.scheme,
                    "citation": handle}
        except CitationUnresolvable:
            return {"ok": False, "error": "citation_unresolvable", "citation": handle}
        superseded = found["status"] != "current"
        result = {"ok": True, "citation": handle, "source": _capture_source(handle),
                  "status": "changed_since" if superseded else "current",
                  "changed_since": superseded, "cited_version": found["source_version"],
                  "current_version": found.get("current_source_version",
                                               None if superseded else found["source_version"]),
                  "current_citation": found["current_citation"], "captured": True,
                  "recorded_at": found["capture"]["recorded_at"],
                  "superseded_at": found["capture"]["superseded_at"],
                  "missing_from_source_since": found["missing_from_source_since"],
                  "cached": False, "atomic_upstream_snapshot": False}
        if include_record:
            result["record"] = dict(found["row"])
        return result

    def _original_source_gate(self, provenance: Mapping[str, Any], actor: Actor) -> bool:
        """May ``actor`` read the source item a source original was read from, now?

        Live check on every read: the drive's reader re-reads the item's
        metadata (and its permissions where the audience requires the item
        ACL) for ``actor`` -- ``SharePointReader.get_item``, never a download --
        so the item's current path and current ACL decide, not the attach-time
        ones. Not visible, removed, moved under a denied folder or source down
        -> denied. No reader for the drive on this host -> nobody (fail closed).
        """
        if provenance.get("source") == "graph-mail-attachment":
            return self._mail_original_gate(provenance, actor)
        if provenance.get("source") == RECORDED_CAPTURE_SOURCE:
            return self._recorded_capture_gate(provenance, actor)
        if self.live is None or provenance.get("source") != "sharepoint-drive":
            return False
        reader = self.live.sharepoint.get(provenance.get("drive_id"))
        item_id = provenance.get("item_id")
        if reader is None or not isinstance(item_id, str):
            return False
        try:
            current = reader.get_item(actor, item_id)
        except _LIVE_ERRORS:
            return False
        return (isinstance(current, Mapping) and current.get("id") == item_id
                and self._employee_acl_allows(actor, "sharepoint", current))

    def _recorded_capture_gate(self, provenance: Mapping[str, Any], actor: Actor) -> bool:
        """May ``actor`` read an original seeded from a recorded guarded capture, now?

        The drive item's permissions recorded at capture apply first, as a live read's
        would (an employee's item ACL and file class: a revocation recorded then is
        never overridden). Where this host has a reader for the drive, its live re-read
        decides (``sharepoint-drive``). Without one the recorded permissions decide
        alone, only while ``capture_decides`` (``MAX_CAPTURE_AGE``, the SharePoint
        capture host's bound); an older, undated or future-dated capture is denied.
        """
        recorded = {key: provenance.get(key) for key in ("path", "name", "drive_id")}
        recorded["source_acl"] = provenance.get("source_acl")
        if not self._employee_acl_allows(actor, "sharepoint", recorded):
            return False
        if self.live is not None and provenance.get("drive_id") in self.live.sharepoint:
            return self._original_source_gate({**provenance, "source": "sharepoint-drive"},
                                              actor)
        return capture_decides(provenance.get("captured_at"), datetime.now(timezone.utc))

    def _mail_original_gate(self, provenance: Mapping[str, Any], actor: Actor) -> bool:
        """May ``actor`` read the message a mail-attachment original came from, now?

        The same decision as a live read, made on every read of the original:
        mail scope and employee owner ACL before any message read, then the
        message metadata re-read for the reader (audience, folder provenance)
        and its record's scope/owner ACL. Never a re-download. Not visible,
        moved out of the mailbox, outside the scope, source down or no mail
        reader on this host -> denied.
        """
        mailbox, message_id = provenance.get("mailbox"), provenance.get("message_id")
        if not isinstance(mailbox, str) or not isinstance(message_id, str):
            return False
        try:
            reader = self._gated_mail_reader(actor, mailbox)
            record = reader.get_message(actor, mailbox, message_id)
        except (LiveSourceNotConfigured, CitationUnresolvable) + _LIVE_ERRORS:
            return False
        return (isinstance(record, Mapping) and record.get("id") == message_id
                and self._mail_record_denial(actor, record) is None)

    def attach_sharepoint_original(self, actor: Actor, drive_id: str, item_id: str, *,
                                   max_bytes: int | None = None) -> dict[str, Any]:
        """Read one SharePoint file for ``actor`` and store it as an original.

        The reader checks the actor's audience (and the item ACL where
        required) before any content request; bytes are read under the byte
        cap (``min(max_bytes, document MAX_BYTES)``), their SHA-256 is the
        original's identity, and the provenance (drive, item, eTag, cTag, path,
        source ACL, ``sharepoint://...#sha256=`` citation) is recorded
        immutably. Errors are ``OriginalError`` / ``LiveSourceNotConfigured``.
        """
        from integrations.document_understanding import MAX_BYTES

        live = self.live
        if live is None or not live.sharepoint:
            raise LiveSourceNotConfigured("sharepoint")
        reader = live.sharepoint.get(drive_id)
        if reader is None:
            raise LiveSourceNotConfigured("sharepoint")
        cap = MAX_BYTES if max_bytes is None else max_bytes
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise OriginalError("invalid_max_bytes")
        cap = min(cap, MAX_BYTES)
        unavailable = OriginalError("sharepoint_item_unavailable",
                                    "not visible to this actor, not a file, or not readable now")
        try:
            if self.employee_gate.entitlement(actor) is not None:
                # An employee's own item permissions are checked on the metadata
                # read, before any byte is downloaded.
                if not self._employee_acl_allows(actor, "sharepoint",
                                                 reader.get_item(actor, item_id)):
                    raise unavailable
            read = reader.get_content(actor, item_id, max_bytes=cap)
        except _LIVE_ERRORS:
            raise unavailable from None
        record = read["record"]
        if not self._employee_acl_allows(actor, "sharepoint", record):
            raise unavailable
        if read.get("content_status") == "exceeds_cap" or read.get("content") is None:
            raise OriginalError("original_too_large", f"at most {cap} bytes")
        content = read["content"]
        provenance = {
            "source": "sharepoint-drive", "identity": [record["drive_id"], record["id"],
                                                       record["etag"]],
            "drive_id": record["drive_id"], "item_id": record["id"], "etag": record["etag"],
            "ctag": record.get("ctag"), "path": record.get("path"), "name": record.get("name"),
            "size": record.get("size"), "mime_type": record.get("mime_type"),
            "source_hashes": record.get("source_hashes") or {},
            "last_modified": record.get("last_modified"),
            "last_modified_by": record.get("last_modified_by"),
            "original_url": record.get("original_url"),
            "source_citation": record["citation"], "source_acl": record.get("source_acl") or {},
            "audience_keys": reader.resources(record["path"], False),
            "content_sha256": read["content_sha256"],
            "read_at": datetime.now(timezone.utc).isoformat()}
        if sha256(content).hexdigest() != read["content_sha256"]:
            raise OriginalError("original_content_changed")
        stored = self.originals.store_source(actor, str(record.get("name") or ""), content,
                                             provenance)
        stored["source_citation"] = record["citation"]
        stored["source_metadata_citation"] = record["citation"].split("#", 1)[0]
        return stored

    def mail_message(self, actor: Actor, mailbox: str, message_id: str) -> dict[str, Any]:
        """One message for ``actor`` with a body-pinned citation (bounded body).

        Returns the record (metadata), the bounded plain-text body for the
        caller to read now, and ``citation`` =
        ``graph-mail://<mailbox>/<message>@<changeKey>#body_sha256=<hex>``; the
        body text itself is never stored by the case runtime.
        """
        reader = self._gated_mail_reader(actor, mailbox)
        try:
            record = reader.get_message(actor, mailbox, message_id, include_body=True,
                                        max_body_chars=MAIL_BODY_CHARS)
        except _LIVE_ERRORS:
            raise CitationUnresolvable("live_citation_unresolvable") from None
        denial = self._mail_record_denial(actor, record)
        if denial is not None:
            raise CitationUnresolvable(_public_denial(denial))
        body = _body_digest(record)
        return {"citation": _pinned_mail_citation(record),
                "metadata_citation": record["citation"], "body_sha256": body,
                "body_chars": len(record.get("body_text") or ""),
                "body_truncated": bool(record.get("body_truncated")),
                "body_bound_chars": MAIL_BODY_CHARS,
                "body_text": record.get("body_text") or "",
                "record": _live_record(record),
                # The mailbox's directory identities (what the scope decided on);
                # the reader already passed the mailbox's audience and owner ACL.
                "mailbox_identities": list((record.get("source_acl") or {})
                                           .get("identities") or []),
                "mailbox_in_scope": self.mail_scope.allows_record(record)}

    def _gated_mail_reader(self, actor: Actor, mailbox: Any) -> Any:
        """The mail reader, after the mailbox decision (scope, employee owner ACL)
        made before any message of ``mailbox`` is requested."""
        live = self.live
        if live is None or live.mail is None:
            raise LiveSourceNotConfigured("graph-mail")
        try:
            denial = self._employee_mailbox_denial(actor, live.mail, mailbox)
        except _LIVE_ERRORS:
            raise CitationUnresolvable("live_citation_unresolvable") from None
        if denial is not None:
            raise CitationUnresolvable(_public_denial(denial))
        return live.mail

    def mail_attachments(self, actor: Actor, mailbox: str, message_id: str) -> dict[str, Any]:
        """Attachment metadata of one message for ``actor`` (no content).

        The mailbox decision first, then the message's own read (folder
        provenance under the mailbox, the record's scope and owner ACL), then
        ``GraphMailReader.list_attachments`` (``$select`` without
        ``contentBytes``). Not visible, nonexistent and unavailable read the
        same (``live_citation_unresolvable``).
        """
        reader = self._gated_mail_reader(actor, mailbox)
        try:
            message = reader.get_message(actor, mailbox, message_id)
            denial = self._mail_record_denial(actor, message)
            if denial is not None:
                raise CitationUnresolvable(_public_denial(denial))
            listing = reader.list_attachments(actor, message["mailbox"], message_id)
        except _LIVE_ERRORS:
            raise CitationUnresolvable("live_citation_unresolvable") from None
        return {"message_citation": message["citation"], "record": _live_record(message),
                "items": listing["items"], "completeness": listing["completeness"],
                "cached": False}

    def mail_attachment(self, actor: Actor, mailbox: str, message_id: str,
                        attachment_id: str, *, max_bytes: int | None = None) -> dict[str, Any]:
        """One file attachment's bytes for ``actor`` with its byte-pinned citation.

        ``citation`` = ``graph-mail://<mailbox>/<message>@<changeKey>#attachment=<id>
        &sha256=<hex>``; ``content`` = the bytes, for the caller to read now (the
        case runtime never stores them; ``attach_mail_attachment_original`` does,
        for a PDF). Same gate as ``mail_message``, made for the actor's person
        principals only: whole originals never go to an agent identity
        (``agent:``/``sp_``), exactly as ``read_mail_original``.
        """
        person = _person_actor(actor)
        if person is None:
            raise CitationUnresolvable("live_citation_unresolvable")
        actor = person
        reader = self._gated_mail_reader(actor, mailbox)
        try:
            read = reader.get_attachment(actor, mailbox, message_id, attachment_id,
                                         max_bytes=max_bytes)
        except _LIVE_ERRORS:
            raise CitationUnresolvable("live_citation_unresolvable") from None
        denial = self._mail_record_denial(actor, read["message"])
        if denial is not None:
            raise CitationUnresolvable(_public_denial(denial))
        return {"citation": read["citation"], "message_citation": read["message"]["citation"],
                "attachment": read["attachment"], "content": read["content"],
                "record": _live_record(read["message"])}

    def read_mail_original(self, handle: str, actor: Actor) -> dict[str, Any]:
        """The original behind a mail citation, re-read now through the GET-only reader.

        * attachment citation (``#attachment=<id>&sha256=<hex>``): the bytes,
          re-hashed; returned only while they are the cited bytes;
        * body-pinned message citation (``#body_sha256=..&shown_sha256=..``):
          the bounded plain-text body, returned only while body and shown
          fields are the cited ones.

        Whole originals go to a person, never to an agent identity (the rule of
        ``OriginalStore.read_pdf``): the read is made for the actor's non-agent
        principals only, so an agent principal never grants it. Returned, not
        raised: ``{"ok": False, "error": ...}`` -- ``citation_unresolvable``
        (not visible, nonexistent, outside the mail scope, unavailable),
        ``citation_changed_since`` (resolvable, but not the cited content; cite
        ``current_citation``), ``source_not_configured`` or
        ``mail_citation_not_pinned`` (a bare message citation pins nothing).
        """
        from integrations.graph_evidence import is_mail_attachment_citation

        refused = {"ok": False, "error": "citation_unresolvable", "citation": handle}
        if not isinstance(handle, str) or not handle.startswith(MAIL_SCHEME):
            return refused
        attachment = is_mail_attachment_citation(handle)
        if not attachment and "#body_sha256=" not in handle:
            return {**refused, "error": "mail_citation_not_pinned"}
        person = _person_actor(actor)
        if person is None:
            return refused
        try:
            resolved = self._live_resolve(handle, person, need_bytes=attachment)
        except LiveSourceNotConfigured as exc:
            return {"ok": False, "error": "source_not_configured", "source": exc.scheme,
                    "citation": handle}
        except CitationUnresolvable as exc:
            if str(exc) == MAILBOX_ACCESS_UNDETERMINED:
                refused["reason"] = MAILBOX_ACCESS_UNDETERMINED
            return refused
        if resolved["status"] != "current":
            return {"ok": False, "error": "citation_changed_since", "citation": handle,
                    "current_citation": resolved["current_citation"]}
        result = {"ok": True, "citation": handle, "status": "current", "cached": False,
                  "read_at": datetime.now(timezone.utc).isoformat(),
                  "via": "GraphMailReader (GET-only guarded client)",
                  "mailbox_in_scope": self.mail_scope.allows_record(resolved["record"])}
        if attachment:
            content = resolved["content"]
            if sha256(content).hexdigest() != resolved["cited_version"]:
                return {**refused, "error": "citation_changed_since"}  # pragma: no cover
            result.update(kind="mail_attachment", content=content,
                          attachment=_attachment_shown(resolved["attachment"]),
                          message={key: resolved["record"].get(key)
                                   for key in _ATTACHMENT_MESSAGE_FIELDS})
            return result
        record = resolved["record"]
        result.update(kind="mail_message", body_text=record.get("body_text") or "",
                      body_sha256=resolved["body_sha256"],
                      body_truncated=bool(record.get("body_truncated")),
                      body_bound_chars=MAIL_BODY_CHARS, record=_shown(record))
        return result

    def attach_mail_attachment_original(self, actor: Actor, mailbox: str, message_id: str,
                                        attachment_id: str, *,
                                        max_bytes: int | None = None) -> dict[str, Any]:
        """Store one PDF mail attachment as an ``original://`` (text, OCR, pages).

        The bytes are read for ``actor`` through ``mail_attachment`` (scope,
        audience, owner ACL, folder provenance), stored content-addressed with
        an immutable provenance record (mailbox, message, attachment, sha256,
        the byte-pinned ``graph-mail://...#attachment=`` citation) and
        extracted like every original. Every later read of the original
        re-decides, for the reader, whether that message is readable now
        (``_original_source_gate``). PDF only (``unsupported_original_type``).
        """
        from integrations.document_understanding import MAX_BYTES

        cap = MAX_BYTES if max_bytes is None else max_bytes
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise OriginalError("invalid_max_bytes")
        cap = min(cap, MAX_BYTES)
        # Read and stored for the person principals only (never an agent identity).
        person = _person_actor(actor)
        if person is None:
            raise OriginalError("mail_attachment_unavailable",
                                "whole originals are read for a person, never an agent")
        actor = person
        try:
            read = self.mail_attachment(actor, mailbox, message_id, attachment_id,
                                        max_bytes=cap)
        except LiveSourceNotConfigured:
            raise
        except CitationUnresolvable:
            raise OriginalError("mail_attachment_unavailable",
                                "not visible to this actor, not a file attachment, over "
                                "the byte bound, or not readable now") from None
        attachment, record = read["attachment"], read["record"]
        content = read["content"]
        if sha256(content).hexdigest() != attachment["content_sha256"]:
            raise OriginalError("original_content_changed")  # pragma: no cover
        provenance = {
            "source": "graph-mail-attachment",
            "identity": [record["mailbox"], record["id"], attachment["id"],
                         attachment["content_sha256"]],
            "mailbox": record["mailbox"], "mailbox_upn": record.get("mailbox_upn"),
            "message_id": record["id"], "attachment_id": attachment["id"],
            "name": attachment.get("name"), "content_type": attachment.get("content_type"),
            "size": attachment.get("bytes"), "message_change_key": record.get("change_key"),
            "received_at": record.get("received_at"),
            "internet_message_id": record.get("internet_message_id"),
            "original_url": record.get("original_url"),
            "source_citation": read["citation"], "message_citation": read["message_citation"],
            "content_sha256": attachment["content_sha256"],
            "read_at": datetime.now(timezone.utc).isoformat()}
        stored = self.originals.store_source(actor, str(attachment.get("name") or ""), content,
                                             provenance)
        stored["source_citation"] = read["citation"]
        return stored

    def list_live(self, actor: Actor, source: str, **options: Any) -> dict[str, Any]:
        """Audience-checked listing of one live source with a citation per item.

        ``source``: ``mail`` (mailbox, folder, received_since), ``sharepoint``
        (drive_id, path) or ``bc`` (company_id, entity_set, equals=(field, value)
        from the entity set's filter allowlist;
        without company_id the companies). Bounded by ``limit``; ``completeness``
        says whether it was truncated or items were withheld.
        """
        live = self.live
        limit = options.get("limit", 50)
        try:
            if source == "mail":
                if live is None or live.mail is None:
                    raise LiveSourceNotConfigured("graph-mail")
                denial = self._employee_mailbox_denial(actor, live.mail, options["mailbox"])
                if denial == MAIL_OUTSIDE_SCOPE:
                    # Outside the case mail scope: nothing of it is listed, for anyone.
                    raise CitationUnresolvable(MAIL_OUTSIDE_SCOPE)
                if denial is not None:
                    # Nothing of the mailbox is listed for this employee, so the
                    # number of withheld messages is unknown: counts only.
                    return {"source": "graph-mail", "items": [], "cached": False,
                            "employee_acl_withheld_reason": denial,
                            "completeness": {"complete": False, "truncated": False,
                                             "items_read": 0, "items_returned": 0,
                                             "withheld_count": None,
                                             "withheld_reason": denial}}
                result = live.mail.list_messages(
                    actor, options["mailbox"], folder=options.get("folder"),
                    received_since=options.get("received_since"),
                    page_size=min(limit, 50), max_pages=1, max_items=min(limit, 50))
            elif source == "sharepoint":
                reader = None if live is None else live.sharepoint.get(options.get("drive_id"))
                if reader is None:
                    raise LiveSourceNotConfigured("sharepoint")
                result = reader.list_folder(actor, options.get("path") or "/",
                                            page_size=min(limit, 200), max_pages=1,
                                            max_items=limit)
            elif source == "bc":
                if live is None or live.bc is None:
                    raise LiveSourceNotConfigured("bc")
                if options.get("company_id") is None:
                    result = live.bc.companies(actor, max_items=limit)
                else:
                    equals = options.get("equals")
                    result = live.bc.list_entities(
                        actor, options["company_id"], options.get("entity_set") or "",
                        equals=None if equals is None else tuple(equals),
                        page_size=min(limit, 1000), max_pages=1, max_items=limit)
            else:
                raise CitationUnresolvable("live_source_unknown")
        except LiveSourceNotConfigured:
            raise
        except _LIVE_ERRORS:
            raise CitationUnresolvable("live_source_unavailable") from None
        items = []
        scheme = {"mail": "graph-mail"}.get(source, source)
        withheld = 0
        reasons: dict[str, int] = {}
        for item in result.get("items", []):
            denial = (self._mail_record_denial(actor, item) if scheme == "graph-mail"
                      else self._employee_acl_denial(actor, scheme, item))
            if denial is not None:
                withheld += 1
                reasons[denial] = reasons.get(denial, 0) + 1
                continue
            entry = _live_record(item)
            if "citation" in item:
                entry["citation"] = item["citation"]
            if isinstance(item.get("source_acl"), Mapping):
                entry["source_acl_status"] = item["source_acl"].get("status")
            items.append(entry)
        listing = {**{key: value for key, value in result.items() if key != "items"},
                   "items": items, "cached": False}
        completeness = dict(result.get("completeness") or {})
        if withheld:
            listing["employee_acl_withheld"] = withheld
            listing["employee_acl_withheld_reasons"] = dict(sorted(reasons.items()))
            completeness["employee_acl_withheld"] = withheld
            completeness["withheld_count"] = int(completeness.get("withheld_count") or 0) \
                + withheld
            completeness["items_returned"] = len(items)
        if completeness.get("withheld_count"):
            # Withheld (by the reader's audience/ACL or the employee's item
            # permissions) is inaccessible, not absent: never "complete".
            completeness["complete"] = False
            completeness["withheld"] = True
        if completeness:
            listing["completeness"] = completeness
        return listing


EMPLOYEE_ACL_DENIED = "employee_acl_denied"
# The source ACL statuses the employee item check (``EmployeeEntitlement.item_acl_allows``)
# accepts per scheme: SharePoint a completely read permission list, mail the derived
# mailbox-owner ACL, a Teams channel its read membership or the derived ``group_fallback``
# of its team's group, a Teams chat its read membership. Any other scheme: a read list only.
_ITEM_ACL_STATUSES = {"sharepoint": ("read",), "graph-mail": ("derived",),
                      "teams-channel": ("read", "derived"), "teams-chat": ("read",),
                      "sharepoint-list": ("read",)}


class _EmployeeItemAcl:
    """The live audience as a capture store reads it for one employee: the audience still
    decides every resource (grant, tenant, clearance); the item's captured source ACL (the
    message's and the channel's latest membership) is decided by the employee's own item
    ACL check (``CaseRuntime._employee_acl_denial``'s ``item_acl_allows``), never by the
    audience file's static identities. A missing or malformed ACL denies.

    Only an explicit allowlist is served: ``tenant``, ``allows`` and ``item_visible`` (all
    ``CaptureStore`` reads), and the identity calls answered with the employee's own
    (``identities_for``, ``visible_all``). Any other audience attribute (``covers_whole``,
    ``principals_for``, ...) raises ``AttributeError``: it would be decided by the file's
    identities, not the employee's. ``visible_all`` is ``item_visible`` for every key (none
    is ignored, unlike the audience's identity-set ``allows_all``): a captured Teams or list
    item answers to one key, so an unmatched key denies.

    One gated call holds the one entitlement the employee gate resolved for it for the
    call's whole duration; a revocation lands on the next call (decided acceptable)."""

    def __init__(self, audience: Any, entitlement: Any, scheme: str) -> None:
        self.tenant = audience.tenant
        self._audience, self._entitlement = audience, entitlement
        self._statuses = _ITEM_ACL_STATUSES[scheme]

    def allows(self, actor: Actor, resource: str) -> bool:
        return self._audience.allows(actor, resource)

    def identities_for(self, actor: Actor) -> frozenset[str]:
        return self._entitlement.source_identities if self._entitlement.actor is not None \
            else frozenset()

    def item_visible(self, actor: Actor, resource: str, source_acl: Any) -> bool:
        return self._audience.allows(actor, resource) and self._entitlement.item_acl_allows(
            source_acl, statuses=self._statuses)

    def visible_all(self, actor: Actor, resources: Sequence[str], source_acl: Any) -> bool:
        keys = [] if isinstance(resources, (str, bytes)) else list(resources)
        return bool(keys) and all(self.item_visible(actor, r, source_acl) for r in keys)


def _person_actor(actor: Any) -> Actor | None:
    """``actor`` reduced to its person principals (no ``agent:``/``sp_``, the rule
    of ``operating.entitlements``), or None when none is left: whole originals
    (attachment bytes, message bodies) are read only for a person."""
    from .entitlements import _AGENT_PREFIXES

    if not isinstance(actor, Actor):
        return None
    principals = frozenset(p for p in actor.principals if not p.startswith(_AGENT_PREFIXES))
    if not principals - {"*"}:
        return None
    return Actor(actor.tenant, principals, actor.clearance)


def _captured_handle(handle: str) -> bool:
    """A handle resolved from a host capture store, never live: every ``teams-channel://``,
    ``teams-chat://``, ``sharepoint-list://`` and ``mc://`` handle (one that is no capture
    handle is unresolvable there) and a BC capture handle."""
    return handle.startswith(TEAMS_SCHEMES + (SHAREPOINT_LIST_SCHEME, MC_SCHEME)) or (
        handle.startswith(BC_SCHEME) and is_capture_handle(handle))


def _capture_source(handle: str) -> str:
    return handle.partition("://")[0]


def _capture_view(found: Mapping[str, Any]) -> dict[str, Any]:
    """Content-stable view of a captured BC, Teams or MC revision: its immutable parts and
    whether a later capture superseded it. The cited row is kept (the capture retains it,
    so an ``as_known_at`` claim check reads it); the captured membership (``meta``) never
    is. ``changed_since`` marks a revision as no longer current, so it is never support and
    its digest differs from the current view's."""
    view = {"citation": found["citation"], "source": _capture_source(found["citation"]),
            "kind": found["kind"],
            "key": found["key"], "row": found["row"], "lineage": found["lineage"],
            "source_version": found["source_version"], "digest": found["digest"],
            "recorded_at": found["capture"]["recorded_at"],
            "status": found["status"], "changed_since": found["changed_since"]}
    if found["changed_since"]:
        view.update(superseded_at=found["capture"]["superseded_at"],
                    current_citation=found["current_citation"])
    return view


def _public_denial(denial: str) -> str:
    """The error text an employee sees: only the undetermined-mailbox label is
    distinguished; every other denial reads like not visible."""
    return denial if denial == MAILBOX_ACCESS_UNDETERMINED else "live_citation_unresolvable"


_ATTACHMENT_CACHE_MAX = 2000

# What an attachment citation's view shows of the attachment and of its message.
_ATTACHMENT_FIELDS = ("id", "name", "content_type", "bytes", "content_sha256", "is_inline")
_ATTACHMENT_MESSAGE_FIELDS = ("mailbox", "id", "internet_message_id")


def _attachment_shown(attachment: Mapping[str, Any]) -> dict[str, Any]:
    return {key: attachment.get(key) for key in _ATTACHMENT_FIELDS}


def _body_digest(record: Mapping[str, Any]) -> str:
    return sha256((record.get("body_text") or "").encode("utf-8")).hexdigest()


# Message fields a cited-mail finding states; a pinned citation covers these.
_SHOWN_MAIL_FIELDS = ("mailbox", "id", "mailbox_upn", "from", "to", "cc", "subject",
                      "received_at", "sent_at", "has_attachments", "internet_message_id",
                      "conversation_id")


def _shown(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _SHOWN_MAIL_FIELDS}


def _shown_digest(record: Mapping[str, Any]) -> str:
    return sha256(json.dumps(_shown(record), sort_keys=True, default=str,
                             ensure_ascii=False).encode("utf-8")).hexdigest()


def _pinned_mail_citation(record: Mapping[str, Any]) -> str:
    return (f"{record['citation']}#body_sha256={_body_digest(record)}"
            f"&shown_sha256={_shown_digest(record)}")


__all__ = ["CASE_RUNTIME_DIR", "CaseRuntime", "CaseRuntimeUnavailable", "LIMITATIONS",
           "LIVE_READER_SOURCES", "LIVE_SCHEMES", "LiveReaders", "LiveSourceNotConfigured",
           "MAIL_BODY_CHARS", "MC_SCHEME", "SHAREPOINT_LIST_SCHEME", "SUPPORTED_SCHEMES",
           "TEAMS_CHANNEL_SCHEME", "TEAMS_CHAT_SCHEME", "build_live_readers",
           "live_source_names"]
