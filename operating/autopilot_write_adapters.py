"""Per-system write adapters for the autopilot action executor (``operating.autopilot_write``).

An adapter turns one routine action (``autopilot_write.action_of``) into the exact request
the owning system's documented write API takes (``request``), performs it once through an
injected transport (``perform``), and names the inverse request when the system can undo
it (``inverse``). An adapter never decides whether an action may run: the executor does
(kill switches, the people exclusion, the routine rules, the write gate, rate limits,
idempotency) and calls ``perform`` only when every check holds.

No transport that reaches a network is constructed here, except the approved-send path
that already exists (``integrations.graph_send.ActionSender`` over
``live_source_guard.send_action``, itself off unless ``PLX_ACTIONS_LIVE=1``, the sender
is listed and the token holds ``Mail.Send``). Every other adapter reports
``unavailable: <reason>`` until a reviewed write transport and its credentials exist;
``needs`` names what is missing. Pointing an adapter at a real system is a reviewed code
change, never configuration.

``perform`` returns ``{"status": "done" | "failed" | "unknown" | "not_performed", ...}``:
``done`` the system accepted it; ``failed`` a definite refusal (nothing written);
``unknown`` it may have been written (a 5xx, a lost answer): never retried, a person
reconciles; ``not_performed`` nothing left (a dry run of the underlying sender).

Evidence label of tests: ``injected-transport`` (fake transports and recorded answers);
live-unverified.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping

Transport = Callable[[Mapping[str, Any], str], tuple[int, Mapping[str, Any]]]

_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,64}$")
_GATE_ID = re.compile(r"^[A-Za-z0-9_:.-]{1,120}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NOT_DEFINITE = frozenset({408, 425, 429})


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


class Adapter:
    """One system's write path. Subclasses set ``system``, ``kind``, ``documented`` and
    ``needs`` and implement ``request`` (and ``inverse`` when the system can undo)."""

    system = ""
    kind = ""
    documented = ""
    needs: tuple[str, ...] = ()
    structural: str | None = None   # why no transport can exist here yet (None: it can)

    def __init__(self, transport: Transport | None = None, *,
                 environ: Mapping[str, str] | None = None) -> None:
        self.transport = transport
        self.environ = dict(os.environ if environ is None else environ)

    def unavailable(self) -> str | None:
        if self.transport is not None:
            return None
        return self.structural or "no reviewed write transport for this system in this build"

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def inverse(self, request: Mapping[str, Any], result: Mapping[str, Any]
                ) -> dict[str, Any] | None:
        return None

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
                "state": "ready" if why is None else f"unavailable: {why}",
                "needs": list(self.needs) if why else []}


# ------------------------------------------------------------------ Mission Control

class MCApprovalAdapter(Adapter):
    """Decide a routine MC approval gate: ``POST /api/approvals/decide {taskId, gateId,
    decision, note, renderedScope, renderedSourceRef}``.
    MC grants ``approval.decide`` only to a human Entra session (admin/owner) and always
    denies it to services, so no key this repo holds can decide; the decider must not be
    the requester (403 ``separation_of_duties``). A decided approval has no revoke: a
    launch approval lapses after MC's 4 h launch TTL."""

    system, kind = "mc", "mc_approval"
    documented = "POST https://mc.plxcustomer.io/api/approvals/decide"
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


# ------------------------------------------------------------------ Business Central

