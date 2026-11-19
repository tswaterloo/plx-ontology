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
``Mail.Send``). Planner, calendar, SharePoint, BC and Portal get their guarded write transport
(``integrations.write_transports``, through ``live_source_guard.write_action``'s exact
shapes) only once the owner opened that system (``PLX_WRITES_LIVE_SYSTEMS``); the
transport's ``missing()`` makes the adapter ``unavailable`` while anything else is lacking.
MC (but with ``live-mc``), Teams and FileMaker have no transport (``structural``); ``needs`` names
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
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

Transport = Callable[[Mapping[str, Any], str], tuple[int, Mapping[str, Any]]]

GRAPH = "https://graph.microsoft.com/v1.0"
MC_BASE = "https://mc.plxcustomer.io"
_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,64}$")
_GATE_ID = re.compile(r"^[A-Za-z0-9_:.-]{1,120}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_GRAPH_ID = re.compile(r"^[A-Za-z0-9_=!.:@-]{1,400}$")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMAILISH = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
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
        # the fields it does set, so the caller can name the system's own field
        raise AdapterRefused([f"{system}: {op} cannot set {', '.join(sorted(extra)[:5])} "
                              f"(it sets: {', '.join(allowed)})"])


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
    # every key of ``target`` the request is built from: any other key would show on the
    # card and be ignored by the write (a company or file named one way, written another),
    # so ``_operation`` refuses it
    TARGET_KEYS: frozenset[str] = frozenset()

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
        target = action.get("target")
        if target is not None and not isinstance(target, Mapping):
            raise AdapterRefused([f"{self.system}: the target must be an object"])
        unused = sorted(str(k)[:40] for k in target or {} if k not in self.TARGET_KEYS)
        if unused:
            raise AdapterRefused([f"{self.system}: the target names {', '.join(unused[:5])}, "
                                  "which this write does not use (only "
                                  f"{', '.join(sorted(self.TARGET_KEYS)) or 'nothing'}): "
                                  "the card would show what is not written"])
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
    rules' ``scope_kind_in`` (launch only) and ``risk_in`` (the requester's stated risk),
    never this adapter."""

    system, kind = "mc", "mc_approval"
    documented = "POST https://mc.plxcustomer.io/api/approvals/decide"
    operations = {"approval_decide": "approve a routine gate; no undo (no revoke in MC)"}
    TARGET_KEYS = frozenset({"task_id", "gate_id"})
    structural = ("MC denies approval.decide to services; the decide route takes only a "
                  "human admin/owner Entra session")
    needs = ("a delegated Entra session token of the owner (MC.Admin/MC.Owner) for the "
             "decide route, issued and refreshed outside git",
             "a reviewed POST shape for /api/approvals/decide in the HTTP egress allowlist",
             "each gate's own MC scope (kind launch) and proposal risk on MCApproval "
             "(captured as gate_scope since AP-02 11-14; projected once the MCApproval "
             "schema change gate_scope/proposal_action/proposal_risk applies in place)")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        self._operation(action)
        # the one decision this route sends is an approval: a card saying anything else
        # would show one decision and send another
        if action.get("recommendation") != "approve":
            raise AdapterRefused(["mc approval: only an approval is sent (the decision "
                                  "must be approve)"])
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
    TARGET_KEYS = frozenset({"task_id", "acting_for"})
    structural = ("MC writes are not opened (live-mc); mc_update_task is a never-sent "
                  "tool in mc_tool_policy.json")
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
            call = _mcp_call("mc_report_progress", {"taskId": task, "notes": notes})
            person = str((action.get("target") or {}).get("acting_for") or "").lower()
            if person:
                call["acting_for"] = person      # whose opt-in authorizes the call
            return call
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
    to the values read before. The environment is the sandbox when one is named, else
    Production (owner 11-11: the real company), which the executor opens only with
    ``live-bc`` and ``live-bc-production`` and writes only on a named person's confirmed
    approval (``autopilot_write.BC_PRODUCTION_FLAG``)."""

    system, kind = "bc", "bc_write"
    documented = "Business Central API v2.0 (POST/PATCH companies({id})/<entity set>)"
    structural = ("BC is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS), and the "
                  "write scopes are not granted (PLX_WRITES_SCOPES_GRANTED unset)")
    needs = ("Vince's grant of BC API write scopes for the autopilot app "
             "(PLX_WRITES_SCOPES_GRANTED=bc): the BC_* Entra app registered in BC with a "
             "permission set that inserts/modifies Sales and Purchase documents",
             "the owner's live-bc and live-bc-production files for Production (a "
             "sandbox, PLX_BC_SANDBOX_ENVIRONMENT, needs live-bc only)",
             "BC opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=bc) and BC_TENANT_ID",
             "the company ids the autopilot writes to (PLX_AUTOPILOT_BC_COMPANY_ID, the "
             "default, plus PLX_AUTOPILOT_BC_COMPANY_IDS)")
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
        "purchase_order_line_create": {
            "parent": "purchaseOrders", "lines": "purchaseOrderLines",
            "fields": ("lineType", "itemId", "lineObjectNumber", "description", "quantity",
                       "unitOfMeasureCode", "locationId", "expectedReceiptDate"),
            "fixed": {}},
    }
    operations = {
        "purchase_order_create": "new purchase order; undo: DELETE with etag while open",
        "sales_order_create": "new sales order; undo: DELETE with etag while unshipped",
        "purchase_order_line_update": "PO line change; undo: PATCH back to the prior values",
        "sales_order_line_create": "item line on an open sales order; undo: DELETE the line",
        "sales_order_comment": "comment line on a sales order; undo: DELETE the line",
        "purchase_order_comment": "comment line on a purchase order; undo: DELETE the line",
        "purchase_order_line_create": "item line on an open purchase order (no cost: BC "
                                      "prices it); undo: DELETE the line",
        "sales_order_update": "sales order header dates/ship-to/reference; undo: PATCH back",
        "purchase_order_update": "purchase order header dates/ship-to; undo: PATCH back",
        "sales_order_line_update": "open sales line quantity/date/description; undo: PATCH back",
        "sales_order_delete": "delete an open sales order nothing of which shipped; no undo",
        "purchase_order_delete": "delete an open purchase order nothing of which was "
                                 "received; no undo",
    }
    # header edits (PATCH the document with the etag as read); never a party, currency,
    # price, terms, posting date or status field
    DOC_UPDATES: Mapping[str, Mapping[str, Any]] = {
        "sales_order_update": {"set": "salesOrders", "fields": (
            "externalDocumentNumber", "requestedDeliveryDate", "shipToName", "shipToContact",
            "partialShipping")},
        "purchase_order_update": {"set": "purchaseOrders", "fields": (
            "requestedReceiptDate", "shipToName", "shipToContact")},
    }
    LINE_UPDATES: Mapping[str, Mapping[str, Any]] = {
        "sales_order_line_update": {"parent": "salesOrders", "lines": "salesOrderLines",
                                    "fields": ("quantity", "shipmentDate", "description")},
    }
    # Ricardo's scope: delete only an open order nothing of which shipped or was received;
    # the reader states it from the document as read (status and the quantities)
    DELETES: Mapping[str, Mapping[str, Any]] = {
        "sales_order_delete": {"set": "salesOrders", "done_field": "shipped"},
        "purchase_order_delete": {"set": "purchaseOrders", "done_field": "received"},
    }
    OPEN_STATUSES = frozenset({"Draft", "Open"})
    # ``company_name`` is BC's own name for ``company_id`` (the desk resolves it from the
    # probe-verified company list, ``prepared_writes.Desk``); a name alone is refused
    TARGET_KEYS = frozenset({"company_id", "company_name", "record_id", "document_id",
                             "line_id"})

    def _environment(self) -> str:
        """The autopilot's BC environment: ``PLX_AUTOPILOT_BC_ENVIRONMENT``, else the
        sandbox (``PLX_BC_SANDBOX_ENVIRONMENT``), else the read environment."""
        from operating.autopilot_write_readiness import bc_environment
        environment = bc_environment(self.environ)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", environment):
            raise AdapterRefused(["bc: the write environment name is malformed"])
        return environment

    def unavailable(self) -> str | None:
        """What a live write still lacks in the environment this adapter writes (the
        sandbox needs the sandbox switch, never the Production one)."""
        if self.transport is None:
            return super().unavailable()
        missing = getattr(self.transport, "missing", None)
        try:
            environment = self._environment()
        except AdapterRefused as error:
            return "; ".join(error.problems)
        try:
            found = missing(environment) if callable(missing) else []
        except TypeError:
            found = missing() if callable(missing) else []
        return "; ".join(str(m) for m in found) or None

    def _company(self, action: Mapping[str, Any]) -> str:
        """The company the action names (``target.company_id``), which must be one of the
        configured ones (``readiness.bc_company_ids``), else the configured default."""
        from operating import autopilot_write_readiness as readiness
        companies = readiness.bc_company_ids(self.environ)
        if not companies:
            raise AdapterRefused(["bc: company id not configured "
                                  f"({readiness.COMPANY_ENV}, {readiness.EXTRA_COMPANIES_ENV})"])
        target = action.get("target") or {}
        named = target.get("company_id")
        if named is None and target.get("company_name") is not None:
            # a name alone is never written to the default: the desk resolves it to the id
            raise AdapterRefused(["bc: the company is named but not by its id "
                                  "(target.company_id)"])
        if named is None:
            return companies[0]
        if not isinstance(named, str) or named.strip().lower() not in companies:
            raise AdapterRefused(["bc: target.company_id is not a company the autopilot "
                                  "writes (one of the configured companies)"])
        return named.strip().lower()

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        from operating import write_intents as wi
        operation = self._operation(action)
        if operation in self.LINE_OPERATIONS:
            return self._line(action, operation)
        if operation in self.DOC_UPDATES or operation in self.LINE_UPDATES:
            return self._update(action, operation)
        if operation in self.DELETES:
            return self._delete(action, operation)
        problems = wi._check_payload(operation, action.get("payload") or {})
        try:
            company = self._company(action)
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
        intent = {"operation": operation, "environment": self._environment(),
                  "company_id": company, "payload": action["payload"],
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
            company = self._company(action)
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
        root = wi.API_ROOT.replace("{environment}", self._environment())
        return {"method": "POST",
                "url": f"{root}/companies({company})/{op['parent']}({document.lower()})/"
                       f"{op['lines']}",
                "headers": {"Content-Type": "application/json"},
                "body": wi._plain(typed), "body_json": wi._json(typed)}

    def _root(self, company: str) -> str:
        from operating import write_intents as wi
        return wi.API_ROOT.replace("{environment}", self._environment()) + \
            f"/companies({company})"

    @staticmethod
    def _etag_problem(pre: Mapping[str, Any]) -> list[str]:
        etag = pre.get("etag")
        if not isinstance(etag, str) or not etag or "\r" in etag or "\n" in etag \
                or len(etag) > 300:
            return ["bc: precondition.etag (the record's etag as read) is required"]
        return []

    def _update(self, action: Mapping[str, Any], operation: str) -> dict[str, Any]:
        """A header or line edit: PATCH with the etag as read; every field set has its
        prior value (the undo), checked against the $metadata pin."""
        from operating import write_intents as wi
        doc = self.DOC_UPDATES.get(operation)
        line = self.LINE_UPDATES.get(operation)
        spec = doc or line
        assert spec is not None
        payload = dict(action.get("payload") or {})
        _only(payload, spec["fields"], "bc", operation)
        problems: list[str] = []
        if not payload:
            problems.append("bc: the update sets nothing")
        target = action.get("target") or {}
        pre = action.get("precondition") or {}
        document = str(target.get("document_id") or "")
        if not _GUID.fullmatch(document):
            problems.append("bc: target.document_id must be the GUID of the open document")
        line_id = str(target.get("line_id") or "")
        if line is not None and not _GUID.fullmatch(line_id):
            problems.append("bc: target.line_id must be the GUID of the line")
        problems += self._etag_problem(pre)
        if pre.get("status") not in self.OPEN_STATUSES:
            problems.append("bc: precondition.status must show the document open")
        if "quantity" in payload:
            qty = wi._decimal(payload["quantity"])
            if qty is None or qty <= 0:
                problems.append("bc: quantity must be above zero")
        if "description" in payload and _text(payload["description"], 100) is None:
            problems.append("bc: description must be 1..100 characters")
        try:
            _prior(action, list(payload), "bc")
        except AdapterRefused as error:
            problems += error.problems
        try:
            company = self._company(action)
        except AdapterRefused as error:
            problems += error.problems
        if problems:
            raise AdapterRefused(problems)
        entity = spec["set"] if doc else spec["lines"]
        meta = wi.default_metadata()
        props = meta[0].properties(entity) if meta else {}
        typed = {k: (wi._decimal(v) if (props.get(k) or {}).get("type") == "Edm.Decimal"
                     else v) for k, v in payload.items()}
        found = wi.validate_request({"entity_set": entity, "body": typed}, meta)
        if found:
            raise AdapterRefused([str(p) for p in found])
        url = (f"{self._root(company)}/{spec['set']}({document.lower()})" if doc else
               f"{self._root(company)}/{spec['parent']}({document.lower()})/"
               f"{spec['lines']}({line_id.lower()})")
        return {"method": "PATCH", "url": url,
                "headers": {"Content-Type": "application/json", "If-Match": pre["etag"]},
                "body": wi._plain(typed), "body_json": wi._json(typed)}

    def _delete(self, action: Mapping[str, Any], operation: str) -> dict[str, Any]:
        """Delete an open order nothing of which shipped or was received (Ricardo's
        scope), with the etag as read; BC itself refuses once anything is posted."""
        spec = self.DELETES[operation]
        if action.get("payload"):
            raise AdapterRefused(["bc: a delete carries no payload"])
        target = action.get("target") or {}
        pre = action.get("precondition") or {}
        problems: list[str] = []
        document = str(target.get("document_id") or "")
        if not _GUID.fullmatch(document):
            problems.append("bc: target.document_id must be the GUID of the open document")
        problems += self._etag_problem(pre)
        if pre.get("status") not in self.OPEN_STATUSES:
            problems.append("bc: only an open (Draft/Open) order is deleted")
        done = pre.get(spec["done_field"])
        if not (isinstance(done, (int, str)) and not isinstance(done, bool)
                and _DIGITS.fullmatch(str(done)) and int(done) == 0):
            problems.append(f"bc: precondition.{spec['done_field']} must be 0 (nothing "
                            f"{spec['done_field']}) as read")
        try:
            company = self._company(action)
        except AdapterRefused as error:
            problems += error.problems
        if problems:
            raise AdapterRefused(problems)
        return {"method": "DELETE",
                "url": f"{self._root(company)}/{spec['set']}({document.lower()})",
                "headers": {"If-Match": pre["etag"]}, "body": None}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request.get("method") == "DELETE":
            return None          # a deleted order is not restored by the system
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

# The Portal's own write routes a ``workbench_write`` key may call (TASK-5101,
# ``portal/src/lib/auth/workbench-write-routes.ts``), pinned at this commit of
# petralabx/plx-customer-portal staging; each body is the route handler's own (read there).
PORTAL_PIN = "plx-customer-portal staging a8b4b92 (2026-11-18)"
# routes asked of the Portal team (not yet on the key's list): written only once the owner
# names them here after the Portal exposed them (comma separated route ids)
PORTAL_ROUTES_ENV = "PLX_PORTAL_WRITE_ROUTES"
_PORTAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PORTAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTACT = {"name": ("string", 200), "email": ("email",), "phone": ("string", 50),
            "phone2": ("string", 50), "position": ("string", 120), "isPrimary": ("boolean",),
            "isActive": ("boolean",)}
_LOCATION = {"locationType": ("enum", ("SHIPPING", "BILLING", "BOTH")),
             "label": ("string", 120), "addressLine1": ("string", 200),
             "addressLine2": ("string", 200), "city": ("string", 100),
             "stateProvince": ("string", 100), "postalCode": ("string", 20),
             "country": ("string", 60), "phone": ("string", 50), "isDefault": ("boolean",)}
PORTAL_ROUTES: Mapping[str, Mapping[str, Any]] = {
    # header fields of a sales order (never lines, prices, terms or the commercial block);
    # a status move is CONFIRMED or CANCELLED only (the route's ALLOWED_STATUS), no undo
    "sales-order.update": {
        "method": "PATCH", "path": "/api/mrp/sales-orders/{id}", "target": ("id",),
        "fields": {"poReference": ("string", 100), "notes": ("text", 4000),
                   "requiredDate": ("date_or_null",), "warehouseId": ("id_or_null",),
                   "releaseRequired": ("boolean",),
                   "status": ("enum", ("CONFIRMED", "CANCELLED"))},
        "one_way": ("status",),
        "record": ("app_sales_order", "order_number", "Sales order",
                   {"poReference": "po_reference", "notes": "notes",
                    "requiredDate": "required_date", "warehouseId": "warehouse_id",
                    "releaseRequired": "release_required", "status": "status"})},
    # a PO's dates, shipping and notes (never its costs, terms or an approval: the route
    # records the approver as the key's user)
    "purchase-order.update": {
        "method": "PATCH", "path": "/api/mrp/purchase-orders/{id}", "target": ("id",),
        "fields": {"expectedDate": ("date",), "shippingMethod": ("string", 100),
                   "trackingNumber": ("string", 100), "notes": ("text", 4000),
                   "warehouseId": ("id_or_null",), "shippedAt": ("instant_or_null",)},
        "record": ("app_purchase_order", "po_number", "Purchase order",
                   {"expectedDate": "expected_date", "shippingMethod": "shipping_method",
                    "trackingNumber": "tracking_number", "notes": "notes",
                    "warehouseId": "warehouse_id", "shippedAt": "shipped_at"})},
    # a ship release of an order's lines to a saved ship-to (an address typed by the model
    # is never sent); cancelled only in the Portal (DELETE stays session-only)
    "ship-release.create": {
        "method": "POST", "path": "/api/mrp/orders/{order_id}/ship-releases",
        "target": ("order_id",), "required": ("customerLocationId", "lines"),
        "fields": {"customerLocationId": ("id",), "requestedShipDate": ("date",),
                   "notes": ("text", 2000), "releaseImmediately": ("boolean",),
                   "lines": ("lines", {"orderLineRef": ("id",), "productId": ("id",),
                                       "qtyAuthorized": ("positive",),
                                       "uom": ("string", 20), "notes": ("text", 500)},
                             ("orderLineRef", "productId", "qtyAuthorized", "uom"))}},
    # release a draft ship release (the route takes no body)
    "ship-release.update": {
        "method": "PATCH", "path": "/api/mrp/orders/{order_id}/ship-releases/{release_id}",
        "target": ("order_id", "release_id"), "fields": {}},
    "activity.note": {
        "method": "POST", "path": "/api/activity/{record_type}/{record_id}/note",
        "target": ("record_type", "record_id"), "required": ("content",),
        "fields": {"content": ("text", 4000)}},
    # asked of the Portal team 11-18 (MASTER "PORTAL ENDPOINT LIST"): the session routes'
    # own bodies (customerContactSchema, customerLocationSchema)
    "customer-contact.create": {
        "asked": True, "method": "POST", "path": "/api/operations/customers/{customer_id}/contacts",
        "target": ("customer_id",), "required": ("name",), "fields": _CONTACT},
    "customer-contact.update": {
        "asked": True, "method": "PUT",
        "path": "/api/operations/customers/{customer_id}/contacts/{contact_id}",
        "target": ("customer_id", "contact_id"), "fields": _CONTACT,
        "record": ("app_customer_contact", "name", "Contact",
                   {"name": "name", "email": "email", "phone": "phone", "phone2": "phone2",
                    "position": "position", "isPrimary": "is_primary",
                    "isActive": "is_active"})},
    "customer-location.create": {
        "asked": True, "method": "POST",
        "path": "/api/operations/customers/{customer_id}/locations",
        "target": ("customer_id",), "required": ("addressLine1", "city"), "fields": _LOCATION},
    "customer-location.update": {
        "asked": True, "method": "PUT",
        "path": "/api/operations/customers/{customer_id}/locations/{location_id}",
        "target": ("customer_id", "location_id"), "fields": _LOCATION,
        "record": ("app_customer_location", "label", "Ship-to",
                   {"locationType": "location_type", "label": "label",
                    "addressLine1": "address_line1", "addressLine2": "address_line2",
                    "city": "city", "stateProvince": "state_province",
                    "postalCode": "postal_code", "country": "country", "phone": "phone",
                    "isDefault": "is_default"})},
}


def portal_routes(environ: Mapping[str, str]) -> dict[str, Mapping[str, Any]]:
    """The Portal routes a write may use now: the key's list as pinned (``PORTAL_PIN``),
    plus each asked route in scope in ``PORTAL_ROUTES_ENV`` once the Portal
    exposed it."""
    named = {n.strip() for n in str(environ.get(PORTAL_ROUTES_ENV) or "").split(",")}
    return {k: v for k, v in PORTAL_ROUTES.items() if not v.get("asked") or k in named}


class PortalAdapter(Adapter):
    """A Customer Portal record written through the Portal's own staff routes that take a
    ``workbench_write`` key (TASK-5101; ``PORTAL_ROUTES``, pinned ``PORTAL_PIN``): the
    same handler, guards and audit a person's session gets, run as the key's bound user,
    each write logged by the Portal as an ``API_KEY_WRITE`` naming the key. Each operation
    is one route id; its body holds only the fields the handler reads, checked here (type,
    length, enum) before anything is shown. An update's values as read come from the
    Portal's read replica (``operating.prepared_read``) with the row's ``updated_at``,
    sent as ``expectedUpdatedAt`` (asked of the Portal: today its routes do not check a
    version, so a record changed between the read and the confirm is overwritten). Undo:
    an update's fields are patched back to the values as read (not a status move); a
    create, a release or a note has no undo through the key (DELETE stays the session's).
    Sign-offs (approve, sign, release with a password) are never written: the Portal would
    record the key's user, not the person who confirmed."""

    system, kind = "portal", "portal_write"
    documented = (f"Portal staff routes with a workbench_write key ({PORTAL_PIN}): "
                  "PATCH /api/mrp/sales-orders/{id} | PATCH /api/mrp/purchase-orders/{id} | "
                  "POST /api/mrp/orders/{id}/ship-releases | PATCH .../ship-releases/{rid} "
                  "| POST /api/activity/{type}/{id}/note")
    operations = {k: v["method"] + " " + v["path"] for k, v in PORTAL_ROUTES.items()}
    TARGET_KEYS = frozenset(k for v in PORTAL_ROUTES.values() for k in v["target"])
    structural = ("Portal writes are not opened (PLX_WRITES_LIVE_SYSTEMS=portal), or the "
                  "Portal's address and workbench_write key are not configured")
    needs = ("the Portal's base URL (PORTAL_WRITE_URL, https; staging first) and a "
             "workbench_write key bound to a Portal staff user (PORTAL_WRITE_KEY), outside git",
             "Portal opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=portal) and "
             "PLX_APPROVED_WRITES=portal_write",
             "the Portal read replica for an update's values as read "
             "(integrations.portal_replica: the host's replica settings and tunnel)")

    def _route(self, action: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        operation = self._operation(action)
        route = portal_routes(self.environ).get(operation)
        if route is None:
            raise AdapterRefused([f"portal: {operation} is not open on the Portal yet (asked "
                                  f"of the Portal team; {PORTAL_ROUTES_ENV})"])
        target = action.get("target") or {}
        if set(target) != set(route["target"]):
            raise AdapterRefused([f"portal: {operation} names {', '.join(route['target'])} "
                                  "in its target and nothing else"])
        return operation, route

    @staticmethod
    def _value(field: str, spec: tuple, value: Any) -> Any:
        kind = spec[0]
        bad = AdapterRefused([f"portal: {field} is not a valid {kind.replace('_', ' ')}"])
        if value is None:
            if kind.endswith("_or_null"):
                return None
            raise bad
        if kind in ("string", "text"):
            text = _text(value, spec[1])
            if text is None or kind == "string" and ("\n" in text or "\r" in text):
                raise bad
            return text
        if kind == "email":
            if not isinstance(value, str) or not _EMAILISH.fullmatch(value.strip()):
                raise bad
            return value.strip().lower()
        if kind == "boolean":
            if not isinstance(value, bool):
                raise bad
            return value
        if kind in ("id", "id_or_null"):
            if not isinstance(value, str) or not _PORTAL_ID.fullmatch(value):
                raise bad
            return value
        if kind in ("date", "date_or_null"):
            if not isinstance(value, str) or not _PORTAL_DATE.fullmatch(value):
                raise bad
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise bad from None
            return value
        if kind == "instant_or_null":
            if not _instant_text(value) or "T" not in value:
                raise bad
            return value
        if kind == "enum":
            if value not in spec[1]:
                raise AdapterRefused([f"portal: {field} must be one of {', '.join(spec[1])}"])
            return value
        if kind == "positive":
            # a quantity: exact decimal text or an integer, sent as JSON number text
            if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
                raise bad
            try:
                number = Decimal(str(value))
            except ArithmeticError:
                raise bad from None
            if not number.is_finite() or number <= 0 or abs(number.as_tuple().exponent) > 6:
                raise bad
            return number
        raise bad

    def _body(self, operation: str, route: Mapping[str, Any], payload: Any) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AdapterRefused(["portal: payload must be an object"])
        fields = route["fields"]
        _only(payload, tuple(fields), "portal", operation)
        lacking = [k for k in route.get("required", ()) if k not in payload]
        if lacking:
            raise AdapterRefused([f"portal: {operation} needs {', '.join(lacking)}"])
        if fields and not payload:
            raise AdapterRefused([f"portal: {operation} sets nothing"])
        body: dict[str, Any] = {}
        for key, value in payload.items():
            spec = fields[key]
            if spec[0] != "lines":
                body[key] = self._value(key, spec, value)
                continue
            if not isinstance(value, list) or not 1 <= len(value) <= 100:
                raise AdapterRefused([f"portal: {key} must be 1..100 lines"])
            rows = []
            for line in value:
                if not isinstance(line, Mapping):
                    raise AdapterRefused([f"portal: a line of {key} is not an object"])
                _only(line, tuple(spec[1]), "portal", f"{operation} line")
                if any(k not in line for k in spec[2]):
                    raise AdapterRefused([f"portal: a line needs {', '.join(spec[2])}"])
                rows.append({k: self._value(k, spec[1][k], v) for k, v in line.items()})
            body[key] = rows
        return body

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation, route = self._route(action)
        target = action.get("target") or {}
        ids = {}
        for key in route["target"]:
            value = target.get(key)
            if not isinstance(value, str) or not _PORTAL_ID.fullmatch(value):
                raise AdapterRefused([f"portal: target.{key} missing or malformed"])
            ids[key] = value
        body = self._body(operation, route, action.get("payload") or {})
        if route.get("record") is not None:
            # an update is shown and undone against the record as read (the replica)
            pre = action.get("precondition") or {}
            seen = pre.get("updated_at")
            if not isinstance(seen, str) or not _instant_text(seen):
                raise AdapterRefused(["portal: precondition.updated_at (the record as read) "
                                      "is required"])
            if not set(body) & set(route.get("one_way", ())):
                _prior(action, list(body), "portal")
            body["expectedUpdatedAt"] = seen
        from operating import write_intents as wi
        # ``body_json`` is the wire text: a quantity as an exact JSON number, never a float
        return {"method": route["method"], "url": route["path"].format(**ids),
                "headers": {"Content-Type": "application/json"},
                "body": _plain_json(body), "body_json": wi._json(body)}

    def people(self, action: Mapping[str, Any]) -> list[str]:
        # every address a record carries reaches that person: cleared like a recipient
        payload = action.get("payload") or {}
        return [v for v in payload.values() if isinstance(v, str) and _EMAILISH.fullmatch(v)] \
            if isinstance(payload, Mapping) else ["(malformed)"]

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        route = PORTAL_ROUTES.get(str(action.get("operation") or "")) or {}
        sent = {k for k in request.get("body") or {} if k != "expectedUpdatedAt"}
        if route.get("record") is None or not sent or sent & set(route.get("one_way", ())):
            return None
        from operating import write_intents as wi
        prior = _prior(action, sorted(sent), "portal")
        return {"method": request["method"], "url": request["url"],
                "headers": {"Content-Type": "application/json"},
                "body": _plain_json(prior), "body_json": wi._json(prior)}

    def undo_note(self) -> str:
        return ("A created record, a released ship release, a note or a status move is undone "
                "in the Portal by a person (the key cannot delete).")


def _plain_json(value: Any) -> Any:
    """``value`` as JSON-ready data, a Decimal as its exact text (never a float)."""
    if isinstance(value, Mapping):
        return {k: _plain_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_json(v) for v in value]
    return format(value, "f") if isinstance(value, Decimal) else value


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
    TARGET_KEYS = frozenset({"database", "layout", "record_id"})
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
    TARGET_KEYS = frozenset({"sender"})
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
        self._operation(action)
        payload = action.get("payload") or {}
        sender = str((action.get("target") or {}).get("sender") or "")
        if "@" not in sender or not payload.get("to") or not payload.get("body"):
            raise AdapterRefused(["mail: sender, recipients or body missing"])
        # the request as shown: each attached file by its name, type and size
        built = request_for("send_email", sender, {**payload, "attachments": [
            {"name": a.get("name"), "content_type": a.get("content_type"),
             "size": a.get("size")} for a in payload.get("attachments") or ()]})
        return {**built, "sender": sender, "payload": dict(payload)}

    @staticmethod
    def _files(payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """``payload`` as the send takes it: each attachment's bytes (``content``); None
        unless they are still exactly the file the person confirmed (its SHA-256)."""
        import base64
        files = []
        for a in payload.get("attachments") or ():
            try:
                data = base64.b64decode(str(a.get("content_b64") or ""), validate=True)
            except ValueError:
                return None
            if not data or hashlib.sha256(data).hexdigest() != a.get("sha256"):
                return None
            files.append({"name": a["name"], "content_type": a["content_type"],
                          "content": data})
        return {**payload, "attachments": files} if files else dict(payload)

    def perform(self, request: Mapping[str, Any], key: str) -> dict[str, Any]:
        payload = self._files(request["payload"])
        if payload is None:   # nothing leaves: the file is not the one confirmed
            return {"status": "failed", "http_status": None,
                    "missing": ["an attachment is not the file you confirmed"]}
        out = self._sender().dispatch("send_email", request["sender"], payload)
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
    for migration, ``Teamwork.Migrate.All``, never used). No undo: like a sent mail, a
    posted message is not recalled from the Workbench (``undo_note``)."""

    system, kind = "graph", "send_teams"
    documented = ("Microsoft Graph v1.0 POST /chats/{chat-id}/messages | POST /teams/{team-id}/"
                  "channels/{channel-id}/messages (delegated only)")
    operations = {"chat_message": "post to a chat as the person; no undo (not recalled)",
                  "channel_message": "post to a channel as the person; no undo (not recalled)"}
    TARGET_KEYS = frozenset({"chat_id", "team_id", "channel_id", "user"})
    structural = ("Graph refuses app-only chat and channel posts; no delegated "
                  "ChatMessage.Send/ChannelMessage.Send token here")
    needs = ("a delegated token of the owner with ChatMessage.Send (chats) and "
             "ChannelMessage.Send (channels): OAuth consent on the AZURE_* app and the "
             "person's one-time Connect Teams (integrations.teams_delegated)",
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
            # ``as``: whose own sign-in posts it (never sent: the transport's choice of token)
            return {**request_for("teams_message", "me@unknown", {"chat_id": chat,
                                                                  "body": body}),
                    "as": str((action.get("target") or {}).get("user")).lower()}
        team, channel = str(payload.get("team_id") or ""), str(payload.get("channel_id") or "")
        if not _GUID.fullmatch(team) or not self._CHANNEL.fullmatch(channel):
            raise AdapterRefused(["teams: team or channel missing or malformed"])
        return {"method": "POST",
                "url": f"{GRAPH}/teams/{team}/channels/{quote(channel, safe=':@')}/messages",
                "body": {"body": {"contentType": "text", "content": body}},
                "as": str((action.get("target") or {}).get("user")).lower()}

    def undo_note(self) -> str:
        return "A posted Teams message cannot be recalled from here."


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
    # the plan of a new task is its payload's planId (the one posted), never a target key
    TARGET_KEYS = frozenset({"task_id"})

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
                  "event_update": "subject/time/location/body; undo: PATCH back",
                  "event_respond": "accept, decline or tentatively accept an invitation in "
                                   "the owner's calendar (the organizer gets the answer); "
                                   "no undo: answer again"}
    structural = ("the calendar is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS), "
                  "and the approved-action app holds Calendars.Read only")
    needs = ("Calendars.ReadWrite (application) admin-consented on the AZURE_* app (it holds "
             "Calendars.Read today), scoped by an Exchange ApplicationAccessPolicy to the "
             "owners' mailboxes",
             "the calendar opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=calendar)")
    FIELDS = ("subject", "body", "start", "end", "location", "attendees")
    TARGET_KEYS = frozenset({"user", "event_id"})
    # an invitation's answer: Graph's own action per response, with the comment it carries
    RESPONSES = {"accept": "accept", "decline": "decline", "tentative": "tentativelyAccept"}
    RESPOND_FIELDS = ("response", "comment")

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
        if operation == "event_respond":
            _only(payload, self.RESPOND_FIELDS, "calendar", operation)
            verb = self.RESPONSES.get(str(payload.get("response") or "").strip().lower())
            if verb is None:
                raise AdapterRefused(["calendar: response is accept, decline or tentative"])
            comment = payload.get("comment", "")
            if not isinstance(comment, str) or len(comment) > 2000:
                raise AdapterRefused(["calendar: comment must be text up to 2000"])
            event = str((action.get("target") or {}).get("event_id") or "")
            if not _GRAPH_ID.fullmatch(event):
                raise AdapterRefused(["calendar: event_id missing or malformed"])
            return {"method": "POST",
                    "url": f"{GRAPH}/users/{user}/events/{quote(event, safe='=-_')}/{verb}",
                    "headers": {"Content-Type": "application/json"},
                    "body": {"comment": comment.strip(), "sendResponse": True}}
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
        if request["method"] == "POST" and self._operation(action) == "event_respond":
            return None                    # an answer sent is answered again, never undone
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
                  "file_upload": "new file, never a replace; undo: DELETE with eTag",
                  "file_replace": "a document's new content with If-Match its eTag as read; "
                                  "undo: PUT the prior bytes back with the eTag answered"}
    structural = ("SharePoint is not opened to approved writes (PLX_WRITES_LIVE_SYSTEMS), "
                  "and no app holds a SharePoint write grant")
    needs = ("Sites.Selected with the write role granted per target site (or "
             "Sites.ReadWrite.All) on the AZURE_* app, admin-consented: Vince's OK",
             "SharePoint opened to approved writes (PLX_WRITES_LIVE_SYSTEMS=sharepoint)")
    MAX_BYTES = 4 * 1024 * 1024
    # a file's name is the record as read (the card's ``record``), never a target key
    TARGET_KEYS = frozenset({"site_id", "list_id", "item_id", "drive_id", "parent_id"})
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
        if operation == "file_replace":
            return self._replace(action, payload)
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

    def _bytes(self, raw: Any, what: str) -> bytes:
        import base64
        import binascii
        try:
            data = base64.b64decode(raw, validate=True) if isinstance(raw, str) else None
        except (binascii.Error, ValueError):
            data = None
        if not data or len(data) > self.MAX_BYTES:
            raise AdapterRefused([f"sharepoint: {what} missing, not base64 or over 4 MiB"])
        return data

    def _replace(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """A document's whole new content (``doc_edit`` made it from the bytes as read):
        ``PUT /drives/{drive}/items/{item}/content`` with ``If-Match`` the eTag as read,
        so a file someone changed since is refused (412), never overwritten. The prior
        bytes (``precondition.prior.content_b64``) are required: they are the undo."""
        import hashlib
        _only(payload, ("content_b64", "content_type"), "sharepoint", "file_replace")
        target = action.get("target") or {}
        drive, item = str(target.get("drive_id") or ""), str(target.get("item_id") or "")
        if not _GRAPH_ID.fullmatch(drive) or not _GRAPH_ID.fullmatch(item):
            raise AdapterRefused(["sharepoint: drive_id or item_id missing or malformed"])
        etag = (action.get("precondition") or {}).get("etag")
        if not isinstance(etag, str) or not etag or len(etag) > 300 or "\n" in etag \
                or "\r" in etag:
            raise AdapterRefused(["sharepoint: precondition.etag (the document as read) is "
                                  "required"])
        data = self._bytes(payload.get("content_b64"), "content")
        self._bytes(_prior(action, ["content_b64"], "sharepoint")["content_b64"],
                    "the prior content")
        kind = payload.get("content_type") or "application/octet-stream"
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z]+/[A-Za-z0-9.+-]{1,100}", kind):
            raise AdapterRefused(["sharepoint: content_type malformed"])
        return {"method": "PUT",
                "url": f"{GRAPH}/drives/{quote(drive, safe='!-_')}/items/"
                       f"{quote(item, safe='!-_')}/content",
                "headers": {"Content-Type": kind, "If-Match": etag},
                "body_b64": payload["content_b64"],
                "body_sha256": hashlib.sha256(data).hexdigest(), "body_bytes": len(data)}

    def undo_request(self, action: Mapping[str, Any], request: Mapping[str, Any]
                     ) -> dict[str, Any] | None:
        if request["method"] == "PUT" and request["url"].endswith("/content") \
                and ":/" not in request["url"].split("/items/", 1)[1]:
            prior = _prior(action, ["content_b64"], "sharepoint")["content_b64"]
            return {"method": "PUT", "url": request["url"],
                    "headers": {"Content-Type": request["headers"]["Content-Type"],
                                "If-Match": "{response:eTag}"},
                    "body_b64": prior}
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
        return ("Delete the created item or uploaded file, put a replaced document's prior "
                "content back (each refused once someone changed it since), or patch the "
                "changed columns back.")


ADAPTER_TYPES = (MCApprovalAdapter, MCTaskAdapter, BCAdapter, SageAdapter, PortalAdapter,
                 FileMakerAdapter, MailAdapter, TeamsAdapter, PlannerAdapter, CalendarAdapter,
                 SharePointAdapter)


def default_adapters(environ: Mapping[str, str] | None = None,
                     state: "Path | None" = None) -> dict[str, Adapter]:
    """The adapters product code uses. A system the owner opened to approved writes
    (``live_source_guard.WRITES_FLAG``: Planner, calendar, SharePoint, BC, Portal) gets its guarded
    write transport (``integrations.write_transports``); Teams, once ``teams`` is opened
    there and the write ``state`` is known, posts as each person on their own sign-in
    (``integrations.teams_delegated``); every other one but mail has none."""
    from integrations import write_transports as wt
    from integrations.live_source_guard import WRITES_FLAG
    env = dict(os.environ if environ is None else environ)
    opened = {s.strip().lower() for s in str(env.get(WRITES_FLAG) or "").split(",")}
    out: dict[str, Adapter] = {}
    for cls in ADAPTER_TYPES:
        transport = wt.for_system(cls.system, env) if cls.system in (
            "planner", "calendar", "sharepoint", "bc", "portal") else None
        if cls is MCTaskAdapter and env.get("PLX_AUTOPILOT_MC_LIVE") == "1":
            transport = wt.MCTaskTransport(env)
        if cls is TeamsAdapter and state is not None and "teams" in opened:
            from integrations import teams_delegated as td
            transport = td.Transport(env, td.Store(Path(state)))
        out[cls.kind] = cls(environ=env) if cls is MailAdapter else cls(transport, environ=env)
    return out
