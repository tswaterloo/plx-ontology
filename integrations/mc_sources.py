"""Mission Control as a capture source (L1): read-only, into the capture store.

L6. Mission Control was a read probe only; this module captures
what the MC operator scope may see into the versioned capture store
(``integrations.capture_store``, source kind ``mission_control``, scheme ``mc``), so
``ontology.project_mc`` projects it as typed objects (MCTask, MCApproval, MCBucket,
MCTaskEvent, KnowledgeEntry) and ``ontology.mentions`` links their text to the business
objects it names.

Reads (and nothing else). Every MC call goes through ``integrations.mc``'s
``MissionControlMcpClient`` restricted to ``CAPTURE_TOOLS``: ``mc_get_context`` (depth
full: every task the operator may see), ``mc_search_tasks`` (the same tasks in keyset
pages, when that answer is too big), ``mc_get_task`` (one task's approval gates and its
latest events), ``mc_list_buckets`` and ``mc_search_knowledge``. The set is checked
at import against the read class of ``integrations/mc_tool_policy.json``; a name outside
it (every write: checkout, complete, create, dismiss ...) is refused before any request,
on top of ``plx_hub_guard``'s own refusal. Each MC call still appends MC's own
``mcp.tool.invoked`` audit row, and while MC's principal authz is ``shadow`` a listing
read that sees a restricted project only through the operator email also appends one
``authz.shadow_mismatch`` row (PLX_MC@0cca756 TASK-3026 ``recordReadShadowMismatch``;
the answer is unchanged). The brain's items are not captured here:
``integrations.knowledge_source`` is the only brain-item path (namespace authority,
personal namespaces refused).

Records. Path ``<scope>/<collection>/<id>`` (``mc://...@<generation>-<capture>``),
collection one of ``COLLECTIONS``. ``row`` is the MC record with MC's own field
names plus ``scope`` and, where the record was read through another, ``task_id`` (a task
event's ``event_id`` is ``<task id>~<seq>``, MC's ``seq`` being a decimal string, or a
content digest when it is anything else);
an approval's ``workbench_intent_id`` is the ``act_<24 hex>`` a Workbench gate reason
names (``operating/action_path.py`` writes that reason), else null. ``meta`` is the
provenance: the tool, the ids it was called with and a digest of the MC
operator (never a read time: an unchanged record keeps its version). When it was known is
the store's: ``recorded_at`` (first read), ``last_seen_at`` (latest read) and
``superseded_at``, so ``as_known_at`` reads work as for every capture. Knowledge entries carry ``trust: untrusted_lead``.

Response shapes are MC's own (PLX_MC@77a5af6): ``mc_get_context`` depth full is
``{data: {tasks, buckets, projects, conflicts, errors, lastSweep}, meta: {filter}}`` (every
visible task, no total or truncation: src/lib/mcp/actions.ts ``actionGetContext``,
create-http-server.ts; tasks carry ``completedAt`` and ``cancellation`` when MC has them);
``mc_get_task`` is ``{data: {taskId, task, accountableOwner,
evidence, checkouts, events, link}}`` with events ``{seq: "<decimal>", ts, kind, actor,
repo, taskId, pr, payload}`` (read-actions.ts ``actionGetTask``, compliance/repo.ts
``eventsForTask``); ``mc_list_buckets`` is bare ``{buckets: [{id, name, owner, health,
project, archivedAt, archivedBy, archiveReason}], count}`` (actions.ts
``actionListBuckets``, ``includeArchived`` keeps retired ones); ``mc_search_tasks`` is
``{data: {tasks, total, nextCursor}, meta}`` (create-http-server.ts:188-205, full rows,
sync/task-search.ts ``searchTaskPage``); ``mc_search_knowledge`` is
``{data: {query, configured, status, hits, message}}`` (read-actions.ts). Any other shape
fails the unit closed. Retired work (TASK-2532): the unfiltered full context and
``mc_list_buckets`` leave out archived buckets, buckets of archived projects and those
buckets' tasks (PLX_MC@2f653cf actions.ts ``actionGetContext`` :193-198,
``actionListBuckets``); ``mc_search_tasks`` and a context scoped by ``taskIds`` keep them.
A task is retired in MC when its bucket is in the ``includeArchived`` list and not in the
default one (a search read), or when the default context leaves out a task the capture
holds and a context read by its id returns it in a bucket the default context does not list
(a context read, ``RETIRED_READ_IDS`` ids per read). It is recorded, with
``row.retired_in_mc`` the reason (``bucket_archived`` when its bucket carries
``archivedAt``, else ``project_archived``), never left ``missing_from_source_since``; a task
MC no longer shows at all is.

Completeness, per unit (``mc:<scope>/<collection>``), never an atomic snapshot:

* ``tasks``: one ``mc_get_context`` full read (MC returns every task the operator may
  see), ``complete``. When that answer exceeds the transport's byte bound
  (``response_bound``; live, an MC of a few thousand tasks), the tasks are read as
  ``mc_search_tasks`` keyset pages instead (no filter: every task the operator may see,
  ``SEARCH_LIMIT`` per page, a page over the bound read again at half the limit), between
  two reads of both bucket lists. MC pages from one horizon and counts ``total`` per page:
  a total that changed, a bucket list that changed, a task returned twice or more tasks
  than the total make the unit ``partial`` (``source_changed_during_read``); a task the
  pages did not return is dropped (``withheld_or_dropped``). A task version's ``meta.tools``
  names both listing tools and the run's ``detail`` (``tool``, ``arguments``) the one that
  read it, so the same task read either way is one version: a context crossing the bound
  re-versions nothing. This replaced a full read per bucket, which can never return a task
  without a bucket row (live 9826f60e9039: the bucket pages held 30 fewer tasks than MC
  counted, every run, so the unit never completed). The unit summary's ``drops`` counts
  each dropped task by reason (counts only): ``no_bucket_row`` (its bucket is in neither
  list while the lists moved), ``bucket_project_invalid`` (its bucket's project is no MC
  project id), ``not_permitted`` (counted in MC's ``total`` but on no page served to this
  principal) and ``other`` (no task id, or returned twice).
  Neither ``mc_get_context`` nor ``mc_search_tasks`` redacts checkout ids (PLX_MC@2f653cf
  create-http-server.ts, actions.ts ``actionGetContext``, sync/task-search.ts), so the
  capture client applies MC's own rule to every answer of every tool (read-actions.ts
  ``redactCheckoutIds``, as ``mc_get_task`` returns it): every ``dsp_*`` id in a string
  value becomes ``dsp_…<last 4>``. A usable id left after that (in a key, which MC's rule
  keeps) fails the unit (``checkout_credential_leaked``). The one value exempt is
  ``mc_search_tasks``' ``nextCursor``: MC's opaque base64url keyset cursor
  (task-search.ts ``searchTaskPage``), which can hold ``dsp_`` by chance, goes back to MC
  unchanged and is never recorded.
* ``approvals`` and ``task_events``: one ``mc_get_task`` per task of that read, in task-id
  order, ``max_task_reads`` per run, in pages of ``page_size`` tasks; the cursor (last
  task read) resumes the next run (``in_progress``, reason ``cap:max_task_reads``). A
  task MC no longer shows (``not_found``) is dropped, so the run ends ``partial``. A
  task the listing shows in another project has its current gates and events saved
  again under the new project in the same cycle, before any detail read.
  ``task_events`` is ``partial`` (``source_window:last_25``) when a task returned MC's
  whole 25-event window: older history was not read.
* ``buckets``: one ``mc_list_buckets`` read (``count`` must match the list), redacted as
  above.
* ``knowledge``: ``mc_search_knowledge`` per configured query: a ranked top-k, so always
  ``partial`` (``ranked_search``) and never evidence that an entry does not exist;
  redacted as above.

A source failure pauses the unit (cursor kept); it is reported, never complete. An
MC-side outage (``UpstreamError.blocks``, the rule ``mc_brain_probe`` shares) is ``reason``
``blocked_upstream`` with ``error`` the layer that failed: ``http_<status>``,
``jsonrpc_<code>``, ``tool_error``, ``transport`` or ``knowledge_status``; the CLI lists
those units in ``blocked_upstream``. Everything else is ``reason`` ``source_error`` (fail
closed), ``error`` the upstream code or the cause: ``http_<status>`` for any other HTTP
error (``auth_refused`` for 401/403), ``response_bound`` for an answer over the byte
bound, ``jsonrpc_<code>`` for a JSON-RPC error about our request
(``jsonrpc_code_invalid`` when its code is no integer), ``tool_<code>`` for any other
tool error (``code`` from ``_MC_TOOL_CODES``, else ``tool_other``; never MC's text), or
the client-side guard that refused an answer (``integrations.mc.McAnswerRefused.code``,
e.g. ``context_lacks_buckets``, ``search_tasks_shape_invalid``, ``tool_result_not_json``).

Permissions. MC returns only what the operator's MC principal may see, and MC scopes
tasks per project: a restricted project's buckets and tasks (and so the task's approval
gates and events) reach only its members, unparented ones are shared
(PLX_MC@77a5af6 src/lib/permissions/project-acl.ts ``canAccessBucket``/``canAccessTask``,
applied by src/lib/mcp/actions.ts ``actionGetContext``/``actionListBuckets`` and
read-actions.ts ``actionGetTask``; knowledge search is not project-scoped,
read-actions.ts ``actionSearchKnowledge``). The capture keeps that key per record, never
the operator's whole view for every reader: a unit's resource is ``mc:<scope>:<collection>``
and each version of a ``PROJECT_COLLECTIONS`` record also needs
``mc:<scope>:project:<project>`` (``meta.project``: the MC project id of the task's
bucket, of the bucket itself, or ``unparented``; ``capture_store.SourceKind.
item_resources``, so the current version's project gates every older one too). A task
with no bucket, or whose bucket MC has no row for, has no project in MC: MC shows it as it
shows an unparented bucket's (project-acl.ts ``canAccessTask`` looks the bucket up and
``canAccessBucket`` lets a missing project through), so it is keyed ``UNPARENTED``, and
needs that project grant like every unparented record; when the bucket lists changed
during a paged read it is dropped instead. A bucket whose project is no MC project id (and
its tasks) is dropped, never shared. MC-shared projects need their project grant too:
the capture holds no project visibility, so the Workbench cannot tell a shared project
from a restricted one. A unit
summary's ``withheld_project_grant`` counts the current records that no principal granted the
collection may read for want of the project grant (captured, readable by nobody; None
when the audience cannot list its principals). The live audience
(``live_source_guard.LiveSourceAudience``) decides which
Workbench principals may read what, and the capture store checks every read (records,
resolve, completeness) against it; the case host grants the projected audience keys
(``audience_key``: ``mc:<scope>/<collection>[/<project>]``) the same way
(``audience_resources``, ``operating/ontology_host.py``). A scope is pinned to one MC
view. MC decides the view from the key's service principal plus, while
``PLX_MC_MCP_PRINCIPAL_AUTHZ`` is ``shadow`` (MC's default), the operator email and its
directory ids; under ``enforce`` the principal alone decides (PLX_MC@8cd548a
src/lib/routing/mutations/actors.ts ``aclPrincipalFromMcp``, src/lib/mcp/auth.ts
``mcpPrincipalAuthzMode``, src/lib/permissions/project-acl.ts ``principalOnlyTokens``;
TASK-3008). So each cycle first reads the principal MC answers as (``mc_search_tasks``
``limit`` 1, ``meta.actor.servicePrincipalId``; none valid: ``search_tasks_actor_unbound``)
and pins the scope to that operator and principal in the store (``VIEW_STATE``) before
any record is saved. Capturing it later as another operator is refused
(``scope_operator_mismatch``), as another principal too (``scope_principal_mismatch``: a
new key would mix two views in one scope), and a scope captured before the pin existed
is refused (``scope_principal_unknown``: its principal is not known; capture a new
scope). When the principal read fails, the cycle reads nothing else: ``tasks``,
``buckets`` (and ``knowledge``) fail with its error.

CLI (the credentialed live verifier, live check ``mc.sources_capture``;
counts, states and booleans only, never a title, id, email or citation)::

    python3 -m integrations.mc_sources capture --store D/mc.sqlite --scope live \\
        --query "PLX Agent Workbench" --max-task-reads 25 --passes 2

Without ``--live-audience`` the store is the live check's own (tenant ``live-check``, one
reader principal). The host's ingest mode (``operating.ingest_forever``) passes its capture
audience and principal (``--live-audience``, ``--principal``) and ``--budget-seconds``: the
store is stamped with that audience's tenant, so the host's ``mc://`` reads resolve for the
readers its own audience grants; the people-exclusion list must load first (its code
otherwise, nothing read); a store stamped for another tenant is moved to
``<name>.tenant-mismatch`` and captured again (``store_reset``: ``tenant_mismatch``), never
relabelled; past the budget the task-detail reads pause (``cap:budget_seconds``) and the
next cycle resumes them.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
import time
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from .capture_store import (MAX_BATCH_HANDLES, CaptureError, CaptureStore, CapturedRecord,
                            SourceKind, register_source_kind)
from .http import SourceUnavailable
from .live_source_guard import ReadOnlyGuardError
from .mc import McAnswerRefused, MissionControlMcpClient
from .plx_hub_guard import (MC_TOOL_FAULTS, HubTransport, McConfig, UpstreamError,
                            normalize_email)
from .vmc import UNTRUSTED_LEAD

if __name__ == "__main__":
    # ``python -m`` runs this file as ``__main__``; resolving a captured handle loads the
    # capture adapters, which import it again as ``integrations.mc_sources``, and two
    # copies would register two ``mission_control`` kinds (``source_kind_conflict``).
    # Run the one registered copy instead.
    from integrations.mc_sources import main as _main
    sys.exit(_main())

KIND = "mission_control"
SCHEME = "mc"
LINEAGE = "mission_control"
COLLECTIONS = ("tasks", "approvals", "task_events", "buckets", "knowledge")
# Collections MC scopes per project (module docstring, Permissions).
PROJECT_COLLECTIONS = frozenset({"tasks", "approvals", "task_events", "buckets"})
UNPARENTED = "unparented"  # a bucket without a project (MC: shared-like)
CAPTURE_DIR, CAPTURE_FILE = "mc-capture", "capture.sqlite"
# The only MC tools a capture calls.
CAPTURE_TOOLS = frozenset({"mc_get_context", "mc_search_tasks", "mc_get_task",
                           "mc_list_buckets", "mc_search_knowledge"})
POLICY_PATH = Path(__file__).with_name("mc_tool_policy.json")
EVENT_WINDOW = 25  # mc_get_task returns the latest 25 events (pack 04 section 3 row 5)
SEARCH_LIMIT = 200  # mc_search_tasks' largest page (task-search-schema.ts: limit 1..200)
MAX_SEARCH_PAGES = 2000  # a cursor that never ends is refused (search_pages_exceeded)
RETIRED_READ_IDS = 50  # task ids per mc_get_context taskIds read (plx_hub_guard's bound)
# The tools that list tasks: a task version names both, the run the one that read it.
TASK_LISTING_TOOLS = ("mc_get_context", "mc_search_tasks")
DROP_REASONS = ("no_bucket_row", "bucket_project_invalid", "not_permitted", "other")
# A host capture reads every task's details (a live MC holds a few thousand tasks); its
# time budget (``deadline``) pauses the run first and the cursor resumes it next cycle.
MAX_TASK_READS = 50_000
# Where a host capture moves a store stamped for another tenant (``run_cli``).
TENANT_MISMATCH_SUFFIX = ".tenant-mismatch"

_POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
if (not CAPTURE_TOOLS <= frozenset(_POLICY["reads"])
        or CAPTURE_TOOLS & frozenset(_POLICY["approvable_writes"] + _POLICY["never_sent"])):
    raise ImportError("mc_sources CAPTURE_TOOLS must be read tools of mc_tool_policy.json")

_SCOPE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
# The ValueError codes this module raises: the only ValueError messages the CLI prints.
_CONFIG_CODES = frozenset({
    "mc_path_invalid", "mc_project_invalid", "mc_audience_key_invalid", "mc_scope_invalid",
    "mc_knowledge_queries_invalid", "mc_knowledge_limit_invalid",
    "mc_max_task_reads_invalid", "mc_page_size_invalid", "mc_principal_missing",
    "mc_budget_seconds_invalid"})
_RECORD_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,40}$")
_PROJECT_ID = re.compile(r"^PRJ-[A-Z0-9-]{1,120}$")  # PLX_MC src/lib/sync/mapping.ts PROJECT_ID_RE
_GATE_ID = re.compile(r"^apg_[A-Za-z0-9_-]{1,80}$")
# An MC service principal id (PLX_MC@8cd548a src/lib/permissions/types.ts
# MCP_AGENT_SERVICE_PRINCIPAL_IDS, e.g. sp_mcp_workbench).
_PRINCIPAL = re.compile(r"^sp_[a-z0-9_]{1,60}$")
VIEW_STATE = "mission_control:view:"  # + scope: the scope's pinned MC view (store state)
_USABLE_CHECKOUT = re.compile(r"dsp_[A-Za-z0-9]{8,}")
_CHECKOUT_ID = re.compile(r"dsp_[A-Za-z0-9]+")  # PLX_MC read-actions.ts CHECKOUT_ID_RE
_SEQ = re.compile(r"^[0-9]{1,20}$")
_INTENT = re.compile(r"^PLX Workbench action (act_[0-9a-f]{24}):")
BLOCKED = "blocked_upstream"
RESPONSE_BOUND = "response_bound"  # plx_hub_guard: an answer over the byte bound
# The ApiError codes PLX_MC@77a5af6 src/lib throws, named in ``tool_<code>``.
_MC_TOOL_CODES = MC_TOOL_FAULTS | frozenset({
    "forbidden", "invalid_query", "invalid_request", "invalid_json", "not_found",
    "invalid_repo", "repo_not_allowlisted", "mcp_service_principal_missing",
    "mcp_service_principal_revoked", "routing_suggest_disabled",
    "routing_suggest_unavailable_for_cohort"})


# ---------------------------------------------------------------- source kind

def _path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 3 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("mc_path_invalid")
    scope, collection, record = parts
    if _SCOPE.fullmatch(scope) is None or collection not in COLLECTIONS \
            or _RECORD_ID.fullmatch(record) is None:
        raise ValueError("mc_path_invalid")
    return scope, collection, record


def unit_of(scope: str, collection: str) -> str:
    """The capture (completeness) unit of one collection of one scope."""
    return MC_KIND.unit(_path((scope, collection, "x")))


def _version_project(path: tuple[str, ...], payload: Mapping[str, Any]) -> str | None:
    """A ``PROJECT_COLLECTIONS`` version's ``meta.project`` (ValueError unless an MC
    project id or ``UNPARENTED``); None for the other collections."""
    if path[1] not in PROJECT_COLLECTIONS:
        return None
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    project = meta.get("project") if isinstance(meta, Mapping) else None
    if project != UNPARENTED and (not isinstance(project, str)
                                  or _PROJECT_ID.fullmatch(project) is None):
        raise ValueError("mc_project_invalid")
    return project


def _item_resources(path: tuple[str, ...], payload: Mapping[str, Any]) -> tuple[str, ...]:
    """``SourceKind.item_resources``: the project resource of one version."""
    project = _version_project(path, payload)
    return () if project is None else (f"mc:{path[0]}:project:{project}",)


def audience_key(path: Sequence[str], payload: Mapping[str, Any]) -> str:
    """The projected audience key of one captured version: ``mc:<scope>/<collection>``,
    plus ``/<project>`` for ``PROJECT_COLLECTIONS`` (ValueError without a valid one)."""
    path = _path(path)
    project = _version_project(path, payload)
    return MC_KIND.unit(path) + ("" if project is None else f"/{project}")


def audience_resources(key: str) -> tuple[str, ...]:
    """Live-audience resources a reader must hold for an ``audience_key``: the
    collection's, and the project's for ``PROJECT_COLLECTIONS`` (whose key without a
    project, as projected before per-project keys, is refused). ValueError for any other
    key."""
    prefix, _, rest = key.partition(":")
    scope, _, rest = rest.partition("/")
    collection, _, project = rest.partition("/")
    if prefix != SCHEME or (collection in PROJECT_COLLECTIONS) != bool(project):
        raise ValueError("mc_audience_key_invalid")
    path = _path((scope, collection, "x"))
    return MC_KIND.resources(path) + _item_resources(path, {"meta": {"project": project}})


MC_KIND = register_source_kind(SourceKind(
    name=KIND, scheme=SCHEME, path_parts=3, canonical_path=_path,
    unit=lambda path: f"mc:{path[0]}/{path[1]}",
    resources=lambda path: (f"mc:{path[0]}:{path[1]}",),
    key=lambda path: {"id": path[2]}, item_resources=_item_resources))


# ---------------------------------------------------------------- clients

class CaptureMcClient(MissionControlMcpClient):
    """The MC client restricted to ``CAPTURE_TOOLS``: any other name (every write
    included) is refused before I/O; it records the tools it called. Every answer is
    returned with MC's ``redactCheckoutIds`` applied to the whole of it (module
    docstring), and refused (``checkout_credential_leaked``) when a usable id is left."""

    def __init__(self, config: McConfig, *, hub: HubTransport | None = None) -> None:
        super().__init__(config, hub=hub)
        self.tools_called: set[str] = set()
        self.operator_digest = sha256(normalize_email(config.operator_email)
                                      .encode()).hexdigest()[:16]

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        if name not in CAPTURE_TOOLS:
            raise ReadOnlyGuardError("tool is not on the mc_sources read allowlist")
        self.tools_called.add(name)
        answer = super().call_tool(name, arguments)
        data = answer.get("data") if isinstance(answer, dict) else None
        cursor = data.get("nextCursor") if name == "mc_search_tasks" \
            and isinstance(data, dict) else None
        payload = _redact_checkout_ids(answer)
        if _USABLE_CHECKOUT.search(json.dumps(payload, default=str)):
            raise McAnswerRefused("checkout_credential_leaked")
        if isinstance(cursor, str):  # MC's opaque cursor, sent back unchanged, never stored
            payload["data"]["nextCursor"] = cursor
        return payload

    def call_write_tool(self, name: str, arguments: Mapping[str, Any], **_kw: Any) -> Any:
        raise ReadOnlyGuardError("mc_sources never calls an MC write tool")


@dataclass(frozen=True)
class CaptureSpec:
    """What one capture cycle of one operator scope reads."""

    scope: str
    knowledge_queries: tuple[str, ...] = ()
    knowledge_limit: int = 10
    max_task_reads: int = 50
    page_size: int = 25

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or _SCOPE.fullmatch(self.scope) is None:
            raise ValueError("mc_scope_invalid")
        queries = self.knowledge_queries
        if isinstance(queries, str) or len(queries) > 10 or any(
                not isinstance(q, str) or not 1 <= len(q.strip()) <= 200 for q in queries):
            raise ValueError("mc_knowledge_queries_invalid")
        for name, low, high in (("knowledge_limit", 1, 25),
                                ("max_task_reads", 0, MAX_TASK_READS),
                                ("page_size", 1, 100)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"mc_{name}_invalid")


# ---------------------------------------------------------------- capture

def _view_state(raw: str) -> dict[str, Any]:
    """A scope's pinned view ``{operator, principal}`` (``scope_view_invalid`` else)."""
    try:
        view = json.loads(raw)
    except ValueError:
        view = None
    principal = view.get("principal") if isinstance(view, dict) else None
    if not isinstance(view, dict) or set(view) != {"operator", "principal"} \
            or not isinstance(view["operator"], str) or not (
                principal is None or isinstance(principal, str)
                and _PRINCIPAL.fullmatch(principal)):
        raise CaptureError("scope_view_invalid", "the scope's pinned MC view is unreadable")
    return view


