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
  host owner minted for one employee (``python3 -m operator_api.workspace.console_ticket mint``),
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

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any, Callable, Mapping, Optional

from operating import session_identity
from operating.session_identity import SessionIdentity, SessionIdentityRefused

SESSION_COOKIE = "plx_app_session"
DEV_SESSION_SECONDS = 8 * 60 * 60          # idle window: each use slides it forward
DEV_SESSION_MAX_SECONDS = 24 * 60 * 60     # but never past a day from sign-in
MAX_DEV_SESSIONS = 256
SESSION_STORE_FILE = "app-sessions.json"   # in the state directory (``DevAuth.store_path``)
SESSION_STORE_SCHEMA = "plx.app_sessions.v1"
# The store's health, next to it: written by the API on every change of state (and at start),
# read by ``operating.ops_alarms`` (``app_sessions_store``: the /ops page and the alarm mail).
SESSION_HEALTH_FILE = "app-sessions-health.json"
SESSION_HEALTH_SCHEMA = "plx.app_sessions_health.v1"
SESSION_STORE_RETRY_SECONDS = 30     # a failed write is retried on a request this long after
SESSION_HEALTH_EVERY_SECONDS = 300   # while failing, the log and health file at most this often
# Sign-outs held only in memory (their write failed). Past this many the list is dropped and
# the store is resynced instead (``DevAuth._resync``): it never grows without bound.
MAX_PENDING_SIGN_OUTS = 4 * 256
# A slide moves an entry's idle expiry later; it is written to disk only once it has moved
# this far, so a request does not write the file. The stored expiry is therefore never later
# than the held one: after a restart a session ends at most this much earlier, never later.
SESSION_STORE_SLIDE_SECONDS = 60
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
        return {"mode": self.mode, "dev_login": False, "sign_in": "microsoft"}


def sign_in_kind(mode: str, proof: str) -> str:
    """How this reader signed in, as the app says it: ``microsoft`` (Entra), ``access_link``
    (a console session ticket over live state, or the owner viewing as someone from one), or
    ``demo`` (mock/fixture data or a configured test identity). Anything unrecognised is
    ``demo``, so a real sign-in is never claimed for a session that is not one."""
    from operating.session_identity import PROOF_ENTRA, PROOF_TICKET, PROOF_VIEW_AS
    if mode == "entra" and proof in (PROOF_ENTRA, PROOF_VIEW_AS):
        return "microsoft"
    if mode == "dev" and proof in (PROOF_TICKET, PROOF_VIEW_AS):
        return "access_link"
    return "demo"


def _ticket_reason(refused: SessionIdentityRefused) -> bool:
    """Refused for the ticket itself (expired, revoked, malformed, another audience)."""
    reason = getattr(refused, "reason", "") or ""
    return reason.startswith("session_ticket_") or reason == "session_expired"


_log = logging.getLogger("operator_api.auth")


