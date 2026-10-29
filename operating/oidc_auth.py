"""Entra access tokens for the remote MCP host and the console: the Workbench as an OAuth
resource server.

An employee signs in
with Entra and the MCP client presents the Entra-issued access token on every request
(MCP authorization, spec revision 2025-06-18; RFC 6750 bearer use, RFC 9728 protected
resource metadata, RFC 8707 audience binding). The Workbench mints no token of its
own here; it only verifies one, before anything is read:

* structure: three base64url parts, at most ``MAX_TOKEN_CHARS``; the header is a JSON
  object whose ``alg`` is exactly ``RS256`` and whose ``kid`` names a key of the
  configured JWKS. No ``crit``, ``jku``, ``x5u`` or embedded ``jwk`` is honoured (a
  token never chooses its own key); an unknown ``kid`` is refused, never fetched.
* signature: RSASSA-PKCS1-v1_5 with SHA-256 against an RSA key of at least 2048 bits,
  computed here with the standard library (no dependency).
* claims, after the signature only: ``iss`` is the configured tenant's v1 or v2 issuer
  and agrees with ``ver``; ``aud`` is one of the configured audiences (a string,
  never a list); ``tid`` is the configured tenant (Entra signs every tenant's tokens
  with the same keys, so a foreign tenant's token is refused here); ``exp``/``nbf``/
  ``iat`` bound the lifetime (``MAX_TOKEN_LIFETIME_SECONDS``; ``CLOCK_SKEW_SECONDS``
  for ``nbf``/``iat`` only: an expired token is expired); ``oid`` is a GUID; the token is delegated (``scp`` holds the
  required scope's name, the part after the audience of a full ``<Application ID URI>/<name>``
  scope; ``idtyp`` when present is ``user``), so an app-only token is refused.

Refusals are ``EntraTokenRefused`` with an RFC 6750 error (``invalid_token`` or
``insufficient_scope``) and a reason code; no token, claim value or key is ever in a
message. Who the employee is and what they may read is decided afterwards by the
existing resolver (``entra_session``: Portal role ∩ Entra groups ∩ item ACLs, the
PLX-confirmed policy file); the token's own name claims (``upn``,
``preferred_username``) are not used for authorization.

The JWKS is configuration: a file the operator keeps current (``JwksFile``, re-read
when it changes) or keys injected by a test (``StaticJwks``). Fetching Entra's
discovery keys is not done here: no HTTP client outside the GET-only module
(hard guards), and an unknown key fails closed.

The console (``operator_ui.case_console --entra-resource``) accepts the same token from an
employee signed in with their PLX Microsoft account: ``entra_session`` with
``channel="console"`` (author ``human``). Its resource file is committed
(``COMMITTED_RESOURCE``, operating/entra_resource.json): identifiers only, never a secret.
A field holds a value the repository states (cited under ``sources``) or a typed
``{"unset": "<later ask>"}``; ``check_resource_file`` is the schema check and names the
unset fields, and ``EntraResource.from_file`` refuses a file with any
(``EntraResourceUnset``), so a half-configured resource verifies nothing. Sign-in uses the
existing app registration (tenant, client id, Application ID URI and the delegated scope
are committed); the host's resource URI and the tenant's JWKS file wait for the hosting VM,
and the console's redirect URI with them: later ask ``console-entra-app-registration``.

Evidence: isolated-fixture (tokens signed in the tests with a generated key);
live-unverified (no Entra app registration for the Workbench yet).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from .employee_permissions import EmployeePermissionResolver, EmployeePrincipal
from . import session_identity
from .session_identity import SessionIdentity, SessionIdentityRefused

RESOURCE_SCHEMA = "plx-entra-resource/v1"
COMMITTED_RESOURCE = Path(__file__).with_name("entra_resource.json")
# The configured fields of a resource file, each a value or a typed unset.
RESOURCE_FIELDS = ("tenant_id", "client_id", "app_id_uri", "audiences", "required_scope",
                   "resource", "jwks_file")
# Optional fields: at least one audience field; jwks_file unless keys are injected.
_AUDIENCE_FIELDS = ("client_id", "app_id_uri", "audiences")
_OPTIONAL_FIELDS = _AUDIENCE_FIELDS + ("jwks_file",)
MAX_TOKEN_CHARS = 16_384
MAX_TOKEN_LIFETIME_SECONDS = 24 * 60 * 60
CLOCK_SKEW_SECONDS = 60
MIN_RSA_BITS = 2048
MAX_JWKS_BYTES = 256_000
INVALID_TOKEN = "invalid_token"
INSUFFICIENT_SCOPE = "insufficient_scope"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SCOPE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_KID = re.compile(r"^[A-Za-z0-9._~+/=-]{1,128}$")
_LATER_ASK = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# DER DigestInfo prefix for SHA-256 (RFC 8017 section 9.2, note 1).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_CLAIMS_INT = ("exp", "iat")


class EntraTokenRefused(PermissionError):
    """A bearer token the Workbench does not accept. ``error`` is the RFC 6750 code
    (``invalid_token`` or ``insufficient_scope``), ``reason`` a code safe to log."""

    def __init__(self, reason: str, error: str = INVALID_TOKEN) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _uint(text: Any) -> int:
    if not isinstance(text, str) or not _B64URL.match(text):
        raise ValueError("jwk_number_invalid")
    return int.from_bytes(_unb64(text), "big")


# ------------------------------------------------------------------ keys

def parse_jwks(document: Any) -> dict[str, tuple[int, int]]:
    """``{kid: (n, e)}`` of the signing RSA keys of a JWKS document. Keys of another
    type, use or algorithm are skipped; a key too short or malformed is an error (the
    file is wrong, not the token)."""
    if not isinstance(document, Mapping) or not isinstance(document.get("keys"), list):
        raise ValueError("jwks_invalid")
    keys: dict[str, tuple[int, int]] = {}
    for jwk in document["keys"]:
        if not isinstance(jwk, Mapping) or jwk.get("kty") != "RSA" \
                or jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") != "RS256":
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not _KID.match(kid) or kid in keys:
            raise ValueError("jwks_kid_invalid")
        n, e = _uint(jwk.get("n")), _uint(jwk.get("e"))
        if n.bit_length() < MIN_RSA_BITS or e < 3 or e % 2 == 0:
            raise ValueError("jwks_key_too_weak")
        keys[kid] = (n, e)
    return keys


class StaticJwks:
    """Keys given in memory (tests, or a host that loaded them itself)."""

    def __init__(self, document: Any) -> None:
        self._keys = parse_jwks(document)

    def key(self, kid: str) -> tuple[int, int] | None:
        return self._keys.get(kid)


class JwksFile:
    """The tenant's signing keys from a JWKS file the operator keeps current; re-read
    when the file changes. An unreadable or invalid file holds no key (fail closed)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._keys: dict[str, tuple[int, int]] = {}

    def key(self, kid: str) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._stamp:
                if stat.st_size > MAX_JWKS_BYTES:
                    raise ValueError("jwks_too_large")
                self._keys, self._stamp = parse_jwks(
                    json.loads(self.path.read_text("utf-8"))), stamp
        except (OSError, ValueError):
            self._keys, self._stamp = {}, None
        return self._keys.get(kid)