class BCAdapter(Adapter):
    """A BC API v2.0 entry, built by ``write_intents.build_request`` (the same request the
    approve-to-write desk previews) for the operations ``write_intents.OPERATIONS``
    declares. The BC client here is GET-only and the environment is Production with no
    sandbox. Undo: the operation's own compensation (a created document: DELETE with its
    etag)."""

    system, kind = "bc", "bc_write"
    documented = "Business Central API v2.0 (POST/PATCH companies({id})/<entity set>)"
    structural = ("the BC client is GET-only; no reviewed BC write transport, and the write "
                  "scopes are not granted (PLX_WRITES_SCOPES_GRANTED unset)")
    needs = ("Vince's grant of BC API write scopes for the autopilot app "
             "(PLX_WRITES_SCOPES_GRANTED=bc)",
             "a BC sandbox environment to verify against (PLX_BC_SANDBOX_ENVIRONMENT); "
             "Production only today",
             "a reviewed BC write transport (POST/PATCH/DELETE) beside the GET-only client",
             "the company id the autopilot writes to (PLX_AUTOPILOT_BC_COMPANY_ID)")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        from operating import write_intents as wi
        operation = str(action.get("operation") or "")
        if operation not in wi.OPERATIONS or wi.OPERATIONS[operation]["system"] != "bc":
            raise AdapterRefused([f"bc: unknown operation {operation[:60]}"])
        problems = wi._check_payload(operation, action.get("payload") or {})
        company = str(self.environ.get("PLX_AUTOPILOT_BC_COMPANY_ID") or "")
        if not _GUID.fullmatch(company):
            problems.append("bc: company id not configured (PLX_AUTOPILOT_BC_COMPANY_ID)")
        if problems:
            raise AdapterRefused(list(problems))
        intent = {"operation": operation, "environment": self.environ.get("BC_ENVIRONMENT")
                  or wi.PRODUCTION, "company_id": company, "payload": action["payload"],
                  "precondition": action.get("precondition") or {},
                  "record_id": (action.get("target") or {}).get("record_id")}
        built = wi.build_request(intent, wi.default_metadata())
        if built.get("problems"):
            raise AdapterRefused([str(p) for p in built["problems"]])
        return {"method": built["method"], "url": built["path"], "headers": built["headers"],
                "body": built["body"], "body_json": built["body_json"]}

    def inverse(self, request: Mapping[str, Any], result: Mapping[str, Any]
                ) -> dict[str, Any] | None:
        response = result.get("response") or {}
        new_id, etag = response.get("id"), response.get("@odata.etag")
        if request.get("method") == "POST" and isinstance(new_id, str) and \
                _GUID.fullmatch(new_id) and isinstance(etag, str):
            return {"method": "DELETE", "url": f"{request['url']}({new_id})",
                    "headers": {"If-Match": etag}, "body": None}
        return None

    def undo_note(self) -> str:
        return "Delete the created document with its etag (BC refuses once it is posted)."


# ------------------------------------------------------------------ Sage 300

class SageAdapter(Adapter):
    """A Sage 300 entry through the Sage 300 Web API (``/Sage300WebApi/v1.0/-/{company}/
    <module>/<resource>``, e.g. ``AR/ARInvoiceBatches``, ``OE/OEShipments``,
    ``AR/ARReceiptAndAdjustmentBatches``). Sage reaches this repo only as the read-only
    ``sage.*`` relations of the Portal reporting API. Undo: an unposted batch can be
    deleted (``DELETE`` of the batch); a posted one is reversed by a person."""

    system, kind = "sage", "sage_write"
    documented = "Sage 300 Web API v1.0 (POST /Sage300WebApi/v1.0/-/{company}/{module}/{resource})"
    structural = ("no Sage write API or credentials in this repo; Sage is read only through "
                  "the Portal reporting replica")
    needs = ("the Sage 300 Web API host, company database id and a write-capable Sage user "
             "(environment file, outside git)",
             "a Sage test company to verify against",
             "a reviewed Sage write transport")
    RESOURCES = {"invoice_entry": ("AR", "ARInvoiceBatches"),
                 "shipment_posting": ("OE", "OEShipments"),
                 "cash_application": ("AR", "ARReceiptAndAdjustmentBatches")}

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        operation = str(action.get("operation") or "")
        if operation not in self.RESOURCES:
            raise AdapterRefused([f"sage: unknown operation {operation[:60]}"])
        company = str(self.environ.get("PLX_SAGE_COMPANY") or "")
        if not re.fullmatch(r"[A-Z0-9]{1,6}", company):
            raise AdapterRefused(["sage: company not configured (PLX_SAGE_COMPANY)"])
        module, resource = self.RESOURCES[operation]
        return {"method": "POST", "url": f"/Sage300WebApi/v1.0/-/{company}/{module}/{resource}",
                "body": dict(action.get("payload") or {})}


# ------------------------------------------------------------------ Portal

