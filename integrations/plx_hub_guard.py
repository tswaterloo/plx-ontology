"""Closed request grammar and guarded, retrying transport for Mission Control
(MC) and the brain (VMC knowledge today, ``brain.plxcustomer.io`` after R7).

Why this exists (§8 R1;
``05-VMC-BRAIN-TOOLS.md`` §0.3, §7.1): neither key keeps us read-only.

* ``MC_MCP_API_KEY`` resolves to a principal holding MC's full write bundle,
  and every MC write is mirrored to production SharePoint lists
  (PLX_MC ``src/lib/permissions/grants.ts:52-68``,
  ``src/lib/mcp/create-http-server.ts:67-100`` @ ``1fba0833``).
* ``VMC_API_KEY`` is accepted by every ``auth:"key"`` VMC route, including
  trading POST routes (agentic-swarm retirement F4/B22 @ ``b1117811c``).

So read-only is enforced here, before any I/O, with the same closed-grammar
pattern as ``live_source_guard``/``live_source_grammar``:

MC (``check_mc_request``)
  * ``GET https://<mc-host>/api/cursor/<route>`` only for the documented
    REST read routes, each with a closed set of query keys.
  * ``POST https://<mc-host>/api/cursor/mcp`` only with a JSON-RPC body whose
    method is ``initialize``, ``tools/list`` or ``tools/call``; ``tools/call``
    only for ``MC_READ_TOOLS`` (pack §3 rows 1-12, the **R** rows), each with
    a closed argument set. ``mc_suggest_work`` upserts a routing session and
    is treated as a write. No flag or env variable widens this.
  * The pack's **W-A** write tools (``MC_APPROVABLE_WRITE_TOOLS``:
    ``mc_create_task``, ``mc_checkout_task``, ``mc_report_progress``,
    ``mc_complete_task``, ``mc_request_approval``; pack 04 §3, §8 R1) pass
    only with an ``McWriteAuthorization``: a human's approval naming that
    exact tool and exactly those arguments, inside its validity window. The
    client adds the explicit opt-in, approver set and single use
    (``integrations.mc``). ``mc_checkout_task`` must carry
    ``repo=petralabx/<name>`` (never the unvalidated ``X-MC-Repo`` default,
    PLX_MC ``src/lib/mcp/checkout-repo.ts``). Every other write (the **NO**
    rows: projects, buckets, routing, conflicts, skills submit, swarm) is
    refused always.
MC REST write routes (POST tasks/checkout/progress/...) are never accepted.

Brain (``check_brain_request``)
  * ``GET`` only, only the 7 ``/api/vmc/knowledge/agent/*`` routes (search,
    items, node/{id}, document/{...id}, subgraph, trail, status), each with a
    closed set of query keys; every other path or method is refused.

Hosts are pinned to the ones the pack names: MC ``mc.plxcustomer.io``;
brain ``missioncontrol.tayloralton.com`` (VMC today) or
``brain.plxcustomer.io`` (after R7). Any other configured base URL is refused.

Both: plain ``https`` origin on exactly the configured host (no port,
userinfo, fragment), no redirects, every path segment decoded once carries no
``/``, ``\\``, ``%``, ``..`` or control character, and any URL named by an
``MC_GO_LIVE_TEAMS_*`` variable is refused (those post Teams messages).

``HubTransport`` is the I/O entry point: guard first, then send, with bounded
retry (at most 2 retries, jittered exponential backoff capped at 4 s,
``Retry-After`` as seconds or an HTTP date honoured up to the cap; injected
sleep, jitter and clock; the shared ``live_source_guard.BoundedBackoff``) on
429/502/503/504 for GETs and read-only MCP calls only. Writes are sent once: a lost answer is an unknown outcome, never a
resend. Secrets are held in ``live_source_guard.Secret`` and scrubbed
from every error; reprs never carry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import random
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from .http import SourceUnavailable
from .live_source_guard import (BACKOFF_CAP_SECONDS, MAX_RETRIES,
                                RETRY_STATUSES, BoundedBackoff, ReadOnlyGuardError, Secret,
                                urllib_sender)


DEFAULT_MC_BASE_URL = "https://mc.plxcustomer.io"
DEFAULT_VMC_BASE_URL = "https://missioncontrol.tayloralton.com"
DEFAULT_BRAIN_BASE_URL = "https://brain.plxcustomer.io"
BRAIN_HOST_AFTER_R7 = "brain.plxcustomer.io"
VMC_HOST = "missioncontrol.tayloralton.com"
MC_HOSTS = frozenset({"mc.plxcustomer.io"})            # pack 04 header, §8 R2
BRAIN_HOSTS = frozenset({VMC_HOST, BRAIN_HOST_AFTER_R7})  # pack 05 §4, §6
# RETRY_STATUSES, MAX_RETRIES, BACKOFF_*: the shared bounded retry in
# ``live_source_guard.BoundedBackoff`` (also used by the BC GET path).

_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,64}$")
_BUCKET_ID = re.compile(r"^BKT-[A-Za-z0-9_-]{1,80}$")
_SEARCH_CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,4096}$")  # PLX_MC sync/task-search.ts
_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9-]{0,62})+$")
_NODE_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,300}$")
_DOC_SEGMENT = re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")

# --------------------------------------------------------------- MC grammar

# Pack 04 §3 rows 1-12 (WB policy R). Value = allowed argument names.
MC_READ_TOOLS: Mapping[str, frozenset[str]] = {
    "mc_self_check": frozenset(),
    "mc_get_context": frozenset({"depth", "bucket", "taskIds"}),
    # cursor: a keyset page's nextCursor; includeArchived: retired buckets too (PLX_MC
    # @77a5af6 task-search-schema.ts, actions.ts actionListBuckets).
    "mc_search_tasks": frozenset({"q", "query", "bucket", "stage", "assignee", "limit",
                                  "cursor"}),
    "mc_list_buckets": frozenset({"q", "project", "includeArchived"}),
    "mc_get_task": frozenset({"id"}),
    "mc_list_checkouts": frozenset({"repo", "taskId", "active", "limit"}),
    "mc_search_knowledge": frozenset({"q", "limit"}),
    "mc_verify_pr": frozenset({"repo", "pr"}),
    "mc_list_conflicts": frozenset({"entityId", "taskId", "field", "limit"}),
    "mc_list_skills": frozenset({"q", "tag", "status"}),
    "mc_install_skills": frozenset({"ids", "mode", "runtimes", "projectRoot", "localRegistry"}),
    "mc_sync_skills": frozenset({"packageId", "localRegistry", "runtimes"}),
}
APPROVAL_REQUEST_TOOL = "mc_request_approval"
# Named so tests and reviewers can see the write set is explicit (pack §3).
MC_WRITE_TOOLS = frozenset({
    "mc_suggest_work", "mc_create_project", "mc_create_bucket", "mc_update_bucket",
    "mc_create_task", "mc_checkout_task", "mc_report_progress", "mc_complete_task",
    "mc_submit_skill", "mc_confirm_existing", "mc_attach_checkout",
    "mc_create_routed_task", "mc_resolve_conflict", "mc_resolve_conflicts",
    "mc_dismiss_conflict", "mc_dismiss_conflicts", APPROVAL_REQUEST_TOOL,
    # TASK-2326 lease releases (PLX_MC @073f192 checkout-release-actions.ts:221-232).
    "mc_release_checkout", "mc_release_checkouts",
    # TASK-2328 task metadata edits and TASK-633 session telemetry (PLX_MC @eeb7db7
    # task-update-actions.ts:168,172, session-telemetry-actions.ts:53).
    "mc_update_task", "mc_update_tasks", "mc_report_session_telemetry",
    # PLX_MC @77a5af6: TASK-2530 project steward edits, TASK-2559 steward-only merged-PR
    # link (project-actions.ts:212, link-merged-pr-actions.ts). mc_list_projects and
    # mc_get_approval_gate read, but stay out of the read door like the swarm reads: the
    # Workbench needs neither, and the gate read takes a bearer checkoutId
    # (approval-actions.ts:105-121; gates are read through mc_get_task).
    "mc_update_project", "mc_link_merged_pr", "mc_list_projects", "mc_get_approval_gate",
    # PLX_MC @e857f8c TASK-3166/3167: an evidence upload to the MC SharePoint library
    # (evidence-actions.ts uploadEvidenceInputShape, create-http-server.ts:346).
    "mc_upload_evidence",
    "dispatch_to_swarm", "list_swarm_teams", "swarm_health"})
# Pack 04 §3 W-A rows: writes a human may authorize per call (§8 R1/R3).
# Value = allowed argument names (PLX_MC @1fba083 create-http-server.ts:267-332,
# approval-actions.ts:14-17, actions.ts:525-544).
MC_APPROVABLE_WRITE_TOOLS: Mapping[str, frozenset[str]] = {
    "mc_create_task": frozenset({"title", "bucket", "reporter", "description", "priority",
                                 "repos", "assignee", "idempotencyKey"}),
    "mc_checkout_task": frozenset({"taskId", "repo"}),
    "mc_report_progress": frozenset({"taskId", "stage", "notes", "progressPct"}),
    "mc_complete_task": frozenset({"checkoutId", "summary", "commitSha", "prUrl",
                                   "verificationCommands", "filesChanged", "rollback",
                                   "testRun", "shots"}),
    APPROVAL_REQUEST_TOOL: frozenset({"taskId", "reason"}),
}
# Upstream inputs of those writes the door never sends (PLX_MC @d88619e); the grammar above
# plus these is the pinned input shape (ontology/tests/test_generator_mcp.py). The cancel
# fields need stage "cancelled" and task.cancel, which only humans and sp_mcp_cos (the Chief
# of Staff runtime, TASK-2635) hold, never the Workbench key (actions.ts actionProgress,
# grants.ts); checkoutId blocks a code checkout, which an action gate never has, and the
# structured proposal is not built (approval-actions.ts:24-45). A created task takes MC's
# default kind:needs-review (TASK-2634, src/lib/tasks/kind.ts), which the steward never
# auto-leases, so labels and kind are not sent. A checkout's optional agent (TASK-3336 @e857f8c create-http-server.ts:320-327) binds commit Agent trailers to a code
# checkout; an action gate has no commits, so it is not sent (omitted = not recorded).
# A completion's optional outcome (completed | blocked | failed, TASK-3362 @5bf29d2
# actions.ts:658-659) lets UAT code work hand in without a PR; the Workbench completes an
# action gate only after its verified outcome, never UAT code work, so it is not sent (MC
# records an omitted outcome as completed, actions.ts:812).
MC_WRITE_ARGUMENTS_NOT_SENT: Mapping[str, frozenset[str]] = {
    "mc_create_task": frozenset({"labels", "kind"}),
    "mc_checkout_task": frozenset({"agent"}),
    "mc_complete_task": frozenset({"outcome"}),
    "mc_report_progress": frozenset({"cancelReason", "replacedBy", "note"}),
    APPROVAL_REQUEST_TOOL: frozenset({"checkoutId", "proposal"}),
}
# Pack 04 §3 NO rows (+ stdio swarm tools): never sent, whatever is approved.
MC_NEVER_TOOLS = MC_WRITE_TOOLS - frozenset(MC_APPROVABLE_WRITE_TOOLS)
CHECKOUT_REPO_OWNER = "petralabx"
# PLX_MC types.ts:9-18. ``cancelled`` (TASK-2529, @77a5af6) is never sent: it needs
# cancelReason/replacedBy, outside this grammar, and task.cancel, which the agent bundle
# lacks (grants.ts MCP_AGENT_CAPABILITIES; only sp_mcp_cos adds it, @d88619e).
MC_TASK_STAGES = frozenset({"backlog", "specced", "approved", "planned", "progress", "qa",
                            "review", "merged", "verified"})
MC_WRITE_AUTHORIZATION_SCHEMA = "plx-mc-write-authorization/v1"
MAX_WRITE_AUTHORIZATION_WINDOW = timedelta(hours=24)
MCP_METHODS = frozenset({"initialize", "tools/list", "tools/call"})
MCP_PATH = "/api/cursor/mcp"

# REST read routes (pack 04 §3 "REST-only surfaces"), closed query keys.
MC_REST_ROUTES: Mapping[str, frozenset[str]] = {
    "/api/cursor/self-check": frozenset(),
    "/api/cursor/context": frozenset({"depth", "bucket", "taskIds"}),
    "/api/cursor/tasks": frozenset({"q", "bucket", "stage", "assignee", "limit"}),
    "/api/cursor/checkouts": frozenset({"repo", "taskId", "active", "limit"}),
    "/api/cursor/conflicts": frozenset({"entityId", "taskId", "field", "limit"}),
    "/api/cursor/buckets": frozenset({"q", "project"}),
    "/api/cursor/knowledge/search": frozenset({"q", "limit"}),
    "/api/cursor/verify": frozenset({"repo", "pr"}),
    "/api/cursor/skills/list": frozenset({"q", "tag", "status"}),
}
_MC_TASK_ROUTE = re.compile(r"^/api/cursor/tasks/(TASK-[A-Za-z0-9-]{1,64})$")

# ------------------------------------------------------------ brain grammar

BRAIN_PREFIX = "/api/vmc/knowledge/agent/"
BRAIN_ROUTES: Mapping[str, frozenset[str]] = {
    "search": frozenset({"q", "source", "mode", "limit"}),
    "items": frozenset({"q", "source", "memoryType", "since", "order", "limit", "offset"}),
    "node": frozenset({"include"}),
    "document": frozenset(),
    "subgraph": frozenset({"center", "hops", "relationTypes", "minConfidence"}),
    "trail": frozenset({"from", "to", "maxHops"}),
    "status": frozenset(),
}


def _teams_urls(environ: Mapping[str, str]) -> list[str]:
    """``MC_GO_LIVE_TEAMS_*`` values as https URLs (scheme-less values too)."""
    urls = []
    for key, value in environ.items():
        if not key.upper().startswith("MC_GO_LIVE_TEAMS_") or not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        if "://" not in value:
            value = "https://" + value.lstrip("/")
        elif value.lower().startswith("http://"):
            value = "https://" + value[7:]
        urls.append(value)
    return urls


def _check_origin(url: str, expected_host: str, environ: Mapping[str, str]):
    if not isinstance(url, str) or len(url) > 8192 or any(
            ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ReadOnlyGuardError("request url invalid")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ReadOnlyGuardError("request url invalid") from None
    if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
            or port is not None or parts.netloc.lower() != host):
        raise ReadOnlyGuardError("request url must be plain https without port or userinfo")
    if host != expected_host or host not in MC_HOSTS | BRAIN_HOSTS:
        raise ReadOnlyGuardError("host is not the configured, pack-named upstream host")
    for teams in _teams_urls(environ):
        try:
            teams_parts = urlsplit(teams)
            teams_host = (teams_parts.hostname or "").lower()
        except ValueError:
            teams_host, teams_parts = "", None
        teams_path = (teams_parts.path if teams_parts else "").rstrip("/")
        if (url.startswith(teams.rstrip("/")) or
                (teams_host == host and (not teams_path or
                                         parts.path.startswith(teams_path)))):
            raise ReadOnlyGuardError("MC_GO_LIVE_TEAMS_* URLs are never called")
    path = parts.path
    for segment in path.split("/")[1:]:
        decoded = unquote(segment)
        if ("/" in decoded or "\\" in decoded or "%" in decoded or ".." in decoded
                or decoded in ("", ".") or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
            raise ReadOnlyGuardError("path segment invalid after decoding")
    try:
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError:
        raise ReadOnlyGuardError("query string invalid") from None
    return path, query


def _check_query_keys(query: list[tuple[str, str]], allowed: frozenset[str]) -> None:
    for key, _value in query:
        if key not in allowed:
            raise ReadOnlyGuardError("query parameter not in the closed grammar")


def _is_int(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _short_text(value: Any, high: int) -> bool:
    return isinstance(value, str) and len(value) <= high and not any(
        ord(c) < 32 for c in value)


def _check_tool_arguments(name: str, args: Mapping[str, Any]) -> None:
    """Typed checks for the arguments that select records or repos."""
    if "limit" in args and not _is_int(args["limit"], 1, 500):
        raise ReadOnlyGuardError("tool limit invalid")
    for key in ("id", "taskId"):
        if key in args and not (isinstance(args[key], str) and _TASK_ID.fullmatch(args[key])):
            raise ReadOnlyGuardError(f"tool argument {key} must be TASK-*")
    if "taskIds" in args and not (isinstance(args["taskIds"], list) and len(args["taskIds"]) <= 50
                                  and all(isinstance(t, str) and _TASK_ID.fullmatch(t)
                                          for t in args["taskIds"])):
        raise ReadOnlyGuardError("tool argument taskIds must be TASK-* ids")
    if "bucket" in args and not is_bucket_id(args["bucket"]):
        raise ReadOnlyGuardError("tool argument bucket must be BKT-*")
    if "cursor" in args and not (isinstance(args["cursor"], str)
                                 and _SEARCH_CURSOR.fullmatch(args["cursor"])):
        raise ReadOnlyGuardError("tool argument cursor invalid")
    if "includeArchived" in args and not isinstance(args["includeArchived"], bool):
        raise ReadOnlyGuardError("tool argument includeArchived invalid")
    if "depth" in args and args["depth"] not in ("compact", "full"):
        raise ReadOnlyGuardError("tool argument depth invalid")
    if "repo" in args and not (isinstance(args["repo"], str) and _REPO_SLUG.fullmatch(args["repo"])):
        # Full owner/name slugs only (legacy bare-repo shim retires 2026-10-15,
        # PLX_MC src/lib/compliance/service.ts:98).
        raise ReadOnlyGuardError("tool argument repo must be an owner/name slug")
    for key in ("q", "query", "stage", "assignee", "project", "tag", "status", "field",
                "entityId", "packageId", "mode", "projectRoot"):
        if key in args and not _short_text(args[key], 500):
            raise ReadOnlyGuardError(f"tool argument {key} invalid")


_HUMAN_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_SERVICE_LABEL = re.compile(r"^(?:sp_|agent:|svc[_:-]|service[_:-])", re.IGNORECASE)
_CHECKOUT_ID = re.compile(r"^dsp_[A-Za-z0-9]{8,80}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")  # PLX_MC task-create-idempotency.ts


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """The exact-call form an approval names: sorted keys, no whitespace."""
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ReadOnlyGuardError("tool arguments are not plain JSON") from None


def _aware(value: Any, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{label} must be ISO 8601") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must carry a UTC offset")
    return value.astimezone(timezone.utc)


def is_human_email(label: Any) -> bool:
    return (isinstance(label, str) and not _SERVICE_LABEL.match(label)
            and bool(_HUMAN_EMAIL.fullmatch(label)))


def is_bucket_id(value: Any) -> bool:
    """Whether ``value`` is an MC bucket id the grammar accepts (``BKT-*``)."""
    return isinstance(value, str) and _BUCKET_ID.fullmatch(value) is not None


def normalize_email(label: str) -> str:
    """Lower-case and drop a ``+tag`` sub-address: ``Cos+x@petrasoap.com`` ->
    ``cos@petrasoap.com``. Used wherever two mailbox labels must not be
    treated as two people (operator vs approver, single-use identity)."""
    local, _, domain = label.strip().lower().rpartition("@")
    return local.split("+", 1)[0] + "@" + domain


@dataclass(frozen=True)
class McWriteAuthorization:
    """A recorded human approval of exactly one MC write call (pack 04 §8 R1).

    It names the tool and the exact arguments (compared in canonical JSON),
    the approving human (never a service principal or ``agent:`` label), the
    approval time, an expiry (at most 24 h later) and where the approval
    itself is kept. **Its origin is not cryptographically verified**: it is a
    local record, and code that can call the client could write one naming a
    configured approver. The authority for a business action stays MC's own
    approval gate (``mc_request_approval`` decided by Vince or another
    admin, re-read with ``approval_decision``). This record only lets the
    Workbench send. It grants nothing alone: the client must also have opted
    in to the tool, name the approver, and record it in its durable
    single-use ledger before I/O.
    """

    tool: str
    arguments_json: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.tool not in MC_APPROVABLE_WRITE_TOOLS:
            raise ValueError("authorization names a tool that can never be approved")
        try:
            arguments = json.loads(self.arguments_json)
        except (TypeError, ValueError):
            raise ValueError("authorization arguments must be JSON") from None
        if not isinstance(arguments, dict) or canonical_arguments(arguments) != self.arguments_json:
            raise ValueError("authorization arguments must be canonical JSON of an object")
        if not is_human_email(self.approved_by):
            raise ValueError("authorization approver must be a human email")
        approved_at = _aware(self.approved_at, "approved_at")
        expires_at = _aware(self.expires_at, "expires_at")
        if not approved_at < expires_at <= approved_at + MAX_WRITE_AUTHORIZATION_WINDOW:
            raise ValueError("authorization window must be positive and at most 24 h")
        if not isinstance(self.evidence_ref, str) or not 1 <= len(self.evidence_ref.strip()) <= 300:
            raise ValueError("authorization evidence_ref required (1..300 characters)")
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def for_call(cls, tool: str, arguments: Mapping[str, Any], *, approved_by: str,
                 approved_at: datetime | str, expires_at: datetime | str,
                 evidence_ref: str) -> "McWriteAuthorization":
        return cls(tool, canonical_arguments(dict(arguments)), approved_by,
                   approved_at, expires_at, evidence_ref)  # type: ignore[arg-type]

    @classmethod
    def from_mapping(cls, record: Mapping[str, Any]) -> "McWriteAuthorization":
        if not isinstance(record, Mapping) or record.get("schema") != MC_WRITE_AUTHORIZATION_SCHEMA:
            raise ValueError(f"write authorization must declare schema {MC_WRITE_AUTHORIZATION_SCHEMA}")
        if not isinstance(record.get("arguments"), Mapping):
            raise ValueError("write authorization arguments must be an object")
        return cls.for_call(str(record.get("tool")), record["arguments"],
                            approved_by=str(record.get("approved_by") or ""),
                            approved_at=record.get("approved_at"),  # type: ignore[arg-type]
                            expires_at=record.get("expires_at"),  # type: ignore[arg-type]
                            evidence_ref=record.get("evidence_ref"))  # type: ignore[arg-type]

    @property
    def identity(self) -> str:
        """What makes two records the same approval: tool, canonical arguments,
        normalized approver and approval time. ``evidence_ref`` and
        ``expires_at`` are excluded, so a reworded copy is not a second use."""
        return json.dumps([self.tool, self.arguments_json, normalize_email(self.approved_by),
                           self.approved_at.isoformat()], separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.identity.encode()).hexdigest()

    def names(self, tool: str, arguments: Mapping[str, Any]) -> bool:
        return self.tool == tool and self.arguments_json == canonical_arguments(arguments)


def _nonblank(value: Any, high: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= high


def _check_write_arguments(name: str, args: Mapping[str, Any]) -> None:
    """Upstream-schema checks plus the pack's traps for the W-A writes."""
    if name == "mc_checkout_task":
        repo = args.get("repo")
        owner, _, repo_name = repo.partition("/") if isinstance(repo, str) else ("", "", "")
        if owner != CHECKOUT_REPO_OWNER or not repo_name:
            # Omitted repo binds the unvalidated X-MC-Repo (ours: tswaterloo/...),
            # PLX_MC checkout-repo.ts:27-31; always pass petralabx/<name>.
            raise ReadOnlyGuardError("mc_checkout_task must pass repo=petralabx/<name>")
        if "taskId" not in args:
            raise ReadOnlyGuardError("mc_checkout_task needs taskId")
    elif name == APPROVAL_REQUEST_TOOL:
        if "taskId" not in args or not _nonblank(args.get("reason"), 500):
            raise ReadOnlyGuardError("mc_request_approval needs taskId and a 1..500 reason")
    elif name == "mc_report_progress":
        if "taskId" not in args or not ("stage" in args or "notes" in args):
            # A bare call: stage=progress before PLX_MC TASK-2560, a no-op since
            # (actions.ts actionProgress @77a5af6); nothing to authorize either way.
            raise ReadOnlyGuardError("mc_report_progress needs taskId and an explicit stage or notes")
        if "stage" in args and args["stage"] not in MC_TASK_STAGES:
            raise ReadOnlyGuardError("mc_report_progress stage invalid")
        if "notes" in args and not _nonblank(args["notes"], 20000):
            raise ReadOnlyGuardError("mc_report_progress notes invalid")
        pct = args.get("progressPct")
        if pct is not None and not (isinstance(pct, (int, float)) and not isinstance(pct, bool)
                                    and 0 <= pct <= 100):
            raise ReadOnlyGuardError("mc_report_progress progressPct must be 0..100")
    elif name == "mc_complete_task":
        commands = args.get("verificationCommands")
        if (not isinstance(args.get("checkoutId"), str)
                or not _CHECKOUT_ID.fullmatch(args["checkoutId"])
                or not _nonblank(args.get("summary"), 20000)
                or not _nonblank(args.get("rollback"), 20000)
                or not isinstance(commands, list) or not commands
                or not all(_nonblank(c, 2000) for c in commands)):
            raise ReadOnlyGuardError("mc_complete_task needs checkoutId, summary, "
                                     "verificationCommands and rollback")
    elif name == "mc_create_task":
        # reporter: z.string().min(1) (PLX_MC@1fba083 create-http-server.ts:272;
        # MC then overwrites it with the operator email).
        if (not _nonblank(args.get("title"), 500) or "bucket" not in args
                or not _nonblank(args.get("reporter"), 320)
                or not isinstance(args.get("idempotencyKey"), str)
                or not _IDEMPOTENCY_KEY.fullmatch(args["idempotencyKey"])):
            raise ReadOnlyGuardError("mc_create_task needs title, bucket, reporter and an idempotencyKey")
        assignee = args.get("assignee")
        if assignee is not None and (not _short_text(assignee, 200)
                                     or assignee.lower().startswith("agent:")):
            # Only sp_mcp_portal may set agent: assignees (agent-assignee-guard.ts).
            raise ReadOnlyGuardError("mc_create_task assignee invalid")


