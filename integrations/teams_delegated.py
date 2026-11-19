"""A person's own Teams sign-in, so a Teams reply they confirm posts *as them*.

Graph takes a chat or channel post only on a delegated token of the person (an app-only
token is refused; ``autopilot_write_adapters.TeamsAdapter``). Vince allowed it (11-18):
each person signs in once through the Workbench and the Workbench keeps their refresh
token, so a reply they confirm in the app is posted by them.

* **Sign-in** (``begin`` / ``finish``): the authorization-code flow with PKCE on the
  approved-action app (through ``integrations.graph_send``, the approved send path that
  alone reads its credentials: the same Entra app as the
  mail send) for exactly ``live_source_guard.TEAMS_DELEGATED_SCOPE``. ``begin`` (the
  signed-in person, through the API) makes a **signed, single-use, short-lived state**:
  a random nonce and its expiry, HMAC-signed with this host's own key (``Store.key``,
  0600), and keeps, under the nonce, the person it binds to (their email and Entra object
  id), the PKCE verifier and the redirect address. Microsoft's redirect back is a
  cross-site navigation that carries no Workbench session (the cookie is SameSite=Strict,
  the Entra bearer is sent only by the app's fetch), so ``finish`` needs none: it checks
  the signature and the expiry before anything is read, takes the kept record once (a
  replay finds nothing), exchanges the code through the guarded token POST and keeps the
  refresh token only for the person the state binds to, and only when the token's own
  ``oid`` is theirs and it carries ``ChatMessage.Send``. Anyone presenting someone else's
  state would have to sign in to Microsoft as that person for anything to be kept; signing
  in with another account is refused and keeps nothing.
* **Kept** (``Store``): one file per person (by their email's digest) under the write
  state, mode 0600 in a 0700 folder, written whole by rename. It holds the refresh token,
  the person's object id, the scopes granted and when; nothing of it is ever returned by
  the API, logged or put in a journal. ``drop`` forgets it (the person disconnects).
* **Posting** (``Transport``): the TeamsAdapter's request names the posting person
  (``as``: the item's owner); their access token is refreshed from the kept token (the
  new refresh token replaces the old), its ``oid`` checked against the kept one, its
  scope against the post, and the post goes out through ``live_source_guard.send_action``
  (the approved Teams shapes, the actions flag) as that person. No kept token: refused
  (``teams_not_connected``), nothing sent. The kept sign-in is read again before every
  post, so a disconnect stops the next post even while an access token is still cached.
* **No recall.** A posted Teams message is not taken back from here (like a sent mail):
  no soft-delete undo, so the sign-in asks for no read-write scope on the person's chats.

The admin step (the app's redirect address and the delegated permissions consented) is
the operator's; ``missing`` names what is not configured. Evidence: injected-transport
(``integrations/tests/test_teams_delegated.py``); live-unverified.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import urlencode

from integrations import graph_send
from integrations import live_source_guard as guard

STORE_DIR = "teams-delegated"
PENDING_DIR = "pending"
KEY_FILE = "state-key"
STATE_SECONDS = 600
REDIRECT_ENV = "PLX_TEAMS_REDIRECT_URI"     # the Workbench's public .../api/teams/callback
NEEDED_SCOPE = "ChatMessage.Send"


class SignInFailed(ValueError):
    """The sign-in did not complete; ``reason`` is a code safe to show and log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def claims(token: str) -> dict[str, Any]:
    """The payload of an access token the guarded token POST just received from Entra over
    TLS (its issuer's own answer: not verified again here); {} when unreadable."""
    try:
        part = token.split(".")[1]
        got = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def _key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]