def rsa_sha256_verify(key: tuple[int, int], signed: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verification with SHA-256 (RFC 8017 section 8.2.2)."""
    n, e = key
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    encoded = pow(s, e, n).to_bytes(size, "big")
    digest = _SHA256_DIGEST_INFO + hashlib.sha256(signed).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(encoded, expected)


# ------------------------------------------------------------------ the resource

class EntraResourceUnset(ValueError):
    """A valid resource file whose ``unset`` fields still wait for a human's later ask; it
    verifies no token. The message names the fields, never a value."""

    def __init__(self, unset: tuple[str, ...]) -> None:
        super().__init__("entra_resource_unset:" + ",".join(unset))
        self.unset = unset


def _tenant(value: Any) -> str:
    tenant = value.strip().lower() if isinstance(value, str) else ""
    if not _GUID.match(tenant):
        raise ValueError("entra_resource_tenant_invalid")
    return tenant


def _audience(value: Any, reason: str = "entra_resource_audiences_invalid") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(reason)
    return value


def _client_id(value: Any) -> str:
    client = value.strip().lower() if isinstance(value, str) else ""
    if not _GUID.match(client):
        raise ValueError("entra_resource_client_id_invalid")
    return client


def _app_id_uri(value: Any) -> str:
    uri = _audience(value, "entra_resource_app_id_uri_invalid")
    if not re.match(r"^(api|https)://\S+$", uri):
        raise ValueError("entra_resource_app_id_uri_invalid")
    return uri


def _audiences(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        raise ValueError("entra_resource_audiences_invalid")
    return frozenset(_audience(a) for a in value)


def _scope(value: Any) -> str:
    if not isinstance(value, str) or not _SCOPE.match(value):
        raise ValueError("entra_resource_scope_invalid")
    return value


def _scope_claim(scope: str, audiences: frozenset[str]) -> str:
    """The name ``scp`` carries for a required scope: the scope itself, or for the full
    form a client requests (``<audience>/<name>``, e.g. ``api://<client id>/Workbench.Use``)
    the name after that audience; a full scope of no configured audience is invalid."""
    if "://" not in scope:
        return scope
    for audience in audiences:
        name = scope[len(audience) + 1:]
        if scope.startswith(audience + "/") and name and "/" not in name:
            return name
    raise ValueError("entra_resource_scope_invalid")


def _resource_uri(value: Any) -> str:
    parts = urlsplit(value) if isinstance(value, str) else None
    if parts is None or parts.scheme not in ("https", "http") or not parts.hostname \
            or parts.query or parts.fragment:
        raise ValueError("entra_resource_uri_invalid")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("entra_resource_uri_needs_https")
    return value


def _jwks_file(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("entra_resource_jwks_invalid")
    return value


_FIELD_CHECKS = {"tenant_id": _tenant, "client_id": _client_id, "app_id_uri": _app_id_uri,
                 "audiences": _audiences, "required_scope": _scope, "resource": _resource_uri,
                 "jwks_file": _jwks_file}


def _unset(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == {"unset"} \
        and isinstance(value["unset"], str) and bool(_LATER_ASK.match(value["unset"]))


def _read_resource(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """The checked fields of a resource file (set ones only) and its unset fields with
    their later asks, in ``RESOURCE_FIELDS`` order; ``ValueError`` when it is not a ``plx-entra-resource/v1``
    file. A field is a valid value or exactly ``{"unset": "<later ask id>"}``; any other
    shape is the field's own invalid reason."""
    document = json.loads(path.read_text("utf-8"))
    if not isinstance(document, Mapping) or document.get("schema") != RESOURCE_SCHEMA \
            or set(document) - {"schema", "note", "sources", *RESOURCE_FIELDS}:
        raise ValueError("entra_resource_invalid")
    sources = document.get("sources", {})
    if not isinstance(sources, Mapping) or any(
            key not in RESOURCE_FIELDS or not isinstance(text, str) or not text.strip()
            for key, text in sources.items()):
        raise ValueError("entra_resource_sources_invalid")
    if not set(_AUDIENCE_FIELDS) & set(document):
        raise ValueError("entra_resource_audiences_invalid")
    fields: dict[str, Any] = {}
    unset: dict[str, str] = {}
    for name in RESOURCE_FIELDS:
        if name not in document and name in _OPTIONAL_FIELDS:
            continue
        if _unset(document.get(name)):
            unset[name] = document[name]["unset"]
        else:
            fields[name] = _FIELD_CHECKS[name](document.get(name))
    if "required_scope" in fields and not set(_AUDIENCE_FIELDS) & set(unset):
        _scope_claim(fields["required_scope"], _audience_set(fields))
    return fields, unset


def _audience_set(fields: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(set(fields.get("audiences", ())) | {
        fields[name] for name in ("client_id", "app_id_uri") if name in fields})


def check_resource_file(path: str | Path) -> dict[str, Any]:
    """The schema check of a ``plx-entra-resource/v1`` file: ``ValueError`` when it is not
    one, else its unset field names and their later asks (names only, never a value)."""
    _, unset = _read_resource(Path(path))
    return {"schema": RESOURCE_SCHEMA, "unset": list(unset),
            "later_asks": sorted(set(unset.values()))}


@dataclass(frozen=True)
class EntraResource:
    """What this Workbench host accepts: one Entra tenant, its own audiences (the app's
    client id and/or Application ID URI), one required delegated scope, the canonical
    resource URI the MCP client binds the token to (RFC 8707), and the tenant's keys.

    ``plx-entra-resource/v1`` JSON: ``tenant_id``, the audiences (``client_id``,
    ``app_id_uri`` and/or a list ``audiences``), ``required_scope``, ``resource``,
    ``jwks_file`` (relative to the file); ``note`` and ``sources`` (field -> where the
    committed repository states its value) are documentation."""

    tenant_id: str
    audiences: frozenset[str]
    required_scope: str
    resource: str
    jwks: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _tenant(self.tenant_id))
        object.__setattr__(self, "audiences", _audiences(self.audiences))
        _scope_claim(_scope(self.required_scope), self.audiences)
        _resource_uri(self.resource)
        if not callable(getattr(self.jwks, "key", None)):
            raise ValueError("entra_resource_jwks_invalid")

    @classmethod
    def from_file(cls, path: str | Path, *, jwks: Any = None) -> "EntraResource":
        """The resource a file configures; ``EntraResourceUnset`` while a field it needs
        is unset (``jwks_file`` is not needed when ``jwks`` is given)."""
        path = Path(path).resolve()
        fields, unset = _read_resource(path)
        unset = tuple(name for name in unset if jwks is None or name != "jwks_file")
        if unset:
            raise EntraResourceUnset(unset)
        if jwks is None:
            jwks = JwksFile(path.parent / _jwks_file(fields.get("jwks_file")))
        return cls(fields["tenant_id"], _audience_set(fields), fields["required_scope"],
                   fields["resource"], jwks)

    @property
    def scope_claim(self) -> str:
        """The required scope's name in the token's ``scp``."""
        return _scope_claim(self.required_scope, self.audiences)

    @property
    def issuers(self) -> dict[str, str]:
        """Accepted ``iss`` per token version (Entra v1 and v2 access tokens)."""
        return {"1.0": f"https://sts.windows.net/{self.tenant_id}/",
                "2.0": f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"}

    def metadata(self) -> dict[str, Any]:
        """RFC 9728 protected resource metadata (public; no secret)."""
        return {"resource": self.resource,
                "authorization_servers": [self.issuers["2.0"]],
                "scopes_supported": [self.required_scope],
                "bearer_methods_supported": ["header"],
                "resource_name": "PLX Agent Workbench"}

    def challenge(self, metadata_url: str, refusal: EntraTokenRefused | None = None) -> str:
        """The ``WWW-Authenticate`` value of a 401/403 (RFC 6750 section 3, RFC 9728)."""
        value = f'Bearer resource_metadata="{metadata_url}"'
        if refusal is not None:
            value += f', error="{refusal.error}"'
            if refusal.error == INSUFFICIENT_SCOPE:
                value += f', scope="{self.required_scope}"'
        return value


def verify_access_token(token: Any, resource: EntraResource, *,
                        now: int | None = None) -> dict[str, Any]:
    """The claims of an access token this resource accepts, else ``EntraTokenRefused``.
    Order: structure -> header -> key -> signature -> claims; nothing in the payload is
    parsed before the signature verifies."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise EntraTokenRefused("token_malformed")
    parts = token.split(".")
    if len(parts) != 3 or not all(_B64URL.match(part) for part in parts):
        raise EntraTokenRefused("token_malformed")
    try:
        header = json.loads(_unb64(parts[0]).decode("utf-8"))
        signature = _unb64(parts[2])
    except (ValueError, UnicodeDecodeError):
        raise EntraTokenRefused("token_malformed") from None
    if not isinstance(header, dict) or header.get("alg") != "RS256" \
            or header.get("typ", "JWT") != "JWT" \
            or {"crit", "jku", "x5u", "jwk"} & set(header):
        raise EntraTokenRefused("token_algorithm_refused")
    kid = header.get("kid")
    key = resource.jwks.key(kid) if isinstance(kid, str) and _KID.match(kid) else None
    if key is None:
        raise EntraTokenRefused("token_key_unknown")
    if not rsa_sha256_verify(key, f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
        raise EntraTokenRefused("token_signature_invalid")
    try:
        claims = json.loads(_unb64(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise EntraTokenRefused("token_malformed") from None
    if not isinstance(claims, dict):
        raise EntraTokenRefused("token_malformed")
    for name in _CLAIMS_INT + ("nbf",):
        value = claims.get(name)
        if (name in _CLAIMS_INT or value is not None) and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise EntraTokenRefused("token_claims_invalid")
    current = int(time.time()) if now is None else int(now)
    if current >= claims["exp"]:
        raise EntraTokenRefused("token_expired")
    if claims.get("nbf", claims["iat"]) > current + CLOCK_SKEW_SECONDS \
            or not 0 < claims["exp"] - claims["iat"] <= MAX_TOKEN_LIFETIME_SECONDS:
        raise EntraTokenRefused("token_lifetime_invalid")
    tid = claims.get("tid")
    if not isinstance(tid, str) or tid.strip().lower() != resource.tenant_id:
        raise EntraTokenRefused("token_tenant_mismatch")
    ver, iss = claims.get("ver"), claims.get("iss")
    issuer = resource.issuers.get(ver) if isinstance(ver, str) else None
    if issuer is None or not isinstance(iss, str) or iss != issuer:
        raise EntraTokenRefused("token_issuer_mismatch")
    if not isinstance(claims.get("aud"), str) or claims["aud"] not in resource.audiences:
        raise EntraTokenRefused("token_audience_mismatch")
    oid = claims.get("oid")
    if not isinstance(oid, str) or not _GUID.match(oid.strip().lower()):
        raise EntraTokenRefused("token_subject_invalid")
    if claims.get("idtyp", "user") != "user" or not isinstance(claims.get("scp"), str):
        # An app-only token (roles, no scp) acts for nobody: never an employee.
        raise EntraTokenRefused("token_not_delegated", INSUFFICIENT_SCOPE)
    if resource.scope_claim not in claims["scp"].split():
        raise EntraTokenRefused("token_scope_missing", INSUFFICIENT_SCOPE)
    uti = claims.get("uti")
    # The token's id for the durable revocation guard: Entra's ``uti``, else the
    # signature's digest (both unique per issued token).
    claims["_token_id"] = (uti if isinstance(uti, str) and 0 < len(uti) <= 128
                           else hashlib.sha256(signature).hexdigest())
    return claims


def token_guard_entry(claims: Mapping[str, Any]) -> dict[str, Any]:
    """The replay guard's view of a verified token: its id and expiry, nothing else."""
    return {"jti": claims["_token_id"], "exp": int(claims["exp"])}


def entra_session(resolver: EmployeePermissionResolver, claims: Mapping[str, Any], *,
                  replay_guard: session_identity.TicketReplayGuard,
                  client: str | None = None, now: int | None = None,
                  channel: str = session_identity.CHANNEL_MCP) -> SessionIdentity:
    """The session of a verified token's employee, or ``SessionIdentityRefused``.

    ``channel``: ``mcp`` (the remote MCP host; its text is authored by an agent) or
    ``console`` (an employee signed in to the console; author ``human``). The author
    follows the channel, never a claim.

    A bearer token is presented on every request (RFC 6750), so it is not consumed;
    the state directory's durable guard refuses a revoked token id
    (``session_ticket_revoked``). The resolver must grant the employee now (an
    active, verified Portal row with this tenant and object id; Portal role ∩ Entra
    groups), so a revocation there applies on the next request."""
    if not isinstance(resolver, EmployeePermissionResolver):
        raise SessionIdentityRefused("employee_entitlements_not_configured")
    if not isinstance(replay_guard, session_identity.TicketReplayGuard):
        raise SessionIdentityRefused("replay_guard_required")
    if channel not in session_identity.CHANNEL_AUTHOR:
        raise SessionIdentityRefused("session_channel_invalid")
    if str(claims.get("tid", "")).strip().lower() != resolver.policy.entra_tenant_id:
        raise SessionIdentityRefused("tenant_mismatch")
    entry = token_guard_entry(claims)
    replay_guard.check(entry, now=now)
    try:
        principal = EmployeePrincipal(resolver.policy.entra_tenant_id, claims["oid"])
    except (KeyError, ValueError):
        raise SessionIdentityRefused("employee_principal_invalid") from None
    entitlement = resolver.resolve(principal)
    if entitlement.actor is None:
        raise SessionIdentityRefused(entitlement.denied_reason or "denied")
    return SessionIdentity(
        tenant=resolver.policy.tenant, channel=channel,
        author_kind=session_identity.CHANNEL_AUTHOR[channel], proof=session_identity.PROOF_ENTRA,
        employee=principal, principals=frozenset({principal.principal}),
        expires_at=int(claims["exp"]), client=client,
        session_ref=hashlib.sha256(str(entry["jti"]).encode("utf-8")).hexdigest(),
        identities=frozenset({i for i in entitlement.source_identities
                              if isinstance(i, str) and i.strip()
                              and not i.startswith("group:")} | {principal.object_id}))


__all__ = ["CLOCK_SKEW_SECONDS", "COMMITTED_RESOURCE", "EntraResource", "EntraResourceUnset",
           "EntraTokenRefused", "INSUFFICIENT_SCOPE", "INVALID_TOKEN", "JwksFile",
           "RESOURCE_FIELDS", "RESOURCE_SCHEMA", "StaticJwks", "check_resource_file",
           "entra_session", "parse_jwks", "rsa_sha256_verify", "token_guard_entry",
           "verify_access_token"]