def check_mcp_body(body: Any, *, write_authorization: "McWriteAuthorization | None" = None,
                   now: datetime | None = None) -> str:
    """Validate one JSON-RPC request; return the tool name (or the method).

    Read tools pass on the closed grammar. A W-A write passes only with a
    ``write_authorization`` naming this exact call and valid at ``now``;
    without one (the default) every write is refused before I/O.
    """
    if not isinstance(body, Mapping) or set(body) - {"jsonrpc", "id", "method", "params"}:
        raise ReadOnlyGuardError("MCP body must be one JSON-RPC request object")
    if body.get("jsonrpc") != "2.0" or not _is_int(body.get("id"), 1, 2**31):
        raise ReadOnlyGuardError("MCP body must be JSON-RPC 2.0 with an integer id")
    method = body.get("method")
    if method not in MCP_METHODS:
        raise ReadOnlyGuardError("MCP method refused")
    params = body.get("params", {})
    if not isinstance(params, Mapping):
        raise ReadOnlyGuardError("MCP params must be an object")
    if method != "tools/call":
        return method
    if set(params) - {"name", "arguments"}:
        raise ReadOnlyGuardError("MCP tools/call params invalid")
    name = params.get("name")
    args = params.get("arguments", {})
    if not isinstance(args, Mapping):
        raise ReadOnlyGuardError("MCP tool arguments must be an object")
    if name in MC_READ_TOOLS:
        if set(args) - MC_READ_TOOLS[name]:
            raise ReadOnlyGuardError("MCP tool argument not in the closed grammar")
        _check_tool_arguments(name, args)
        return name
    if name not in MC_APPROVABLE_WRITE_TOOLS or write_authorization is None:
        raise ReadOnlyGuardError("MC write tool refused: only MC read tools are allowed "
                                 "without a per-call write authorization")
    if not isinstance(write_authorization, McWriteAuthorization):
        raise ReadOnlyGuardError("write authorization must be an McWriteAuthorization")
    if not write_authorization.names(name, args):
        raise ReadOnlyGuardError("write authorization names another call")
    try:
        instant = _aware(now, "now")
    except ValueError:
        raise ReadOnlyGuardError("write authorization needs the current time") from None
    if not write_authorization.approved_at <= instant < write_authorization.expires_at:
        raise ReadOnlyGuardError("write authorization is outside its validity window")
    if set(args) - MC_APPROVABLE_WRITE_TOOLS[name]:
        raise ReadOnlyGuardError("MCP tool argument not in the closed grammar")
    _check_tool_arguments(name, {k: v for k, v in args.items() if k != "limit"})
    _check_write_arguments(name, args)
    return name


