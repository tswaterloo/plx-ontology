"""Console session tickets for a named employee, minted and revoked on the host.

    python3 -m operator_ui.console_ticket mint --state-dir DIR --host-config FILE \\
        [--reader-env PLX_EMPLOYEE_READER] [--ttl-minutes 60] [--port 8765] --out FILE
    python3 -m operator_ui.console_ticket revoke --state-dir DIR --ticket-file FILE
    python3 -m operator_ui.console_ticket preflight --state-dir DIR --host-config FILE \\
        [--reader-env PLX_EMPLOYEE_READER]

The live console (``operator_ui.case_console``, ``python3 -m operating.host serve``) takes
each request's identity from a signed session ticket (author ``human``); the Portal does
not mint one yet. ``mint`` is the host's stand-in for it, for one recorded employee
session (requirement WS-16): it reads the employee's identity claims (``uid``, ``email``,
``entraTenantId``, ``entraObjectId``: the format ``PLX_EMPLOYEE_READER`` holds) from the
variable ``--reader-env`` names, signs a human ticket with ``PLX_WORKBENCH_SESSION_SECRET``
for at most two hours, and verifies it exactly as the console will
(``session_identity.employee_session``: the host config's employee policy and Portal access
snapshot must grant that employee now, the ticket's e-mail must be the Portal row's, the
state directory's replay guard must not have revoked it). Only then is the console URL
(``http://127.0.0.1:PORT/#access=<ticket>``) written to ``--out``, a new file readable by
its owner only, for the employee's own browser. The ticket is never printed: the command
prints the employee principal, the expiry and the session reference (the SHA-256 of the
ticket id, which ``operator_ui.session_journal summary --session`` takes). A refusal prints
its reason code, writes nothing and exits 2. The state directory must be live
(``session_identity.state_label``): a fixture or dev directory, or a new one, is refused.

Each successful mint and revoke appends one JSON line to ``DIR/console-ticket-audit.jsonl``
(``AUDIT_FILE``; opened append-only, created with mode 0600, never through a link):
``{event: mint|revoke, at, session_ref, employee, expires_at, ttl, os_user,
minted_by|revoked_by: host}`` (``minted_by`` on a mint row, ``revoked_by`` on a revoke row). ``employee`` is the principal ``employee:<Entra object id>`` (never the
e-mail), ``ttl`` the ticket's lifetime in seconds and ``os_user`` the host account that ran
the command. The row never holds the ticket, its id, an e-mail address or the secret; it
is what tells a host-minted console session from a Portal-minted one
(``operator_ui.session_journal summary`` reports ``minted_by`` per session). A mint whose
audit row cannot be written refuses with ``audit_unwritable`` and leaves no URL file (one
whose URL cannot be written after its row refuses with ``out_unwritable`` and removes the
file, so its row names a session no one was handed); a
revoke whose row cannot be written still revokes (ending a session is never blocked) and
prints ``"audit": "audit_unwritable"`` and exits 2.

``revoke`` makes the ticket in ``--ticket-file`` unusable on every host over DIR for the
rest of its lifetime (the replay guard ``session_identity.replay_guard_for`` shares) and
deletes the file; an already expired ticket needs no revocation and its file is deleted.

``preflight`` mints nothing and reads no business system: it checks, by name, the run
book's preconditions on this host (``employee_policy`` and ``portal_access`` named by the
host config and present, ``session_secret`` set with at least ``MIN_SECRET_BYTES``,
``state_dir`` a live, existing directory, ``reader_env`` holding the four identity claims)
and prints ``{status: ready|not_ready, checks: {name: ok|<reason code>}}``; exit 2 when one
is missing. No secret, claim, e-mail or identifier is printed.

No network: the claims, the policy and the snapshot are local files and variables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from operating import session_identity
from operating.employee_permissions import EmployeePrincipal
from operating.session_identity import SessionIdentityRefused

from .session_journal import AUDIT_FILE, MINTED_BY_HOST


READER_ENV = "PLX_EMPLOYEE_READER"
READER_CLAIMS = ("uid", "email", "entraTenantId", "entraObjectId")
URL_MARK = "#access="
MAX_TICKET_FILE = 8192


class TicketRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def reader_claims(environ: Mapping[str, str], name: str) -> dict[str, str]:
    """The named employee's claims, or ``TicketRefused`` (the value is never shown)."""
    raw = environ.get(name) or ""
    if not raw.strip():
        raise TicketRefused("no_employee_identity")
    try:
        claims = json.loads(raw)
    except ValueError:
        raise TicketRefused("reader_identity_invalid") from None
    if not isinstance(claims, dict) or not all(
            isinstance(claims.get(key), str) and claims[key].strip() for key in READER_CLAIMS):
        raise TicketRefused("reader_identity_invalid")
    return {key: claims[key].strip() for key in READER_CLAIMS}