def _text_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _RECORD_ID.fullmatch(value) else None


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str).encode()).hexdigest()


def _decimal_text(value: Any) -> Any:
    """A float score as exact decimal text (the store keeps no binary floats)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        return format(Decimal(str(value)), "f")
    except InvalidOperation:
        return None


def _version(row: Mapping[str, Any], *fields: str) -> str:
    for name in fields:
        if isinstance(row.get(name), str) and row[name]:
            return f"{name}:{row[name]}"[:200]
    return "sha256:" + _digest(row)


def _redact_checkout_ids(value: Any) -> Any:
    """MC's ``redactCheckoutIds`` (PLX_MC@2f653cf read-actions.ts): a copy of a JSON value
    with every ``dsp_*`` id in a string replaced by ``checkoutRef`` (``dsp_…`` + its last
    4); keys are kept, as MC keeps them."""
    if isinstance(value, str):
        return _CHECKOUT_ID.sub(lambda m: "dsp_\u2026" + m.group(0)[-4:], value)
    if isinstance(value, list):
        return [_redact_checkout_ids(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_checkout_ids(item) for key, item in value.items()}
    return value


def _bucket_project(bucket: Mapping[str, Any]) -> str | None:
    """A bucket's MC project id, ``UNPARENTED`` for a null or empty one (shared-like
    upstream), None for anything else (dropped: fail closed)."""
    project = bucket.get("project")
    if project is None or project == "":
        return UNPARENTED
    return project if isinstance(project, str) and _PROJECT_ID.fullmatch(project) else None


def _upstream_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code[:40] if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,40}", code) \
        else type(error).__name__


def _diagnosis(error: BaseException) -> tuple[str, str]:
    """``(reason, error)`` for a failed read (module docstring): ``UpstreamError.blocks``
    decides ``blocked_upstream`` or ``source_error``; ``error`` names the layer or code."""
    if not isinstance(error, UpstreamError):
        return "source_error", _upstream_code(error)
    blocks, layer = error.blocks(), error.layer
    if layer == "jsonrpc":
        code = ("jsonrpc_code_invalid" if error.rpc_code is None
                else f"jsonrpc_{error.rpc_code}")
    elif layer == "tool":
        code = ("tool_error" if blocks else f"tool_{error.code}"
                if error.code in _MC_TOOL_CODES else "tool_other")
    elif blocks:
        code = f"http_{error.status}" if layer == "http" else layer
    elif layer == "http" and error.code == "upstream_error" and type(error.status) is int:
        code = f"http_{error.status}"
    else:  # response_bound, auth_refused, checkout_held, or an unknown layer
        code = _upstream_code(error)
    return (BLOCKED if blocks else "source_error"), code


@dataclass(frozen=True)
class _TaskRead:
    """One read of every visible task: the rows, the buckets that key them, each retired
    bucket's reason (module docstring), the provenance, the count the rows must hold (None:
    one answer), whether the source moved while it was read, and whether a task with no
    bucket row is keyed ``UNPARENTED`` (only when the bucket list is the one the tasks were
    read with)."""

    tasks: list[Any]
    buckets: list[Any]
    retired: Mapping[str, str]
    tool: str
    arguments: dict[str, Any]
    expected: int | None
    moved: bool
    orphans_unparented: bool


def _retired_reason(bucket: Mapping[str, Any]) -> str:
    """Why MC's default view leaves a bucket out (actions.ts ``isArchived``)."""
    return "bucket_archived" if bucket.get("archivedAt") else "project_archived"