def check_mc_request(method: str, url: str, *, mc_host: str, body: Any = None,
                     environ: Mapping[str, str] | None = None,
                     write_authorization: "McWriteAuthorization | None" = None,
                     now: datetime | None = None) -> str:
    """Validate one MC request before I/O; return an operation label."""
    env = os.environ if environ is None else environ
    if mc_host not in MC_HOSTS:
        raise ReadOnlyGuardError("MC host is not a pack-named Mission Control host")
    path, query = _check_origin(url, mc_host, env)
    method = method.upper() if isinstance(method, str) else ""
    if method == "GET":
        if body is not None:
            raise ReadOnlyGuardError("GET carries no body")
        if path in MC_REST_ROUTES:
            _check_query_keys(query, MC_REST_ROUTES[path])
            return "GET " + path
        if _MC_TASK_ROUTE.fullmatch(path):
            _check_query_keys(query, frozenset())
            return "GET /api/cursor/tasks/{id}"
        raise ReadOnlyGuardError("MC path is not a read route of the closed grammar")
    if method == "POST":
        if path != MCP_PATH or query:
            raise ReadOnlyGuardError("MC POST is allowed only to /api/cursor/mcp (JSON-RPC)")
        return "MCP " + check_mcp_body(body, write_authorization=write_authorization, now=now)
    raise ReadOnlyGuardError(f"method {method or '?'} refused")