def _iso(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_id(token: str) -> str:
    """What the session store keys a session by: never the cookie value itself."""
    return hashlib.sha256(b"plx-app-session-id\0" + token.encode("utf-8")).hexdigest()


def _token_key(token: str, purpose: bytes) -> bytes:
    return hmac.new(token.encode("utf-8"), b"plx-app-session\0" + purpose,
                    hashlib.sha256).digest()


def _keystream(key: bytes, size: int) -> bytes:
    out = bytearray()
    block = 0
    while len(out) < size:
        out += hmac.new(key, block.to_bytes(8, "big"), hashlib.sha256).digest()
        block += 1
    return bytes(out[:size])


def seal_ticket(token: str, ticket: str) -> str:
    """The session's ticket as the store keeps it: encrypted and authenticated under keys
    only the session's cookie value derives (HMAC-SHA256 as a counter-mode keystream and as
    the MAC; one token seals exactly one ticket, so no nonce is needed). Whoever can read
    the store file but holds no cookie learns no ticket and cannot forge a session."""
    plain = ticket.encode("utf-8")
    body = bytes(a ^ b for a, b in zip(plain, _keystream(_token_key(token, b"enc"), len(plain))))
    tag = hmac.new(_token_key(token, b"mac"), body, hashlib.sha256).hexdigest()
    return tag + ":" + body.hex()


def open_ticket(token: str, sealed: str) -> Optional[str]:
    """``seal_ticket`` reversed, or None when the seal does not verify (fail closed)."""
    tag, _, body_hex = sealed.partition(":")
    try:
        body = bytes.fromhex(body_hex)
    except ValueError:
        return None
    want = hmac.new(_token_key(token, b"mac"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(tag, want):
        return None
    plain = bytes(a ^ b for a, b in zip(body, _keystream(_token_key(token, b"enc"), len(body))))
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _foreign(path: Path) -> bool:
    """True when ``path`` exists and another user owns it. The state directory is the jobs'
    user's and sticky: that user may replace or remove any file in it, so a process of that
    user (a dev API, a preflight or a test run over the live state) could take the API's
    store, lock or health file from it, after which the API can never write them again
    (EPERM: the 11-17 app_sessions_store alarm). No process ever replaces or removes a
    sessions file another user owns; it fails (or skips the health) instead."""
    try:
        return os.lstat(path).st_uid != os.geteuid()
    except FileNotFoundError:
        return False
    except OSError:
        return True


class SessionStore:
    """Dev sessions on disk, so an API restart or deploy does not sign everyone out.

    One JSON file in the state directory, made like the replay guard's (``os.open`` 0o600,
    ``keep_shared``: owner and the shared group, never others; never followed through a
    symlink). Each entry is keyed by a hash of the cookie value and holds the same idle
    expiry, ticket and day cap the memory table holds, the ticket sealed under the cookie
    (``seal_ticket``). Every change re-reads the file under an exclusive lock and changes
    only its own entry, so two API processes over one state (a deploy's overlap) keep each
    other's sign-ins and sign-outs. A file that is unreadable, of another schema, or open
    to others holds no session (everyone signs in again), never an error."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._seen: Optional[tuple[int, int, int]] = None

    def _stamp(self) -> Optional[tuple[int, int, int]]:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def changed(self) -> bool:
        return self._stamp() != self._seen

    def load(self, now: float) -> dict[str, tuple[float, str, float]]:
        """Live entries: token id -> (idle expiry, sealed ticket or "", cap)."""
        self._seen = self._stamp()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return {}
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o007 \
                    or st.st_size > 4 * 1024 * 1024:
                return {}
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                document = json.load(handle)
        except (OSError, ValueError):
            return {}
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(document, dict) or document.get("schema") != SESSION_STORE_SCHEMA \
                or not isinstance(document.get("sessions"), dict):
            return {}
        out: dict[str, tuple[float, str, float]] = {}
        for key, entry in document["sessions"].items():
            if not (isinstance(key, str) and len(key) == 64
                    and all(c in "0123456789abcdef" for c in key)):
                continue
            if not (isinstance(entry, dict) and set(entry) == {"expires", "cap", "ticket"}):
                continue
            exp, cap, sealed = entry["expires"], entry["cap"], entry["ticket"]
            if type(exp) not in (int, float) or type(cap) not in (int, float) \
                    or not isinstance(sealed, str) or len(sealed) > 16384:
                continue
            # an entry never outlives the window it could have had at sign-in
            if exp <= now or exp > cap or cap > now + DEV_SESSION_MAX_SECONDS:
                continue
            out[key] = (float(exp), sealed, float(cap))
        return out

    @property
    def lock_path(self) -> Path:
        return self.path.with_name("." + self.path.name + ".lock")

    def problem(self) -> Optional[str]:
        """Why this process could not keep the store (checked at API start), or None. In the
        sticky shared state directory only a file's owner may replace it, and with
        ``fs.protected_regular`` only its owner may create-open it: a store or lock file
        another user made (a dev API run as the host user) stops every write."""
        for path in (self.path, self.lock_path):
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                return f"{path.name} unreadable ({type(exc).__name__})"
            if not stat.S_ISREG(st.st_mode):
                return f"{path.name} is not a regular file"
            if st.st_uid != os.geteuid():
                return f"{path.name} is owned by uid {st.st_uid}, not this API's user"
        if not os.access(self.path.parent, os.W_OK | os.X_OK):
            return "the state directory is not writable by this API's user"
        return None

    def _refuse_foreign(self) -> None:
        """EPERM, before anything is written, when the store is another user's
        (``_foreign``): it is never replaced, whoever may."""
        if _foreign(self.path):
            raise PermissionError(errno.EPERM, "the sessions store is another user's",
                                  str(self.path))

    def drop(self) -> bool:
        """Remove the whole file (a sign-out that could not be written must never come back
        from it: everyone signs in again instead). True when it is gone. Another user's
        file is never removed (``_foreign``): False."""
        import fcntl
        try:
            lock = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            return False                    # no lock, no removal
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _foreign(self.path):         # checked under the lock: made since
                return False
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return True
        finally:
            os.close(lock)

    def update(self, now: float, changes: Mapping[str, Optional[tuple[float, str, float]]],
               *, keep_only: Optional[set[str]] = None, force: bool = False) -> None:
        """Set (an entry) or remove (None) sessions, keeping every other process's; with
        ``keep_only`` every entry not named there is removed too (a resync). Written when
        something changed or ``force`` (the start-up check that the file can be written)."""
        import fcntl
        from operating.file_modes import keep_shared
        lock_path = self.lock_path
        self._refuse_foreign()
        lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            keep_shared(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            sessions = self.load(now)
            dirty = force
            if keep_only is not None:
                for k in [k for k in sessions if k not in keep_only]:
                    del sessions[k]
                    dirty = True
            for key, entry in changes.items():
                if entry is None:
                    dirty = sessions.pop(key, None) is not None or dirty
                else:
                    sessions.pop(key, None)
                    while len(sessions) >= MAX_DEV_SESSIONS:
                        sessions.pop(min(sessions, key=lambda k: sessions[k][0]))
                    sessions[key] = entry
                    dirty = True
            if not dirty:
                return
            self._refuse_foreign()          # again under the lock: made since
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}."
                                            f"{threading.get_ident()}")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                         0o600)
            try:
                keep_shared(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"schema": SESSION_STORE_SCHEMA,
                               "sessions": {k: {"expires": e, "cap": c, "ticket": t}
                                            for k, (e, t, c) in sessions.items()}}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
            # ``_seen`` stays at the file as read: the holder reloads once, which also takes
            # in the other processes' changes this write carried over
        finally:
            os.close(lock)


class DevAuth:
    """Demo sign-in behind an explicit flag (module doc). ``identity`` resolves the reader
    of a dev session on every request (an employee re-resolves through the resolver, so a
    revocation in the Portal snapshot applies at once). ``ticket_identity``: instead, the
    person presents a console session ticket at dev-login (``operator_api.workspace.console_ticket
    mint``, the live console's own credential) and each request re-verifies it
    (``session_identity.employee_session``): the only dev identity a live state accepts.

    ``store_path``: the sessions also live in a ``SessionStore`` there, so they survive an
    API restart with the same idle window, day cap and per-request ticket check; without
    it they are held in memory only."""

    def __init__(self, identity: Callable[[], SessionIdentity] | None = None, *,
                 ticket_identity: Callable[[str], SessionIdentity] | None = None,
                 sign_in_identity: Callable[[str], SessionIdentity] | None = None,
                 mode: str = "dev", clock: Callable[[], float] = time.time,
                 store_path: str | os.PathLike[str] | None = None) -> None:
        if mode not in ("dev", "mock"):
            raise ValueError("dev_auth_mode_invalid")
        if (identity is None) == (ticket_identity is None):
            raise ValueError("dev_auth_needs_exactly_one_identity")
        self.mode, self.identity, self.ticket_identity = mode, identity, ticket_identity
        # ``sign_in_identity``: how a ticket is verified at sign-in (it uses the ticket up);
        # every later request re-verifies it with ``ticket_identity`` without using it up.
        self.sign_in_identity = sign_in_identity or ticket_identity
        self.clock = clock
        # the employee resolver behind the identity (``make_auth`` sets it): read only by
        # /api/health for the employee policy in force, never to decide a request
        self.resolver: Any = None
        # token id -> (idle expiry, ticket, day cap, sealed ticket, expiry last stored)
        self._sessions: dict[str, tuple[float, Optional[str], float, str, float]] = {}
        self._lock = threading.Lock()
        self._store = SessionStore(store_path) if store_path is not None else None
        # A store write that failed: sign-ins held only here (``_unsaved``) and sign-outs the
        # file may still contradict (``_ended``: token id -> when that session would have
        # ended anyway), both kept over every reload (a sign-out is never undone by one) and
        # retried (``_flush``). ``_resync``: the file may hold a sign-out this or an earlier
        # process could not remove, so no entry is taken from it, and the next good write
        # keeps only the sessions held here.
        self._unsaved: set[str] = set()
        self._ended: dict[str, float] = {}
        self._resync = False
        # ``_lost``: a sign-out may be in the file and the file could not be removed: kept in
        # the health file too, so the next process starts resyncing (``_start``)
        self._lost = False
        self._retry_at = 0.0
        self._failing: Optional[dict[str, Any]] = None
        self._health_at = 0.0
        if self._store is not None:
            with self._lock:
                self._start(self.clock())

    @property
    def needs_ticket(self) -> bool:
        return self.ticket_identity is not None

    # ---- the store's health (logged; ``SESSION_HEALTH_FILE`` for the ops alarm) ----------

    def _health_path(self) -> Path:
        return self._store.path.with_name(SESSION_HEALTH_FILE)

    def _start(self, now: float) -> None:
        """API start (lock held): trust the file only if this process can keep it. A store
        or lock file of another user, an unwritable directory, a failed test write, or a
        health file saying an earlier process left a sign-out in it: no entry is loaded
        (everyone signs in again), and the alarm is raised."""
        previous = self._read_health()
        if previous is not None and previous.get("resync") is True:
            self._resync = self._lost = True
        problem = self._store.problem()
        if problem is not None:
            self._resync = True
            self._fail(now, problem, force=True)
            return
        if not self._resync:
            self._reload(now)
        self._flush(now, force=True)
        if self._failing is None:
            self._write_health(now)
        else:
            # the file cannot be kept, so a sign-out may be missing from it: nothing from it
            self._sessions, self._resync = {}, True
            self._unsaved.clear()
            self._fail(now, self._failing["error"], force=True)

    def _read_health(self) -> Optional[dict[str, Any]]:
        try:
            fd = os.open(self._health_path(), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                document = json.loads(handle.read(65536))
        except (OSError, ValueError):
            return None
        if isinstance(document, dict) and document.get("schema") == SESSION_HEALTH_SCHEMA:
            return document
        return None

    def _write_health(self, now: float) -> None:
        """The health file (lock held): no cookie, token id or ticket, only the state."""
        from operating.file_modes import keep_shared
        failing = self._failing
        document = {"schema": SESSION_HEALTH_SCHEMA, "ok": failing is None,
                    "at": _iso(now), "pid": os.getpid(), "resync": self._lost,
                    "pending_sign_outs": len(self._ended),
                    "unsaved_sign_ins": len(self._unsaved),
                    "since": None if failing is None else _iso(failing["since"]),
                    "error": None if failing is None else failing["error"],
                    "failures": 0 if failing is None else failing["count"]}
        path = self._health_path()
        self._health_at = now
        if _foreign(path):
            # the API's own (or another process's): never taken from its owner; the alarm
            # pass reports a file of another user than the API's by itself
            _log.warning(json.dumps({"event": "app_sessions_health_not_ours",
                                     "path": str(path)}))
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                         0o600)
            try:
                keep_shared(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError as exc:
            _log.warning(json.dumps({"event": "app_sessions_health_unwritable",
                                     "path": str(path), "error": type(exc).__name__}))

    def _fail(self, now: float, error: str, *, force: bool = False) -> None:
        """A store write failed or the store cannot be kept: logged and in the health file
        when it starts failing, and again at most every ``SESSION_HEALTH_EVERY_SECONDS``."""
        self._retry_at = now + SESSION_STORE_RETRY_SECONDS
        first = self._failing is None
        if first:
            self._failing = {"since": now, "error": error, "count": 0}
        self._failing["count"] += 1
        self._failing["error"] = error
        if first or force or now - self._health_at >= SESSION_HEALTH_EVERY_SECONDS:
            _log.warning(json.dumps({
                "event": "app_sessions_store_failing", "path": str(self._store.path),
                "error": error, "since": _iso(self._failing["since"]),
                "failures": self._failing["count"], "pending_sign_outs": len(self._ended),
                "unsaved_sign_ins": len(self._unsaved), "store_ignored": self._resync,
                "next_start_signs_everyone_out": self._lost}))
            self._write_health(now)

    # ---- the store --------------------------------------------------------------------

    def _reload(self, now: float) -> None:
        """The memory table from the store (lock held): entries another process removed are
        gone, new ones are added (their ticket opened when their cookie is presented), and
        an entry's later in-memory expiry is kept. While resyncing no new entry is taken."""
        stored = self._store.load(now)
        merged: dict[str, tuple[float, Optional[str], float, str, float]] = {
            key: self._sessions[key] for key in self._unsaved if key in self._sessions}
        for key, (exp, sealed, cap) in stored.items():
            if key in self._ended or key in merged:
                continue
            held = self._sessions.get(key)
            if held is not None and held[3] == sealed and held[2] == cap:
                merged[key] = (max(held[0], exp), held[1], cap, sealed, exp)
            elif not self._resync:
                merged[key] = (exp, None, cap, sealed, exp)
        self._sessions = merged

    def _flush(self, now: float, changes: Optional[dict[str, Optional[tuple[float, str, float]]]]
               = None, *, force: bool = False) -> None:
        """Write ``changes`` and everything a failed write left (lock held). On failure a
        sign-out is kept in ``_ended`` and the whole file is removed so it cannot come back
        (if even that fails: ``_resync``, also in the health file a restart reads)."""
        if self._store is None:
            return
        changes = dict(changes or {})
        for key in self._unsaved:
            held = self._sessions.get(key)
            if key not in changes and held is not None:
                changes[key] = (held[4], held[3], held[2])
        for key in self._ended:
            changes.setdefault(key, None)
        try:
            self._store.update(now, changes, force=force,
                               keep_only=set(self._sessions) if self._resync else None)
        except OSError as exc:
            for key, entry in changes.items():
                if entry is None:
                    held = self._sessions.get(key)
                    self._ended.setdefault(key, now + DEV_SESSION_MAX_SECONDS
                                           if held is None else held[2])
                    self._unsaved.discard(key)
                else:
                    self._unsaved.add(key)
            for key in [k for k, until in self._ended.items() if until <= now]:
                del self._ended[key]          # that session has ended anyway
            if len(self._ended) > MAX_PENDING_SIGN_OUTS:
                self._ended.clear()
                self._resync = self._lost = True
            if self._ended and self._store.drop():
                # the file is gone, so none of these sign-outs can come back from it; the
                # sessions held here are kept, and written again once writes work
                self._ended.clear()
                self._unsaved = set(self._sessions)
            elif self._ended:
                self._resync = self._lost = True
            self._fail(now, f"{type(exc).__name__}: errno {exc.errno}")
            return
        self._unsaved.clear()
        self._ended.clear()
        lost, self._resync, self._lost = self._lost, False, False
        if self._failing is not None:
            _log.warning(json.dumps({"event": "app_sessions_store_recovered",
                                     "path": str(self._store.path),
                                     "failures": self._failing["count"]}))
            self._failing = None
            self._write_health(now)
        elif lost:
            self._write_health(now)

    def _retry(self, now: float) -> None:
        if (self._unsaved or self._ended or self._resync or self._failing is not None) \
                and now >= self._retry_at:
            # a forced write: a store that only looks fine (nothing to change) is not
            # taken as recovered
            self._flush(now, force=True)

    def login(self, ticket: Optional[str] = None) -> tuple[str, Reader]:
        """A new dev session's cookie value and its reader (``AuthRequired`` if refused)."""
        if self.needs_ticket != bool(ticket):
            raise AuthRequired("dev_ticket_required" if self.needs_ticket
                               else "dev_ticket_not_accepted", 400)
        reader = self._reader(ticket, signing_in=True)
        token = secrets.token_urlsafe(32)
        key = _token_id(token)
        sealed = seal_ticket(token, ticket) if ticket else ""
        now = self.clock()
        with self._lock:
            for k in [k for k, v in self._sessions.items() if v[0] <= now]:
                del self._sessions[k]
                self._unsaved.discard(k)
            while len(self._sessions) >= MAX_DEV_SESSIONS:
                dropped = next(iter(self._sessions))
                self._sessions.pop(dropped)
                self._unsaved.discard(dropped)
            expires = now + DEV_SESSION_SECONDS
            if reader.session.expires_at is not None:
                expires = min(expires, float(reader.session.expires_at))
            cap = now + DEV_SESSION_MAX_SECONDS
            self._sessions[key] = (expires, ticket, cap, sealed, expires)
            self._flush(now, {key: (expires, sealed, cap)})
        return token, reader

    def logout(self, token: str | None) -> None:
        if token and len(token) <= 100:
            key = _token_id(token)
            now = self.clock()
            with self._lock:
                if self._store is not None and self._store.changed():
                    self._reload(now)
                # only a session held here (or in the file just read) is written: an unknown
                # cookie never adds a pending sign-out
                if self._sessions.pop(key, None) is not None:
                    self._unsaved.discard(key)
                    self._flush(now, {key: None})

    def _held(self, token: str) -> tuple[bool, Optional[str]]:
        now = self.clock()
        key = _token_id(token)
        with self._lock:
            if self._store is not None:
                self._retry(now)
                # one stat per request: the file is read again only when it changed (mtime,
                # size, inode), so an unknown cookie costs no parse
                if self._store.changed():
                    self._reload(now)
            entry = self._sessions.get(key)
            if entry is None:
                return False, None
            exp, ticket, cap, sealed, stored = entry
            if exp <= now:
                # the stored expiry is never later than this one: the file's entry has ended
                # too (and is purged by the next write), nothing to write now
                del self._sessions[key]
                self._unsaved.discard(key)
                return False, None
            if sealed and ticket is None:
                # held since before a restart: the cookie opens its ticket, or it is no session
                ticket = open_ticket(token, sealed)
                if ticket is None:
                    del self._sessions[key]
                    return False, None
            elif not sealed and self.needs_ticket:
                return False, None
            # sliding on activity; a ticket's own expiry is still checked per request
            slid = min(max(exp, now + DEV_SESSION_SECONDS), cap)
            if slid - stored >= SESSION_STORE_SLIDE_SECONDS:
                # held first, so a failed write keeps it in ``_unsaved`` with this expiry
                self._sessions[key] = (slid, ticket, cap, sealed, slid)
                self._flush(now, {key: (slid, sealed, cap)})
                stored = slid
            self._sessions[key] = (slid, ticket, cap, sealed, stored)
            return True, ticket

    def _reader(self, ticket: Optional[str], *, signing_in: bool = False) -> Reader:
        try:
            if self.ticket_identity is None:
                session = self.identity()
            else:
                session = (self.sign_in_identity if signing_in else self.ticket_identity)(ticket)
        except SessionIdentityRefused as refused:
            # An expired, revoked or malformed ticket is the link's fault, not the person's
            # access: at sign-in the link is refused, later the session has ended (the
            # reason itself is never returned).
            if ticket and _ticket_reason(refused):
                raise (AuthRequired("access_link_refused", 403) if signing_in
                       else AuthRequired("sign_in_required")) from None
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
        link = self.mode == "dev" and self.needs_ticket
        return {"mode": self.mode, "dev_login": True, "sign_in": "access_link" if link else "demo"}


def ticket_identity(resolver: Any, secret: Any, replay_guard: Any, *, single_use: bool = False
                    ) -> Callable[[str], SessionIdentity]:
    """A console session ticket verified as the live console verifies it, per request.
    ``single_use``: the verification also uses the ticket's id up in the replay guard, so a
    second presentation is refused (the sign-in; later requests do not use it up)."""
    def identity(ticket: str) -> SessionIdentity:
        if secret is None:
            raise SessionIdentityRefused("session_secret_unavailable")
        return session_identity.employee_session(
            resolver, channel=session_identity.CHANNEL_CONSOLE, ticket=ticket,
            secret=secret, replay_guard=replay_guard, single_use=single_use,
            client="plx-workbench-app")
    return identity


def configured_employee(resolver: Any, object_id: str) -> Callable[[], SessionIdentity]:
    """The dev identity of one employee: resolved by the employee resolver on each call
    exactly as a signed-in employee is (Portal role ∩ Entra groups), proof
    ``configured_employee`` so every record says it was not a sign-in. Never over live
    state (the caller binds the state directory as ``dev``,
    ``session_identity.bind_state_dir``): live state takes ``ticket_identity``. The app's
    read-ahead never uses it either: it follows a real session with
    ``session_identity.warm_session``, which serves nothing."""
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
            access=entitlement.full_read_code(),
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