def _bucket_listing(payload: Any) -> list[Any]:
    """``mc_list_buckets``' bare ``{buckets, count}`` (``buckets_shape_invalid`` else)."""
    buckets = payload.get("buckets") if isinstance(payload, dict) else None
    count = payload.get("count") if isinstance(payload, dict) else None
    if not isinstance(buckets, list) or isinstance(count, bool) or count != len(buckets):
        raise McAnswerRefused("buckets_shape_invalid")
    return buckets


class McSourcesCapture:
    """One capture cycle of one MC operator scope into a ``CaptureStore``."""

    def __init__(self, store: CaptureStore, mc: CaptureMcClient | None, *,
                 deadline: float | None = None, clock: Any = time.monotonic) -> None:
        if not isinstance(store, CaptureStore):
            raise TypeError("store must be a CaptureStore")
        if mc is not None and not isinstance(mc, CaptureMcClient):
            raise TypeError("mc must be a CaptureMcClient")
        self.store, self.mc = store, mc
        # ``clock()`` at or past ``deadline`` pauses the task-detail reads
        # (``cap:budget_seconds``); the cursor resumes them in the next cycle
        self.deadline, self._clock = deadline, clock
        self.principal: str | None = None  # the MC service principal (``_pin_view``)

    # -- helpers ----------------------------------------------------------------

    def _record(self, spec: CaptureSpec, collection: str, record_id: str,
                row: Mapping[str, Any], meta: Mapping[str, Any], version: str,
                project: str | None = None) -> CapturedRecord:
        project_meta = {"project": project} if collection in PROJECT_COLLECTIONS else {}
        return CapturedRecord(
            path=(spec.scope, collection, record_id),
            payload={"row": {**row, "scope": spec.scope},
                     "meta": {**meta, **project_meta,
                              "operator": self.mc.operator_digest if self.mc else None}},
            source_version=version, lineage=LINEAGE)

    def _begin(self, spec: CaptureSpec, collection: str) -> dict[str, Any]:
        unit = unit_of(spec.scope, collection)
        return self.store.begin_run(KIND, unit, MC_KIND.resources((spec.scope, collection, "")))

    def _detail(self) -> dict[str, Any]:
        return {"operator": self.mc.operator_digest if self.mc else None,
                "principal": self.principal}

    def _pin_view(self, spec: CaptureSpec) -> BaseException | None:
        """Keep the scope one MC view (module docstring, Permissions): its operator and
        the service principal MC answers as, pinned in the store before any record is
        saved. Refuses another operator (``scope_operator_mismatch``), another principal
        (``scope_principal_mismatch``) and a scope captured before its view was pinned
        (``scope_principal_unknown``); returns the error of a failed principal read."""
        key = VIEW_STATE + spec.scope
        raw = self.store.state(key)
        if raw is None:
            if any(self.store.run(KIND, unit_of(spec.scope, c)) for c in COLLECTIONS):
                raise CaptureError("scope_principal_unknown",
                                   "this scope was captured before its MC principal was "
                                   "recorded; capture into a new scope")
            view: dict[str, Any] = {"operator": self.mc.operator_digest, "principal": None}
            self.store.set_state(key, json.dumps(view, sort_keys=True))
        else:
            view = _view_state(raw)
        if view["operator"] != self.mc.operator_digest:
            raise CaptureError("scope_operator_mismatch",
                               "this scope was captured as another MC operator")
        try:
            principal = self._principal()
        except (SourceUnavailable, ReadOnlyGuardError) as error:
            return error
        if view["principal"] is None:
            self.store.set_state(key, json.dumps({**view, "principal": principal},
                                                 sort_keys=True))
        elif view["principal"] != principal:
            raise CaptureError("scope_principal_mismatch",
                               "this scope was captured as another MC service principal")
        self.principal = principal
        return None

    def _principal(self) -> str:
        """The service principal MC answers this key as: ``mc_search_tasks``'
        ``meta.actor.servicePrincipalId`` (PLX_MC@8cd548a src/lib/mcp/create-http-server.ts
        195-203); an answer without a valid one is refused (``search_tasks_actor_unbound``)."""
        meta = self.mc.search_tasks("", limit=1).get("meta")
        actor = meta.get("actor") if isinstance(meta, dict) else None
        principal = actor.get("servicePrincipalId") if isinstance(actor, dict) else None
        if not isinstance(principal, str) or _PRINCIPAL.fullmatch(principal) is None:
            raise McAnswerRefused("search_tasks_actor_unbound",
                                  "mc_search_tasks names no service principal")
        return principal

    def _failed(self, run_id: int, error: BaseException) -> None:
        reason, code = _diagnosis(error)
        self.store.pause_run(run_id, reason=reason, error=code)

    def _summary(self, spec: CaptureSpec, collection: str) -> dict[str, Any]:
        unit = unit_of(spec.scope, collection)
        run = self.store.run(KIND, unit) or {}
        detail = json.loads(run["detail"]) if run.get("detail") else {}
        return {"collection": collection, "state": run.get("state", "not_run"),
                "reason": run.get("reason"), "error": run.get("error"),
                "resumable": run.get("state") == "in_progress",
                "pages": run.get("pages", 0), "records": run.get("records", 0),
                "new_versions": run.get("new_versions", 0),
                "dropped": run.get("dropped", 0),
                "drops": detail.get("drops") if collection == "tasks" else None,
                "records_current": len(self.store.current_versions(KIND, unit)),
                "withheld_project_grant": self._withheld_project_grant(unit)
                if collection in PROJECT_COLLECTIONS else 0}

    def _withheld_project_grant(self, unit: str) -> int | None:
        """Current records of the unit that no principal granted its collection may read
        for want of their project grant (a record without a valid project key needs one
        nothing grants); None when the audience cannot list principals (not measured)."""
        principals_for = getattr(self.store.audience, "principals_for", None)
        if principals_for is None:
            return None
        count = 0
        for record in self.store.current_records(KIND, unit):
            readers = principals_for(MC_KIND.resources(record.path)[0])
            try:
                needed = _item_resources(record.path, record.payload)
            except ValueError:
                needed = None
            count += bool(readers) and (needed is None or any(
                not readers & principals_for(r) for r in needed))
        return count

    def _rekey_moved(self, projects: Mapping[str, str], *runs: Mapping[str, Any]) -> None:
        """Save again, under its task's current project, each current gate and event of a
        task the listing now shows in another project: a move re-keys them in this cycle,
        not when the task-detail cursor next reaches the task."""
        for run in runs:
            moved = []
            for record in self.store.current_records(KIND, run["unit"]):
                meta = record.payload.get("meta") or {}
                project = projects.get((record.payload.get("row") or {}).get("task_id"))
                if project is not None and meta.get("project") != project:
                    moved.append(CapturedRecord(record.path,
                                                {**record.payload,
                                                 "meta": {**meta, "project": project}},
                                                record.source_version, record.lineage))
            if moved:
                self.store.save_page(run["run_id"], moved, run["cursor"])

    # -- the cycle --------------------------------------------------------------

    def capture(self, spec: CaptureSpec) -> dict[str, Any]:
        """Read every configured collection once; per-unit summaries (counts only)."""
        collections: list[str] = []
        failure = self._pin_view(spec) if self.mc is not None else None
        if failure is not None:  # no read without its view: the units fail, reported
            collections = ["tasks", "buckets"] + (["knowledge"] if spec.knowledge_queries
                                                  else [])
            for collection in collections:
                self._failed(self._begin(spec, collection)["run_id"], failure)
        elif self.mc is not None:
            projects = self._tasks(spec)
            collections.append("tasks")
            if projects is not None:
                self._task_details(spec, projects)
                collections += ["approvals", "task_events"]
            self._buckets(spec)
            collections.append("buckets")
            if spec.knowledge_queries:
                self._knowledge(spec)
                collections.append("knowledge")
        return {"scope": spec.scope, "units": [self._summary(spec, c) for c in collections],
                "tools_called": sorted(self.mc.tools_called) if self.mc else []}

    def _context_read(self) -> _TaskRead | None:
        """One ``mc_get_context`` full read: every task and bucket MC's default view
        holds, in one answer; None when that answer exceeds the byte bound."""
        try:
            data = self.mc.get_context(depth="full")["data"]
        except UpstreamError as error:
            if error.code == RESPONSE_BOUND:
                return None
            raise
        if not isinstance(data.get("buckets"), list):
            raise McAnswerRefused("context_lacks_buckets")
        return _TaskRead(data["tasks"], data["buckets"], {}, "mc_get_context",
                         {"depth": "full"}, None, False, True)

    def _with_retired(self, spec: CaptureSpec, read: _TaskRead) -> _TaskRead:
        """A context read plus the retired tasks it leaves out: every current task record
        the default context no longer lists is read again by id (``mc_get_context``
        ``taskIds``, which keeps retired work); one returned in a bucket the default context
        does not list is retired, one returned in a listed bucket moved during the read, and
        one not returned stays out (it goes missing)."""
        listed = {task.get("id") for task in read.tasks if isinstance(task, dict)}
        absent = sorted(path[2] for path, _id in self.store.current_versions(
            KIND, unit_of(spec.scope, "tasks")) if path[2] not in listed)
        default = {b.get("id") for b in read.buckets if isinstance(b, dict)}
        tasks, buckets, retired, moved = list(read.tasks), list(read.buckets), {}, read.moved
        for start in range(0, len(absent), RETIRED_READ_IDS):
            asked = absent[start:start + RETIRED_READ_IDS]
            data = self.mc.get_context(depth="full", task_ids=asked)["data"]
            if not isinstance(data.get("buckets"), list):
                raise McAnswerRefused("context_lacks_buckets")
            rows = {b["id"]: b for b in data["buckets"]
                    if isinstance(b, dict) and _text_id(b.get("id"))}
            for task in data["tasks"]:
                if not isinstance(task, dict) or task.get("id") not in asked:
                    raise McAnswerRefused("context_task_not_asked")
                bucket = rows.get(task.get("bucket"))
                if bucket is None or bucket["id"] in default:
                    moved = True  # the default context should have listed it
                    continue
                if bucket["id"] not in retired:
                    buckets.append(bucket)
                    retired[bucket["id"]] = _retired_reason(bucket)
                tasks.append(task)
        return replace(read, tasks=tasks, buckets=buckets, retired=retired, moved=moved)

    def _bucket_lists(self) -> tuple[list[Any], list[Any]]:
        """Every bucket the operator may see, retired ones included, and MC's default
        list (``mc_list_buckets``; actions.ts ``actionListBuckets``)."""
        return (_bucket_listing(self.mc.call_tool("mc_list_buckets", {"includeArchived": True})),
                _bucket_listing(self.mc.call_tool("mc_list_buckets", {})))

    def _search_read(self) -> _TaskRead:
        """Every task the operator may see, as ``mc_search_tasks`` keyset pages (module
        docstring, ``tasks``), between two reads of the bucket lists."""
        before = self._bucket_lists()
        rows: list[Any] = []
        totals: set[int] = set()
        cursor, limit = None, SEARCH_LIMIT
        for _ in range(MAX_SEARCH_PAGES):
            try:
                data = self.mc.search_tasks("", limit=limit, cursor=cursor)["data"]
            except UpstreamError as error:
                if error.code != RESPONSE_BOUND or limit == 1:
                    raise
                limit //= 2  # the same page again, smaller
                continue
            totals.add(data["total"])
            rows += data["tasks"]
            cursor = data["nextCursor"]
            if cursor is None:
                break
            if not data["tasks"]:
                raise McAnswerRefused("search_page_empty")
        else:
            raise McAnswerRefused("search_pages_exceeded")
        stable = self._bucket_lists() == before
        everything, default = before
        listed = {b.get("id") for b in default if isinstance(b, dict)}
        retired = {b["id"]: _retired_reason(b) for b in everything if isinstance(b, dict)
                   and _text_id(b.get("id")) and b["id"] not in listed}
        return _TaskRead(rows, everything, retired, "mc_search_tasks", {}, min(totals),
                         len(totals) > 1 or not stable, stable)

    def _tasks(self, spec: CaptureSpec) -> dict[str, str] | None:
        """Capture the tasks; their task id -> project (None when the read failed)."""
        run = self._begin(spec, "tasks")["run_id"]
        try:
            read = self._context_read()
            read = self._with_retired(spec, read) if read else self._search_read()
        except (UpstreamError, SourceUnavailable, ReadOnlyGuardError) as error:
            self._failed(run, error)
            return None
        bucket_projects: dict[str, str | None] = {}
        for bucket in read.buckets:
            bucket_id = _text_id(bucket.get("id")) if isinstance(bucket, dict) else None
            if bucket_id:
                bucket_projects[bucket_id] = _bucket_project(bucket)
        records, projects, seen, moved = [], {}, 0, read.moved
        drops = dict.fromkeys(DROP_REASONS, 0)
        task_ids: set[str] = set()
        for task in read.tasks:
            task_id = task.get("id") if isinstance(task, dict) else None
            if isinstance(task_id, str):
                if task_id in task_ids:
                    drops["other"] += 1  # returned twice: it moved mid-read
                    moved = True
                    continue
                task_ids.add(task_id)
            seen += 1
            bucket = task.get("bucket") if isinstance(task, dict) else None
            if isinstance(bucket, str) and bucket in bucket_projects:
                project = bucket_projects[bucket]
            elif (bucket is None or isinstance(bucket, str)) and read.orphans_unparented:
                project = UNPARENTED  # no bucket row: MC's canAccessTask treats it so
            else:
                project = None
            if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
                drops["other"] += 1
                continue
            if project is None:
                drops["bucket_project_invalid" if isinstance(bucket, str)
                      and bucket in bucket_projects else "no_bucket_row"
                      if bucket is None or isinstance(bucket, str) else "other"] += 1
                continue
            row = {k: v for k, v in task.items() if k != "approvalGates"}
            if isinstance(bucket, str) and bucket in read.retired:
                row["retired_in_mc"] = read.retired[bucket]
            records.append(self._record(spec, "tasks", task_id, row,
                                        {"tools": list(TASK_LISTING_TOOLS),
                                         "ids": {"task_id": task_id}},
                                        "sha256:" + _digest(row), project=project))
            projects[task_id] = project
        if read.expected is not None:
            drops["not_permitted"] += max(0, read.expected - seen)  # counted, on no page
            moved = moved or seen > read.expected
        dropped = sum(drops.values())
        chunks = [records[i:i + spec.page_size]
                  for i in range(0, len(records), spec.page_size)] or [[]]
        for number, chunk in enumerate(chunks, 1):
            self.store.save_page(run, chunk, None,
                                 dropped=dropped if number == len(chunks) else 0)
        self.store.finish_run(run, "partial" if moved else "complete",
                              reason="source_changed_during_read" if moved else None,
                              detail={**self._detail(), "tool": read.tool,
                                      "arguments": read.arguments, "drops": drops})
        return projects

    def _task_details(self, spec: CaptureSpec, projects: Mapping[str, str]) -> None:
        task_ids = sorted(projects)
        gates_run = self._begin(spec, "approvals")
        events_run = self._begin(spec, "task_events")
        self._rekey_moved(projects, gates_run, events_run)
        cursors = [gates_run["cursor"] if gates_run["resumed"] else None,
                   events_run["cursor"] if events_run["resumed"] else None]
        after = min(cursors) if all(cursors) else None
        pending = [t for t in task_ids if after is None or t > after]
        reads = pending[:spec.max_task_reads]
        for start in range(0, len(reads), spec.page_size):
            if self.deadline is not None and self._clock() >= self.deadline:
                for run in (gates_run, events_run):
                    self.store.pause_run(run["run_id"], reason="cap:budget_seconds")
                return
            chunk = reads[start:start + spec.page_size]
            gates, events, dropped, windowed = [], [], 0, False
            for task_id in chunk:
                try:
                    data = self.mc.get_task(task_id)["data"]
                except UpstreamError as error:
                    if error.code == "not_found":  # restricted or gone since the listing
                        dropped += 1
                        continue
                    self._failed(gates_run["run_id"], error)
                    self._failed(events_run["run_id"], error)
                    return
                except (SourceUnavailable, ReadOnlyGuardError) as error:
                    self._failed(gates_run["run_id"], error)
                    self._failed(events_run["run_id"], error)
                    return
                gates += self._gates(spec, task_id, data, projects[task_id])
                task_events = [e for e in data.get("events") or [] if isinstance(e, dict)]
                windowed |= len(task_events) >= EVENT_WINDOW
                events += self._events(spec, task_id, task_events, projects[task_id])
            marks = ("windowed",) if windowed else ()
            self.store.save_page(gates_run["run_id"], gates, chunk[-1], dropped=dropped)
            self.store.save_page(events_run["run_id"], events, chunk[-1], dropped=dropped,
                                 marks=marks)
        if len(pending) > len(reads):
            for run in (gates_run, events_run):
                self.store.pause_run(run["run_id"], reason="cap:max_task_reads")
            return
        self.store.finish_run(gates_run["run_id"], "complete", detail=self._detail())
        windowed = "windowed" in self.store.run_marks(events_run["run_id"])
        self.store.finish_run(events_run["run_id"], "partial" if windowed else "complete",
                              reason="source_window:last_25" if windowed else None,
                              detail=self._detail())

    def _gates(self, spec: CaptureSpec, task_id: str, data: Mapping[str, Any],
               project: str) -> list[CapturedRecord]:
        out = []
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        for gate in task.get("approvalGates") or []:
            gate_id = gate.get("id") if isinstance(gate, dict) else None
            if not isinstance(gate_id, str) or _GATE_ID.fullmatch(gate_id) is None:
                continue
            reason = gate.get("reason")
            intent = _INTENT.match(reason) if isinstance(reason, str) else None
            row = {**gate, "task_id": task_id,
                   "workbench_intent_id": intent.group(1) if intent else None}
            out.append(self._record(spec, "approvals", gate_id, row,
                                    {"tool": "mc_get_task", "arguments": {"id": task_id},
                                     "ids": {"task_id": task_id, "gate_id": gate_id}},
                                    _version(row, "decidedAt", "requestedAt")
                                    + f"|{row.get('status')}", project=project))
        return out

    def _events(self, spec: CaptureSpec, task_id: str, events: Iterable[Mapping[str, Any]],
                project: str) -> list[CapturedRecord]:
        out = []
        for event in events:
            seq = event.get("seq")
            local = seq if isinstance(seq, str) and _SEQ.fullmatch(seq) else _digest(event)[:16]
            record_id = f"{task_id}~{local}"
            row = {**event, "task_id": task_id, "event_id": record_id}
            out.append(self._record(spec, "task_events", record_id, row,
                                    {"tool": "mc_get_task", "arguments": {"id": task_id},
                                     "ids": {"task_id": task_id, "event": local}},
                                    "sha256:" + _digest(event), project=project))
        return out

    def _buckets(self, spec: CaptureSpec) -> None:
        run = self._begin(spec, "buckets")["run_id"]
        try:
            buckets = _bucket_listing(self.mc.call_tool("mc_list_buckets", {}))
        except (UpstreamError, SourceUnavailable, ReadOnlyGuardError) as error:
            self._failed(run, error)
            return
        records, dropped = [], 0
        for row in buckets:
            bucket_id = _text_id(row.get("id")) if isinstance(row, dict) else None
            project = _bucket_project(row) if bucket_id else None
            if project is None:
                dropped += 1
                continue
            records.append(self._record(spec, "buckets", bucket_id, row,
                                        {"tool": "mc_list_buckets", "arguments": {},
                                         "ids": {"bucket_id": bucket_id}},
                                        "sha256:" + _digest(row), project=project))
        self.store.save_page(run, records, None, dropped=dropped)
        self.store.finish_run(run, "complete", detail=self._detail())

    def _knowledge(self, spec: CaptureSpec) -> None:
        run = self._begin(spec, "knowledge")["run_id"]
        hits: dict[str, dict[str, Any]] = {}
        failure: tuple[str, str] | None = None  # a product failure outranks a blocked one
        for query in spec.knowledge_queries:
            try:
                payload = self.mc.search_knowledge(query, limit=spec.knowledge_limit)
            except (UpstreamError, SourceUnavailable, ReadOnlyGuardError) as error:
                if failure is None or failure[0] == BLOCKED:
                    failure = _diagnosis(error)
                continue
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
                failure = ("source_error", "knowledge_shape_invalid")
                continue
            for hit in data["hits"]:
                hit_id = _text_id(hit.get("id")) if isinstance(hit, dict) else None
                if hit_id is None:
                    continue
                entry = hits.setdefault(hit_id, {**hit, "score": _decimal_text(hit.get("score")),
                                                 "queries": [], "trust": UNTRUSTED_LEAD["label"]})
                entry["queries"] = sorted(set(entry["queries"]) | {query.strip()})
        records = [self._record(spec, "knowledge", hit_id, row,
                                {"tool": "mc_search_knowledge", "ids": {"entry_id": hit_id},
                                 "trust": dict(UNTRUSTED_LEAD)}, "sha256:" + _digest(row))
                   for hit_id, row in sorted(hits.items())]
        self.store.save_page(run, records, None)
        # A ranked top-k is never a complete enumeration: nothing is marked missing.
        self.store.finish_run(run, "partial",
                              reason=failure[0] if failure else "ranked_search",
                              error=failure[1] if failure else None, detail=self._detail())