def check_brain_request(method: str, url: str, *, brain_host: str,
                        environ: Mapping[str, str] | None = None) -> str:
    """Validate one brain request before I/O: GET to the 7 agent routes only."""
    env = os.environ if environ is None else environ
    if brain_host not in BRAIN_HOSTS:
        raise ReadOnlyGuardError("brain host is not a pack-named brain host")
    path, query = _check_origin(url, brain_host, env)
    if not isinstance(method, str) or method.upper() != "GET":
        raise ReadOnlyGuardError("brain requests are GET only")
    if not path.startswith(BRAIN_PREFIX):
        raise ReadOnlyGuardError("brain path is not one of the 7 agent knowledge routes")
    rest = path[len(BRAIN_PREFIX):].split("/")
    route = rest[0]
    if route not in BRAIN_ROUTES:
        raise ReadOnlyGuardError("brain path is not one of the 7 agent knowledge routes")
    if route == "node":
        if len(rest) != 2 or not _NODE_ID.fullmatch(unquote(rest[1])):
            raise ReadOnlyGuardError("brain node id invalid")
    elif route == "document":
        if not 2 <= len(rest) <= 9 or not all(_DOC_SEGMENT.fullmatch(unquote(s)) for s in rest[1:]):
            raise ReadOnlyGuardError("brain document id invalid")
    elif len(rest) != 1:
        raise ReadOnlyGuardError("brain path is not one of the 7 agent knowledge routes")
    _check_query_keys(query, BRAIN_ROUTES[route])
    return "GET agent/" + route


