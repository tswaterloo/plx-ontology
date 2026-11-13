"""Write transports for person-approved writes: Microsoft Graph (Planner, calendar,
SharePoint) and Business Central API v2.0. Each sends one request through the one guarded
client (``live_source_guard.write_action``: exact method and path shapes, closed headers,
the owner's ``PLX_WRITES_LIVE_SYSTEMS`` per system, re-checked inside the sender), never
retries and never follows a redirect.

Nothing here decides whether a write may happen: the autopilot write executor
(``operating.autopilot_write``: approval, people exclusion, kill switches, idempotency,
rate limits) and the BC write desk (``operating.write_intents``: preview, approval, etag
re-read) do. A transport is constructed only for a system the owner opened
(``for_system``); ``missing()`` names what a live write still lacks, before any token is
asked for.

* Graph: the app-only token of the approved-action app, the same app as the approved
  mail send, issued by ``graph_send.action_bearer``. The token must carry the system's application
  permission (``GRAPH_PERMISSIONS``); otherwise nothing is sent (a definite refusal).
  Teams posts take only a person's delegated token, which no code here holds: no Teams
  transport.
* BC: the ``BC_*`` app's token for ``BC_SCOPE``; the request's ``{tenant}`` is the BC
  tenant id; live needs ``write_intents.live_gate`` too (the owner's production or sandbox
  flag and Vince's scopes grant ``PLX_WRITES_SCOPES_GRANTED=bc``).

Outcomes: ``(status, body)`` for the adapters (``Adapter.perform``); a refusal before any
I/O answers 4xx with a code (nothing was written); a lost answer raises ``OSError``
(``unknown``: never retried, a person reconciles).

Evidence label of tests: ``injected-transport``; live-unverified.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any, Callable, Mapping

from integrations import live_source_guard as guard

GRAPH_PERMISSIONS = {
    "planner": frozenset({"Tasks.ReadWrite.All"}),
    "calendar": frozenset({"Calendars.ReadWrite"}),
    "sharepoint": frozenset({"Sites.ReadWrite.All", "Sites.Selected", "Sites.Manage.All",
                             "Sites.FullControl.All", "Files.ReadWrite.All"}),
}
_TENANT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

Send = Callable[..., tuple[int, Mapping[str, str], bytes]]


def _body(request: Mapping[str, Any]) -> bytes | None:
    if request.get("body_b64") is not None:
        try:
            return base64.b64decode(str(request["body_b64"]), validate=True)
        except (binascii.Error, ValueError):
            raise guard.ReadOnlyGuardError("write: upload bytes are not base64") from None
    if request.get("body_json") is not None:
        return str(request["body_json"]).encode("utf-8")
    if request.get("body") is None:
        return None
    return json.dumps(request["body"], separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def _answer(raw: bytes) -> dict[str, Any]:
    try:
        found = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


class _Transport:
    system = ""

    def __init__(self, environ: Mapping[str, str] | None = None, *,
                 bearer: Callable[[], str] | None = None, send: Send | None = None) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self._bearer = bearer
        self._send = send

    def missing(self) -> list[str]:
        if self.system not in guard.writes_enabled(self.environ):
            return [f"{self.system} writes are not opened ({guard.WRITES_FLAG})"]
        return []

    def _token(self) -> str:
        raise NotImplementedError

    def _url(self, request: Mapping[str, Any]) -> str:
        return str(request.get("url") or "")

    def _lacks(self, token: str) -> str | None:
        return None

    def _missing_for(self, request: Mapping[str, Any]) -> list[str]:
        """What a write of ``request`` still lacks (the BC transport judges its target
        environment: a sandbox write needs the sandbox switch, not the live one)."""
        return self.missing()

    def __call__(self, request: Mapping[str, Any], key: str) -> tuple[int, Mapping[str, Any]]:
        if self._missing_for(request):
            return 403, {"code": "write_not_opened"}
        method, url = str(request.get("method") or ""), self._url(request)
        headers = {str(k): str(v) for k, v in (request.get("headers") or {}).items()}
        try:
            body = _body(request)
            guard.check_write_request(method, url, headers, body, environ=self.environ)
        except guard.ReadOnlyGuardError:
            return 400, {"code": "guard_refused"}
        try:
            token = self._token()
        except (guard.SourceUnavailable, guard.ReadOnlyGuardError, ValueError):
            return 401, {"code": "no_token"}
        lack = self._lacks(token)
        if lack:
            return 403, {"code": "permission_missing", "error": lack}
        try:
            status, _headers, raw = guard.write_action(
                method, url, guard.Secret(token), headers, body, environ=self.environ,
                sender=self._send)
        except guard.ReadOnlyGuardError:
            return 400, {"code": "guard_refused"}
        except guard.SourceUnavailable as error:
            raise OSError(type(error).__name__) from None
        return int(status), _answer(raw)


class GraphWriteTransport(_Transport):
    """Planner, calendar or SharePoint writes on the approved-action app's token."""

    def __init__(self, system: str, environ: Mapping[str, str] | None = None, **kw: Any) -> None:
        if system not in GRAPH_PERMISSIONS:
            raise ValueError("no Graph write transport for this system")
        super().__init__(environ, **kw)
        self.system = system

    def missing(self) -> list[str]:
        from integrations.graph_send import credential_prefix
        out = super().missing()
        if credential_prefix(self.environ) is None:
            out.append("the approved-action app is not configured")
        return out

    def _token(self) -> str:
        if self._bearer is None:
            from integrations.graph_send import action_bearer
            self._bearer = action_bearer(self.environ)
        return self._bearer()

    def _lacks(self, token: str) -> str | None:
        from integrations.graph_send import token_permissions
        roles, _scopes = token_permissions(token)
        if roles & GRAPH_PERMISSIONS[self.system]:
            return None
        return f"{' or '.join(sorted(GRAPH_PERMISSIONS[self.system]))} not granted"