def _live_state(state_dir: Path) -> Path:
    state_dir = state_dir.resolve()
    label = session_identity.state_label(state_dir) if state_dir.is_dir() else None
    if label != "live":
        raise TicketRefused(f"state_not_live:{label or 'new'}")
    return state_dir


def _os_user() -> str:
    """The host account running the command (the effective uid's name, else the uid)."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return f"uid:{os.geteuid()}" if hasattr(os, "geteuid") else "unknown"


def append_audit(state_dir: Path, *, event: str, session_ref: str, employee: str | None,
                 expires_at: int, ttl: int) -> None:
    """Append one audit row to ``state_dir/AUDIT_FILE``; ``TicketRefused`` if it cannot
    be written whole. Holds no ticket, ticket id, e-mail address or secret."""
    row = {"event": event,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")
           .replace("+00:00", "Z"),
           "session_ref": session_ref, "employee": employee, "expires_at": expires_at,
           "ttl": ttl, "os_user": _os_user(),
           "minted_by" if event == "mint" else "revoked_by": MINTED_BY_HOST}
    line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) \
        | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(state_dir / AUDIT_FILE, flags, 0o600)
    except OSError:
        raise TicketRefused("audit_unwritable") from None
    try:
        import stat
        if not stat.S_ISREG(os.fstat(descriptor).st_mode) \
                or os.write(descriptor, line) != len(line):
            raise TicketRefused("audit_unwritable")
        os.fsync(descriptor)
    except OSError:
        raise TicketRefused("audit_unwritable") from None
    finally:
        os.close(descriptor)


def _resolver(args: argparse.Namespace) -> Any:
    from .case_console import employee_resolver

    policy, portal = args.employee_policy, args.portal_access
    if args.host_config is not None:
        from operating.host import HostConfig, HostError
        try:
            config = HostConfig.from_file(args.host_config)
        except HostError as error:
            raise TicketRefused(error.code) from None
        policy, portal = config.employee_policy, config.portal_access
    if policy is None:
        raise TicketRefused("employee_policy_not_configured")
    try:
        return employee_resolver(policy, portal)
    except ValueError:
        raise TicketRefused("employee_policy_unusable") from None


def mint(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    state_dir = _live_state(args.state_dir)
    secret = session_identity.session_secret(environ)
    if secret is None:
        raise TicketRefused("session_secret_unavailable")
    claims = reader_claims(environ, args.reader_env)
    resolver = _resolver(args)
    try:
        ticket = session_identity.mint_session_ticket(
            uid=claims["uid"], email=claims["email"], tenant_id=claims["entraTenantId"],
            object_id=claims["entraObjectId"], author=session_identity.AUTHOR_HUMAN,
            secret=secret, ttl_seconds=args.ttl_minutes * 60)
        # Exactly the console's check, now: a ticket it would refuse is never handed out.
        session = session_identity.employee_session(
            resolver, channel=session_identity.CHANNEL_CONSOLE, ticket=ticket, secret=secret,
            replay_guard=session_identity.replay_guard_for(state_dir))
    except SessionIdentityRefused as error:
        raise TicketRefused(error.reason) from None
    out = args.out.resolve()
    try:
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise TicketRefused("out_exists") from None
    except OSError:
        raise TicketRefused("out_unwritable") from None
    # The URL file exists (empty) before the audit row and holds the URL only after it:
    # a row is written for every URL handed out, and a mint that cannot be audited leaves
    # no file behind.
    try:
        append_audit(state_dir, event="mint", session_ref=session.session_ref,
                     employee=session.employee.principal, expires_at=session.expires_at,
                     ttl=args.ttl_minutes * 60)
    except TicketRefused:
        os.close(descriptor)
        out.unlink()
        raise
    try:
        handle = os.fdopen(descriptor, "w", encoding="ascii")
    except OSError:
        os.close(descriptor)
        out.unlink(missing_ok=True)
        raise TicketRefused("out_unwritable") from None
    try:
        with handle:
            handle.write(f"http://127.0.0.1:{args.port}/{URL_MARK}{ticket}\n")
    except OSError:   # e.g. a full disk: no partial URL file blocks the next mint
        out.unlink(missing_ok=True)
        raise TicketRefused("out_unwritable") from None
    return {"status": "minted", "employee": session.employee.principal,
            "expires_at": session.expires_at, "session": session.session_ref,
            "out": str(out)}


def revoke(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    state_dir = _live_state(args.state_dir)
    secret = session_identity.session_secret(environ)
    if secret is None:
        raise TicketRefused("session_secret_unavailable")
    path = args.ticket_file
    try:
        text = path.read_text("ascii")[:MAX_TICKET_FILE].strip()
    except (OSError, UnicodeDecodeError):
        raise TicketRefused("ticket_file_unreadable") from None
    ticket = text.split(URL_MARK, 1)[1] if URL_MARK in text else text
    try:
        claims = session_identity.verify_session_ticket(ticket, secret)
    except SessionIdentityRefused as error:
        if error.reason != "session_ticket_expired":
            raise TicketRefused(error.reason) from None
        path.unlink()
        return {"status": "expired", "ticket_file": "deleted"}
    try:
        session_identity.replay_guard_for(state_dir).revoke(claims)
    except SessionIdentityRefused as error:   # a full guard: the ticket stays usable
        raise TicketRefused(error.reason) from None
    path.unlink()
    session_ref = hashlib.sha256(claims["jti"].encode("utf-8")).hexdigest()
    try:
        employee: str | None = EmployeePrincipal(claims["entraTenantId"],
                                                 claims["entraObjectId"]).principal
    except ValueError:   # never the raw claim: it is not checked to be an object id
        employee = None
    result = {"status": "revoked", "expires_at": claims["exp"], "ticket_file": "deleted",
              "session": session_ref}
    try:
        append_audit(state_dir, event="revoke", session_ref=session_ref, employee=employee,
                     expires_at=claims["exp"], ttl=claims["exp"] - claims["iat"])
    except TicketRefused as refusal:   # revoked all the same; the operator is told
        result["audit"] = refusal.reason
    return result


def preflight(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, Any]:
    """Each runbook precondition ``mint`` needs, by name, ``ok`` or its reason code."""
    from operating.host import HostConfig, HostError
    checks: dict[str, str] = {}
    try:
        config = HostConfig.from_file(args.host_config)
        checks["employee_policy"] = "ok" if config.employee_policy is not None \
            and config.employee_policy.is_file() else "employee_policy_not_configured"
        checks["portal_access"] = "ok" if config.portal_access is not None \
            and config.portal_access.is_file() else "portal_access_not_configured"
    except HostError as error:
        checks["employee_policy"] = checks["portal_access"] = error.code
    secret = session_identity.session_secret(environ)
    checks["session_secret"] = "session_secret_unavailable" if secret is None else (
        "ok" if len(secret.encode("utf-8")) >= session_identity.MIN_SECRET_BYTES
        else "session_secret_too_short")
    try:
        _live_state(args.state_dir)
        checks["state_dir"] = "ok"
    except TicketRefused as refusal:
        checks["state_dir"] = refusal.reason
    try:
        reader_claims(environ, args.reader_env)
        checks["reader_env"] = "ok"
    except TicketRefused as refusal:
        checks["reader_env"] = refusal.reason
    ready = all(value == "ok" for value in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}


def _ttl(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 120:
        raise argparse.ArgumentTypeError("1..120 minutes")
    return int(raw)


def _port(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 65535:
        raise argparse.ArgumentTypeError("1..65535")
    return int(raw)


def main(argv: Sequence[str] | None = None, *,
         environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operator_ui.console_ticket",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    minting = sub.add_parser("mint", help="a human console ticket for a named employee")
    minting.add_argument("--state-dir", required=True, type=Path)
    source = minting.add_mutually_exclusive_group(required=True)
    source.add_argument("--host-config", type=Path,
                        help="plx-host-config/v1: its employee_policy and portal_access")
    source.add_argument("--employee-policy", type=Path)
    minting.add_argument("--portal-access", type=Path, default=None,
                         help="with --employee-policy: the Portal access snapshot")
    minting.add_argument("--reader-env", default=READER_ENV,
                         help="variable holding the employee's JSON identity claims")
    minting.add_argument("--ttl-minutes", type=_ttl, default=60)
    minting.add_argument("--port", type=_port, default=8765,
                         help="the console's loopback port, for the URL")
    minting.add_argument("--out", required=True, type=Path,
                         help="new file for the console URL (mode 0600)")
    revoking = sub.add_parser("revoke", help="end a console session's ticket")
    revoking.add_argument("--state-dir", required=True, type=Path)
    revoking.add_argument("--ticket-file", required=True, type=Path)
    checking = sub.add_parser("preflight", help="check the run book's preconditions")
    checking.add_argument("--state-dir", required=True, type=Path)
    checking.add_argument("--host-config", required=True, type=Path)
    checking.add_argument("--reader-env", default=READER_ENV)
    args = parser.parse_args(argv)
    if args.command == "mint" and args.host_config is not None \
            and args.portal_access is not None:
        parser.error("--portal-access goes with --employee-policy; a host config names its own")
    environ = os.environ if environ is None else environ
    if args.command == "preflight":
        result = preflight(args, environ)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "ready" else 2
    try:
        result = (mint if args.command == "mint" else revoke)(args, environ)
    except TicketRefused as refusal:
        print(json.dumps({"status": "refused", "reason": refusal.reason}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 2 if "audit" in result else 0


if __name__ == "__main__":
    sys.exit(main())