# ------------------------------------------------------------- configuration

def _origin_host(base_url: str) -> str:
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
            or parts.query or parts.path not in ("", "/") or parts.port is not None
            or parts.netloc.lower() != host or not _HOST.fullmatch(host)):
        raise ValueError("base URL must be a plain https origin")
    return host


@dataclass(frozen=True, repr=False)
class BrainConfig:
    """The brain host and key as one pair (retirement R7, pack 05 §7.1.2).

    ``BRAIN_API_KEY`` (``brn_*``) + ``BRAIN_BASE_URL`` (default
    ``https://brain.plxcustomer.io``) win when the key is set; otherwise
    ``VMC_API_URL`` (default ``https://missioncontrol.tayloralton.com``) +
    ``VMC_API_KEY``. The VMC key is never sent to the brain host.
    """

    base_url: str
    key: Secret
    pair: str  # "brain" or "vmc-fallback"

    def __post_init__(self) -> None:
        host = _origin_host(self.base_url)
        if not isinstance(self.key, Secret):
            raise ValueError("brain key must be a Secret")
        if self.pair not in ("brain", "vmc-fallback"):
            raise ValueError("brain pair invalid")
        if self.pair == "brain" and not self.key.reveal().startswith("brn_"):
            raise ValueError("BRAIN_API_KEY must be a brn_ brain key")
        if host not in BRAIN_HOSTS:
            raise ValueError("brain base URL host is not one the pack names")
        if self.pair == "vmc-fallback" and host == BRAIN_HOST_AFTER_R7:
            raise ValueError("VMC_API_KEY is never sent to the brain host")

    @property
    def host(self) -> str:
        return _origin_host(self.base_url)

    def __repr__(self) -> str:
        return f"BrainConfig(host={self.host!r}, pair={self.pair!r}, key=<redacted>)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "BrainConfig":
        env = os.environ if environ is None else environ
        brain_key = (env.get("BRAIN_API_KEY") or "").strip()
        try:
            if brain_key:
                return cls((env.get("BRAIN_BASE_URL") or DEFAULT_BRAIN_BASE_URL).strip(),
                           Secret(brain_key), "brain")
            vmc_key = (env.get("VMC_API_KEY") or "").strip()
            if not vmc_key:
                raise SourceUnavailable("missing environment variables: BRAIN_API_KEY or VMC_API_KEY")
            return cls((env.get("VMC_API_URL") or DEFAULT_VMC_BASE_URL).strip(),
                       Secret(vmc_key), "vmc-fallback")
        except ValueError as exc:
            raise SourceUnavailable(f"brain configuration invalid: {exc}") from None


