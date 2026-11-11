"""Per-system write adapters for the autopilot action executor (``operating.autopilot_write``).

An adapter turns one routine action (``autopilot_write.actions_of``) into the exact request
the owning system's documented write API takes (``request``) and the exact request that
undoes it (``undo_request``), performs the request once through an injected transport
(``perform``), and never decides whether an action may run: the executor does (kill
switches, the people exclusion, the routine rules, the write gate, rate limits,
idempotency) and calls ``perform`` only when every check holds. While a workflow is not
live, the executor records both requests as a ``dry_run``.

**Undo.** ``undo_request`` is built before anything is sent. A value only the system's
answer holds (the id it assigned, the etag after the write) is a placeholder
``{response:<path>}`` (path segments split by ``/``); after a ``done`` write, ``fill``
puts the answer's values in and refuses (no undo) when one is missing or not a plain
id/etag. An edit's undo restores the values the action read before it (``precondition.
prior``, required for every field the edit sets); a system with no undo for an action
answers ``None`` and ``undo_note`` says what a person does instead.

**Transports.** Mail goes through the approved send path (``integrations.graph_send.
ActionSender``, off unless ``PLX_ACTIONS_LIVE=1``, the sender is listed and the token holds
``Mail.Send``). Planner, calendar, SharePoint and BC get their guarded write transport
(``integrations.write_transports``, through ``live_source_guard.write_action``'s exact
shapes) only once the owner opened that system (``PLX_WRITES_LIVE_SYSTEMS``); the
transport's ``missing()`` makes the adapter ``unavailable`` while anything else is lacking.
MC, Teams, Portal and FileMaker have no transport (``structural``); ``needs`` names
exactly what is missing.

``perform`` returns ``{"status": "done" | "failed" | "unknown" | "not_performed", ...}``:
``done`` the system accepted it; ``failed`` a definite refusal (nothing written);
``unknown`` it may have been written (a 5xx, a lost answer): never retried, a person
reconciles; ``not_performed`` nothing left (a dry run of the underlying sender).

Evidence label of tests: ``injected-transport`` (fake transports and recorded answers);
live-unverified.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import os
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

Transport = Callable[[Mapping[str, Any], str], tuple[int, Mapping[str, Any]]]

GRAPH = "https://graph.microsoft.com/v1.0"
MC_BASE = "https://mc.plxcustomer.io"
_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,64}$")
_GATE_ID = re.compile(r"^[A-Za-z0-9_:.-]{1,120}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_GRAPH_ID = re.compile(r"^[A-Za-z0-9_=!.:@-]{1,400}$")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGITS = re.compile(r"^\d{1,12}$")
_NOT_DEFINITE = frozenset({408, 425, 429})
_PLACEHOLDER = re.compile(r"\{response:([A-Za-z0-9@._/-]{1,80})\}")
_URL_VALUE = re.compile(r"^[A-Za-z0-9_=!.:@-]{1,400}$")


class AdapterRefused(ValueError):
    """The action cannot be turned into a request this adapter sends (nothing was sent)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _outcome(status: int, body: Mapping[str, Any] | None) -> dict[str, Any]:
    if 200 <= status < 300:
        return {"status": "done", "http_status": status, "response": dict(body or {})}
    if 400 <= status < 500 and status not in _NOT_DEFINITE:
        return {"status": "failed", "http_status": status,
                "detail": str((body or {}).get("code") or (body or {}).get("error") or "")[:80]}
    return {"status": "unknown", "http_status": status}


# ------------------------------------------------------------------ undo placeholders

def _answer(response: Mapping[str, Any], path: str) -> Any:
    value: Any = response
    for part in path.split("/"):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def fill(plan: Mapping[str, Any] | None, response: Mapping[str, Any] | None
         ) -> dict[str, Any] | None:
    """``plan`` with every ``{response:<path>}`` replaced by the system's answer; None
    when a value is missing or unsafe where it goes (a URL takes a plain id, a header no
    line break, a body value a string or number): then there is no undo."""
    if plan is None:
        return None
    response = response if isinstance(response, Mapping) else {}
    failed: list[str] = []

    def text(match: re.Match[str], where: str) -> str:
        got = _answer(response, match.group(1))
        value = str(got) if isinstance(got, (str, int)) and not isinstance(got, bool) else ""
        ok = _URL_VALUE.fullmatch(value) if where == "url" else (
            value and len(value) <= 300 and "\r" not in value and "\n" not in value)
        if not ok:
            failed.append(match.group(1))
            return ""
        return quote(value, safe="=!.:@-_") if where == "url" else value

    def body(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: body(v) for k, v in value.items()}
        if isinstance(value, list):
            return [body(v) for v in value]
        if isinstance(value, str):
            whole = _PLACEHOLDER.fullmatch(value)
            if whole:
                got = _answer(response, whole.group(1))
                if isinstance(got, bool) or not isinstance(got, (str, int)):
                    failed.append(whole.group(1))
                    return None
                return got
            return _PLACEHOLDER.sub(lambda m: text(m, "body"), value)
        return value

    out = dict(plan)
    out["url"] = _PLACEHOLDER.sub(lambda m: text(m, "url"), str(plan.get("url") or ""))
    if isinstance(plan.get("headers"), Mapping):
        out["headers"] = {k: _PLACEHOLDER.sub(lambda m: text(m, "header"), str(v))
                          for k, v in plan["headers"].items()}
    out["body"] = body(plan.get("body"))
    return None if failed else out


