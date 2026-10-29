"""Who is asking: every API request but sign-in configuration resolves one reader, or 401.

Modes (one per server, chosen by explicit flags; there is no default principal):

* ``entra`` (production): the front end signs the employee in with Microsoft (MSAL, SPA
  authorization code + PKCE against the committed app registration) and sends the Entra
  access token as ``Authorization: Bearer``. The API is a resource server exactly like the
  remote MCP host and the console (``operating.oidc_auth.verify_access_token``: JWKS
  signature, tenant, issuer, audience, scope, lifetime; then ``entra_session`` on the
  ``console`` channel: the state directory's revocation guard and the employee resolver,
  Portal role ∩ Entra groups, re-resolved on every request). The API mints no token and
  makes no call to Entra (a token exchange would be a POST to a business identity system).
* ``dev`` (demo only, ``--dev``, until the Entra app registration has the app's redirect
  URI): a configured test identity. Over live state it is a console session ticket the
  host owner minted for one employee (``python3 -m operator_ui.console_ticket mint``),
  presented once at POST /api/auth/dev-login and re-verified on every request exactly as
  the live console does; over a dev state an employee object id the employee resolver
  resolves on every request (``PROOF_CONFIGURED``; never live state); over a fixture
  state fixture principals. A person still has to hold a session: dev-login sets an
  HttpOnly cookie for a random server-side session; without it every route is 401. The
  server refuses to bind anything but loopback or a Tailscale address in this mode.
* ``mock`` (``--mock``): dev mode over the fixture payloads (``operator_api.mock``).

Names of tokens, cookies and claims are never logged or returned.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import ipaddress
import secrets
import threading
import time
from typing import Any, Callable, Mapping, Optional

from operating import session_identity
from operating.session_identity import SessionIdentity, SessionIdentityRefused

SESSION_COOKIE = "plx_app_session"
DEV_SESSION_SECONDS = 8 * 60 * 60
MAX_DEV_SESSIONS = 256
# Tailscale's addresses: the CGNAT range it assigns and its IPv6 ULA prefix.
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"),
                  ipaddress.ip_network("fd7a:115c:a1e0::/48"))


class AuthRequired(Exception):
    """No reader for this request. ``code`` is safe to return; ``status`` 401 or 403."""

    def __init__(self, code: str, status: int = 401) -> None:
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Reader:
    session: SessionIdentity
    mode: str
    display_name: str
    email: Optional[str] = None

    @property
    def key(self) -> str:
        """Stable per-person key for this reader's own stores (recent questions)."""
        return ",".join(sorted(self.session.principals))


def loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def tailscale(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in TAILSCALE_NETS)


def check_bind(address: str, mode: str) -> None:
    """Loopback only for ``entra`` (Caddy terminates TLS in front); loopback or a Tailscale
    address for ``dev``/``mock``. Anything else (0.0.0.0, a public or LAN address, a name)
    is refused with ``ValueError``."""
    if loopback(address):
        return
    if mode in ("dev", "mock") and tailscale(address):
        return
    raise ValueError(f"operator_api_{mode}_mode_refuses_bind:{address}"[:120])


def _display(session: SessionIdentity, claims: Mapping[str, Any] | None = None) -> str:
    name = (claims or {}).get("name")
    if isinstance(name, str) and 0 < len(name.strip()) <= 120:
        return name.strip()
    if session.email:
        return session.email.split("@", 1)[0].replace(".", " ").title()
    if session.employee is not None:
        return "Employee"
    return "Demo reader"