def store_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / CAPTURE_DIR / CAPTURE_FILE


def store_exists(state_dir: str | Path) -> bool:
    return store_path(state_dir).is_file()


class McCaptureReader:
    """The ingestion worker's MC reader: one capture cycle per call into the state
    directory's MC capture store (``store_path``), opened for that cycle only."""

    def __init__(self, state_dir: str | Path, audience: Any,
                 mc: CaptureMcClient | None) -> None:
        self.path, self.audience, self.mc = store_path(state_dir), audience, mc

    def capture(self, spec: CaptureSpec) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with CaptureStore(self.path, self.audience) as store:
            return McSourcesCapture(store, self.mc).capture(spec)


# ---------------------------------------------------------------- CLI

def _reader_check(store: CaptureStore, units: Sequence[Mapping[str, Any]], scope: str,
                  reader: Any, outsider: Any) -> dict[str, Any]:
    """Every captured record resolves offline for a granted reader; an ungranted one
    sees no unit (counts and booleans only)."""
    handles, trimmed = [], True
    for unit in units:
        key = unit_of(scope, unit["collection"])
        after = None
        while True:
            try:
                page = store.records(reader, KIND, key, after_path=after, limit=1000)
            except CaptureError:
                break
            handles += [item["citation"] for item in page["items"]]
            after = page["next_after_path"]
            if after is None:
                break
        try:
            store.records(outsider, KIND, key, limit=1)
            trimmed = False
        except CaptureError as error:
            trimmed = trimmed and error.code == "not_visible"
    # in batches: a scope past MAX_BATCH_HANDLES records (8,262 live on 2026-10-26) made
    # one call refuse the whole check (``invalid_handles``) after a good capture
    results = []
    for start in range(0, len(handles), MAX_BATCH_HANDLES):
        results += store.resolve_many(handles[start:start + MAX_BATCH_HANDLES],
                                      reader).get("results") or []
    # a reader the audience grants nothing sees no record: never a vacuous pass
    granted = bool(handles) or not any(u.get("records_current") for u in units)
    return {"handles_resolved": len(results),
            "resolve_all_ok": granted and all(r.get("ok") for r in results)
            and len(results) == len(handles),
            "audience_trim_ok": trimmed}


