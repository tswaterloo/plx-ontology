"""Send adapter for confirmed card actions: Graph mail (send, reply in thread, forward)
and a Teams chat message, sent as the signed-in person from their own mailbox.

Gated, default off. A request goes out only when all of these hold, checked in order:

1. ``PLX_ACTIONS_LIVE=1`` in the environment;
2. the sender (the signed-in person's own address) is listed in ``PLX_ACTIONS_SENDERS``
   (comma separated): a shared or service mailbox is never the sender;
3. the send app's credentials give a token: the prefix named by
   ``PLX_ACTIONS_GRAPH_CREDENTIALS`` (``MICROSOFT_GRAPH`` by default; ``AZURE`` for the
   Entra admin app that holds Mail.Send). That token is used here, for the four
   ``send_action`` shapes only; every read keeps the read-only ``MICROSOFT_GRAPH_*`` app;
4. the token carries the permission the action needs: ``Mail.Send`` for mail
   (application ``roles`` or delegated ``scp``); for Teams ``ChatMessage.Send`` or
   ``Chat.ReadWrite`` as a *delegated* scope (Graph refuses an app-only chat post).

Otherwise the action is a dry run: the exact request (method, URL, JSON body) is returned
with every missing condition named ("Mail.Send not granted on AZURE app"). The request itself passes
``live_source_guard.send_action`` (four closed shapes, the flag re-checked inside the one
sender); this module does no I/O of its own. Nothing here decides who may send what to
whom: ``operating.card_actions`` does, before calling ``dispatch``.

Evidence label of tests: ``injected-transport`` (a fake Graph); live-unverified.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable, Mapping
from urllib.parse import quote

from . import live_source_guard as guard

FLAG = guard.ACTION_FLAG
SENDERS_ENV = "PLX_ACTIONS_SENDERS"
CREDENTIALS_ENV = guard.ACTION_CREDENTIALS_ENV
GRAPH = f"https://{guard.GRAPH_HOST}/v1.0"
MAIL_KINDS = frozenset({"send_email", "reply_in_thread", "forward"})
TEAMS_KINDS = frozenset({"teams_message"})
MAIL_PERMISSION = "Mail.Send"
TEAMS_PERMISSIONS = ("ChatMessage.Send", "Chat.ReadWrite")

Post = Callable[[str, "guard.Secret", bytes], tuple[int, Mapping[str, str], bytes]]


def configured_senders(environ: Mapping[str, str]) -> frozenset[str]:
    raw = environ.get(SENDERS_ENV) or ""
    return frozenset(a.strip().lower() for a in raw.replace(";", ",").split(",")
                     if "@" in a.strip())


def _recipients(addresses: Any) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": a}} for a in addresses or ()]


def request_for(kind: str, sender: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The exact Graph request of one action: ``{"method", "url", "body"}``. A reply and a
    forward name the cited message in the sender's own mailbox (``message_id``)."""
    # plain path parts: the guard refuses any '%' (an address or id needing encoding)
    user = quote(sender.lower(), safe="@+'")
    if kind == "send_email":
        message = {"subject": payload["subject"],
                   "body": {"contentType": "Text", "content": payload["body"]},
                   "toRecipients": _recipients(payload.get("to"))}
        if payload.get("cc"):
            message["ccRecipients"] = _recipients(payload["cc"])
        return {"method": "POST", "url": f"{GRAPH}/users/{user}/sendMail",
                "body": {"message": message, "saveToSentItems": True}}
    if kind == "reply_in_thread":
        message = {"toRecipients": _recipients(payload.get("to"))}
        if payload.get("cc"):
            message["ccRecipients"] = _recipients(payload["cc"])
        return {"method": "POST", "url": f"{GRAPH}/users/{user}/messages/"
                f"{quote(payload['message_id'], safe='=')}/reply",
                "body": {"message": message, "comment": payload["body"]}}
    if kind == "forward":
        return {"method": "POST", "url": f"{GRAPH}/users/{user}/messages/"
                f"{quote(payload['message_id'], safe='=')}/forward",
                "body": {"comment": payload["body"],
                         "toRecipients": _recipients(payload.get("to"))}}
    if kind == "teams_message":
        return {"method": "POST", "url": f"{GRAPH}/chats/{quote(payload['chat_id'], safe=':@')}"
                "/messages", "body": {"body": {"contentType": "text",
                                               "content": payload["body"]}}}
    raise ValueError("action_kind_not_sendable")


def token_permissions(bearer: str) -> tuple[frozenset[str], frozenset[str]]:
    """(application roles, delegated scopes) a Graph access token carries, read from its
    payload (the token is the one we were just issued; nothing here trusts it further)."""
    try:
        part = bearer.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return frozenset(), frozenset()
    if not isinstance(claims, dict):
        return frozenset(), frozenset()
    roles = claims.get("roles") if isinstance(claims.get("roles"), list) else []
    scp = claims.get("scp") if isinstance(claims.get("scp"), str) else ""
    return (frozenset(r for r in roles if isinstance(r, str)), frozenset(scp.split()))


def missing_permission(kind: str, bearer: str) -> str | None:
    roles, scopes = token_permissions(bearer)
    if kind in MAIL_KINDS:
        return None if MAIL_PERMISSION in roles | scopes else f"{MAIL_PERMISSION} not granted"
    if any(p in scopes for p in TEAMS_PERMISSIONS):
        return None
    return ("ChatMessage.Send not granted (a delegated permission: Teams takes chat "
            "messages only on a signed-in person's token)")