class BCWriteTransport(_Transport):
    """BC API v2.0 writes on the ``BC_*`` app's token, for the autopilot adapter
    (``__call__``) and the BC write desk (``send``)."""

    system = "bc"

    def __init__(self, environ: Mapping[str, str] | None = None, **kw: Any) -> None:
        super().__init__(environ, **kw)
        self._provider: Any = None

    def _tenant(self) -> str | None:
        tenant = str(self.environ.get("BC_TENANT_ID") or "").strip().lower()
        return tenant if _TENANT.fullmatch(tenant) else None

    def missing(self, environment: str | None = None) -> list[str]:
        from operating import write_intents as wi
        out = super().missing()
        if self._tenant() is None:
            out.append("BC tenant id not configured (BC_TENANT_ID)")
        out += [m for m in wi.live_gate(self.environ, environment or wi.PRODUCTION, self)
                if "client exists" not in m]
        return out

    def _url(self, request: Mapping[str, Any]) -> str:
        path = str(request.get("url") or request.get("path") or "")
        tenant = self._tenant()
        if not path.startswith("/v2.0/{tenant}/") or tenant is None:
            return ""
        return f"https://{guard.BC_HOST}" + path.replace("{tenant}", tenant, 1)

    def _environment(self, request: Mapping[str, Any]) -> str:
        path = str(request.get("url") or request.get("path") or "")
        parts = path.split("/")
        return parts[3] if len(parts) > 3 else ""

    def _missing_for(self, request: Mapping[str, Any]) -> list[str]:
        # INT-001: judged once, for the request's own environment (the base call no
        # longer re-judges it as Production)
        return self.missing(self._environment(request))

    def _token(self) -> str:
        if self._bearer is not None:
            return self._bearer()
        if self._provider is None:
            self._provider = guard.TokenProvider(
                guard.ClientCredentials.from_env("BC", self.environ), guard.BC_SCOPE,
                guard.GuardedTransport(environ=self.environ))
        return self._provider.bearer()

    def send(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """The BC write desk's call (``write_intents.WriteDesk.execute``):
        ``{"status": "written" | "failed" | "unknown", "http_status"}``."""
        try:
            status, _body = self(request, "")
        except OSError:
            return {"status": "unknown", "http_status": None}
        if 200 <= status < 300:
            return {"status": "written", "http_status": status}
        if 400 <= status < 500 and status not in (408, 425, 429):
            return {"status": "failed", "http_status": status}
        return {"status": "unknown", "http_status": status}


def for_system(system: str, environ: Mapping[str, str] | None = None) -> _Transport | None:
    """The write transport of ``system`` when the owner opened it, else None."""
    env = dict(os.environ if environ is None else environ)
    if system not in guard.writes_enabled(env):
        return None
    if system == "bc":
        return BCWriteTransport(env)
    return GraphWriteTransport(system, env) if system in GRAPH_PERMISSIONS else None