def _all_records(store: CaptureStore, actor: Any, unit: str) -> list[dict[str, Any]]:
    items, after = [], None
    while True:
        try:
            page = store.records(actor, KIND, unit, include_missing=True, after_path=after,
                                 limit=1000)
        except CaptureError:
            return items
        items += page["items"]
        after = page["next_after_path"]
        if after is None:
            return items


def _project_check(store: CaptureStore, units: Sequence[Mapping[str, Any]], scope: str,
                   reader: Any) -> dict[str, Any]:
    """Per-project trimming: a reader granted every collection of the scope but one
    project only (the first captured) sees, through records, resolve and completeness,
    exactly that project's records and none of another project's (counts only).
    ``project_trim`` is ``not_measured`` below two projects: with one there is no other
    project to withhold, so ``ungranted_project_records`` 0 proves nothing."""
    from ontology.types import Actor
    from .live_source_guard import LiveSourceAudience

    collections = [u["collection"] for u in units if u["collection"] in PROJECT_COLLECTIONS]
    full = {c: _all_records(store, reader, unit_of(scope, c)) for c in collections}
    projects = sorted({item["meta"].get("project") for items in full.values() for item in items})
    granted = projects[0] if projects else None
    limited = Actor("live-check", frozenset({"live-check:project-reader"}), "restricted")
    grants = {f"mc:{scope}:{c}": [next(iter(limited.principals))] for c in COLLECTIONS}
    if granted is not None:
        grants[f"mc:{scope}:project:{granted}"] = [next(iter(limited.principals))]
    ungranted = seen = expected = 0
    with CaptureStore(store.path, LiveSourceAudience(grants, tenant=store.audience.tenant)
                      ) as view:
        for collection, items in full.items():
            unit = unit_of(scope, collection)
            own = [i for i in items if i["meta"].get("project") == granted]
            expected += len(own)
            visible = _all_records(view, limited, unit)
            seen += len(visible)
            ungranted += sum(i["meta"].get("project") != granted for i in visible)
            step = min(1000, MAX_BATCH_HANDLES)
            for start in range(0, len(items), step):
                results = view.resolve_many([i["citation"] for i in items[start:start + step]],
                                             limited)["results"]
                ungranted += sum(bool(r.get("ok")) and r["meta"].get("project") != granted
                                 for r in results)
            try:
                counted = view.completeness(limited, KIND, unit)["records_visible"]
            except CaptureError:
                counted = 0
            ungranted += max(0, counted - len(own))
    return {"projects_seen": len(projects),
            "project_trim": "measured" if len(projects) >= 2 else "not_measured",
            "project_scoped_records": sum(len(items) for items in full.values()),
            "ungranted_project_records": ungranted,
            "granted_project_records_ok": seen == expected}