@dataclass(frozen=True, repr=False)
class McConfig:
    """MC endpoint, key and admission headers (read-only use).

    ``repo`` is the honest ``x-mc-repo`` audit label (pack 04 §8 R2).
    """

    base_url: str
    key: Secret
    operator_email: str
    repo: str = "tswaterloo/OntologyProject"
    runtime: str = "plx-workbench"

    def __post_init__(self) -> None:
        if _origin_host(self.base_url) not in MC_HOSTS:
            raise ValueError("MC base URL host is not one the pack names")
        if not isinstance(self.key, Secret):
            raise ValueError("MC key must be a Secret")
        if (not isinstance(self.operator_email, str) or
                not re.fullmatch(r"[^@\s]{1,64}@[A-Za-z0-9.-]{1,255}", self.operator_email)):
            raise ValueError("MC operator email invalid")
        if not isinstance(self.repo, str) or not _REPO_SLUG.fullmatch(self.repo):
            raise ValueError("MC repo must be owner/name")
        if not isinstance(self.runtime, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", self.runtime):
            raise ValueError("MC runtime label invalid")

    @property
    def host(self) -> str:
        return _origin_host(self.base_url)

    def __repr__(self) -> str:
        return f"McConfig(host={self.host!r}, repo={self.repo!r}, key=<redacted>)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *,
                 operator_email: str | None = None) -> "McConfig":
        env = os.environ if environ is None else environ
        key = (env.get("MC_MCP_API_KEY") or "").strip()
        email = (operator_email or env.get("MC_OPERATOR_EMAIL") or "").strip()
        missing = [name for name, value in (("MC_MCP_API_KEY", key),
                                            ("MC_OPERATOR_EMAIL", email)) if not value]
        if missing:
            raise SourceUnavailable("missing environment variables: " + ", ".join(missing))
        try:
            return cls((env.get("MC_BASE_URL") or DEFAULT_MC_BASE_URL).strip(), Secret(key), email,
                       repo=(env.get("MC_REPO") or "tswaterloo/OntologyProject").strip())
        except ValueError as exc:
            raise SourceUnavailable(f"MC configuration invalid: {exc}") from None