def _write_private(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


class Store:
    """The kept sign-ins and the pending states (module doc)."""

    def __init__(self, state: Path) -> None:
        self.root = Path(state) / STORE_DIR
        self._lock = threading.Lock()

    def _person(self, email: str) -> Path:
        return self.root / f"{_key(email)}.json"

    def key(self) -> bytes:
        """This host's state-signing key: 32 random bytes made once, 0600 (never leaves)."""
        path = self.root / KEY_FILE
        with self._lock:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.root, 0o700)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:          # another process made it first
                    raw = path.read_bytes()
                else:
                    raw = secrets.token_bytes(32)
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(raw)
        if len(raw) != 32:
            raise SignInFailed("state_key_unreadable")
        return raw

    def _pending(self, nonce: str) -> Path:
        return self.root / PENDING_DIR / (hashlib.sha256(nonce.encode()).hexdigest()[:40] + ".json")

    def put_pending(self, nonce: str, record: Mapping[str, Any]) -> None:
        _write_private(self._pending(nonce), record)
        stale = time.time() - 2 * STATE_SECONDS          # sign-ins never finished
        for old in (self.root / PENDING_DIR).glob("*.json"):
            try:
                if old.stat().st_mtime < stale:
                    old.unlink(missing_ok=True)
            except OSError:
                pass

    def take_pending(self, nonce: str) -> Optional[dict[str, Any]]:
        """The pending sign-in of ``nonce``, once (removed whatever its age): it is first
        renamed away, which only one taker can do, even across processes."""
        path = self._pending(nonce)
        taken = path.with_name(f".taken.{secrets.token_hex(8)}")
        try:
            os.rename(path, taken)
        except OSError:
            return None
        try:
            doc = json.loads(taken.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        finally:
            taken.unlink(missing_ok=True)
        return doc if isinstance(doc, dict) else None

    @contextmanager
    def _held_file(self) -> Iterator[None]:
        """One writer at a time across processes (a sign-in, a rotation, a disconnect)."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self._lock:
                yield
        finally:
            os.close(fd)

    def save(self, email: str, record: Mapping[str, Any]) -> None:
        with self._held_file():
            _write_private(self._person(email), record)

    def rotate(self, email: str, connected_at: Any, record: Mapping[str, Any]) -> bool:
        """Replace the kept sign-in after a refresh, only while it is still the one the
        refresh used (a disconnect or a new sign-in in between wins)."""
        with self._held_file():
            now = self.load(email)
            if now is None or now.get("connected_at") != connected_at:
                return False
            _write_private(self._person(email), record)
            return True

    def load(self, email: str) -> Optional[dict[str, Any]]:
        try:
            doc = json.loads(self._person(email).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and doc.get("refresh_token") else None

    def drop(self, email: str) -> bool:
        path = self._person(email)
        with self._held_file():
            existed = path.exists()
            path.unlink(missing_ok=True)
        return existed

    def status(self, email: str) -> dict[str, Any]:
        """What the person may see of their own sign-in: never the token."""
        doc = self.load(email)
        if doc is None:
            return {"connected": False}
        return {"connected": True, "scopes": sorted(doc.get("scopes") or ()),
                "connected_at": doc.get("connected_at"), "used_at": doc.get("used_at")}


def missing(environ: Mapping[str, str]) -> list[str]:
    """What a sign-in still needs on this host (empty: it can start)."""
    out: list[str] = []
    try:
        graph_send.action_app(environ)
    except Exception:  # noqa: BLE001 - absent or unreadable: not configured
        out.append("the approved-action app (graph_send: not configured on this host)")
    redirect = str(environ.get(REDIRECT_ENV) or "")
    if not redirect.startswith("https://") or not redirect.endswith("/api/teams/callback"):
        out.append(f"{REDIRECT_ENV}: the Workbench's https .../api/teams/callback address, "
                   "registered on that app")
    return out


# nonce . expiry (epoch seconds) . HMAC-SHA256, all base64url / ASCII digits
_STATE = re.compile(r"([A-Za-z0-9_-]{20,100})\.([0-9]{1,12})\.([A-Za-z0-9_-]{43})")


def _mac(key: bytes, nonce: str, until: int) -> str:
    return _b64url(hmac.new(key, f"teams-sign-in|{nonce}|{until}".encode("ascii"),
                            hashlib.sha256).digest())


def _opened(store: Store, state: str, now: float) -> str:
    """The nonce of a state this host signed and that has not expired; raises otherwise
    (nothing is read or removed for a forged or old state)."""
    found = _STATE.fullmatch(state or "")
    if found is None:
        raise SignInFailed("state_unknown")
    nonce, until, mac = found.group(1), int(found.group(2)), found.group(3)
    if not hmac.compare_digest(mac, _mac(store.key(), nonce, until)):
        raise SignInFailed("state_unknown")
    if until < now:
        raise SignInFailed("state_expired")
    return nonce


def begin(environ: Mapping[str, str], store: Store, email: str, oid: str,
          now: Optional[float] = None) -> str:
    """The Microsoft sign-in address for this person (module doc); raises
    ``SignInFailed`` when the host is not configured or the person has no identity."""
    if missing(environ):
        raise SignInFailed("not_configured")
    if "@" not in email or not oid:
        raise SignInFailed("no_identity")
    tenant_id, client_id = graph_send.action_app(environ)
    nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    until = int(now or time.time()) + STATE_SECONDS
    state = f"{nonce}.{until}.{_mac(store.key(), nonce, until)}"
    redirect = str(environ[REDIRECT_ENV])
    store.put_pending(nonce, {"email": email.lower(), "oid": oid.lower(), "verifier": verifier,
                              "redirect_uri": redirect, "until": until})
    query = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect,
             "response_mode": "query", "scope": guard.TEAMS_DELEGATED_SCOPE, "state": state,
             "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
             "code_challenge_method": "S256", "login_hint": email, "prompt": "select_account"}
    return (f"https://{guard.LOGIN_HOST}/{tenant_id}/oauth2/v2.0/authorize?"
            + urlencode(query))


Request = Callable[..., tuple[int, Mapping[str, str], bytes]]


def _token(environ: Mapping[str, str], form: Mapping[str, str],
           request: Optional[Request] = None) -> dict[str, Any]:
    """One guarded token POST (authorization code or refresh) for the Teams scope, made by
    the approved send path (``graph_send.delegated_token``: it alone holds the app's
    secret); the tokens in the answer are registered so no log or error shows them."""
    try:
        status, raw = graph_send.delegated_token(environ, form, request)
    except (guard.ReadOnlyGuardError, guard.SourceUnavailable, OSError, ValueError):
        raise SignInFailed("token_request_failed") from None
    if status != 200:
        raise SignInFailed(f"token_refused_{int(status)}")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise SignInFailed("token_unreadable") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise SignInFailed("token_unreadable")
    return payload


def _checked(payload: Mapping[str, Any], oid: str, tenant: str) -> list[str]:
    got = claims(str(payload.get("access_token") or ""))
    if str(got.get("oid") or "").lower() != oid.lower():
        raise SignInFailed("another_account")
    if tenant and str(got.get("tid") or "").lower() != tenant.lower():
        raise SignInFailed("another_tenant")
    scopes = str(got.get("scp") or "").split()
    if NEEDED_SCOPE not in scopes:
        raise SignInFailed("scope_missing")
    return scopes


def finish(environ: Mapping[str, str], store: Store, state: str, code: str,
           now: Optional[float] = None, request: Optional[Request] = None,
           declined: bool = False) -> dict[str, Any]:
    """Complete the sign-in a state was signed for, without any session (module doc): the
    state is this host's, unexpired and used once; the token is the account of the person
    it binds to and carries the post scope. Keeps the refresh token; returns
    ``Store.status`` of that person. ``declined`` (Microsoft answered an error): the state
    is spent, nothing asked or kept. Raises ``SignInFailed``."""
    now = now or time.time()
    pending = store.take_pending(_opened(store, state, now))
    if pending is None:
        raise SignInFailed("state_unknown")
    if float(pending.get("until") or 0) < now:
        raise SignInFailed("state_expired")
    if declined:
        raise SignInFailed("declined")
    if not code:
        raise SignInFailed("no_code")
    email, oid = str(pending.get("email") or ""), str(pending.get("oid") or "")
    if "@" not in email or not oid:
        raise SignInFailed("no_identity")
    payload = _token(environ, {"grant_type": "authorization_code", "code": code,
                               "redirect_uri": str(pending["redirect_uri"]),
                               "code_verifier": str(pending["verifier"])}, request)
    scopes = _checked(payload, oid, graph_send.action_app(environ)[0])
    if not isinstance(payload.get("refresh_token"), str):
        raise SignInFailed("no_refresh_token")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    store.save(email, {"email": email.lower(), "oid": oid.lower(), "scopes": scopes,
                       "refresh_token": payload["refresh_token"], "connected_at": stamp})
    return store.status(email)


@dataclass
class _Held:
    token: str
    until: float
    connected_at: Any = None


class Transport:
    """``TeamsAdapter``'s transport: posts as the person the request names (``as``) with
    their own delegated token (module doc). ``request`` stands in for the guarded token
    POST and ``post`` for ``send_action`` in tests."""

    def __init__(self, environ: Mapping[str, str], store: Store, *,
                 request: Optional[Request] = None,
                 post: Optional[Callable[[str, Any, bytes], tuple[int, Mapping[str, str], bytes]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.environ = environ
        self.store = store
        self._request = request
        self._post = post or (lambda url, bearer, body: guard.send_action(
            url, bearer, body, environ=self.environ))
        self._clock = clock
        self._held: dict[str, _Held] = {}
        self._lock = threading.Lock()

    def missing(self) -> list[str]:
        return missing(self.environ)

    def bearer(self, email: str) -> str:
        """The person's access token (refreshed from the kept one when near expiry)."""
        with self._lock:
            now = self._clock()
            # the kept sign-in is read on every post: once the person disconnects (or
            # connects again), a cached access token of the old sign-in is never used
            kept = self.store.load(email)
            if kept is None:
                self._held.pop(email, None)
                raise SignInFailed("teams_not_connected")
            held = self._held.get(email)
            if (held is not None and held.connected_at == kept.get("connected_at")
                    and now < held.until - 120):
                return held.token
            payload = _token(self.environ, {"grant_type": "refresh_token",
                                            "refresh_token": str(kept["refresh_token"])},
                             self._request)
            scopes = _checked(payload, str(kept.get("oid") or ""),
                              graph_send.action_app(self.environ)[0])
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            if not self.store.rotate(email, kept.get("connected_at"), {
                    **kept, "scopes": scopes, "used_at": stamp,
                    "refresh_token": payload.get("refresh_token")
                    if isinstance(payload.get("refresh_token"), str)
                    else kept["refresh_token"]}):
                raise SignInFailed("teams_not_connected")    # disconnected meanwhile
            lifetime = int(payload.get("expires_in") or 3599)
            self._held[email] = _Held(payload["access_token"],
                                      now + max(0, min(lifetime, 86_400)),
                                      kept.get("connected_at"))
            return payload["access_token"]

    def __call__(self, request: Mapping[str, Any], key: str) -> tuple[int, Mapping[str, Any]]:
        email = str(request.get("as") or "").strip().lower()
        if "@" not in email:
            return 400, {"code": "teams_poster_unknown"}
        if request.get("method") != "POST" or not isinstance(request.get("body"), Mapping):
            return 400, {"code": "guard_refused"}
        body = json.dumps(request["body"], separators=(",", ":"), ensure_ascii=False).encode()
        try:
            check = guard.check_action_request("POST", str(request.get("url") or ""), body,
                                               environ=self.environ)
        except guard.ReadOnlyGuardError:
            return 400, {"code": "guard_refused"}
        del check
        try:
            token = self.bearer(email)
        except SignInFailed as failed:
            code = failed.reason if failed.reason == "teams_not_connected" else "no_token"
            return 401, {"code": code}
        try:
            status, _h, raw = self._post(str(request["url"]), guard.Secret(token), body)
        except guard.ReadOnlyGuardError:
            return 400, {"code": "guard_refused"}
        except guard.SourceUnavailable as error:
            raise OSError(type(error).__name__) from None
        try:
            answer = json.loads(raw) if raw else {}
        except ValueError:
            answer = {}
        return int(status), answer if isinstance(answer, dict) else {}
