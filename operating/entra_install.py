"""The Entra sign-in install for the app API (S-06, row O-02): render its files, keep the
tenant's signing keys current, and check an install is consistent without signing anyone in.

``render``   writes, into a directory the operator reviews (never ``/etc``):
             ``entra-resource.json`` (``plx-entra-resource/v1``: the committed tenant,
             client id, Application ID URI and scope from ``operating/entra_resource.json``,
             the app's canonical URL as ``resource`` and ``entra-jwks.json`` beside it),
             ``workbench-api.env.entra`` (the served env file with ``PLX_API_ARGS`` swapped
             from ``--dev --dev-ticket-login`` to ``--entra-resource <file>`` and
             ``PLX_APP_REDIRECT_URI`` set), and the JWKS refresh service and timer.
``refresh-jwks`` reads the tenant's public signing keys (``GET https://login.microsoftonline.
             com/<tenant>/discovery/v2.0/keys``: public metadata, no credential, no business
             system) and replaces the file atomically only with a document that parses and
             holds at least one key. The API re-reads the file when it changes
             (``oidc_auth.JwksFile``); without the refresh, Microsoft's key rotation would
             refuse every reader's token.
``check``    reads an install (the env file, the resource file, its JWKS, the host config
             and the Portal snapshot it names, the people exclusion) and, with ``--api-url``,
             the API's public ``GET /api/auth/config``; prints one line per check, ``ok`` or
             a reason, never a value, and exits 1 when any fails. It signs nobody in and
             mints nothing.

Evidence: ``isolated-fixture`` tests (``tests/test_entra_install.py``).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from . import oidc_auth

ROOT = Path(__file__).resolve().parent.parent
COMMITTED = ROOT / "operating" / "entra_resource.json"
JWKS_NAME = "entra-jwks.json"
JWKS_MAX_AGE_HOURS = 48.0     # the timer runs every 6 hours; two days old means it stopped
DEV_FLAGS = ("--dev", "--dev-ticket-login", "--mock")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class InstallError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ render

def resource_document(app_host: str, committed: Mapping[str, Any]) -> dict[str, Any]:
    """The installed resource file: the committed identifiers, the app's URL, the JWKS
    file name. Refuses a host that is not a plain DNS name."""
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", app_host or ""):
        raise InstallError("app_host_invalid")
    doc = {k: committed[k] for k in ("tenant_id", "client_id", "app_id_uri", "required_scope")}
    doc.update(schema=oidc_auth.RESOURCE_SCHEMA, resource=f"https://{app_host}/",
               jwks_file=JWKS_NAME,
               note="Installed by operating.entra_install render. Identifiers only, never "
                    "a secret. The JWKS beside it is kept current by plx-entra-jwks.timer.")
    return doc


def redirect_uri(app_host: str) -> str:
    """The SPA redirect URI the app registration must list (the app's /signin page,
    ``app/src/lib/msal.ts``)."""
    return f"https://{app_host}/signin"


def swap_api_args(env_text: str, resource_path: str, redirect: str,
                  host_config: str | None = None) -> str:
    """The env file with ``PLX_API_ARGS`` in Entra mode (and ``--host-config`` re-pointed
    when ``host_config`` is given) and ``PLX_APP_REDIRECT_URI`` set; every other line
    kept. Refuses a file without ``PLX_API_ARGS``."""
    lines, seen = [], False
    for line in env_text.splitlines():
        if line.startswith("PLX_API_ARGS="):
            seen = True
            args = [a for a in shlex.split(line.split("=", 1)[1]) if a not in DEV_FLAGS]
            if "--entra-resource" in args:
                i = args.index("--entra-resource")
                del args[i:i + 2]
            if host_config is not None and "--host-config" in args:
                args[args.index("--host-config") + 1] = host_config
            lines.append("PLX_API_ARGS=" + " ".join(["--entra-resource", resource_path] + args))
        elif line.startswith("PLX_APP_REDIRECT_URI="):
            continue
        else:
            lines.append(line)
    if not seen:
        raise InstallError("api_args_absent")
    lines += ["# Entra sign-in (operating.entra_install render): the app's SPA redirect URI.",
              f"PLX_APP_REDIRECT_URI={redirect}"]
    return "\n".join(lines) + "\n"


def units(python: str, resource_dir: str) -> dict[str, str]:
    service = f"""[Unit]