# --------------------------------------------------------------- transport

Sender = Callable[[str, str, Mapping[str, str], "bytes | None", float, int],
                  "tuple[int, Mapping[str, str], bytes]"]


class UpstreamError(SourceUnavailable):
    """A non-success answer. ``code`` is ``upstream_unreachable``,
    ``upstream_error``, ``auth_refused``, ``checkout_held`` or ``response_bound``
    (an answer over the send's byte bound, never retried); ``status`` the
    HTTP status (None when unreachable). Messages never carry secrets.

    Diagnosis, sanitized: ``layer`` is where it failed
    (``transport``, ``http``, ``jsonrpc``, ``tool`` or ``knowledge_status``, the
    last an MC tool answer whose in-band status is not ``ok``); ``rpc_code`` the
    integer JSON-RPC error code; ``upstream_code`` the body's ``error.code`` when
    it is in the closed set ``UPSTREAM_CODES`` (else ``other``); ``cause_class`` the exception class of a
    transport failure. Never a message, body or secret."""

    def __init__(self, message: str, *, code: str, status: int | None = None,
                 attempts: int = 1, layer: str | None = None, rpc_code: int | None = None,
                 upstream_code: str | None = None, cause_class: str | None = None) -> None:
        super().__init__(message)
        self.code, self.status, self.attempts = code, status, attempts
        self.layer = layer
        self.rpc_code = (rpc_code if type(rpc_code) is int and -2**31 <= rpc_code < 2**31
                         else None)
        self.upstream_code = (None if upstream_code is None else
                              upstream_code if upstream_code in UPSTREAM_CODES else "other")
        self.cause_class = (cause_class if isinstance(cause_class, str)
                            and _CAUSE_CLASS.fullmatch(cause_class) else None)

    def blocks(self) -> bool:
        """An upstream outage (``blocked_upstream``, never a pass), not a failure."""
        return upstream_blocks(self.layer, self.code, self.status, self.rpc_code)


# JSON-RPC errors about our request (parse, invalid request/method/params): failures.
OUR_RPC_CODES = frozenset({-32700, -32600, -32601, -32602})
# MC tool codes for MC's own fault (PLX_MC@eeb7db7 src/lib/mcp/envelope.ts unhandled throw,
# skills-actions.ts 502); every other tool code is about our call or operator: a failure.
MC_TOOL_FAULTS = frozenset({"internal", "catalog_unavailable"})
# ``mc_search_knowledge``'s in-band statuses for MC's brain relay being down.
KNOWLEDGE_RETRY_STATUSES = frozenset({"upstream_error", "upstream_unreachable"})


# Graph's documented mapping (``integrations.capture_teams``): a refused credential or
# application permission is the host's misconfiguration, a blocked part, never a pass.
GRAPH_BLOCKS = {401: "unauthorized", 403: "missing_permission"}


def upstream_blocks(layer: str | None, code: Any, status: Any, rpc_code: Any,
                    source: str = "mc") -> bool:
    """The one upstream rule, by ``source``. MC and brain (``mc``): the upstream is out
    (transport, HTTP 5xx/408/429 after retries, a JSON-RPC error not about our request,
    an MC tool fault, MC's brain relay down); anything else, 401/403 and unknown layers or
    codes included, is a failure (fail closed). Graph (``graph``): an HTTP status of
    ``GRAPH_BLOCKS`` only. An unknown source never blocks."""
    if source != "mc":
        return (source == "graph" and layer == "http" and type(status) is int
                and status in GRAPH_BLOCKS)
    return (layer == "transport"
            or layer == "http" and type(status) is int
            and (status >= 500 or status in (408, 429))
            or layer == "jsonrpc" and type(rpc_code) is int and rpc_code not in OUR_RPC_CODES
            or layer == "tool" and code in MC_TOOL_FAULTS
            or layer == "knowledge_status" and code in KNOWLEDGE_RETRY_STATUSES)


# The closed vocabulary of ``upstream_code``: any other value reads ``other``.
# These are the codes Mission Control's MCP route emits (PLX_MC 073f192, src/lib/mcp), plus
# two 503s: a malformed key rotation config (``auth.ts:119@d88619e``, TASK-619) and MC's DB
# schema behind its code (``scripts/lib/schema-version.mjs:18@d88619e``, TASK-2556).
UPSTREAM_CODES = frozenset({"invalid_api_key", "mcp_disabled", "mcp_key_not_configured",
                            "missing_operator", "operator_not_allowed", "missing_repo",
                            "mcp_service_principal_missing", "mcp_service_principal_revoked",
                            "invalid_request", "invalid_json", "internal", "forbidden",
                            "not_found", "checkout_held", "mcp_key_config_invalid",
                            "schema_behind"})