def credential_prefix(environ: Mapping[str, str]) -> str | None:
    """The send app's credential prefix, None when ``PLX_ACTIONS_GRAPH_CREDENTIALS``
    names no known app (then nothing is sent)."""
    try:
        return guard.action_credential_prefix(environ)
    except ValueError:
        return None


def _default_bearer(environ: Mapping[str, str]) -> Callable[[], str]:
    """The send app's Graph token (client credentials of the prefix in
    ``PLX_ACTIONS_GRAPH_CREDENTIALS``), one provider kept so the token is reused until
    near expiry. Only ``ActionSender`` uses it, only for ``send_action``."""
    held: list[guard.TokenProvider] = []

    def bearer() -> str:
        if not held:
            transport = guard.GuardedTransport(environ=environ)
            credentials = guard.action_credentials(environ)
            held.append(guard.TokenProvider(credentials, guard.GRAPH_SCOPE, transport))
        return held[0].bearer()
    return bearer


class ActionSender:
    """``dispatch`` one confirmed action. ``bearer`` returns a Graph token and ``post``
    sends (``live_source_guard.send_action``); both are injected in tests (a fake Graph)."""

    def __init__(self, environ: Mapping[str, str] | None = None, *,
                 bearer: Callable[[], str] | None = None, post: Post | None = None) -> None:
        self.environ = os.environ if environ is None else environ
        self._bearer = bearer or _default_bearer(self.environ)
        self._post = post or (lambda url, token, body: guard.send_action(
            url, token, body, environ=self.environ))
        self.injected = post is not None

    def gate(self, kind: str, sender: str | None) -> tuple[list[str], str | None]:
        """(what is missing for a live send, the bearer when nothing is). The token is
        asked for only once the flag and the sender are in place."""
        missing = []
        prefix = credential_prefix(self.environ)
        if prefix is None:
            missing.append(f"{CREDENTIALS_ENV} names no known send app")
        if not guard.action_send_enabled(self.environ):
            missing.append(f"Sending is off ({FLAG}=1 not set)")
        if not sender:
            missing.append("No signed-in mailbox to send from")
        elif sender.lower() not in configured_senders(self.environ):
            missing.append(f"Your mailbox is not configured to send ({SENDERS_ENV})")
        if missing:
            return missing, None
        try:
            token = self._bearer()
        except (guard.SourceUnavailable, guard.ReadOnlyGuardError, ValueError, OSError):
            return [f"{prefix} app credentials did not give a token"], None
        lack = missing_permission(kind, token)
        return ([f"{lack} on {prefix} app"], None) if lack else ([], token)

    def dispatch(self, kind: str, sender: str | None, payload: Mapping[str, Any]
                 ) -> dict[str, Any]:
        """``{"status": "sent" | "dry_run" | "failed" | "unknown", "missing": [...],
        "request": ..., "http_status"}``. ``failed``: Graph refused it (4xx) or the guard
        stopped it before I/O; ``unknown``: a 5xx or a transport failure after the request
        left, so it may have gone out. Nothing is ever retried."""
        request = request_for(kind, sender or "me@unknown", payload)
        missing, token = self.gate(kind, sender)
        if token is None:
            return {"status": "dry_run", "missing": missing, "request": request,
                    "http_status": None}
        body = json.dumps(request["body"], separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        try:
            status, _headers, _raw = self._post(request["url"], guard.Secret(token), body)
        except guard.ReadOnlyGuardError as error:
            return {"status": "failed", "missing": [], "request": request,
                    "http_status": None, "detail": type(error).__name__}
        except (guard.SourceUnavailable, OSError) as error:
            return {"status": "unknown", "missing": [], "request": request,
                    "http_status": None, "detail": type(error).__name__}
        # sendMail, reply and forward answer 202; a chat message 201.
        ok = status in (200, 201, 202)
        result = {"status": "sent" if ok else "unknown" if status >= 500 else "failed",
                  "missing": [], "request": request, "http_status": status}
        if status == 403 and not ok:
            lack = missing_permission(kind, "")
            result["missing"] = [f"{lack} on {credential_prefix(self.environ)} app" if lack
                                 else "permission refused by Graph"]
        return result


def probe(environ: Mapping[str, str] | None = None, *,
          bearer: Callable[[], str] | None = None) -> dict[str, Any]:
    """What a live send would lack now, without sending anything: the flag, how many
    sender mailboxes are configured, and whether the app's Graph token carries the mail
    and chat permissions (one token request; booleans and counts only)."""
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {"flag_on": guard.action_send_enabled(env),
                           "credentials": credential_prefix(env) or "unknown",
                           "senders_configured": len(configured_senders(env)),
                           "token": "not_requested", "mail_send_granted": None,
                           "chat_send_granted": None, "sends": 0}
    if out["credentials"] == "unknown":
        out["token"] = "refused"
        return out
    try:
        token = (bearer or _default_bearer(env))()
    except (guard.SourceUnavailable, guard.ReadOnlyGuardError, ValueError, OSError):
        out["token"] = "refused"
        return out
    out["token"] = "ok"
    out["mail_send_granted"] = missing_permission("send_email", token) is None
    out["chat_send_granted"] = missing_permission("teams_message", token) is None
    return out


def main(argv: list[str] | None = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.graph_send probe", file=sys.stderr)
        return 2
    found = probe()
    print(json.dumps(found, sort_keys=True))
    return 0 if found["token"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