def _set_aside_other_tenant(path: Path, audience: Any) -> str | None:
    """A store at ``path`` stamped for another tenant is never captured into, read under
    ``audience`` or relabelled: it is moved, with its WAL files, to
    ``<name>.tenant-mismatch`` (replacing an earlier one) and this cycle captures into a
    new store stamped for ``audience.tenant``. ``tenant_mismatch`` when it did, else None.
    A store of another format still refuses (``format_mismatch``)."""
    if not path.is_file():
        return None
    with CaptureStore.open_read_only(path, audience) as kept:
        stamped = kept.stamped_tenant()
    if stamped == audience.tenant:
        return None
    for suffix in ("", "-wal", "-shm"):
        source = path.with_name(path.name + suffix)
        target = path.with_name(path.name + TENANT_MISMATCH_SUFFIX + suffix)
        if source.exists():
            os.replace(source, target)
        elif target.exists():
            target.unlink()   # no stale WAL beside the moved store
    return "tenant_mismatch"


def run_cli(args: argparse.Namespace, environ: Mapping[str, str], *,
            hub: HubTransport | None = None) -> dict[str, Any]:
    from ontology.types import Actor
    from .live_source_guard import LiveSourceAudience

    live = hub is None
    hub = hub or HubTransport(environ=environ)
    operator = args.operator_email
    source = "argument" if operator else None
    if not operator:
        if (environ.get("MC_OPERATOR_EMAIL") or "").strip():
            source = "env"
        elif args.operator_email_default:
            operator, source = args.operator_email_default, "spec_default"
        else:
            source = "missing"
    report: dict[str, Any] = {"evidence_label": "live-read" if live else "injected-transport",
                              "operator_email_source": source, "missing_inputs": []}
    try:
        mc = CaptureMcClient(McConfig.from_env(environ, operator_email=operator), hub=hub)
    except SourceUnavailable as error:
        mc = None
        report["missing_inputs"].append(str(error)[:200])
    spec = CaptureSpec(args.scope, tuple(args.query or ()), max_task_reads=args.max_task_reads)
    budget = getattr(args, "budget_seconds", None)
    if budget is not None and not 1 <= budget <= 7200:
        raise ValueError("mc_budget_seconds_invalid")
    deadline = None if budget is None else time.monotonic() + budget
    host_audience = getattr(args, "live_audience", None)
    reset = None
    if host_audience is None:
        # the live check's throwaway store: its own tenant and one reader principal
        reader = Actor("live-check", frozenset({"live-check:mc-reader"}), "restricted")
        outsider = Actor("live-check", frozenset({"live-check:outsider"}), "restricted")
        audience = LiveSourceAudience({f"mc:{args.scope}:*": [next(iter(reader.principals))]},
                                      tenant="live-check")
    else:
        # The host's capture (operating.ingest_forever): the store is stamped with the
        # host's capture audience and read as its capture principal, so the host's
        # ``mc://`` reads (its tenant) resolve for the readers its audience grants.
        if not getattr(args, "principal", None):
            raise ValueError("mc_principal_missing")
        from .people_exclusion import PeopleExclusion
        refusal = PeopleExclusion.from_env(environ).unavailable
        if refusal:   # the capture re-checks the people guard itself: fail closed
            raise CaptureError(refusal, "people exclusion list unavailable")
        audience = LiveSourceAudience.from_file(host_audience)
        reader = Actor(audience.tenant, frozenset({args.principal}), "restricted")
        outsider = Actor(audience.tenant, frozenset({"workbench:mc-outsider-probe"}),
                         "restricted")
        reset = _set_aside_other_tenant(Path(args.store), audience)
    Path(args.store).parent.mkdir(parents=True, exist_ok=True)
    passes = []
    with CaptureStore(args.store, audience) as store:
        capture = McSourcesCapture(store, mc, deadline=deadline)
        for _ in range(args.passes):
            passes.append(capture.capture(spec))
        units = passes[-1]["units"] if passes else []
        check = _reader_check(store, units, args.scope, reader, outsider)
        check.update(_project_check(store, units, args.scope, reader))
    called = sorted({t for p in passes for t in p["tools_called"]})
    # The units an MC-side failure stopped, named by layer: the live verifier records the
    # check blocked, never failed (the live verifier).
    blocked = [{"part": u["collection"], "source": "mission-control-mcp",
                "error_code": u["error"], "cause": "upstream_outage"}
               for u in units if u["reason"] == BLOCKED]
    report.update({
        "blocked_upstream": blocked,
        "passes": [{"units": [{k: u[k] for k in ("collection", "state", "reason", "error",
                                                 "records_current", "new_versions",
                                                 "dropped")} for u in p["units"]]}
                   for p in passes],
        "requests_sent": hub.requests_sent,
        "store_reset": reset,
        "tools_called": called,
        "_summary": {
            "units_total": len(units),
            "units_complete": sum(u["state"] == "complete" for u in units),
            "units_failed": sum(u["reason"] == "source_error" for u in units),
            "units_blocked": len(blocked),
            "records_current": sum(u["records_current"] for u in units),
            "new_versions_per_pass": [sum(u["new_versions"] for u in p["units"])
                                      for p in passes],
            "tools_outside_policy": len(set(called) - CAPTURE_TOOLS),
            "tasks_dropped_by_reason": next((u["drops"] for u in units
                                             if u["collection"] == "tasks"), None),
            "atomic_upstream_snapshot": False, "deletions_proven": False, **check}})
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         hub: HubTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.mc_sources",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture", help="capture one operator scope (counts only)")
    cap.add_argument("--store", required=True, type=Path)
    cap.add_argument("--scope", required=True)
    cap.add_argument("--query", action="append", help="mc_search_knowledge query (repeat)")
    cap.add_argument("--max-task-reads", type=int, default=25)
    cap.add_argument("--passes", type=int, default=1, choices=(1, 2, 3))
    cap.add_argument("--operator-email", default=None, help="not printed")
    cap.add_argument("--operator-email-default", default=None,
                     help="used only when MC_OPERATOR_EMAIL is unset; not printed")
    cap.add_argument("--live-audience", type=Path, default=None,
                     help="the host's capture audience: stamp the store with its tenant "
                          "(with --principal); absent: the live check's own audience")
    cap.add_argument("--principal", default=None,
                     help="the host's capture principal (with --live-audience)")
    cap.add_argument("--budget-seconds", type=float, default=None,
                     help="pause the task-detail reads past this; the next run resumes")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        report = run_cli(args, env, hub=hub)
    except (CaptureError, ValueError) as error:
        # Configuration (a scope, a grant, a project id): a code on stdout, no traceback.
        # A ValueError's message is printed only when it is one of this module's codes
        # (another module's message may be code-shaped and still carry a value).
        if isinstance(error, CaptureError):
            known = isinstance(error.code, str) and _ERROR_CODE.fullmatch(error.code)
            code = error.code
        else:
            code = str(error.args[0]) if error.args else ""
            known = code in _CONFIG_CODES
        print(json.dumps({"ok": False, "error_class": type(error).__name__,
                          "error": code if known else "configuration_invalid"},
                         indent=1, sort_keys=True))
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    summary = report["_summary"]
    return 0 if summary["units_total"] and not summary["units_failed"] else 1


__all__ = ["CAPTURE_TOOLS", "COLLECTIONS", "CaptureMcClient", "CaptureSpec",
           "KIND", "LINEAGE", "MC_KIND", "McCaptureReader", "McSourcesCapture",
           "PROJECT_COLLECTIONS", "UNPARENTED", "audience_key", "audience_resources",
           "store_exists", "store_path", "unit_of"]
