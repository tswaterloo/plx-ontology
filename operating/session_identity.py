"""Session identity: every session that reads live data is one employee.

10-DECISION #7: no live data without a real employee identity (the MCP host's
``eng`` fallback is gone). A session host (``operating.mcp_server``,
``operator_ui.case_console``) binds each session to exactly one
``SessionIdentity`` and builds its case runtime with ``require_employee=True``,
so ``EmployeeActorGate`` refuses, before any read, an actor that carries no
employee. What the employee may read is decided, on every call, by
``operating.employee_permissions`` (Portal role ∩ Entra groups ∩ SharePoint
item ACL); this module only says *who* the session is.

Where the identity comes from:

* **Console** (``channel="console"``, author ``human``): from the
  authenticated request. Every ``/api/*`` request carries a signed session
  ticket as its bearer credential; the console verifies it (MAC, expiry,
  audience, author) and re-resolves the employee on every request. A console
  configured with an Entra resource (``--entra-resource``) also accepts the
  employee's own Entra access token (proof ``entra_access_token``: signed in with
  their PLX Microsoft account), verified per request by ``operating.oidc_auth`` and
  resolved the same way.
* **MCP** (``channel="mcp"``, author ``agent``): from a signed start-up ticket
  whose author is ``agent``, single use (consumed in the state directory's durable
  ``replay_guard_for``); the resolver must find an active, identity-verified Portal
  employee row carrying that Entra tenant and object id, at startup and on every
  call. An employee named by Entra object id on the command line authenticates
  nobody: the host accepts it only as a development session (state labelled
  ``dev``, no real source reader).
* **Remote MCP** (``channel="mcp"``, author ``agent``, proof ``entra_access_token``):
  ``operating.mcp_http_host`` verifies an Entra access token on every request
  (``operating.oidc_auth``) and resolves its tenant and object id the same way.

A free-text ``--principal`` list is a fixture/demo identity only:
``fixture_session`` builds it, and hosts accept it only with their explicit
fixture flag over fixture state (never with a real source reader).

Author kind: a console session's text is authored by a person; an MCP
session's text is authored by an agent acting for the employee (the agent is
never a principal: its reads are the employee's, narrowed). Downstream code
(``operating.injection_guard``, the case objective) reads ``author_kind`` from
the session to tell who wrote a piece of text.

Ticket wire format, mirrored from the Portal's signed upload ticket
(portal/src/lib/attachments/upload-ticket.ts at plx-customer-portal@c3ffda3):
``v1.<base64url(JSON claims)>.<base64url(HMAC-SHA256)>``; the MAC covers the
literal ``v1.<payload>`` and its key is derived from the secret with a
domain-separation label; verification runs structure -> MAC -> decode ->
schema -> expiry, and never parses untrusted JSON before the MAC passes. The
claims are the identity part of the Portal's NextAuth session user
(portal/src/lib/auth/auth.ts ``session`` callback: ``id``, ``email``,
``entraTenantId``, ``entraObjectId``); policy grants, denies and elevations
stay out of it, as the Portal keeps them out of its JWT
(portal/src/lib/auth/auth-claims.ts). The secret comes from
``PLX_WORKBENCH_SESSION_SECRET`` with no fallback and no default: absent, no
ticket verifies (fail closed). A ticket lives at most 2 hours (as the Portal's
upload ticket) and carries a ``jti``; ``TicketReplayGuard`` refuses a replayed or
revoked ``jti`` within its TTL, durably (it survives a restart). The Portal does not
mint these tickets today (devloop/PERMISSIONS.md blocker 1; devloop/OPEN-DECISIONS.md).

Subject and actor of an intent (``intent_identity``, ``plx-intent-identity/v2``,
devloop/DECISIONS/2026-10-04-decider-subject.md and 2026-10-04-goal2-identity.md):
every goal-2 intent, every later step on it and every workbench-internal action
records RFC 8693-style ``sub`` (the employee the session is, with that employee's
own identities: Entra object id and Portal email, and ``oid``) and ``act`` (who
acted for them: the session's author kind, channel, ``client`` host id and
``session``, the SHA-256 of the ticket's ``jti`` or of a per-session host nonce).
Both come only from this verified session object, never from tool arguments or
source text. ``act`` is attribution only: it is never a principal and no ACL reads
it (the session's ``actor`` carries the employee's principals alone). The action
path refuses an MC decision whose decider is a subject, and one for an intent whose
subject is unknown.

Pure and stdlib-only: no network, no clock except an injectable ``now``.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any, Iterable, Iterator, Mapping

from ontology.core import LEVELS, Actor

from .employee_permissions import (EMPLOYEE_PREFIX, EmployeeAccessRefused,
                                   EmployeePermissionResolver, EmployeePrincipal)
from .entitlements import _AGENT_PREFIXES

SESSION_TICKET_VERSION = "v1"
SESSION_TICKET_SECRET_ENV = "PLX_WORKBENCH_SESSION_SECRET"
SESSION_TICKET_AUDIENCE = "plx-workbench"
# The Portal's upload ticket caps at 2h too (upload-ticket.ts UPLOAD_TICKET_TTL_SECONDS).
SESSION_TICKET_TTL_SECONDS = 2 * 60 * 60
SESSION_TICKET_MAX_LENGTH = 4096
SESSION_TICKET_CLOCK_SKEW_SECONDS = 60
MIN_SECRET_BYTES = 32
_KEY_DERIVATION_LABEL = b"plx-workbench-session-v1"
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")

AUTHOR_HUMAN = "human"
AUTHOR_AGENT = "agent"
CHANNEL_CONSOLE = "console"
CHANNEL_MCP = "mcp"
# Who authors text in a session is decided by the channel, never by a claim.
CHANNEL_AUTHOR = {CHANNEL_CONSOLE: AUTHOR_HUMAN, CHANNEL_MCP: AUTHOR_AGENT}

PROOF_TICKET = "signed_session_ticket"
PROOF_CONFIGURED = "configured_employee"
PROOF_FIXTURE = "fixture"
# An Entra access token verified per request by the remote MCP host (operating.oidc_auth).
PROOF_ENTRA = "entra_access_token"
# The declared eval principal (operating.eval_principal): a signed ticket for a sentinel
# identity that is no person, labelled as such in every intent and result.
PROOF_EVAL = "eval_principal"
_EXPIRING = (PROOF_TICKET, PROOF_ENTRA, PROOF_EVAL)

_CLAIMS = frozenset({"v", "aud", "uid", "email", "entraTenantId", "entraObjectId",
                     "author", "iat", "exp", "jti"})
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_REPLAY_ENTRIES = 10_000
INTENT_IDENTITY_SCHEMA = "plx-intent-identity/v2"
# The host a session runs in (``act.client``), when the host declares none.
DEFAULT_CLIENT = {CHANNEL_CONSOLE: "plx-workbench-console", CHANNEL_MCP: "plx-workbench-mcp"}
_CLIENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SUBJECT_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")


class SessionIdentityRefused(EmployeeAccessRefused):
    """No session identity; ``reason`` is a code, safe to log (no ticket, no identity)."""


# ------------------------------------------------------------------ tickets

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _secret_bytes(secret: Any) -> bytes:
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(raw, bytes) or len(raw.strip()) < MIN_SECRET_BYTES:
        raise SessionIdentityRefused("session_secret_unavailable")
    return raw.strip()


def _sign(secret: Any, signed_input: str) -> bytes:
    key = hmac.new(_secret_bytes(secret), _KEY_DERIVATION_LABEL, hashlib.sha256).digest()
    return hmac.new(key, signed_input.encode("ascii"), hashlib.sha256).digest()


def session_secret(environ: Mapping[str, str]) -> str | None:
    """``PLX_WORKBENCH_SESSION_SECRET`` or None: no fallback, no dev default."""
    value = environ.get(SESSION_TICKET_SECRET_ENV)
    return value.strip() if isinstance(value, str) and value.strip() else None


def mint_session_ticket(*, uid: str, email: str, tenant_id: str, object_id: str,
                        author: str, secret: Any, now: int | None = None,
                        ttl_seconds: int = SESSION_TICKET_TTL_SECONDS,
                        jti: str | None = None) -> str:
    """The ticket the Portal would mint for its signed-in user (format reference)."""
    if author not in (AUTHOR_HUMAN, AUTHOR_AGENT):
        raise ValueError("session_ticket_author_invalid")
    if not 0 < ttl_seconds <= SESSION_TICKET_TTL_SECONDS:
        raise ValueError("session_ticket_ttl_invalid")
    jti = secrets.token_urlsafe(16) if jti is None else jti
    if not isinstance(jti, str) or not _JTI.match(jti):
        raise ValueError("session_ticket_jti_invalid")
    iat = int(time.time()) if now is None else int(now)
    claims = {"v": 1, "jti": jti, "aud": SESSION_TICKET_AUDIENCE, "uid": uid, "email": email,
              "entraTenantId": tenant_id, "entraObjectId": object_id, "author": author,
              "iat": iat, "exp": iat + ttl_seconds}
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(_sign(secret, f"{SESSION_TICKET_VERSION}.{payload}"))
    return f"{SESSION_TICKET_VERSION}.{payload}.{signature}"


def verify_session_ticket(ticket: Any, secret: Any, *, now: int | None = None) -> dict[str, Any]:
    """Claims of a valid ticket, else ``SessionIdentityRefused``.

    Order as in upload-ticket.ts: structure -> MAC -> decode -> schema ->
    expiry. Malformed and forged tickets share one code (no oracle).
    """
    malformed = SessionIdentityRefused("session_ticket_invalid")
    if not isinstance(ticket, str) or not ticket or len(ticket) > SESSION_TICKET_MAX_LENGTH:
        raise malformed
    parts = ticket.split(".")
    if (len(parts) != 3 or parts[0] != SESSION_TICKET_VERSION
            or not _BASE64URL.match(parts[1]) or not _BASE64URL.match(parts[2])):
        raise malformed
    expected = _sign(secret, f"{parts[0]}.{parts[1]}")
    try:
        given = _unb64(parts[2])
    except ValueError:
        raise malformed from None
    if not hmac.compare_digest(given, expected):
        raise malformed
    try:
        claims = json.loads(_unb64(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise malformed from None
    if not isinstance(claims, dict) or set(claims) != _CLAIMS or claims["v"] != 1:
        raise malformed
    for name in ("uid", "email", "entraTenantId", "entraObjectId", "author", "aud", "jti"):
        if not isinstance(claims[name], str) or not claims[name].strip():
            raise malformed
    for name in ("iat", "exp"):
        if isinstance(claims[name], bool) or not isinstance(claims[name], int) \
                or claims[name] < 0:
            raise malformed
    if not _JTI.match(claims["jti"]):
        raise malformed
    if claims["aud"] != SESSION_TICKET_AUDIENCE:
        raise SessionIdentityRefused("session_ticket_audience_mismatch")
    if claims["author"] not in (AUTHOR_HUMAN, AUTHOR_AGENT):
        raise malformed
    if not 0 < claims["exp"] - claims["iat"] <= SESSION_TICKET_TTL_SECONDS:
        raise malformed
    current = int(time.time()) if now is None else int(now)
    if current >= claims["exp"]:
        raise SessionIdentityRefused("session_ticket_expired")
    if claims["iat"] > current + SESSION_TICKET_CLOCK_SKEW_SECONDS:
        raise malformed
    return claims


_REPLAY_DDL = """
CREATE TABLE IF NOT EXISTS ticket_jti (
  jti_sha256 TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('used', 'revoked')),
  exp INTEGER NOT NULL,
  PRIMARY KEY (jti_sha256, kind)
);
"""


class TicketReplayGuard:
    """Remembers ticket ids (``jti``) until their own expiry, durably at ``path``.

    ``consume`` records a jti and refuses it the second time within its TTL
    (``session_ticket_replayed``): for a host that establishes a session from
    one presentation (the MCP host's start-up ticket). ``revoke`` makes a jti
    unusable for the rest of its TTL, which a host that re-authenticates the
    bearer on every request (the console) checks with ``check``. Both are kept in
    a SQLite file (only the jti's SHA-256), so a restart within the 2 h TTL
    forgets nothing; hosts sharing the file share the guard. After the ticket's
    ``exp`` the ticket is refused as expired anyway, so entries are dropped then.
    Bounded: a full guard refuses new tickets (fail closed).
    """

    def __init__(self, path: str | Path, max_entries: int = MAX_REPLAY_ENTRIES) -> None:
        self.path = str(path)
        self._max = max_entries
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(_REPLAY_DDL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    @staticmethod
    def _key(claims: Mapping[str, Any]) -> str:
        return hashlib.sha256(str(claims["jti"]).encode("utf-8")).hexdigest()

    def _record(self, db: sqlite3.Connection, key: str, kind: str, exp: int) -> None:
        held = db.execute("SELECT COUNT(*) FROM ticket_jti WHERE kind=?", (kind,)).fetchone()[0]
        if held >= self._max:
            raise SessionIdentityRefused("session_ticket_replay_guard_full")
        db.execute("INSERT OR REPLACE INTO ticket_jti VALUES(?,?,?)", (key, kind, int(exp)))

    def check(self, claims: Mapping[str, Any], *, consume: bool = False,
              now: int | None = None) -> None:
        current = int(time.time()) if now is None else int(now)
        key = self._key(claims)
        with self._connect() as db:
            db.execute("DELETE FROM ticket_jti WHERE exp <= ?", (current,))
            found = {row[0] for row in db.execute(
                "SELECT kind FROM ticket_jti WHERE jti_sha256=?", (key,))}
            if "revoked" in found:
                raise SessionIdentityRefused("session_ticket_revoked")
            if consume:
                if "used" in found:
                    raise SessionIdentityRefused("session_ticket_replayed")
                self._record(db, key, "used", claims["exp"])

    def revoke(self, claims: Mapping[str, Any]) -> None:
        with self._connect() as db:
            self._record(db, self._key(claims), "revoked", claims["exp"])


REPLAY_GUARD_FILE = "session-replay.sqlite"


def replay_guard_for(state_dir: str | Path) -> TicketReplayGuard:
    """The one durable replay guard of a host's state directory: every host over the same
    state (the MCP start-up, the console) shares it, so a ticket consumed or revoked by
    one is refused by the others, across restarts within its TTL."""
    return TicketReplayGuard(Path(state_dir) / REPLAY_GUARD_FILE)


# ------------------------------------------------------------------ session

@dataclass(frozen=True)
class SessionIdentity:
    """Who one session is, how that was proven, and who authors its text.

    ``actor`` is what the session presents to the case runtime: for an
    employee exactly ``employee:<oid>``, which the runtime's gate replaces on
    every call with what the employee is entitled to now; for a fixture
    session the fixture principals (accepted only by a runtime that does not
    require an employee).
    """

    tenant: str
    channel: str
    author_kind: str
    proof: str
    employee: EmployeePrincipal | None
    principals: frozenset[str]
    clearance: str = "internal"
    portal_user_id: str | None = None
    email: str | None = None
    expires_at: int | None = None
    # The subject employee's own identities (lower case): for an employee the Entra
    # object id and the Portal email the resolver matched (plus a ticket's email);
    # for a fixture session only a host-configured fixture email (and Entra object id,
    # ``subject_oid``), else none (unknown).
    identities: frozenset[str] = frozenset()
    subject_oid: str | None = None
    # ``act.client``: the host this session runs in (default per channel, DEFAULT_CLIENT).
    client: str | None = None
    # ``act.session``: SHA-256 of the ticket's jti, else of a nonce drawn per session.
    session_ref: str | None = None

    def __post_init__(self) -> None:
        if self.channel not in CHANNEL_AUTHOR:
            raise ValueError("session_channel_invalid")
        if self.author_kind != CHANNEL_AUTHOR[self.channel]:
            raise ValueError("session_author_kind_must_match_channel")
        if self.proof not in (*_EXPIRING, PROOF_CONFIGURED, PROOF_FIXTURE):
            raise ValueError("session_proof_invalid")
        if self.clearance not in LEVELS:
            raise ValueError("session_clearance_invalid")
        object.__setattr__(self, "principals", frozenset(self.principals))
        if self.proof == PROOF_FIXTURE:
            if self.employee is not None:
                raise ValueError("fixture_session_has_no_employee")
        else:
            if not isinstance(self.employee, EmployeePrincipal) \
                    or self.employee.kind != "employee":
                raise ValueError("employee_session_requires_employee")
            if self.principals != {self.employee.principal}:
                raise ValueError("employee_session_principal_must_be_the_employee")
        if (self.proof in _EXPIRING) != (self.expires_at is not None):
            raise ValueError("session_expiry_only_for_tickets")
        if any(not isinstance(i, str) or not i.strip() for i in self.identities):
            raise ValueError("session_identities_invalid")
        held = frozenset(i.strip().lower() for i in self.identities)
        if any(i.startswith(("group:", EMPLOYEE_PREFIX, *_AGENT_PREFIXES)) for i in held):
            raise ValueError("session_identities_invalid")
        if self.employee is not None and self.employee.object_id not in held:
            raise ValueError("employee_session_identities_must_name_the_employee")
        oid = self.employee.object_id if self.employee is not None else self.subject_oid
        if self.subject_oid is not None and (oid != self.subject_oid or not isinstance(
                oid, str) or not _GUID.fullmatch(oid) or oid not in held):
            raise ValueError("session_subject_oid_invalid")
        object.__setattr__(self, "subject_oid", oid)
        object.__setattr__(self, "identities", held)
        client = DEFAULT_CLIENT[self.channel] if self.client is None else self.client
        if not isinstance(client, str) or not _CLIENT.fullmatch(client):
            raise ValueError("session_client_invalid")
        object.__setattr__(self, "client", client)
        ref = (hashlib.sha256(secrets.token_bytes(32)).hexdigest()
               if self.session_ref is None else self.session_ref)
        if not isinstance(ref, str) or not _SHA256.fullmatch(ref):
            raise ValueError("session_ref_invalid")
        object.__setattr__(self, "session_ref", ref)
        Actor(self.tenant, self.principals, self.clearance)  # validates

    @property
    def fixture(self) -> bool:
        return self.proof == PROOF_FIXTURE

    @property
    def eval_principal(self) -> bool:
        return self.proof == PROOF_EVAL

    @property
    def actor(self) -> Actor:
        return Actor(self.tenant, self.principals, self.clearance)

    def expired(self, now: int | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (int(time.time()) if now is None else int(now)) >= self.expires_at

    def intent_identity(self, actor: Actor) -> dict[str, Any]:
        """``sub``/``act`` for an intent or action recorded by ``actor`` in this session.

        ``actor`` must be exactly this session's actor and the session current, so a
        caller cannot attach this session's subject to another actor's record. ``sub``
        is None when the session names no subject identity (a fixture session without
        a configured fixture email): downstream that is an unknown subject, which no
        decision can authorize. ``act`` names this session (kind, channel, client host,
        session digest) and is never a principal.
        """
        if self.expired():
            raise SessionIdentityRefused("session_expired")
        if actor != self.actor:
            raise SessionIdentityRefused("session_actor_mismatch")
        sub = None if not self.identities else {
            "principal": None if self.employee is None else self.employee.principal,
            "kind": "fixture" if self.fixture else PROOF_EVAL if self.eval_principal
            else "employee", "proof": self.proof,
            "identities": sorted(self.identities), "oid": self.subject_oid}
        return {"schema": INTENT_IDENTITY_SCHEMA, "sub": sub,
                "act": [{"kind": self.author_kind, "channel": self.channel,
                         "client": self.client, "session": self.session_ref}]}

    def describe(self) -> dict[str, Any]:
        """For the session endpoint and audit records: no ticket, no secret."""
        return {"channel": self.channel, "author_kind": self.author_kind,
                "proof": self.proof, "fixture": self.fixture,
                "eval_principal": self.eval_principal,
                "employee": None if self.employee is None else self.employee.principal,
                "expires_at": self.expires_at}


def employee_session(resolver: EmployeePermissionResolver, *, channel: str,
                     object_id: str | None = None, ticket: str | None = None,
                     secret: Any = None, now: int | None = None,
                     replay_guard: TicketReplayGuard | None = None,
                     single_use: bool = False, client: str | None = None) -> SessionIdentity:
    """A verified employee session, or ``SessionIdentityRefused``.

    Exactly one of ``object_id`` (a configured employee; MCP only) or
    ``ticket`` (a signed session ticket, verified with ``secret``). Either way
    the resolver must grant the employee now: an active, identity-verified
    Portal employee row whose Entra tenant and object id are this identity,
    in the policy's Entra tenant (``EmployeePermissionResolver.resolve``).
    ``client``: the host's id for ``act.client`` (default per channel).
    """
    if not isinstance(resolver, EmployeePermissionResolver):
        raise SessionIdentityRefused("employee_entitlements_not_configured")
    if channel not in CHANNEL_AUTHOR:
        raise SessionIdentityRefused("session_channel_invalid")
    if (object_id is None) == (ticket is None):
        raise SessionIdentityRefused("session_identity_required")
    tenant_id = resolver.policy.entra_tenant_id
    claims: dict[str, Any] | None = None
    if ticket is not None:
        if secret is None:
            raise SessionIdentityRefused("session_secret_unavailable")
        claims = verify_session_ticket(ticket, secret, now=now)
        if replay_guard is not None:
            replay_guard.check(claims, consume=single_use, now=now)
        elif single_use:
            raise SessionIdentityRefused("replay_guard_required")
        if claims["author"] != CHANNEL_AUTHOR[channel]:
            # A console (human) ticket never opens an agent session, nor the reverse.
            raise SessionIdentityRefused("session_ticket_author_mismatch")
        if claims["entraTenantId"].strip().lower() != tenant_id:
            raise SessionIdentityRefused("tenant_mismatch")
        object_id = claims["entraObjectId"]
    elif channel != CHANNEL_MCP:
        # The console takes its identity from the authenticated request only.
        raise SessionIdentityRefused("console_identity_requires_session_ticket")
    try:
        principal = EmployeePrincipal(tenant_id, object_id)
    except ValueError:
        raise SessionIdentityRefused("employee_principal_invalid") from None
    entitlement = resolver.resolve(principal)
    if entitlement.actor is None:
        raise SessionIdentityRefused(entitlement.denied_reason or "denied")
    if claims is not None and claims["email"].strip().lower() \
            not in entitlement.source_identities:
        # The ticket and the Portal row must name the same person.
        raise SessionIdentityRefused("session_ticket_identity_mismatch")
    return SessionIdentity(
        tenant=resolver.policy.tenant, channel=channel, author_kind=CHANNEL_AUTHOR[channel],
        proof=PROOF_TICKET if claims is not None else PROOF_CONFIGURED,
        employee=principal, principals=frozenset({principal.principal}),
        portal_user_id=None if claims is None else claims["uid"],
        email=None if claims is None else claims["email"].strip().lower(),
        expires_at=None if claims is None else claims["exp"], client=client,
        session_ref=None if claims is None
        else hashlib.sha256(claims["jti"].encode("utf-8")).hexdigest(),
        identities=frozenset(
            {i for i in entitlement.source_identities
             if isinstance(i, str) and i.strip() and not i.startswith("group:")}
            | {principal.object_id}
            | (set() if claims is None else {claims["email"].strip().lower()})))


def fixture_session(tenant: str, principals: Iterable[str], *, channel: str,
                    clearance: str = "internal", subject_email: str | None = None,
                    subject_oid: str | None = None,
                    client: str | None = None) -> SessionIdentity:
    """A fixture/demo identity from free-text principals (hosts: explicit fixture flag only).

    It cannot impersonate an employee or an agent, and a host refuses it on a
    runtime that requires an employee (``check_runtime``). ``subject_email``: the
    fixture subject's email (host configuration, never a tool argument); without it
    the session's intents have an unknown subject. ``subject_oid``: that subject's
    Entra object id (host configuration; only with ``subject_email``).
    """
    held = frozenset(p.strip() for p in principals if isinstance(p, str) and p.strip())
    if not held or "*" in held:
        raise SessionIdentityRefused("fixture_principals_required")
    if any(p.lower().startswith((EMPLOYEE_PREFIX, *_AGENT_PREFIXES)) for p in held):
        raise SessionIdentityRefused("fixture_principal_reserved")
    if channel not in CHANNEL_AUTHOR:
        raise SessionIdentityRefused("session_channel_invalid")
    if subject_email is not None and (not isinstance(subject_email, str)
                                      or not _SUBJECT_EMAIL.fullmatch(subject_email.strip())):
        raise SessionIdentityRefused("fixture_subject_email_invalid")
    oid = subject_oid.strip().lower() if isinstance(subject_oid, str) else subject_oid
    if oid is not None and (subject_email is None or not isinstance(oid, str)
                            or not _GUID.fullmatch(oid)):
        raise SessionIdentityRefused("fixture_subject_oid_invalid")
    return SessionIdentity(tenant=tenant, channel=channel, author_kind=CHANNEL_AUTHOR[channel],
                           proof=PROOF_FIXTURE, employee=None, principals=held,
                           clearance=clearance, subject_oid=oid, client=client,
                           identities=frozenset() if subject_email is None
                           else frozenset({subject_email.strip().lower()}
                                          | ({oid} if oid else set())))


def intent_identity(session: Any, actor: Actor) -> dict[str, Any]:
    """``SessionIdentity.intent_identity`` for a host's session. Every mutating step
    records who acted, so a missing session is refused (``session_required``), never
    recorded as an unknown ``act`` (lead decision, goal2-identity; an in-process
    harness passes an explicit fixture session). Anything other than a
    ``SessionIdentity`` is refused: a mapping from tool arguments or source text can
    never stand in for the session."""
    if session is None:
        raise SessionIdentityRefused("session_required")
    if type(session) is not SessionIdentity:
        raise SessionIdentityRefused("session_identity_required")
    return session.intent_identity(actor)


def check_runtime(runtime: Any, session: SessionIdentity) -> None:
    """A live (employee) session needs a runtime that refuses non-employee actors."""
    if not isinstance(session, SessionIdentity):
        raise SessionIdentityRefused("session_identity_required")
    gate = getattr(runtime, "employee_gate", None)
    if gate is None:
        raise SessionIdentityRefused("live_session_requires_employee_runtime")
    requires = gate.require_employee is True
    if session.fixture and requires:
        raise SessionIdentityRefused("fixture_session_on_live_runtime")
    if not session.fixture and not requires:
        raise SessionIdentityRefused("live_session_requires_employee_runtime")
    if gate.tenant != session.tenant:
        raise SessionIdentityRefused("tenant_mismatch")


STATE_LABEL_FILE = "session-state-label.json"
STATE_LABEL_SCHEMA = "plx-session-state-label/v1"
# Written by the case console fixture seeds (operator_ui/case_console.py FIXTURE_MARKER).
_CONSOLE_FIXTURE_MARKER = "fixture-seed.json"


STATE_LABELS = ("fixture", "dev", "live")


def state_label(state_dir: str | Path) -> str | None:
    """``fixture``, ``dev``, ``live`` or None (a new directory) for a host's state
    directory.

    Fixture: a console seed marker or a fixture label. Dev: written by a host whose
    employee was configured on its command line (``--dev-configured-employee``), never
    authenticated, so it holds no real source data. Live: a live label, an ingestion
    store, or case state nobody labelled (written before labels existed; it may hold
    real captures, so it is treated as live). An unreadable label is live
    (conservative).
    """
    root = Path(state_dir)
    label = root / STATE_LABEL_FILE
    if label.is_file():
        try:
            value = json.loads(label.read_text("utf-8"))
        except (OSError, ValueError):
            return "live"
        if isinstance(value, dict) and value.get("schema") == STATE_LABEL_SCHEMA \
                and value.get("label") in STATE_LABELS:
            return value["label"]
        return "live"
    if (root / _CONSOLE_FIXTURE_MARKER).is_file():
        return "fixture"
    from .ingestion import STORE_DIR, STORE_FILE

    if (root / STORE_DIR / STORE_FILE).is_file() or (root / "case-runtime").exists():
        return "live"
    return None


def bind_state_dir(state_dir: str | Path, *, fixture: bool, dev: bool = False) -> str:
    """Refuse a host on state another kind of host wrote (fixture, dev, live); label a
    new directory with the host's kind. Returns the label. ``dev``: an employee session
    configured on the command line, never authenticated: it never opens live state, and
    an authenticated host never opens what it wrote."""
    if fixture is not True and fixture is not False:
        raise TypeError("fixture must be a bool")
    if dev is not True and dev is not False:
        raise TypeError("dev must be a bool")
    if fixture and dev:
        raise ValueError("a host is fixture or dev, not both")
    wanted = "fixture" if fixture else "dev" if dev else "live"
    current = state_label(state_dir)
    if current is not None and current != wanted:
        raise SessionIdentityRefused(f"{wanted}_session_on_{current}_state")
    if current is None:
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / STATE_LABEL_FILE).write_text(json.dumps(
            {"schema": STATE_LABEL_SCHEMA, "label": wanted}), "utf-8")
    return wanted


__all__ = ["DEFAULT_CLIENT", "INTENT_IDENTITY_SCHEMA", "STATE_LABEL_FILE", "AUTHOR_AGENT", "AUTHOR_HUMAN", "CHANNEL_AUTHOR", "CHANNEL_CONSOLE", "CHANNEL_MCP",
           "PROOF_CONFIGURED", "PROOF_ENTRA", "PROOF_EVAL", "PROOF_FIXTURE", "PROOF_TICKET", "SESSION_TICKET_AUDIENCE",
           "SESSION_TICKET_SECRET_ENV", "SESSION_TICKET_TTL_SECONDS", "SessionIdentity",
           "SessionIdentityRefused", "REPLAY_GUARD_FILE", "TicketReplayGuard", "bind_state_dir", "check_runtime", "employee_session", "fixture_session",
           "intent_identity", "mint_session_ticket", "replay_guard_for", "session_secret",
           "state_label", "verify_session_ticket"]