class PortalAdapter(Adapter):
    """A Customer Portal entry. The Portal's reporting API is read-only and the Portal has
    no write entity the autopilot prepares (``write_intents.SYSTEMS`` refuses every Portal
    intent); its governed writes go through ``operating.action_adapters.PortalAdapter``
    behind a human MC approval."""

    system, kind = "portal", "portal_write"
    documented = "Customer Portal app API (no autopilot write route defined)"
    structural = "the Portal API this repo holds is read-only and defines no write route"
    needs = ("a Portal write route for the operation and a service key scoped to it",
             "Portal UAT to verify against")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        raise AdapterRefused(["portal: no write route is defined for autopilot entries"])


# ------------------------------------------------------------------ FileMaker

class FileMakerAdapter(Adapter):
    """A FileMaker entry through the FileMaker Data API (``POST /fmi/data/vLatest/databases/
    {db}/layouts/{layout}/records`` with ``{"fieldData": {...}}``). FileMaker reaches this
    repo only as read-only ``public.*`` relations of the Portal reporting API. Undo: the
    created record is deleted (``DELETE .../records/{recordId}``)."""

    system, kind = "filemaker", "filemaker_write"
    documented = "FileMaker Data API vLatest (POST /fmi/data/vLatest/databases/{db}/layouts/{layout}/records)"
    structural = "no FileMaker Server host or Data API account in this repo"
    needs = ("the FileMaker Server host, database and a Data API account with fmrest "
             "privilege on the target layouts (environment file, outside git)",
             "a reviewed FileMaker write transport")
    _NAME = re.compile(r"^[A-Za-z0-9 _-]{1,60}$")

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        target = action.get("target") or {}
        db, layout = str(target.get("database") or ""), str(target.get("layout") or "")
        if not (self._NAME.fullmatch(db) and self._NAME.fullmatch(layout)):
            raise AdapterRefused(["filemaker: database or layout missing"])
        return {"method": "POST",
                "url": f"/fmi/data/vLatest/databases/{db}/layouts/{layout}/records",
                "body": {"fieldData": dict(action.get("payload") or {})}}

    def inverse(self, request: Mapping[str, Any], result: Mapping[str, Any]
                ) -> dict[str, Any] | None:
        rid = ((result.get("response") or {}).get("response") or {}).get("recordId")
        if isinstance(rid, (str, int)) and str(rid).isdigit():
            return {"method": "DELETE", "url": f"{request['url']}/{rid}", "body": None}
        return None


# ------------------------------------------------------------------ sends

class MailAdapter(Adapter):
    """Send the prepared email as the item's owner from their own mailbox, through the one
    approved send path (``integrations.graph_send.ActionSender``), which sends only with
    ``PLX_ACTIONS_LIVE=1``, the owner listed in ``PLX_ACTIONS_SENDERS`` and a token holding
    ``Mail.Send``; otherwise it answers a dry run naming what is missing. Mail has no undo."""

    system, kind = "graph", "send_mail"
    documented = "Microsoft Graph v1.0 POST /users/{sender}/sendMail"
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
    """Post the prepared Teams chat message (Graph ``POST /chats/{id}/messages``). Graph
    refuses an application token on chat posts: it needs a delegated ``ChatMessage.Send``
    token of the owner."""

    system, kind = "graph", "send_teams"
    documented = "Microsoft Graph v1.0 POST /chats/{chat-id}/messages (delegated only)"
    structural = "Graph refuses app-only chat posts; no delegated ChatMessage.Send token here"
    needs = ("a delegated ChatMessage.Send token of the owner (OAuth consent and refresh "
             "token held outside git)",)

    def request(self, action: Mapping[str, Any]) -> dict[str, Any]:
        from integrations.graph_send import request_for
        payload = action.get("payload") or {}
        if not payload.get("chat_id") or not payload.get("body"):
            raise AdapterRefused(["teams: chat or body missing"])
        return request_for("teams_message", "me@unknown", payload)

    def undo_note(self) -> str:
        return "A chat message can be deleted by its sender (softDelete, delegated)."


ADAPTER_TYPES = (MCApprovalAdapter, BCAdapter, SageAdapter, PortalAdapter, FileMakerAdapter,
                 MailAdapter, TeamsAdapter)


def default_adapters(environ: Mapping[str, str] | None = None) -> dict[str, Adapter]:
    """The adapters product code uses: every one but mail without a transport."""
    return {cls.kind: cls(environ=environ) for cls in ADAPTER_TYPES}
