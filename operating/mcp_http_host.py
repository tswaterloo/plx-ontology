"""Remote MCP host: Streamable HTTP for many employees, each request an Entra-signed-in one.

Lead decision docs/decisions/2026-10-05-employee-door.md (D1). The host serves the
tools of the stdio host (``operating.mcp_server``, the same ``MCPServer`` per session)
at one endpoint, ``/mcp``, as an OAuth 2.0 resource server (MCP authorization, spec
revision 2025-06-18):

* Every request carries ``Authorization: Bearer <Entra access token>``, verified before
  anything else is read (``operating.oidc_auth.verify_access_token``: JWKS signature,
  issuer, audience, expiry, tenant, delegated scope). Then the token id is checked
  against the state directory's durable revocation guard
  (``session_identity.replay_guard_for``, shared with the console) and the employee is
  resolved through the employee resolver (Portal role ∩ Entra groups, the PLX-confirmed
  policy file; item ACLs on every read), so a revoked employee is refused on the next
  request. There is no fallback principal and no fixture identity.
* 401 carries ``WWW-Authenticate: Bearer resource_metadata=...`` and the RFC 9728
  metadata is served at ``/.well-known/oauth-protected-resource`` (and its ``/mcp``
  suffix form): the authorization server is the tenant's Entra v2 endpoint.
* Sessions (Streamable HTTP): ``initialize`` opens one ``MCPServer`` bound to the
  employee and returns ``Mcp-Session-Id``; later requests must carry it and the same
  employee's token (anyone else's gets 404, as an unknown session). Each request binds
  its own verified session (``MCPServer.rebind``), so an expired token ends nothing but
  that request. Each session has its own ontology tools (no cached answer is shared
  between employees); the case runtime's gate re-resolves the employee on every read.
  A refused employee's sessions are closed.
* Responses are JSON; a request that makes the server ask the client something (a
  confirmation form) is answered as a short SSE stream (the request, then the
  response). GET (no server stream is offered) is 405; DELETE ends a session.
* Bound to loopback only: a non-loopback address is refused at start-up. Where a
  production host runs, and its TLS, is decided: Tailscale Funnel
  (docs/decisions/2026-10-08-employee-host-hosting.md); nothing is deployed.
* An ``Origin`` header (a browser) must be one of ``--allowed-origin`` (DNS rebinding).

One request at a time (``http.server.HTTPServer``): the case runtime's SQLite stores
are used from the serving thread only.

Evidence: isolated-fixture (tokens signed in the tests with a generated key, JWKS
injected); live-unverified (no Workbench Entra app registration, no
GroupMember.Read.All, Portal RBAC rows unserved: docs/reference/permissions.md blockers 1-3).
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from . import mcp_server, session_identity
from .employee_permissions import EMPLOYEE_PREFIX
from .mcp_server import MCPServer, SessionHost
from .oidc_auth import EntraResource, EntraTokenRefused, entra_session, verify_access_token
from .session_identity import SessionIdentityRefused

ENDPOINT = "/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-03-26", "2025-06-18",
                                         mcp_server.PROTOCOL_VERSION})
MAX_SESSIONS = 64
SESSION_IDLE_SECONDS = 2 * 60 * 60
DEFAULT_PORT = 8766
_JSON = "application/json"


Reply = tuple[int, dict[str, str], bytes]


def _json(status: int, value: Any, headers: Mapping[str, str] | None = None) -> Reply:
    return status, {"Content-Type": _JSON, "Cache-Control": "no-store", **(headers or {})}, \
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _rpc_error(status: int, code: int, message: str) -> Reply:
    return _json(status, {"jsonrpc": "2.0", "id": None,
                          "error": {"code": code, "message": message}})


class _Session:
    __slots__ = ("employee", "server", "used")

    def __init__(self, employee: str, server: MCPServer, used: float) -> None:
        self.employee, self.server, self.used = employee, server, used


class McpHttpHost:
    """The request logic of the remote host, independent of the socket server:
    ``respond(method, path, headers, body) -> (status, headers, body)``."""

    def __init__(self, services: SessionHost, resource: EntraResource, resolver: Any,
                 replay_guard: session_identity.TicketReplayGuard, *,
                 allowed_origins: frozenset[str] = frozenset(),
                 clock: Callable[[], float] = time.time) -> None:
        if resource.tenant_id != resolver.policy.entra_tenant_id:
            raise ValueError("entra_resource_tenant_must_equal_the_policy_tenant")
        self.services, self.resource, self.resolver = services, resource, resolver
        self.replay_guard = replay_guard
        self.allowed_origins = allowed_origins
        self.clock = clock
        self.sessions: OrderedDict[str, _Session] = OrderedDict()
        parts = urlsplit(resource.resource)
        # RFC 9728 section 3.1: the well-known path, then the resource's own path.
        self.metadata_url = (f"{parts.scheme}://{parts.netloc}{METADATA_PATH}"
                             f"{parts.path.rstrip('/')}")

    # ---- authentication -------------------------------------------------------

    def _unauthorized(self, refusal: EntraTokenRefused | None) -> Reply:
        status = 403 if refusal is not None and refusal.error == "insufficient_scope" else 401
        body = {"error": {"code": refusal.error if refusal else "unauthorized",
                          "reason": refusal.reason if refusal else "bearer_token_required"}}
        return _json(status, body, {"WWW-Authenticate": self.resource.challenge(
            self.metadata_url, refusal)})

    def authenticate(self, headers: Mapping[str, str]) -> session_identity.SessionIdentity:
        """The verified employee session of this request (``EntraTokenRefused`` or
        ``SessionIdentityRefused`` otherwise)."""
        value = headers.get("authorization") or ""
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise EntraTokenRefused("bearer_token_required")
        now = int(self.clock())
        claims = verify_access_token(token.strip(), self.resource, now=now)
        try:
            return entra_session(self.resolver, claims, replay_guard=self.replay_guard,
                                 now=now)
        except SessionIdentityRefused:
            # Refused now (revoked in the Portal, policy or guard): whatever this
            # employee had open is closed with it.
            self._close_employee(EMPLOYEE_PREFIX + str(claims["oid"]).strip().lower())
            raise

    # ---- sessions ---------------------------------------------------------------

    def _close(self, session_id: str) -> None:
        held = self.sessions.pop(session_id, None)
        if held is not None:
            held.server.close()

    def _close_employee(self, employee: str) -> None:
        for session_id in [k for k, v in self.sessions.items() if v.employee == employee]:
            self._close(session_id)

    def _expire(self) -> None:
        now = self.clock()
        for session_id in [k for k, v in self.sessions.items()
                           if now - v.used > SESSION_IDLE_SECONDS]:
            self._close(session_id)

    def _session(self, headers: Mapping[str, str], employee: str) -> _Session | None:
        held = self.sessions.get(headers.get("mcp-session-id") or "")
        if held is None or held.employee != employee:
            return None   # another employee's session reads as an unknown one
        held.used = self.clock()
        self.sessions.move_to_end(headers["mcp-session-id"])
        return held

    # ---- requests ---------------------------------------------------------------

    def respond(self, method: str, path: str, headers: Mapping[str, str],
                body: bytes) -> Reply:
        headers = {k.lower(): v for k, v in headers.items()}
        path = path.split("?", 1)[0]
        if method == "GET" and path in (METADATA_PATH, METADATA_PATH + ENDPOINT):
            return _json(200, self.resource.metadata())
        if path != ENDPOINT:
            return _json(404, {"error": {"code": "not_found"}})
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            return _json(403, {"error": {"code": "origin_refused"}})
        if method not in ("POST", "DELETE"):
            return _json(405, {"error": {"code": "method_not_allowed"}},
                         {"Allow": "POST, DELETE"})
        try:
            session = self.authenticate(headers)
        except EntraTokenRefused as refusal:
            return self._unauthorized(None if refusal.reason == "bearer_token_required"
                                      else refusal)
        except SessionIdentityRefused as refusal:
            return _json(403, {"error": {"code": "employee_refused",
                                         "reason": refusal.reason}})
        self._expire()
        employee = session.employee.principal
        version = headers.get("mcp-protocol-version")
        if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
            return _rpc_error(400, -32600, "Unsupported MCP-Protocol-Version")
        if method == "DELETE":
            held = self._session(headers, employee)
            if held is None:
                return _json(404, {"error": {"code": "session_not_found"}})
            self._close(headers["mcp-session-id"])
            return 204, {}, b""
        if not headers.get("content-type", "").lower().startswith(_JSON):
            return _rpc_error(415, -32700, "Content-Type must be application/json")
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _rpc_error(400, -32700, "Parse error")
        if not isinstance(message, dict):
            return _rpc_error(400, -32600, "Invalid Request")
        if message.get("method") == "initialize":
            if headers.get("mcp-session-id"):
                return _rpc_error(400, -32600, "initialize opens a new session")
            server = self.services.server(session)
            response = server.handle(message)
            if response is None or "result" not in response:
                server.close()
                return _json(200, response) if response else (202, {}, b"")
            while len(self.sessions) >= MAX_SESSIONS:
                self._close(next(iter(self.sessions)))
            session_id = secrets.token_urlsafe(32)
            self.sessions[session_id] = _Session(employee, server, self.clock())
            return _json(200, response, {"Mcp-Session-Id": session_id})
        if not headers.get("mcp-session-id"):
            return _rpc_error(400, -32600, "Mcp-Session-Id required")
        held = self._session(headers, employee)
        if held is None:
            return _json(404, {"error": {"code": "session_not_found"}})
        held.server.rebind(session)
        response = held.server.handle(message)
        outbound, held.server.outbound[:] = list(held.server.outbound), []
        if not outbound:
            return _json(200, response) if response is not None else (202, {}, b"")
        events = b"".join(b"event: message\ndata: " + json.dumps(
            item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
            for item in outbound + ([response] if response is not None else []))
        return 200, {"Content-Type": "text/event-stream", "Cache-Control": "no-store"}, events

    def close(self) -> None:
        while self.sessions:
            self._close(next(iter(self.sessions)))


class _Handler(BaseHTTPRequestHandler):
    server_version = "plx-workbench-mcp"
    sys_version = ""

    def _serve(self) -> None:
        host: McpHttpHost = self.server.mcp_host   # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= mcp_server.MAX_LINE_BYTES:
            status, headers, body = _rpc_error(413, -32600, "Request too large")
        else:
            raw = self.rfile.read(length) if length else b""
            try:
                status, headers, body = host.respond(self.command, self.path,
                                                     dict(self.headers.items()), raw)
            except Exception:  # noqa: BLE001 - never a stack trace to the client
                status, headers, body = _rpc_error(500, -32603, "Internal error")
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _serve

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return   # no request line (it may carry a session id) in any log


def loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def make_server(host: McpHttpHost, address: str, port: int) -> HTTPServer:
    if not loopback(address):
        raise ValueError("mcp_http_host_binds_loopback_only")
    server = HTTPServer((address, port), _Handler)
    server.mcp_host = host   # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None, *, directory: Callable[[Any], Any] | None = None,
         jwks: Any = None, ready: Callable[[HTTPServer], None] | None = None) -> int:
    """``directory``: the Entra ``DirectoryProvider`` for the resolver (``operating.host``
    builds it when configured); ``jwks`` (tests): injected signing keys instead of the
    resource file's ``jwks_file``; ``ready``: called with the bound server before it
    serves (tests stop it with ``shutdown``)."""
    from .employee_permissions import (EmployeePermissionPolicy, EmployeePermissionResolver,
                                       PortalAccessSnapshot)

    parser = argparse.ArgumentParser(prog="python3 -m operating.mcp_http_host",
                                     description=__doc__.splitlines()[0])
    mcp_server.add_host_arguments(parser)
    parser.add_argument("--entra-resource", type=Path, required=True,
                        help="plx-entra-resource/v1 JSON: Entra tenant, the Workbench API's "
                             "audiences, required delegated scope, canonical resource URI "
                             "and the JWKS file")
    parser.add_argument("--employee-policy", type=Path, required=True,
                        help="plx-employee-permission-policy/v1 JSON (PLX-confirmed)")
    parser.add_argument("--portal-access", type=Path,
                        help="plx-portal-access-snapshot/v1 JSON, re-read on every request; "
                             "without it every employee is refused (portal_access_unavailable)")
    parser.add_argument("--bind", default="127.0.0.1", help="loopback address only")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allowed-origin", action="append", default=[],
                        help="browser Origin allowed to call /mcp (default none)")
    args = parser.parse_args(argv)
    if not loopback(args.bind):
        parser.error("--bind must be a loopback address (production hosting is Tailscale Funnel, "
                     "see docs/decisions/2026-10-08-employee-host-hosting.md)")
    capture_bounds = mcp_server.check_host_arguments(parser, args)
    try:
        policy = EmployeePermissionPolicy.from_file(args.employee_policy)
        resource = EntraResource.from_file(args.entra_resource, jwks=jwks)
    except (ValueError, OSError, UnicodeDecodeError) as error:
        parser.error("identity configuration is invalid: " +
                     (str(error) if isinstance(error, ValueError) else type(error).__name__))
    portal_access: Callable[[Any], Any] = lambda principal: None  # noqa: E731
    if args.portal_access is not None:
        try:
            portal_access = PortalAccessSnapshot(args.portal_access)
        except (ValueError, OSError, UnicodeDecodeError) as error:
            parser.error("portal access snapshot is invalid: " +
                         (str(error) if isinstance(error, ValueError) else type(error).__name__))
    resolver = EmployeePermissionResolver(policy, portal_access, directory=directory)
    services = SessionHost(parser, args, tenant=policy.tenant, resolver=resolver,
                           fixture=False, dev=False, capture_bounds=capture_bounds)
    try:
        host = McpHttpHost(services, resource, resolver,
                           session_identity.replay_guard_for(args.state_dir.resolve()),
                           allowed_origins=frozenset(args.allowed_origin))
    except ValueError as error:
        services.close()
        parser.error(str(error))
    server = make_server(host, args.bind, args.port)
    try:
        if ready is not None:
            ready(server)
        else:
            print(json.dumps({"mcp": f"http://{args.bind}:{server.server_address[1]}"
                                     f"{ENDPOINT}"}), file=sys.stderr, flush=True)
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        host.close()
        services.close()
    return 0


__all__ = ["ENDPOINT", "METADATA_PATH", "McpHttpHost", "loopback", "main", "make_server"]


if __name__ == "__main__":
    raise SystemExit(main())