class EntraAuth:
    mode = "entra"

    def __init__(self, resource: Any, resolver: Any, replay_guard: Any, *,
                 clock: Callable[[], float] = time.time) -> None:
        if resource.tenant_id != resolver.policy.entra_tenant_id:
            raise ValueError("entra_resource_tenant_must_equal_the_policy_tenant")
        self.resource, self.resolver, self.replay_guard = resource, resolver, replay_guard
        self.clock = clock

    def authenticate(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> Reader:
        from operating import oidc_auth

        scheme, _, token = (headers.get("authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthRequired("sign_in_required")
        now = int(self.clock())
        try:
            claims = oidc_auth.verify_access_token(token.strip(), self.resource, now=now)
            session = oidc_auth.entra_session(
                self.resolver, claims, replay_guard=self.replay_guard, now=now,
                channel=session_identity.CHANNEL_CONSOLE, client="plx-workbench-app")
        except oidc_auth.EntraTokenRefused as refusal:
            raise AuthRequired("sign_in_required" if refusal.error == "invalid_token"
                               else "insufficient_scope",
                               401 if refusal.error == "invalid_token" else 403) from None
        except SessionIdentityRefused:
            raise AuthRequired("no_workbench_access", 403) from None
        email = claims.get("preferred_username") or claims.get("upn")
        return Reader(session, self.mode, _display(session, claims),
                      email if isinstance(email, str) and "@" in email else None)

    def config(self) -> dict[str, Any]:
        return {"mode": self.mode, "dev_login": False}


class DevAuth:
    """Demo sign-in behind an explicit flag (module doc). ``identity`` resolves the reader
    of a dev session on every request (an employee re-resolves through the resolver, so a
    revocation in the Portal snapshot applies at once). ``ticket_identity``: instead, the
    person presents a console session ticket at dev-login (``operator_ui.console_ticket
    mint``, the live console's own credential) and each request re-verifies it
    (``session_identity.employee_session``): the only dev identity a live state accepts."""

    def __init__(self, identity: Callable[[], SessionIdentity] | None = None, *,
                 ticket_identity: Callable[[str], SessionIdentity] | None = None,
                 mode: str = "dev", clock: Callable[[], float] = time.time) -> None:
        if mode not in ("dev", "mock"):
            raise ValueError("dev_auth_mode_invalid")
        if (identity is None) == (ticket_identity is None):
            raise ValueError("dev_auth_needs_exactly_one_identity")
        self.mode, self.identity, self.ticket_identity = mode, identity, ticket_identity
        self.clock = clock
        self._sessions: dict[str, tuple[float, Optional[str]]] = {}
        self._lock = threading.Lock()

    @property
    def needs_ticket(self) -> bool:
        return self.ticket_identity is not None

    def login(self, ticket: Optional[str] = None) -> tuple[str, Reader]:
        """A new dev session's cookie value and its reader (``AuthRequired`` if refused)."""
        if self.needs_ticket != bool(ticket):
            raise AuthRequired("dev_ticket_required" if self.needs_ticket
                               else "dev_ticket_not_accepted", 400)
        reader = self._reader(ticket)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            for key in [k for k, (exp, _) in self._sessions.items() if exp <= now]:
                del self._sessions[key]
            while len(self._sessions) >= MAX_DEV_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            expires = now + DEV_SESSION_SECONDS
            if reader.session.expires_at is not None:
                expires = min(expires, float(reader.session.expires_at))
            self._sessions[token] = (expires, ticket)
        return token, reader

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _held(self, token: str) -> tuple[bool, Optional[str]]:
        now = self.clock()
        with self._lock:
            for key, (exp, ticket) in list(self._sessions.items()):
                if hmac.compare_digest(key, token):
                    if exp > now:
                        return True, ticket
                    del self._sessions[key]
        return False, None

    def _reader(self, ticket: Optional[str]) -> Reader:
        try:
            session = (self.ticket_identity(ticket) if self.ticket_identity is not None
                       else self.identity())
        except SessionIdentityRefused:
            raise AuthRequired("no_workbench_access", 403) from None
        return Reader(session, self.mode, _display(session), session.email)

    def authenticate(self, headers: Mapping[str, str], cookies: Mapping[str, str]) -> Reader:
        token = cookies.get(SESSION_COOKIE)
        if not token or len(token) > 100:
            raise AuthRequired("sign_in_required")
        held, ticket = self._held(token)
        if not held:
            raise AuthRequired("sign_in_required")
        return self._reader(ticket)

    def config(self) -> dict[str, Any]:
        return {"mode": self.mode, "dev_login": True}


def ticket_identity(resolver: Any, secret: Any, replay_guard: Any
                    ) -> Callable[[str], SessionIdentity]:
    """A console session ticket verified as the live console verifies it, per request."""
    def identity(ticket: str) -> SessionIdentity:
        if secret is None:
            raise SessionIdentityRefused("session_secret_unavailable")
        return session_identity.employee_session(
            resolver, channel=session_identity.CHANNEL_CONSOLE, ticket=ticket,
            secret=secret, replay_guard=replay_guard, client="plx-workbench-app")
    return identity


def configured_employee(resolver: Any, object_id: str) -> Callable[[], SessionIdentity]:
    """The dev identity of one employee: resolved by the employee resolver on each call
    exactly as a signed-in employee is (Portal role ∩ Entra groups), proof
    ``configured_employee`` so every record says it was not a sign-in. Never over live
    state (the caller binds the state directory as ``dev``,
    ``session_identity.bind_state_dir``): live state takes ``ticket_identity``."""
    from operating.employee_permissions import EmployeePrincipal

    def identity() -> SessionIdentity:
        try:
            principal = EmployeePrincipal(resolver.policy.entra_tenant_id, object_id)
        except ValueError:
            raise SessionIdentityRefused("employee_principal_invalid") from None
        entitlement = resolver.resolve(principal)
        if entitlement.actor is None:
            raise SessionIdentityRefused(entitlement.denied_reason or "denied")
        return SessionIdentity(
            tenant=resolver.policy.tenant, channel=session_identity.CHANNEL_CONSOLE,
            author_kind=session_identity.AUTHOR_HUMAN,
            proof=session_identity.PROOF_CONFIGURED, employee=principal,
            principals=frozenset({principal.principal}), client="plx-workbench-app-dev",
            identities=frozenset({i for i in entitlement.source_identities
                                  if isinstance(i, str) and i.strip()
                                  and not i.startswith("group:")} | {principal.object_id}))
    identity()   # refused now: the server does not start with an identity nobody grants
    return identity


def fixture_identity(tenant: str, principals: list[str], *,
                     clearance: str = "internal") -> Callable[[], SessionIdentity]:
    session = session_identity.fixture_session(
        tenant, principals, channel=session_identity.CHANNEL_CONSOLE, clearance=clearance,
        client="plx-workbench-app-dev")
    return lambda: session
