"""Remote MCP host: Streamable HTTP for many employees, each request an Entra-signed-in one.

The host serves the
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
  production host runs, and its TLS, is decided: Tailscale Funnel;
  nothing is deployed.
* An ``Origin`` header (a browser) must be one of ``--allowed-origin`` (DNS rebinding).

Connections are read concurrently (SEC-003): each accepted connection gets its own
thread (at most ``MAX_CONNECTIONS``; one more is answered 503 and closed), which reads the
request under the whole-request deadline. A fully read request is then queued (at most
``MAX_QUEUED`` waiting; one more is answered 503) to the one execution worker, the thread
that called ``serve_forever``: authentication and every tool call run there, so the case
runtime's SQLite stores stay on the thread that opened them, and a slow or stalled client
holds only its own connection thread, never the worker.

Evidence: isolated-fixture (tokens signed in the tests with a generated key, JWKS
injected); live-unverified (no Workbench Entra app registration, no
GroupMember.Read.All).
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import sys
import threading
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
# One employee's reconnects replace that employee's own oldest session; they never evict
# another employee's (a full host refuses a new session instead).
MAX_SESSIONS_PER_EMPLOYEE = 4
# A request's headers and body must arrive within this many seconds in all: a stalled or
# dripping client is closed, not waited on (it holds only its own connection thread).
REQUEST_READ_SECONDS = 10
# Connections read at once (one thread each); one more is answered 503 and closed.
MAX_CONNECTIONS = 32
# Fully read requests waiting for the execution worker; one more is answered 503.
MAX_QUEUED = 16
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
            own = [k for k, v in self.sessions.items() if v.employee == employee]
            for session_id in own[:max(0, len(own) - MAX_SESSIONS_PER_EMPLOYEE + 1)]:
                self._close(session_id)
            if len(self.sessions) >= MAX_SESSIONS:
                server.close()
                return _json(503, {"error": {"code": "sessions_full"}}, {"Retry-After": "60"})
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


class _DeadlineReader(io.RawIOBase):
    """The request side of a connection: every receive waits at most until the handler's
    deadline, so a request that arrives a byte at a time still ends at the deadline."""

    def __init__(self, handler: "_Handler") -> None:
        self.handler = handler

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        left = self.handler.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("mcp_request_deadline")
        self.handler.connection.settimeout(left)
        return self.handler.connection.recv_into(buffer)


class _Handler(BaseHTTPRequestHandler):
    server_version = "plx-workbench-mcp"
    sys_version = ""
    deadline = 0.0

    def setup(self) -> None:
        super().setup()
        self.rfile = io.BufferedReader(_DeadlineReader(self))

    def handle_one_request(self) -> None:
        # One deadline per request, headers and body together: a read past it raises a
        # timeout, which ``handle_one_request`` turns into a closed connection (before
        # any authentication). The idle wait for a kept-alive request counts too.
        self.deadline = time.monotonic() + REQUEST_READ_SECONDS
        super().handle_one_request()

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
            if len(raw) != length:   # the client went away mid-body: nothing to answer
                self.close_connection = True
                return
            status, headers, body = self.server.execute(   # type: ignore[attr-defined]
                host.respond, self.command, self.path, dict(self.headers.items()), raw)
        self.connection.settimeout(REQUEST_READ_SECONDS)   # the reply's own bound
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


_BUSY = (b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\n"
         b"Connection: close\r\n\r\n")


class _Job:
    """One read request for the execution worker: run once, or cancelled before it starts
    (a stopping server), never both."""

    __slots__ = ("call", "done", "lock", "reply", "started", "cancelled")

    def __init__(self, call: Callable[[], Reply]) -> None:
        self.call, self.done, self.lock = call, threading.Event(), threading.Lock()
        self.reply: Reply | None = None
        self.started = self.cancelled = False


class _Server(ThreadingHTTPServer):
    """Concurrent reads, one execution worker (see the module notes, SEC-003).

    ``serve_forever`` runs the accept loop on a helper thread and the execution worker on
    the calling thread until ``shutdown``; a request not yet started then is answered 503."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._jobs: queue.Queue[_Job] = queue.Queue(MAX_QUEUED)
        self._stopped = threading.Event()

    # ---- connections -----------------------------------------------------------------

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:   # too many connections at once: refused at once, never left unread
                request.settimeout(1)
                request.sendall(_BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    # ---- execution -------------------------------------------------------------------

    def execute(self, respond: Callable[..., Reply], *args: Any) -> Reply:
        """``respond(*args)`` on the execution worker: 503 when the queue is full or the
        server stops before it starts, 500 (no stack trace) when it raises."""
        def call() -> Reply:
            try:
                return respond(*args)
            except Exception:  # noqa: BLE001 - never a stack trace to the client
                return _rpc_error(500, -32603, "Internal error")

        busy = _json(503, {"error": {"code": "busy"}}, {"Retry-After": "5"})
        job = _Job(call)
        if self._stopped.is_set():
            return busy
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            return busy
        while not job.done.wait(0.25):
            if self._stopped.is_set():
                with job.lock:
                    if not job.started:
                        job.cancelled = True
                        return busy
        return job.reply if job.reply is not None else busy

    def _work(self) -> None:
        while not self._stopped.is_set():
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            with job.lock:
                if job.cancelled or self._stopped.is_set():
                    job.cancelled = True
                    job.done.set()
                    continue
                job.started = True
            try:
                job.reply = job.call()
            finally:
                job.done.set()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        def accept() -> None:
            try:
                super(_Server, self).serve_forever(poll_interval)
            finally:
                self._stopped.set()

        acceptor = threading.Thread(target=accept, name="mcp-http-accept", daemon=True)
        acceptor.start()
        try:
            self._work()
        finally:
            self._stopped.set()
            if acceptor.is_alive():   # the worker failed: stop accepting too
                self.shutdown()
            acceptor.join(timeout=5)


def make_server(host: McpHttpHost, address: str, port: int) -> HTTPServer:
    if not loopback(address):
        raise ValueError("mcp_http_host_binds_loopback_only")
    server = _Server((address, port))
    server.mcp_host = host   # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None, *, directory: Callable[[Any], Any] | None = None,
         jwks: Any = None, ready: Callable[[HTTPServer], None] | None = None) -> int:
    """``directory``: the Entra ``DirectoryProvider`` for the resolver (``operating.host``
    builds it when configured); ``jwks`` (tests): injected signing keys instead of the
    resource file's ``jwks_file``; ``ready``: called with the bound server before it
    serves (tests stop it with ``shutdown``)."""
    from .employee_permissions import (EmployeePermissionResolver, PolicyFile,
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
    parser.add_argument("--portal-access-max-age-hours", type=float, default=None,
                        help="refuse the Portal access snapshot (every employee denied) "
                             "once its read_at is older than this many hours (default 168: "
                             "every snapshot expires, SEC-004)")
    parser.add_argument("--bind", default="127.0.0.1", help="loopback address only")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allowed-origin", action="append", default=[],
                        help="browser Origin allowed to call /mcp (default none)")
    args = parser.parse_args(argv)
    if not loopback(args.bind):
        parser.error("--bind must be a loopback address (production hosting is Tailscale Funnel, "
                     "see devloop/DECISIONS/2026-10-08-employee-host-hosting.md)")
    capture_bounds = mcp_server.check_host_arguments(parser, args)
    try:
        # re-read when the file changes: an edit applies on the next request without a
        # restart; an edit that does not load keeps the last good policy and raises the
        # host alarm (SEC-005)
        policy_file = PolicyFile(args.employee_policy)
        policy = policy_file.policy
        resource = EntraResource.from_file(args.entra_resource, jwks=jwks)
    except (ValueError, OSError, UnicodeDecodeError) as error:
        parser.error("identity configuration is invalid: " +
                     (str(error) if isinstance(error, ValueError) else type(error).__name__))
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