Description=PLX Workbench: refresh the Entra tenant's signing keys
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={ROOT}
ExecStart={python} -m operating.entra_install refresh-jwks --resource {resource_dir}/entra-resource.json
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={resource_dir}
PrivateTmp=true
"""
    timer = """[Unit]
Description=PLX Workbench: refresh the Entra signing keys every 6 hours

[Timer]
OnBootSec=2min
OnUnitActiveSec=6h
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {"plx-entra-jwks.service": service, "plx-entra-jwks.timer": timer}


# ------------------------------------------------------------------ JWKS

def jwks_url(tenant: str) -> str:
    if not _GUID.fullmatch(tenant or ""):
        raise InstallError("tenant_invalid")
    return f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


def _fetch(url: str) -> Any:
    """One GET of the tenant's public signing keys through the shared GET-only client
    (integrations.http.get_json: no redirect, bounded body); the host is pinned first."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "login.microsoftonline.com":
        raise InstallError("jwks_url_refused")
    from integrations.http import SourceUnavailable, get_json
    try:
        return get_json(url, {"Accept": "application/json"}, max_bytes=256 * 1024,
                        timeout_seconds=30)
    except SourceUnavailable as exc:
        if "exceeds" in str(exc):
            raise InstallError("jwks_too_large") from None
        raise


def refresh_jwks(resource_file: Path, *, fetch: Callable[[str], Any] = _fetch) -> int:
    """The number of signing keys now in the resource's JWKS file; the file is replaced
    only by a document that parses and holds a key (a failed read keeps the old file)."""
    doc = json.loads(resource_file.read_text("utf-8"))
    target = resource_file.resolve().parent / doc.get("jwks_file", JWKS_NAME)
    try:
        fresh = fetch(jwks_url(str(doc.get("tenant_id", "")).lower()))
    except InstallError:
        raise
    except Exception:  # noqa: BLE001 - the old keys stay; the check reports their age
        raise InstallError("jwks_unreadable") from None
    try:
        keys = oidc_auth.parse_jwks(fresh)
    except ValueError:
        raise InstallError("jwks_invalid") from None
    if not keys:
        raise InstallError("jwks_empty")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(fresh), "utf-8")
    os.chmod(tmp, 0o644)       # public keys; the API's user must read them
    os.replace(tmp, target)
    return len(keys)


# ------------------------------------------------------------------ check

def read_env(path: Path) -> dict[str, str]:
    return read_env_text(path.read_text("utf-8"))


def read_env_text(text: str) -> dict[str, str]:
    """``KEY=value`` lines of a systemd environment file (quotes stripped)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def _arg(args: Sequence[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) \
        else None


def check(env_file: Path, *, app_host: str | None = None, environ: Mapping[str, str] = {},
          api_config: Callable[[], Any] | None = None, now: float | None = None
          ) -> list[tuple[str, str]]:
    """``[(check, "ok" | reason)]`` for one install. Never a value in a reason."""
    results: list[tuple[str, str]] = []

    def put(name: str, ok: bool, reason: str) -> bool:
        results.append((name, "ok" if ok else reason))
        return ok

    try:
        env = read_env(env_file)
    except OSError:
        put("env_file", False, "unreadable")
        return results
    args = shlex.split(env.get("PLX_API_ARGS", ""))
    put("api_args_entra", "--entra-resource" in args and not set(DEV_FLAGS) & set(args),
        "PLX_API_ARGS must name --entra-resource and no --dev/--dev-ticket-login/--mock")
    redirect = env.get("PLX_APP_REDIRECT_URI", "")
    parts = urlsplit(redirect)
    put("redirect_uri", parts.scheme == "https" and bool(parts.hostname)
        and not parts.query and not parts.fragment
        and (app_host is None or parts.hostname == app_host),
        "PLX_APP_REDIRECT_URI must be https on the app's host (unset: sign-in stays off)")
    resource_path = _arg(args, "--entra-resource")
    resource = None
    if resource_path:
        try:
            resource = oidc_auth.EntraResource.from_file(resource_path)
            put("resource_file", True, "")
        except oidc_auth.EntraResourceUnset as unset:
            put("resource_file", False, "fields still unset: " + ",".join(unset.unset))
        except (OSError, ValueError) as error:
            put("resource_file", False, "invalid: " + (str(error) if isinstance(error, ValueError)
                                                       else type(error).__name__))
    committed = json.loads(COMMITTED.read_text("utf-8"))
    if resource is not None:
        put("resource_matches_registration",
            resource.tenant_id == committed["tenant_id"]
            and committed["client_id"] in resource.audiences
            and resource.required_scope == committed["required_scope"],
            "tenant, client id or scope differ from operating/entra_resource.json")
        put("resource_uri_host", app_host is None or urlsplit(resource.resource).hostname
            == app_host, "resource is not the app's https URL")
        doc = json.loads(Path(resource_path).read_text("utf-8"))
        jwks = Path(resource_path).resolve().parent / doc["jwks_file"]
        try:
            keys = oidc_auth.parse_jwks(json.loads(jwks.read_text("utf-8")))
            age = ((time.time() if now is None else now) - jwks.stat().st_mtime) / 3600
            put("jwks_keys", bool(keys), "the JWKS file holds no signing key")
            put("jwks_fresh", age <= JWKS_MAX_AGE_HOURS,
                f"the JWKS file is {age:.0f} h old: is plx-entra-jwks.timer running?")
        except (OSError, ValueError):
            put("jwks_keys", False, "the JWKS file is unreadable or invalid")
    host_config = _arg(args, "--host-config")
    if not put("host_config", bool(host_config), "PLX_API_ARGS names no --host-config"):
        return results
    _check_host(Path(host_config), resource, environ, put)
    if api_config is not None:
        try:
            body = api_config()
            entra = body.get("entra") or {} if isinstance(body, dict) else {}
            put("api_auth_config", isinstance(body, dict) and body.get("mode") == "entra"
                and body.get("sign_in_available") is True
                and entra.get("redirect_uri") == redirect,
                "the running API does not serve Entra sign-in with this redirect URI")
        except Exception as error:  # noqa: BLE001 - a probe failure is a failed check
            put("api_auth_config", False, "unreachable: " + type(error).__name__)
    return results


def _check_host(path: Path, resource: Any, environ: Mapping[str, str],
                put: Callable[[str, bool, str], bool]) -> None:
    from . import employee_permissions as ep
    try:
        host = json.loads(path.read_text("utf-8"))
        policy = ep.EmployeePermissionPolicy.from_file(path.parent / host["employee_policy"])
    except (OSError, ValueError, KeyError, TypeError):
        put("employee_policy", False, "the host config or its employee policy does not load")
        return
    put("employee_policy", True, "")
    if resource is not None:
        put("policy_tenant", policy.entra_tenant_id == resource.tenant_id,
            "the policy's tenant is not the resource's (EntraAuth refuses to start)")
    snapshot_path = host.get("portal_access")
    bound = host.get("portal_access_max_age_hours")
    put("snapshot_age_bound", isinstance(bound, (int, float)) and 0 < bound <= 72,
        "portal_access_max_age_hours unset: a Portal deactivation would never apply")
    if not put("portal_snapshot", bool(snapshot_path), "the host config names no snapshot"):
        return
    status = ep.portal_access_status(path.parent / snapshot_path, max_age_hours=bound)
    put("portal_snapshot_fresh", status["state"] == "fresh",
        "snapshot " + status["state"] + (": " + status["reason"] if status.get("reason")
                                          else ""))
    snapshot = json.loads((path.parent / snapshot_path).read_text("utf-8"))
    note = str(snapshot.get("source", ""))
    put("portal_snapshot_live", "eval_owner_host" not in note and "synthetic" not in note,
        "the snapshot is the synthetic eval one, not a live Portal read")
    listed = {str(u.get("entraObjectId", "")).lower() for u in snapshot.get("users", [])}
    missing = [g for g in policy.full_read if g not in listed]
    put("owners_in_snapshot", not missing,
        f"{len(missing)} full-read owner(s) have no Portal row: they could not sign in")
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    guard = PeopleExclusion.from_env(environ_with_exclusion(environ))
    if put("people_exclusion", not guard.unavailable,
           "PLX_EXCLUDED_PEOPLE_OIDS unusable: every read and sign-in path refuses"):
        put("excluded_not_in_snapshot", not any(guard.is_excluded(o) for o in listed if o),
            "a person the exclusion names has a snapshot row")


def _api_config(url: str) -> Callable[[], Any]:
    """A GET of the local API's ``/api/auth/config`` (loopback only, no redirect)."""
    from integrations.http import get_local_json
    parts = urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise InstallError("api_url_not_loopback")
    return lambda: get_local_json(url.rstrip("/") + "/api/auth/config")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.entra_install",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--app-host", required=True)
    r.add_argument("--env-file", type=Path, default=Path("/etc/plx/workbench-api.env"))
    r.add_argument("--install-dir", default="/etc/plx/entra")
    r.add_argument("--python", default="/data/venv/bin/python3")
    r.add_argument("--host-config", help="the installed rollout host config")
    j = sub.add_parser("refresh-jwks")
    j.add_argument("--resource", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("--env-file", type=Path, default=Path("/etc/plx/workbench-api.env"))
    c.add_argument("--app-host")
    c.add_argument("--api-url", help="loopback API base, e.g. http://127.0.0.1:8780")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            out = args.out.resolve()
            if out == ROOT or ROOT in out.parents or out == Path("/etc") or str(out).startswith("/etc/"):
                raise InstallError("out_dir_forbidden")
            out.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(out, 0o700)        # the env copy holds the database address
            doc = resource_document(args.app_host, json.loads(COMMITTED.read_text("utf-8")))
            (out / "entra-resource.json").write_text(json.dumps(doc, indent=1) + "\n")
            oidc_auth.check_resource_file(out / "entra-resource.json")
            env_text = args.env_file.read_text("utf-8")
            (out / "workbench-api.env.entra").write_text(swap_api_args(
                env_text, f"{args.install_dir}/entra-resource.json", redirect_uri(args.app_host),
                args.host_config))
            os.chmod(out / "workbench-api.env.entra", 0o600)
            for name, text in units(args.python, args.install_dir).items():
                (out / name).write_text(text)
            print(json.dumps({"rendered": sorted(p.name for p in out.iterdir()),
                              "redirect_uri": redirect_uri(args.app_host)}))
            return 0
        if args.command == "refresh-jwks":
            print(json.dumps({"keys": refresh_jwks(args.resource)}))
            return 0
        results = check(args.env_file, app_host=args.app_host, environ=os.environ,
                        api_config=_api_config(args.api_url) if args.api_url else None)
    except InstallError as refusal:
        print(json.dumps({"refused": refusal.code}), file=sys.stderr)
        return 2
    width = max(len(name) for name, _ in results)
    for name, outcome in results:
        print(f"{name.ljust(width)}  {outcome}")
    failed = sum(outcome != "ok" for _, outcome in results)
    print(f"{len(results) - failed} ok, {failed} failing at "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