# A class name is a Python identifier.
_CAUSE_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class CheckoutHeld(UpstreamError):
    """MC answered 409 ``checkout_held`` (frontier P4 exclusive lease).

    ``upstream_message`` is MC's text, "<task> is checked out on <repo> by
    another principal until <expires_at>." (PLX_MC
    ``docs/specs/frontier-implementation-spec.md:606`` @1fba083), scrubbed and
    bounded; it never names the holder."""

    def __init__(self, message: str, *, code: str = "checkout_held", status: int | None = 409,
                 attempts: int = 1, upstream_message: str | None = None) -> None:
        super().__init__(message, code=code, status=status, attempts=attempts)
        self.upstream_message = upstream_message


class HubTransport:
    """Guard -> send -> bounded retry. The only I/O path for MC and brain."""

    def __init__(self, sender: Sender = urllib_sender, *,
                 environ: Mapping[str, str] | None = None,
                 timeout_seconds: float = 15.0,
                 sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[], float] = random.random,
                 max_retries: int = MAX_RETRIES,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout must be 0..60 seconds")
        if not _is_int(max_retries, 0, MAX_RETRIES):
            raise ValueError(f"max_retries must be 0..{MAX_RETRIES}")
        self._sender = sender
        self._environ = environ
        self._timeout = float(timeout_seconds)
        self._max_retries = max_retries
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._backoff_policy = BoundedBackoff(max_retries=max_retries, sleep=sleep,
                                              jitter=jitter, clock=self.now)
        self._secrets: list[Secret] = []
        self.requests_sent = 0
        self.last_attempts = 0

    def __repr__(self) -> str:
        return f"HubTransport(requests_sent={self.requests_sent})"

    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def register_secret(self, secret: Secret) -> None:
        if not any(s is secret for s in self._secrets):
            if len(self._secrets) >= 8:
                raise RuntimeError("secret registry bound exceeded")
            self._secrets.append(secret)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret.reveal() in text:
                text = text.replace(secret.reveal(), "<redacted>")
        return text

    def now(self) -> datetime:
        """The injected clock (UTC); used for Retry-After dates and write windows."""
        return _aware(self._clock(), "clock")

    @property
    def slept_seconds(self) -> float:
        return self._backoff_policy.slept_seconds

    def pause(self, attempt: int, headers: Mapping[str, str] | None = None) -> None:
        """Sleep one bounded, jittered backoff step (also used for MC's
        in-band ``upstream_error`` knowledge status)."""
        self._backoff_policy.pause(attempt, headers or {})

    @property
    def max_retries(self) -> int:
        return self._max_retries

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, *,
             retry: bool, max_bytes: int = 4_000_000) -> tuple[int, Mapping[str, str], bytes]:
        """Send an already-guarded request. Callers must guard first."""
        attempts = 1 + (self._max_retries if retry else 0)
        host = urlsplit(url).hostname
        for attempt in range(attempts):
            self.requests_sent += 1
            self.last_attempts = attempt + 1
            try:
                status, response_headers, raw = self._sender(
                    method, url, dict(headers), body, self._timeout, max_bytes)
            except ReadOnlyGuardError:
                raise
            except Exception as exc:
                raise UpstreamError(self.scrub(
                    f"{method} to {host} failed: {type(exc).__name__}"),
                    code="upstream_unreachable", attempts=attempt + 1, layer="transport",
                    cause_class=type(exc).__name__) from None
            if not isinstance(status, int) or not isinstance(raw, (bytes, bytearray)):
                raise UpstreamError("sender returned an invalid response",
                                    code="upstream_error", attempts=attempt + 1,
                                    layer="transport")
            if 300 <= status < 400:
                raise ReadOnlyGuardError("redirect refused; credentials are never forwarded")
            if len(raw) > max_bytes:
                raise UpstreamError(f"response exceeds the {max_bytes}-byte bound",
                                    code="response_bound", status=status, attempts=attempt + 1,
                                    layer="http")
            if status in RETRY_STATUSES and attempt + 1 < attempts:
                self.pause(attempt, response_headers or {})
                continue
            return status, dict(response_headers or {}), bytes(raw)
        raise AssertionError("unreachable")  # pragma: no cover


def error_from_response(status: int, raw: bytes, *, attempts: int,
                        scrub: Callable[[str], str]) -> UpstreamError:
    """Map a non-2xx answer to a typed error with only code/status text."""
    code = ""
    upstream_message = None
    try:
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            code = error["code"][:80]
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            upstream_message = scrub(error["message"][:300])
    except (ValueError, TypeError):
        pass
    message = scrub(f"upstream answered HTTP {status}" + (f" ({code})" if code else ""))
    if status == 409 and code == "checkout_held":
        return CheckoutHeld(message, code="checkout_held", status=status, attempts=attempts,
                            upstream_message=upstream_message)
    kind = "auth_refused" if status in (401, 403) else "upstream_error"
    return UpstreamError(message, code=kind, status=status, attempts=attempts, layer="http",
                         upstream_code=scrub(code) if code else None)


__all__ = ["APPROVAL_REQUEST_TOOL", "BRAIN_HOSTS", "BRAIN_ROUTES", "CHECKOUT_REPO_OWNER",
           "MC_APPROVABLE_WRITE_TOOLS", "MC_HOSTS", "MC_NEVER_TOOLS", "MC_WRITE_ARGUMENTS_NOT_SENT",
           "MC_WRITE_AUTHORIZATION_SCHEMA", "BrainConfig", "CheckoutHeld",
           "HubTransport", "KNOWLEDGE_RETRY_STATUSES", "MC_READ_TOOLS", "MC_REST_ROUTES", "MC_WRITE_TOOLS",
           "McConfig", "McWriteAuthorization", "RETRY_STATUSES", "ReadOnlyGuardError",
           "UpstreamError", "canonical_arguments", "check_brain_request", "check_mc_request",
           "check_mcp_body", "error_from_response", "is_bucket_id", "is_human_email",
           "normalize_email",
           "GRAPH_BLOCKS", "upstream_blocks"]