def placeholders(plan: Any) -> list[str]:
    """The answer values an undo plan waits for (for the dry-run record)."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str):
            found.extend(_PLACEHOLDER.findall(value))
    walk(plan)
    return sorted(set(found))


# ------------------------------------------------------------------ shared checks

def _text(value: Any, limit: int) -> str | None:
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else None


def _prior(action: Mapping[str, Any], keys: Sequence[str], system: str) -> dict[str, Any]:
    """The values the edited fields had when the action read the record: every field the
    edit sets must be there, or the edit is refused (it could not be undone)."""
    prior = (action.get("precondition") or {}).get("prior")
    if not isinstance(prior, Mapping):
        raise AdapterRefused([f"{system}: an edit needs precondition.prior (the values as "
                              "read) for its undo"])
    missing = [k for k in keys if k not in prior]
    if missing:
        raise AdapterRefused([f"{system}: precondition.prior lacks {', '.join(missing[:5])}"])
    return {k: prior[k] for k in keys}


def _only(payload: Mapping[str, Any], allowed: Sequence[str], system: str, op: str) -> None:
    extra = [k for k in payload if k not in allowed]
    if extra:
        raise AdapterRefused([f"{system}: {op} cannot set {', '.join(sorted(extra)[:5])}"])


def _instant_text(value: Any) -> bool:
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return isinstance(value, str)
    except ValueError:
        return False


class Adapter:
    """One system's write path. Subclasses set ``system``, ``kind``, ``documented``,
    ``operations`` (name -> what it does and its undo), ``needs`` and ``structural``, and
    implement ``request`` and ``undo_request``."""

    system = ""
    kind = ""
    documented = ""
    operations: Mapping[str, str] = {}
    needs: tuple[str, ...] = ()
    structural: str | None = None   # why no transport can exist here yet (None: it can)

    def __init__(self, transport: Transport | None = None, *,
                 environ: Mapping[str, str] | None = None) -> None:
        self.transport = transport
        self.environ = dict(os.environ if environ is None else environ)

    def unavailable(self) -> str | None:
        if self.transport is not None:
            missing = getattr(self.transport, "missing", None)
            found = missing() if callable(missing) else []
            return "; ".join(str(m) for m in found) or None
        return self.structural or "no reviewed write transport for this system in this build"

    def _operation(self, action: Mapping[str, Any]) -> str:
        operation = str(action.get("operation") or "")
        if operation not in self.operations:
            raise AdapterRefused([f"{self.system}: unknown operation {operation[:60]}"])
        return operation

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        """The request that undoes ``request`` (placeholders for the answer's values), or
        None when the system has no undo for it (``undo_note``)."""
        return None

    def people(self, action: Mapping[str, Any]) -> list[str]:
        """Object ids or addresses the action acts for beyond those in its text (cleared
        by the people exclusion like every recipient)."""
        return []

    def undo_note(self) -> str:
        return "The system has no undo for this action."

    def perform(self, request: Mapping[str, Any], key: str) -> dict[str, Any]:
        why = self.unavailable()
        if why:
            return {"status": "not_performed", "missing": [why]}
        try:
            status, body = self.transport(request, key)            # type: ignore[misc]
        except (OSError, TimeoutError) as error:
            return {"status": "unknown", "http_status": None, "detail": type(error).__name__}
        return _outcome(int(status), body)

    def describe(self) -> dict[str, Any]:
        why = self.unavailable()
        return {"system": self.system, "kind": self.kind, "documented_api": self.documented,
                "operations": sorted(self.operations),
                "state": "ready" if why is None else f"unavailable: {why}",
                "needs": list(self.needs) if why else []}


# ------------------------------------------------------------------ Mission Control

def _mcp_call(tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """One MCP ``tools/call`` as ``integrations.mc.MissionControlMcpClient`` sends it (the
    client numbers the JSON-RPC id and adds the key and operator headers)."""
    return {"method": "POST", "url": f"{MC_BASE}/api/cursor/mcp",
            "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": tool, "arguments": dict(arguments)}}}


class MCApprovalAdapter(Adapter):
    """Decide a routine MC approval gate: ``POST /api/approvals/decide {taskId, gateId,
    decision, note, renderedScope, renderedSourceRef}``.
    MC grants ``approval.decide`` only to a human Entra session (admin/owner) and always
    denies it to services, so no key this repo holds can decide; the decider must not be
    the requester (403 ``separation_of_duties``). A decided approval has no revoke: a
    launch approval lapses after MC's 4 h launch TTL. Which gates are routine is the
    rules' ``scope_kind_in`` (launch only), never this adapter."""

    system, kind = "mc", "mc_approval"
    documented = "POST https://mc.plxcustomer.io/api/approvals/decide"
    operations = {"approval_decide": "approve a routine gate; no undo (no revoke in MC)"}
    structural = ("MC denies approval.decide to services; the decide route takes only a "
                  "human admin/owner Entra session")
    needs = ("a delegated Entra session token of the owner (MC.Admin/MC.Owner) for the "
             "decide route, issued and refreshed outside git",
             "a reviewed POST shape for /api/approvals/decide in the HTTP egress allowlist",
             "MC capture of each gate's scope (kind launch) so a gate can be classed routine")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        target, payload = action.get("target") or {}, action.get("payload") or {}
        problems = []
        if not _TASK_ID.fullmatch(str(target.get("task_id") or "")):
            problems.append("task_id")
        if not _GATE_ID.fullmatch(str(target.get("gate_id") or "")):
            problems.append("gate_id")
        scope = payload.get("scope")
        if not isinstance(scope, Mapping) or not scope.get("kind"):
            problems.append("scope")
        if problems:
            raise AdapterRefused([f"mc approval: {p} missing or malformed" for p in problems])
        body = {"taskId": target["task_id"], "gateId": target["gate_id"],
                "decision": "approved", "note": str(payload.get("note") or "")[:500],
                "renderedScope": dict(scope)}
        if payload.get("source_ref"):
            body["renderedSourceRef"] = str(payload["source_ref"])[:500]
        return {"method": "POST", "url": self.documented.split(" ", 1)[1], "body": body}

    def undo_note(self) -> str:
        return "MC has no revoke of a decided approval; a launch approval lapses after 4 h."


class MCTaskAdapter(Adapter):
    """A routine Mission Control task write through the MCP door
    (``MissionControlMcpClient.call_write_tool``):

    * ``task_note``: ``mc_report_progress {taskId, notes}`` (never ``stage``, never the
      cancel fields): MC adds the note as a comment by the operator email. MC has no
      tool that removes a comment, so a note has no undo.
    * ``task_update``: ``mc_update_task {taskId, patch}`` with only ``addLabels``,
      ``removeLabels``, ``title``, ``priority``.
      Undo: ``mc_update_task`` with the inverse patch computed from the
      labels, title and priority the action read (``precondition.prior``).

    Never money, prices, credit, permissions or judgment calls: the routine rules refuse
    those before this adapter is asked."""

    system, kind = "mc", "mc_task"
    documented = "MC MCP POST /api/cursor/mcp tools/call mc_report_progress | mc_update_task"
    operations = {"task_note": "mc_report_progress notes; no undo (comments stay)",
                  "task_update": "mc_update_task labels/title/priority; undo: inverse patch"}
    structural = ("no Workbench MC principal holds a write grant; mc_update_task is a "
                  "never-sent tool in mc_tool_policy.json")
    needs = ("an MC service principal for the Workbench autopilot (e.g. sp_mcp_workbench) "
             "with task.progress and the project ACL, its key in MC_MCP_API_KEY outside git "
             "(the shared sp_mcp_cursor key is never borrowed for writes)",
             "mc_update_task reclassified from never_sent to approvable_writes in "
             "integrations/mc_tool_policy.json and plx_hub_guard after re-reading "
             "src/lib/mcp/task-update-actions.ts at a re-pinned PLX_MC commit",
             "mc_report_progress needs an active checkout of the task by our principal "
             "(MC answers 403 otherwise, TASK-2560)",
             "an autopilot authorization path in place of the per-call human "
             "McWriteAuthorization the MCP write door requires today")
    _LABEL = re.compile(r"^[A-Za-z0-9_./-]{1,40}(?::[A-Za-z0-9_./-]{1,40})?$")
    _PRIORITY = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
    PATCH_KEYS = ("addLabels", "removeLabels", "title", "priority")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = self._operation(action)
        task = str((action.get("target") or {}).get("task_id") or "")
        if not _TASK_ID.fullmatch(task):
            raise AdapterRefused(["mc task: task_id missing or malformed"])
        payload = action.get("payload") or {}
        if operation == "task_note":
            _only(payload, ("notes",), "mc task", operation)
            notes = _text(payload.get("notes"), 4000)
            if notes is None:
                raise AdapterRefused(["mc task: notes missing or longer than 4000"])
            return _mcp_call("mc_report_progress", {"taskId": task, "notes": notes})
        _only(payload, self.PATCH_KEYS, "mc task", operation)
        if not payload:
            raise AdapterRefused(["mc task: the update sets nothing"])
        patch: dict[str, Any] = {}
        for key in ("addLabels", "removeLabels"):
            if key not in payload:
                continue
            labels = payload[key]
            if not isinstance(labels, list) or not 1 <= len(labels) <= 10 or not all(
                    isinstance(x, str) and self._LABEL.fullmatch(x) for x in labels):
                raise AdapterRefused([f"mc task: {key} must be 1..10 plain labels"])
            if any(x.lower().startswith(("lane:", "kind:")) for x in labels):
                raise AdapterRefused(["mc task: lane:* and kind:* labels are MC governance"])
            patch[key] = list(dict.fromkeys(labels))
        if set(patch.get("addLabels", ())) & set(patch.get("removeLabels", ())):
            raise AdapterRefused(["mc task: a label is both added and removed"])
        if "title" in payload:
            title = _text(payload["title"], 200)
            if title is None:
                raise AdapterRefused(["mc task: title empty or longer than 200"])
            patch["title"] = title
        if "priority" in payload:
            if not isinstance(payload["priority"], str) or \
                    not self._PRIORITY.fullmatch(payload["priority"]):
                raise AdapterRefused(["mc task: priority malformed"])
            patch["priority"] = payload["priority"]
        self._inverse_patch(action, patch)      # an update that cannot be undone is refused
        return _mcp_call("mc_update_task", {"taskId": task, "patch": patch})

    def _inverse_patch(self, action: Mapping[str, Any], patch: Mapping[str, Any]
                       ) -> dict[str, Any]:
        keys = [k for k in ("title", "priority") if k in patch]
        if "addLabels" in patch or "removeLabels" in patch:
            keys.append("labels")
        prior = _prior(action, keys, "mc task")
        out: dict[str, Any] = {}
        if "labels" in prior:
            had = prior["labels"]
            if not isinstance(had, list) or not all(isinstance(x, str) for x in had):
                raise AdapterRefused(["mc task: precondition.prior.labels must be a list"])
            added = [x for x in patch.get("addLabels", ()) if x not in had]
            removed = [x for x in patch.get("removeLabels", ()) if x in had]
            if removed:
                out["addLabels"] = removed
            if added:
                out["removeLabels"] = added
        for key in ("title", "priority"):
            if key in prior and prior[key] != patch[key]:
                if not isinstance(prior[key], str) or not prior[key]:
                    raise AdapterRefused([f"mc task: precondition.prior.{key} is not a value"])
                out[key] = prior[key]
        if not out:
            raise AdapterRefused(["mc task: the update changes nothing the task holds"])
        return out

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        args = request["body"]["params"]["arguments"]
        if request["body"]["params"]["name"] != "mc_update_task":
            return None
        return _mcp_call("mc_update_task", {"taskId": args["taskId"],
                                            "patch": self._inverse_patch(action, args["patch"])})

    def undo_note(self) -> str:
        return ("A label/title/priority update is undone by the inverse mc_update_task; "
                "a note stays on the task as a comment (MC has no comment delete).")


# ------------------------------------------------------------------ Business Central

class BCAdapter(Adapter):
    """A BC API v2.0 entry. The document operations of ``write_intents.OPERATIONS`` (a new
    purchase or sales order, a purchase order line change) are built by
    ``write_intents.build_request``, the same request the approve-to-write desk previews.
    Lines on an existing document (``LINE_OPERATIONS``: a sales order line, a comment line
    on a sales or purchase order; BC API v2.0 has no comment entity, a comment is a line of
    ``lineType`` Comment) are posted to the document's contained lines and checked
    against the same $metadata pin. Undo: a created document or line is deleted with the
    etag BC answered (BC refuses once posted or shipped); a changed line is patched back
    to the values read before. The BC client here is GET-only and the environment is
    Production with no sandbox."""

    system, kind = "bc", "bc_write"
    documented = "Business Central API v2.0 (POST/PATCH companies({id})/<entity set>)"
    structural = ("BC is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS), and the "
                  "write scopes are not granted (PLX_WRITES_SCOPES_GRANTED unset)")
    needs = ("Vince's grant of BC API write scopes for the autopilot app "
             "(PLX_WRITES_SCOPES_GRANTED=bc): the BC_* Entra app registered in BC with a "
             "permission set that inserts/modifies Sales and Purchase documents",
             "a BC sandbox environment to verify against (PLX_BC_SANDBOX_ENVIRONMENT); "
             "Production only today",
             "BC opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=bc) and BC_TENANT_ID",
             "the company id the autopilot writes to (PLX_AUTOPILOT_BC_COMPANY_ID)")
    LINE_OPERATIONS: Mapping[str, Mapping[str, Any]] = {
        "sales_order_line_create": {
            "parent": "salesOrders", "lines": "salesOrderLines",
            "fields": ("lineType", "itemId", "lineObjectNumber", "description", "quantity",
                       "unitOfMeasureCode", "locationId", "shipmentDate"),
            "fixed": {}},
        "sales_order_comment": {"parent": "salesOrders", "lines": "salesOrderLines",
                                "fields": ("description",), "fixed": {"lineType": "Comment"}},
        "purchase_order_comment": {"parent": "purchaseOrders", "lines": "purchaseOrderLines",
                                   "fields": ("description",),
                                   "fixed": {"lineType": "Comment"}},
    }
    operations = {
        "purchase_order_create": "new purchase order; undo: DELETE with etag while open",
        "sales_order_create": "new sales order; undo: DELETE with etag while unshipped",
        "purchase_order_line_update": "PO line change; undo: PATCH back to the prior values",
        "sales_order_line_create": "item line on an open sales order; undo: DELETE the line",
        "sales_order_comment": "comment line on a sales order; undo: DELETE the line",
        "purchase_order_comment": "comment line on a purchase order; undo: DELETE the line",
    }

    def _company(self) -> str:
        company = str(self.environ.get("PLX_AUTOPILOT_BC_COMPANY_ID") or "")
        if not _GUID.fullmatch(company):
            raise AdapterRefused(["bc: company id not configured (PLX_AUTOPILOT_BC_COMPANY_ID)"])
        return company.lower()

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        from operating import write_intents as wi
        operation = self._operation(action)
        if operation in self.LINE_OPERATIONS:
            return self._line(action, operation)
        problems = wi._check_payload(operation, action.get("payload") or {})
        try:
            company = self._company()
        except AdapterRefused as error:
            problems += error.problems
        pre = action.get("precondition") or {}
        method = wi.OPERATIONS[operation]["method"]
        record = (action.get("target") or {}).get("record_id")
        if method == "PATCH":
            if not (isinstance(record, str) and _GUID.fullmatch(record)):
                problems.append("bc: record_id must be the GUID of the record it changes")
            if not isinstance(pre.get("etag"), str) or not pre["etag"]:
                problems.append("bc: precondition.etag (the record's etag as read) is required")
            try:
                _prior(action, list((action.get("payload") or {}).keys()), "bc")
            except AdapterRefused as error:
                problems += error.problems
        if problems:
            raise AdapterRefused(list(problems))
        intent = {"operation": operation, "environment": self.environ.get("BC_ENVIRONMENT")
                  or wi.PRODUCTION, "company_id": company, "payload": action["payload"],
                  "precondition": pre, "record_id": record}
        built = wi.build_request(intent, wi.default_metadata())
        if built.get("problems"):
            raise AdapterRefused([str(p) for p in built["problems"]])
        return {"method": built["method"], "url": built["path"], "headers": built["headers"],
                "body": built["body"], "body_json": built["body_json"]}

    def _line(self, action: Mapping[str, Any], operation: str) -> dict[str, Any]:
        from operating import write_intents as wi
        op = self.LINE_OPERATIONS[operation]
        payload = dict(action.get("payload") or {})
        _only(payload, op["fields"], "bc", operation)
        problems: list[str] = []
        document = str((action.get("target") or {}).get("document_id") or "")
        if not _GUID.fullmatch(document):
            problems.append("bc: target.document_id must be the GUID of the open document")
        if op["fixed"]:
            if _text(payload.get("description"), 100) is None:
                problems.append("bc: a comment line needs a description of 1..100 characters")
        else:
            if payload.get("lineType") != "Item":
                problems.append("bc: only Item lines are added to an order")
            if not (payload.get("itemId") or payload.get("lineObjectNumber")):
                problems.append("bc: the line names no item")
            qty = wi._decimal(payload.get("quantity"))
            if qty is None or qty <= 0:
                problems.append("bc: quantity must be above zero")
            if "description" in payload and _text(payload["description"], 100) is None:
                problems.append("bc: description must be 1..100 characters")
        try:
            company = self._company()
        except AdapterRefused as error:
            problems += error.problems
        if problems:
            raise AdapterRefused(problems)
        meta = wi.default_metadata()
        props = meta[0].properties(op["lines"]) if meta else {}
        body = {**op["fixed"], **payload}
        typed = {k: (wi._decimal(v) if (props.get(k) or {}).get("type") == "Edm.Decimal"
                     else v) for k, v in body.items()}
        found = wi.validate_request({"entity_set": op["lines"], "body": typed}, meta)
        if found:
            raise AdapterRefused([str(p) for p in found])
        root = wi.API_ROOT.replace("{environment}", self.environ.get("BC_ENVIRONMENT")
                                   or wi.PRODUCTION)
        return {"method": "POST",
                "url": f"{root}/companies({company})/{op['parent']}({document.lower()})/"
                       f"{op['lines']}",
                "headers": {"Content-Type": "application/json"},
                "body": wi._plain(typed), "body_json": wi._json(typed)}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request.get("method") == "POST":
            return {"method": "DELETE", "url": f"{request['url']}({{response:id}})",
                    "headers": {"If-Match": "{response:@odata.etag}"}, "body": None}
        if request.get("method") == "PATCH":
            from operating import write_intents as wi
            prior = _prior(action, list((action.get("payload") or {}).keys()), "bc")
            meta = wi.default_metadata()
            entity = request["url"].rsplit("/", 1)[1].split("(", 1)[0]
            props = meta[0].properties(entity) if meta else {}
            typed = {k: (wi._decimal(v) if (props.get(k) or {}).get("type") == "Edm.Decimal"
                         and wi._decimal(v) is not None else v) for k, v in prior.items()}
            # ``body_json`` is the exact wire text (decimals as numbers), kept in the journal
            return {"method": "PATCH", "url": request["url"],
                    "headers": {"Content-Type": "application/json",
                                "If-Match": "{response:@odata.etag}"},
                    "body": wi._plain(typed), "body_json": wi._json(typed)}
        return None

    def undo_note(self) -> str:
        return ("Delete the created document or line with its etag (BC refuses once it is "
                "posted, shipped or received); a changed line is patched back to its prior "
                "values.")


# ------------------------------------------------------------------ Sage 300

class SageAdapter(Adapter):
    """Sage 300 has no write path in its integration: Sage reaches PLX one way, by
    ``sage_staging_sync.py`` copying Sage tables into the Portal's staging database every
    10 minutes, read here only as the ``sage.*`` relations of the read-only Portal
    reporting API. No Sage API host, company or user is
    known to this repo, and every Sage entry an autopilot prepares (invoice entry,
    shipment posting, cash application) moves money. Every action is refused; nothing is
    recorded as a request because there is no evidenced API to address."""

    system, kind = "sage", "sage_write"
    documented = "none: Sage is read-only through the Portal reporting replica"
    operations: Mapping[str, str] = {}
    structural = ("no Sage write path in its integration (one-way Sage -> staging DB sync, "
                  "read through the Portal reporting API)")
    needs = ("evidence of a Sage write API at PLX (Sage 300 Web API host, company "
             "database id) and a write-capable Sage user, outside git",
             "a Sage test company to verify against",
             "a reviewed Sage write transport")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        raise AdapterRefused(["sage: no write path exists in the Sage integration"])


# ------------------------------------------------------------------ Portal

class PortalAdapter(Adapter):
    """A Customer Portal app-route write. The reporting API (``PLX_WORKBENCH_API_KEY``) is
    read-only; the Portal's own app routes do write behind a signed-in Portal session
    (``requireRole("STAFF")``), and the action catalog declares one of them as a real
    capability (``portal.sales_order.set_customer_po``):
    ``PATCH /api/mrp/sales-orders/{id} {"poReference": ...}``, editable until the order's
    first shipment or cancel (else 409 ``ORDER_NOT_EDITABLE``). It is written only when the
    order holds no PO reference as read (``precondition.prior.poReference`` empty: never an
    overwrite, the catalog's ``target_has_no_po``). Undo: PATCH it back to the value read
    (empty), while the order is still editable."""

    system, kind = "portal", "portal_write"
    documented = "Customer Portal app API PATCH /api/mrp/sales-orders/{id} (STAFF session)"
    operations = {"sales_order_set_customer_po":
                  "set an empty customer PO reference; undo: PATCH back to empty"}
    structural = ("the Portal key this repo holds is the read-only reporting API key; no "
                  "Portal session or service identity for the app routes")
    needs = ("a Portal service identity with the STAFF role scoped to PATCH "
             "/api/mrp/sales-orders/{id}, distinct from any person and from the MC key, "
             "its credential outside git (none exists upstream)",
             "Portal UAT (staging.plxcustomer.io) to verify against",
             "a reviewed Portal write transport (the action path's portal_http adapter)")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        self._operation(action)
        order = str((action.get("target") or {}).get("order_id") or "")
        if not _SLUG.fullmatch(order):
            raise AdapterRefused(["portal: target.order_id missing or malformed"])
        payload = action.get("payload") or {}
        _only(payload, ("poReference",), "portal", "sales_order_set_customer_po")
        po = _text(payload.get("poReference"), 100)
        if po is None:
            raise AdapterRefused(["portal: poReference empty or longer than 100"])
        prior = _prior(action, ["poReference"], "portal")["poReference"]
        if prior not in (None, ""):
            raise AdapterRefused(["portal: the order already holds a PO reference (never "
                                  "overwritten)"])
        return {"method": "PATCH", "url": f"/api/mrp/sales-orders/{order}",
                "headers": {"Content-Type": "application/json"}, "body": {"poReference": po}}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        prior = _prior(action, ["poReference"], "portal")["poReference"]
        return {"method": "PATCH", "url": request["url"],
                "headers": {"Content-Type": "application/json"},
                "body": {"poReference": prior}}

    def undo_note(self) -> str:
        return ("PATCH the PO reference back to empty while the order is editable (before "
                "its first shipment or cancel); the route's acceptance of an empty value is "
                "unverified.")


# ------------------------------------------------------------------ FileMaker

class FileMakerAdapter(Adapter):
    """A FileMaker record through the FileMaker Data API vLatest:

    * ``record_create``: ``POST .../layouts/{layout}/records {"fieldData": {...}}``; undo
      ``DELETE .../records/{recordId}`` (the id FileMaker answered).
    * ``record_edit``: ``PATCH .../records/{recordId} {"fieldData": {...}, "modId": N}``
      (the modification id as read: FileMaker refuses it when the record changed since);
      undo ``PATCH`` the prior values with the modId FileMaker answered.

    Only the layout's own fields (no related ``table::field``, no portal rows). FileMaker
    reaches this repo only as read-only ``public.*`` relations of the Portal reporting
    API."""

    system, kind = "filemaker", "filemaker_write"
    documented = ("FileMaker Data API vLatest (POST|PATCH /fmi/data/vLatest/databases/{db}/"
                  "layouts/{layout}/records[/{recordId}])")
    operations = {"record_create": "new record; undo: DELETE the record",
                  "record_edit": "edit fields with modId; undo: PATCH back to prior values"}
    structural = "no FileMaker Server host or Data API account in this repo"
    needs = ("the FileMaker Server host, database and a Data API account with the fmrest "
             "extended privilege on the target layouts (environment file, outside git); "
             "the session token comes from POST .../sessions with that account",
             "a FileMaker test copy of the database to verify against",
             "a reviewed FileMaker write transport")
    _NAME = re.compile(r"^[A-Za-z0-9 _-]{1,60}$")
    _FIELD = re.compile(r"^[A-Za-z0-9 _.#-]{1,100}$")

    def _base(self, action: Mapping[str, Any]) -> str:
        target = action.get("target") or {}
        db, layout = str(target.get("database") or ""), str(target.get("layout") or "")
        if not (self._NAME.fullmatch(db) and self._NAME.fullmatch(layout)):
            raise AdapterRefused(["filemaker: database or layout missing"])
        return f"/fmi/data/vLatest/databases/{quote(db)}/layouts/{quote(layout)}/records"

    def _fields(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        bad = [k for k in payload if not isinstance(k, str) or not self._FIELD.fullmatch(k)]
        if not payload or bad:
            raise AdapterRefused(["filemaker: fieldData empty or a field is not one of the "
                                  "layout's own (no table::field, no portal rows)"])
        if any(isinstance(v, (Mapping, list)) for v in payload.values()):
            raise AdapterRefused(["filemaker: a field value must be text or a number"])
        return {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in payload.items()}

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = self._operation(action)
        base, fields = self._base(action), self._fields(action.get("payload") or {})
        if operation == "record_create":
            return {"method": "POST", "url": base,
                    "headers": {"Content-Type": "application/json"},
                    "body": {"fieldData": fields}}
        rid = str((action.get("target") or {}).get("record_id") or "")
        mod = str((action.get("precondition") or {}).get("mod_id") or "")
        if not _DIGITS.fullmatch(rid) or not _DIGITS.fullmatch(mod):
            raise AdapterRefused(["filemaker: record_id and precondition.mod_id (as read) "
                                  "are required"])
        _prior(action, list(fields), "filemaker")
        return {"method": "PATCH", "url": f"{base}/{rid}",
                "headers": {"Content-Type": "application/json"},
                "body": {"fieldData": fields, "modId": mod}}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request.get("method") == "POST":
            return {"method": "DELETE", "url": f"{request['url']}/{{response:response/recordId}}",
                    "body": None}
        prior = _prior(action, list(request["body"]["fieldData"]), "filemaker")
        return {"method": "PATCH", "url": request["url"],
                "headers": {"Content-Type": "application/json"},
                "body": {"fieldData": prior, "modId": "{response:response/modId}"}}

    def undo_note(self) -> str:
        return "Delete the created record, or patch the edited fields back to their prior values."


# ------------------------------------------------------------------ sends

class MailAdapter(Adapter):
    """Send the prepared email as the item's owner from their own mailbox, through the one
    approved send path (``integrations.graph_send.ActionSender``), which sends only with
    ``PLX_ACTIONS_LIVE=1``, the owner listed in ``PLX_ACTIONS_SENDERS`` and a token holding
    ``Mail.Send``; otherwise it answers a dry run naming what is missing. Mail has no undo."""

    system, kind = "graph", "send_mail"
    documented = "Microsoft Graph v1.0 POST /users/{sender}/sendMail"
    operations = {"send_email": "send as the owner; no undo (mail cannot be recalled)"}
    needs = ("the owner's OK to send as them (PLX_ACTIONS_SENDERS lists their mailbox)",
             "PLX_ACTIONS_LIVE=1 and the send app holding Mail.Send (graph_send probe)")

    def __init__(self, sender: Any = None, *, environ: Mapping[str, str] | None = None) -> None:
        super().__init__(None, environ=environ)
        self.sender = sender

    def _sender(self) -> Any:
        if self.sender is None:
            from integrations.graph_send import ActionSender
            self.sender = ActionSender(self.environ)
        return self.sender

    def unavailable(self) -> str | None:
        return None

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        from integrations.graph_send import request_for
        payload = action.get("payload") or {}
        sender = str((action.get("target") or {}).get("sender") or "")
        if "@" not in sender or not payload.get("to") or not payload.get("body"):
            raise AdapterRefused(["mail: sender, recipients or body missing"])
        built = request_for("send_email", sender, payload)
        return {**built, "sender": sender, "payload": dict(payload)}

    def perform(self, request: Mapping[str, Any], key: str) -> dict[str, Any]:
        out = self._sender().dispatch("send_email", request["sender"], request["payload"])
        status = {"sent": "done", "dry_run": "not_performed"}.get(out.get("status"),
                                                                  out.get("status"))
        return {"status": status if status in ("done", "failed", "unknown", "not_performed")
                else "unknown", "http_status": out.get("http_status"),
                "missing": list(out.get("missing") or ())}

    def undo_note(self) -> str:
        return "A sent email cannot be recalled."


class TeamsAdapter(Adapter):
    """Post the prepared Teams message as the owner: a chat message (``POST /chats/{id}/
    messages``) or a channel message (``POST /teams/{team}/channels/{channel}/messages``).
    Graph takes both only on a delegated token of the person (app-only posting exists only
    for migration, ``Teamwork.Migrate.All``, never used). Undo: the sender soft-deletes
    the message (``.../messages/{id}/softDelete``, delegated)."""

    system, kind = "graph", "send_teams"
    documented = ("Microsoft Graph v1.0 POST /chats/{chat-id}/messages | POST /teams/{team-id}/"
                  "channels/{channel-id}/messages (delegated only)")
    operations = {"chat_message": "post to a chat; undo: softDelete as the sender",
                  "channel_message": "post to a channel; undo: softDelete as the sender"}
    structural = ("Graph refuses app-only chat and channel posts; no delegated "
                  "ChatMessage.Send/ChannelMessage.Send token here")
    needs = ("a delegated token of the owner with ChatMessage.Send (chats) and "
             "ChannelMessage.Send (channels), plus Chat.ReadWrite and ChannelMessage.ReadWrite "
             "for the softDelete undo: OAuth consent on the AZURE_* app and a refresh token "
             "held outside git",
             "a reviewed Graph write transport for these shapes (live_source_guard.send_action "
             "takes only the chat post today)")
    _CHANNEL = re.compile(r"^19:[A-Za-z0-9_.@-]{1,200}$")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = self._operation(action)
        payload = action.get("payload") or {}
        body = _text(payload.get("body"), 4000)
        if body is None:
            raise AdapterRefused(["teams: body missing or longer than 4000"])
        if "@" not in str((action.get("target") or {}).get("user") or ""):
            raise AdapterRefused(["teams: the posting owner is not known"])
        if operation == "chat_message":
            from integrations.graph_send import request_for
            chat = str(payload.get("chat_id") or "")
            if not _GRAPH_ID.fullmatch(chat):
                raise AdapterRefused(["teams: chat missing or malformed"])
            return request_for("teams_message", "me@unknown", {"chat_id": chat, "body": body})
        team, channel = str(payload.get("team_id") or ""), str(payload.get("channel_id") or "")
        if not _GUID.fullmatch(team) or not self._CHANNEL.fullmatch(channel):
            raise AdapterRefused(["teams: team or channel missing or malformed"])
        return {"method": "POST",
                "url": f"{GRAPH}/teams/{team}/channels/{quote(channel, safe=':@')}/messages",
                "body": {"body": {"contentType": "text", "content": body}}}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if "/teams/" in request["url"]:
            return {"method": "POST", "url": f"{request['url']}/{{response:id}}/softDelete",
                    "body": None}
        user = quote(str((action.get("target") or {}).get("user")).lower(), safe="@+'")
        chat = request["url"].rsplit("/chats/", 1)[1].rsplit("/messages", 1)[0]
        return {"method": "POST", "url": f"{GRAPH}/users/{user}/chats/{chat}/messages/"
                "{response:id}/softDelete", "body": None}

    def undo_note(self) -> str:
        return "The sender soft-deletes the message (Teams shows it was deleted)."


# ------------------------------------------------------------------ Planner, calendar

class PlannerAdapter(Adapter):
    """A Microsoft Planner task through Graph: ``task_create`` (``POST /planner/tasks``,
    assignees by Entra object id) and ``task_update`` (``PATCH /planner/tasks/{id}``
    with ``If-Match`` the etag as read and ``Prefer: return=representation`` so the new
    etag comes back). Undo: delete the created task, or patch the prior values back, each
    with the etag Graph answered. Reassigning a task is never an update here."""

    system, kind = "planner", "planner_write"
    documented = "Microsoft Graph v1.0 POST /planner/tasks | PATCH /planner/tasks/{id}"
    operations = {"task_create": "new Planner task; undo: DELETE with etag",
                  "task_update": "title/due/progress/bucket; undo: PATCH back with etag"}
    structural = "Planner is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS)"
    needs = ("the approved-action app's application permission Tasks.ReadWrite.All (held, "
             "verified 11-04) used for Planner writes: Vince's OK covers it",
             "Planner opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=planner)")
    UPDATE_FIELDS = ("title", "dueDateTime", "percentComplete", "bucketId")

    def _check(self, payload: Mapping[str, Any]) -> None:
        if "title" in payload and _text(payload["title"], 255) is None:
            raise AdapterRefused(["planner: title empty or longer than 255"])
        if "dueDateTime" in payload and payload["dueDateTime"] is not None \
                and not _instant_text(payload["dueDateTime"]):
            raise AdapterRefused(["planner: dueDateTime is not an instant"])
        if "percentComplete" in payload and payload["percentComplete"] not in (0, 50, 100):
            raise AdapterRefused(["planner: percentComplete is 0, 50 or 100"])
        for key in ("planId", "bucketId"):
            if key in payload and not (isinstance(payload[key], str)
                                       and _GRAPH_ID.fullmatch(payload[key])):
                raise AdapterRefused([f"planner: {key} malformed"])

    def people(self, action: Mapping[str, Any]) -> list[str]:
        found = (action.get("payload") or {}).get("assignees", [])
        return [str(a) for a in found] if isinstance(found, list) else ["(malformed)"]

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = self._operation(action)
        payload = dict(action.get("payload") or {})
        if operation == "task_create":
            _only(payload, ("planId", "bucketId", "title", "dueDateTime", "assignees"),
                  "planner", operation)
            if "planId" not in payload or "title" not in payload:
                raise AdapterRefused(["planner: planId and title are required"])
            self._check(payload)
            assignees = payload.pop("assignees", [])
            if not isinstance(assignees, list) or len(assignees) > 5 or not all(
                    isinstance(a, str) and _GUID.fullmatch(a) for a in assignees):
                raise AdapterRefused(["planner: assignees are 0..5 Entra object ids"])
            if assignees:
                payload["assignments"] = {a.lower(): {
                    "@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}
                    for a in assignees}
            return {"method": "POST", "url": f"{GRAPH}/planner/tasks",
                    "headers": {"Content-Type": "application/json"}, "body": payload}
        _only(payload, self.UPDATE_FIELDS, "planner", operation)
        if not payload:
            raise AdapterRefused(["planner: the update sets nothing"])
        self._check(payload)
        task = str((action.get("target") or {}).get("task_id") or "")
        etag = (action.get("precondition") or {}).get("etag")
        if not _GRAPH_ID.fullmatch(task) or not isinstance(etag, str) or not etag \
                or "\n" in etag or "\r" in etag:
            raise AdapterRefused(["planner: task_id and precondition.etag (as read) are "
                                  "required"])
        _prior(action, list(payload), "planner")
        return {"method": "PATCH", "url": f"{GRAPH}/planner/tasks/{task}",
                "headers": {"Content-Type": "application/json", "If-Match": etag,
                            "Prefer": "return=representation"}, "body": payload}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request["method"] == "POST":
            return {"method": "DELETE", "url": f"{request['url']}/{{response:id}}",
                    "headers": {"If-Match": "{response:@odata.etag}"}, "body": None}
        return {"method": "PATCH", "url": request["url"],
                "headers": {"Content-Type": "application/json",
                            "If-Match": "{response:@odata.etag}",
                            "Prefer": "return=representation"},
                "body": _prior(action, list(request["body"]), "planner")}

    def undo_note(self) -> str:
        return "Delete the created task, or patch the changed fields back (etag checked)."


class CalendarAdapter(Adapter):
    """An event in the owner's own calendar through Graph: ``event_create`` (``POST
    /users/{owner}/events``; attendees get Outlook's invitation) and ``event_update``
    (``PATCH /users/{owner}/events/{id}``; attendees get the update). Undo: delete the
    created event (attendees get a cancellation) or patch the prior values back. The
    mailbox is always the item's owner (``autopilot_write.actions_of``)."""

    system, kind = "calendar", "calendar_write"
    documented = "Microsoft Graph v1.0 POST /users/{owner}/events | PATCH /users/{owner}/events/{id}"
    operations = {"event_create": "new event in the owner's calendar; undo: DELETE",
                  "event_update": "subject/time/location/body; undo: PATCH back"}
    structural = ("the calendar is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS), "
                  "and the approved-action app holds Calendars.Read only")
    needs = ("Calendars.ReadWrite (application) admin-consented on the AZURE_* app (it holds "
             "Calendars.Read today), scoped by an Exchange ApplicationAccessPolicy to the "
             "owners' mailboxes",
             "the calendar opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=calendar)")
    FIELDS = ("subject", "body", "start", "end", "location", "attendees")

    def _graph(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "subject" in payload:
            if _text(payload["subject"], 255) is None:
                raise AdapterRefused(["calendar: subject empty or longer than 255"])
            out["subject"] = payload["subject"].strip()
        if "body" in payload:
            if not isinstance(payload["body"], str) or len(payload["body"]) > 8000:
                raise AdapterRefused(["calendar: body must be text up to 8000"])
            out["body"] = {"contentType": "text", "content": payload["body"]}
        for key in ("start", "end"):
            if key in payload:
                when = payload[key]
                if not isinstance(when, Mapping) or not _instant_text(when.get("dateTime")) \
                        or not _text(when.get("timeZone"), 64):
                    raise AdapterRefused([f"calendar: {key} needs dateTime and timeZone"])
                out[key] = {"dateTime": when["dateTime"], "timeZone": when["timeZone"]}
        if "location" in payload:
            if _text(payload["location"], 255) is None:
                raise AdapterRefused(["calendar: location empty or longer than 255"])
            out["location"] = {"displayName": payload["location"].strip()}
        if "attendees" in payload:
            found = payload["attendees"]
            if not isinstance(found, list) or len(found) > 10 or not all(
                    isinstance(a, str) and re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,253}", a)
                    for a in found):
                raise AdapterRefused(["calendar: attendees are 0..10 addresses"])
            out["attendees"] = [{"emailAddress": {"address": a.lower()}, "type": "required"}
                                for a in found]
        return out

    def people(self, action: Mapping[str, Any]) -> list[str]:
        found = (action.get("payload") or {}).get("attendees", [])
        return [str(a) for a in found] if isinstance(found, list) else ["(malformed)"]

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = self._operation(action)
        payload = action.get("payload") or {}
        owner = str((action.get("target") or {}).get("user") or "").lower()
        if "@" not in owner:
            raise AdapterRefused(["calendar: the owner's mailbox is not known"])
        user = quote(owner, safe="@+'")
        if operation == "event_create":
            _only(payload, self.FIELDS, "calendar", operation)
            if not all(k in payload for k in ("subject", "start", "end")):
                raise AdapterRefused(["calendar: subject, start and end are required"])
            return {"method": "POST", "url": f"{GRAPH}/users/{user}/events",
                    "headers": {"Content-Type": "application/json"},
                    "body": self._graph(payload)}
        _only(payload, ("subject", "body", "start", "end", "location"), "calendar", operation)
        if not payload:
            raise AdapterRefused(["calendar: the update sets nothing"])
        event = str((action.get("target") or {}).get("event_id") or "")
        if not _GRAPH_ID.fullmatch(event):
            raise AdapterRefused(["calendar: event_id missing or malformed"])
        self._graph(_prior(action, list(payload), "calendar"))    # the undo must be valid too
        return {"method": "PATCH", "url": f"{GRAPH}/users/{user}/events/{quote(event, safe='=-_')}",
                "headers": {"Content-Type": "application/json"}, "body": self._graph(payload)}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request["method"] == "POST":
            return {"method": "DELETE", "url": f"{request['url']}/{{response:id}}", "body": None}
        payload = action.get("payload") or {}
        return {"method": "PATCH", "url": request["url"],
                "headers": {"Content-Type": "application/json"},
                "body": self._graph(_prior(action, list(payload), "calendar"))}

    def undo_note(self) -> str:
        return ("Delete the created event (attendees get a cancellation) or patch the changed "
                "fields back (attendees get the update).")


# ------------------------------------------------------------------ SharePoint

class SharePointAdapter(Adapter):
    """A SharePoint list item or a new file through Microsoft Graph:

    * ``list_item_create``: ``POST /sites/{site}/lists/{list}/items {"fields": {...}}``;
      undo ``DELETE .../items/{id}`` (the id Graph answered).
    * ``list_item_update``: ``PATCH /sites/{site}/lists/{list}/items/{id}/fields`` with
      ``If-Match`` the item's etag as read; undo ``PATCH`` the prior values back with the
      etag Graph answered.
    * ``file_upload``: a new file in a folder, ``PUT /drives/{drive}/items/{parent}:/{name}:
      /content?@microsoft.graph.conflictBehavior=fail`` (never replaces a file: an existing
      name is a 409); undo ``DELETE /drives/{drive}/items/{id}`` with ``If-Match`` the eTag
      Graph answered (refused once someone changed the file). Up to 4 MiB (Graph's simple
      upload); the bytes travel base64 in the payload and the journal keeps their digest.

    Only a list's own columns (no system fields such as ``Author``, ``Editor``,
    ``ContentType``, ``Attachments`` or ``_``-prefixed ones), text, number or yes/no values."""

    system, kind = "sharepoint", "sharepoint_write"
    documented = ("Microsoft Graph v1.0 POST /sites/{site}/lists/{list}/items | PATCH "
                  ".../items/{id}/fields | PUT /drives/{drive}/items/{parent}:/{name}:/content")
    operations = {"list_item_create": "new list item; undo: DELETE the item",
                  "list_item_update": "edit list columns with etag; undo: PATCH back",
                  "file_upload": "new file, never a replace; undo: DELETE with eTag"}
    structural = ("SharePoint is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS), "
                  "and no app holds a SharePoint write grant")
    needs = ("Sites.Selected with the write role granted per target site (or "
             "Sites.ReadWrite.All) on the AZURE_* app, admin-consented: Vince's OK",
             "SharePoint opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=sharepoint)")
    MAX_BYTES = 4 * 1024 * 1024
    _COLUMN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    _SYSTEM_COLUMNS = frozenset(c.lower() for c in (
        "Author", "Editor", "Created", "Modified", "ContentType", "ContentTypeId",
        "Attachments", "FileLeafRef", "FileRef", "FSObjType", "ID", "GUID", "UniqueId",
        "Edit", "LinkTitle", "LinkTitleNoMenu", "DocIcon", "ItemChildCount",
        "FolderChildCount", "AppAuthor", "AppEditor", "ComplianceAssetId", "id"))
    # the names the write guard lets through (``live_source_guard._W_NAME``: letters,
    # digits, ``. _ ( ) -`` and spaces), so a name it would refuse is refused at the draft,
    # before anyone approves the upload (``file_name_refused``)
    _FILE_NAME = re.compile(r"^[A-Za-z0-9._()-](?:[A-Za-z0-9._() -]{0,198}[A-Za-z0-9._()-])?$")
    # a person or lookup column (``<Name>LookupId``) names or assigns someone the people
    # exclusion cannot clear from a number: refused, whatever the column
    _LOOKUP = re.compile(r"lookupids?$", re.IGNORECASE)

    @classmethod
    def file_name_refused(cls, name: Any) -> str | None:
        """Why ``name`` cannot be uploaded (None: it can): ask before the approval."""
        if not isinstance(name, str) or not cls._FILE_NAME.fullmatch(name) \
                or name.endswith(".") or name in (".", ".."):
            return ("sharepoint: the file name may hold only letters, digits, spaces and "
                    ". _ ( ) - (no apostrophe, &, comma or accented letter)")
        return None

    # a site id is ``{hostname},{site-collection guid},{web guid}`` (or the guids alone)
    _SITE = re.compile(r"^(?:[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5},)?"
                       r"[0-9a-fA-F-]{36},[0-9a-fA-F-]{36}$")

    def _list_base(self, action: Mapping[str, Any]) -> str:
        target = action.get("target") or {}
        site, lst = str(target.get("site_id") or ""), str(target.get("list_id") or "")
        if not self._SITE.fullmatch(site) or not _SLUG.fullmatch(lst) and not _GUID.fullmatch(lst):
            raise AdapterRefused(["sharepoint: site_id or list_id missing or malformed"])
        return f"{GRAPH}/sites/{quote(site, safe=',.-_')}/lists/{quote(lst, safe='-_')}/items"

    def _columns(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not payload:
            raise AdapterRefused(["sharepoint: the item sets no column"])
        bad = [k for k in payload if not isinstance(k, str) or not self._COLUMN.fullmatch(k)
               or k.lower() in self._SYSTEM_COLUMNS]
        if bad:
            raise AdapterRefused(["sharepoint: only the list's own columns (no system "
                                  "field): " + ", ".join(str(b)[:40] for b in bad[:5])])
        people = [k for k in payload if self._LOOKUP.search(k)]
        if people:
            raise AdapterRefused(["sharepoint: a person or lookup column is not written "
                                  "(the people exclusion cannot clear it): "
                                  + ", ".join(people[:5])])
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if isinstance(v, Decimal):
                v = format(v, "f")
            if v is not None and not isinstance(v, (str, int, bool)):
                raise AdapterRefused(["sharepoint: a column value is text, a number or yes/no"])
            if isinstance(v, str) and len(v) > 4000:
                raise AdapterRefused(["sharepoint: a column value is longer than 4000"])
            out[k] = v
        return out

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = self._operation(action)
        payload = action.get("payload") or {}
        if operation == "file_upload":
            return self._upload(action, payload)
        base = self._list_base(action)
        fields = self._columns(payload)
        if operation == "list_item_create":
            return {"method": "POST", "url": base,
                    "headers": {"Content-Type": "application/json"},
                    "body": {"fields": fields}}
        target = action.get("target") or {}
        item = str(target.get("item_id") or "")
        etag = (action.get("precondition") or {}).get("etag")
        if not _DIGITS.fullmatch(item) or not isinstance(etag, str) or not etag \
                or "\n" in etag or "\r" in etag:
            raise AdapterRefused(["sharepoint: item_id and precondition.etag (as read) are "
                                  "required"])
        self._columns(_prior(action, list(fields), "sharepoint"))   # the undo is valid too
        return {"method": "PATCH", "url": f"{base}/{item}/fields",
                "headers": {"Content-Type": "application/json", "If-Match": etag},
                "body": fields}

    def _upload(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        import base64
        import binascii
        import hashlib
        _only(payload, ("name", "content_b64", "content_type"), "sharepoint", "file_upload")
        target = action.get("target") or {}
        drive, parent = str(target.get("drive_id") or ""), str(target.get("parent_id") or "")
        if not _GRAPH_ID.fullmatch(drive) or not _GRAPH_ID.fullmatch(parent):
            raise AdapterRefused(["sharepoint: drive_id or parent_id missing or malformed"])
        name = payload.get("name")
        refused = self.file_name_refused(name)
        if refused:
            raise AdapterRefused([refused])
        raw = payload.get("content_b64")
        try:
            data = base64.b64decode(raw, validate=True) if isinstance(raw, str) else None
        except (binascii.Error, ValueError):
            data = None
        if not data or len(data) > self.MAX_BYTES:
            raise AdapterRefused(["sharepoint: content missing, not base64 or over 4 MiB"])
        kind = payload.get("content_type") or "application/octet-stream"
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z]+/[A-Za-z0-9.+-]{1,100}", kind):
            raise AdapterRefused(["sharepoint: content_type malformed"])
        return {"method": "PUT",
                "url": f"{GRAPH}/drives/{quote(drive, safe='!-_')}/items/"
                       f"{quote(parent, safe='!-_')}:/{quote(str(name), safe='()')}:/content"
                       "?@microsoft.graph.conflictBehavior=fail",
                "headers": {"Content-Type": kind}, "body_b64": raw,
                "body_sha256": hashlib.sha256(data).hexdigest(), "body_bytes": len(data)}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request["method"] == "PUT":
            drive = request["url"].split("/drives/", 1)[1].split("/", 1)[0]
            return {"method": "DELETE", "url": f"{GRAPH}/drives/{drive}/items/{{response:id}}",
                    "headers": {"If-Match": "{response:eTag}"}, "body": None}
        if request["method"] == "POST":
            return {"method": "DELETE", "url": f"{request['url']}/{{response:id}}", "body": None}
        return {"method": "PATCH", "url": request["url"],
                "headers": {"Content-Type": "application/json",
                            "If-Match": "{response:@odata.etag}"},
                "body": self._columns(_prior(action, list(request["body"]), "sharepoint"))}

    def undo_note(self) -> str:
        return ("Delete the created item or uploaded file (refused once someone changed the "
                "file), or patch the changed columns back.")


ADAPTER_TYPES = (MCApprovalAdapter, MCTaskAdapter, BCAdapter, SageAdapter, PortalAdapter,
                 FileMakerAdapter, MailAdapter, TeamsAdapter, PlannerAdapter, CalendarAdapter,
                 SharePointAdapter)


def default_adapters(environ: Mapping[str, str] | None = None) -> dict[str, Adapter]:
    """The adapters product code uses. A system the owner opened to approved writes
    (``live_source_guard.WRITES_FLAG``: Planner, calendar, SharePoint, BC) gets its guarded
    write transport (``integrations.write_transports``); every other one but mail has none."""
    from integrations import write_transports as wt
    env = dict(os.environ if environ is None else environ)
    out: dict[str, Adapter] = {}
    for cls in ADAPTER_TYPES:
        transport = wt.for_system(cls.system, env) if cls.system in (
            "planner", "calendar", "sharepoint", "bc") else None
        out[cls.kind] = cls(environ=env) if cls is MailAdapter else cls(transport, environ=env)
    return out
