"""Write readiness: which system an autopilot can write to now, re-checked on a timer.

Every 15 minutes (``deploy/systemd/plx-write-readiness.timer``) ``probe`` reads, per
system, what a live write needs and records it in the control directory
(``autopilot_write.control_dir``: ``write-readiness.json``, owner-only). Nothing here
writes to a business system: Graph and BC are read with the app's token (the token's
permission claims are read from the token itself; BC's company list is one GET through
the GET-only client), MC by its configuration. When a scope or credential appears (an
admin consent, an ``/etc/plx/vince-*.env`` file, the BC app registered in BC) the next
probe records the system ready, and the next executor sees it (``write_environ``,
``autopilot_write.opened_environ``) with no code change or restart. Only the
``live-<system>`` file stays a person's switch; this module never creates one.

Status per system:

* ``ready-live``: every scope and credential is there and the owner placed
  ``live-<system>``: an opted-in person's routine item is written for real.
* ``ready-dry-run``: everything is there except ``live-<system>`` (a person's switch).
  BC written in Production (no sandbox named) needs ``live-bc-production`` as well, and
  even then only a named person's confirmed approval of the item writes it.
* ``ready-but-scope-missing``: the credentials work, a permission is missing
  (``missing`` names it exactly).
* ``blocked``: no credential, an unreadable answer, or a structural block (``missing``).

Never prints or records a token, key or secret: only permission names, company names and
statuses.

CLI::

    python3 -m operating.autopilot_write_readiness probe --state DIR
    python3 -m operating.autopilot_write_readiness show --state DIR

Evidence label of tests: ``injected-transport``; live-unverified.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import glob
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

READINESS_FILE = "write-readiness.json"
SCHEMA = "plx-write-readiness/v1"
ENV_FILES_ENV = "PLX_WRITE_ENV_FILES"               # glob of credential env files
ENV_FILES_DEFAULT = "/etc/plx/vince-*.env"
# what an env file may add: credentials and where to write, never a switch (the
# PLX_WRITES_*/PLX_AUTOPILOT_WRITES*/PLX_APPROVED_WRITES/kill/live switches stay a person's)
ENV_FILE_KEYS = re.compile(r"^(?:AZURE_[A-Z0-9_]{1,60}|BC_[A-Z0-9_]{1,60}|MC_[A-Z0-9_]{1,60}"
                           r"|GRAPH_[A-Z0-9_]{1,60}|PLX_ACTIONS_GRAPH_CREDENTIALS"
                           r"|PLX_BC_SANDBOX_ENVIRONMENT|PLX_AUTOPILOT_BC_COMPANY_ID"
                           r"|PLX_AUTOPILOT_BC_ENVIRONMENT)$")
MAX_AGE = timedelta(minutes=45)        # a record older than three probes counts as none
GRAPH_NEEDS = {"calendar": "Calendars.ReadWrite", "planner": "Tasks.ReadWrite.All",
               "sharepoint": "Sites.Selected (or Sites.ReadWrite.All)"}
BC_ROLE = "API.ReadWrite.All"
# Ricardo 11-13: test and write in these BC companies only; a company whose name holds a
# denied fragment is never written, whatever else is configured
BC_COMPANIES_ALLOWED = ("PLX", "Petra Soap")
BC_COMPANY_DENIED = ("phsi",)
SYSTEMS = ("bc", "calendar", "planner", "sharepoint", "mc")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ------------------------------------------------------------------ credentials

def _env_file_values(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines of one env file the host keeps for us: refused (nothing read)
    unless it is a regular file not writable by group or others."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            return {}
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if ENV_FILE_KEYS.fullmatch(key) and value and "\n" not in value:
            out[key] = value
    return out


def write_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` plus the credentials in the host's env files (``ENV_FILES_ENV``, sorted
    by name; a later file does not override an earlier one, and the process environment
    wins over every file). Only credential and target names (``ENV_FILE_KEYS``) are read,
    never a switch. Read on every call: a file placed later counts at once."""
    env = dict(environ)
    pattern = str(env.get(ENV_FILES_ENV) or ENV_FILES_DEFAULT)
    for name in sorted(glob.glob(pattern))[:20]:
        for key, value in _env_file_values(Path(name)).items():
            env.setdefault(key, value)
    return env


# ------------------------------------------------------------------ probes

def _roles(token: str) -> frozenset[str]:
    from integrations.graph_send import token_permissions
    return token_permissions(token)[0]


def _graph_token(env: Mapping[str, str]) -> str:
    from integrations.graph_send import action_bearer
    return action_bearer(env)()


def _bc_token(env: Mapping[str, str]) -> str:
    from integrations import live_source_guard as guard
    return guard.TokenProvider(guard.ClientCredentials.from_env("BC", env), guard.BC_SCOPE,
                               guard.GuardedTransport(environ=env)).bearer()


def _bc_get(env: Mapping[str, str], token: str, url: str) -> Any:
    """One GET through the GET-only client with the token just issued."""
    from integrations import live_source_guard as guard

    class _Held:
        def bearer(self) -> str:
            return token
    api = guard.SourceApi("bc", _Held(), guard.GuardedTransport(environ=env))  # type: ignore[arg-type]
    return api.get_json(url)


def bc_environment(env: Mapping[str, str]) -> str:
    """The BC environment the autopilot writes to (``autopilot_write_adapters.BCAdapter``):
    its own setting, else the sandbox, else the read environment, else Production."""
    return str(env.get("PLX_AUTOPILOT_BC_ENVIRONMENT") or env.get("PLX_BC_SANDBOX_ENVIRONMENT")
               or env.get("BC_ENVIRONMENT") or "Production").strip()


def company_allowed(name: Any) -> bool:
    text = str(name or "").strip()
    if not text or any(d in text.lower() for d in BC_COMPANY_DENIED):
        return False
    return text.lower() in {c.lower() for c in BC_COMPANIES_ALLOWED}


def _status(missing: Sequence[str], scope_missing: bool, live: bool) -> str:
    if missing:
        return "ready-but-scope-missing" if scope_missing else "blocked"
    return "ready-live" if live else "ready-dry-run"


def _probe_graph(system: str, env: Mapping[str, str], token: Callable[[], str] | None
                 ) -> tuple[list[str], bool]:
    from integrations.graph_send import credential_prefix
    from integrations.write_transports import GRAPH_PERMISSIONS
    if credential_prefix(env) is None:
        return ["the approved-action app is not configured (PLX_ACTIONS_GRAPH_CREDENTIALS "
                "and its client credentials)"], False
    try:
        held = _roles((token or (lambda: _graph_token(env)))())
    except Exception:  # noqa: BLE001 - no token: blocked, the reason class only
        return ["the approved-action app's token could not be issued"], False
    if held & GRAPH_PERMISSIONS[system]:
        return [], False
    return [f"Microsoft Graph application permission {GRAPH_NEEDS[system]} "
            "(admin consent on the approved-action app)"], True


def _probe_bc(env: Mapping[str, str], token: Callable[[], str] | None,
              get: Callable[[str, str], Any] | None) -> tuple[list[str], bool, dict[str, Any]]:
    info: dict[str, Any] = {"environment": bc_environment(env)}
    company = str(env.get("PLX_AUTOPILOT_BC_COMPANY_ID") or "").strip().lower()
    tenant = str(env.get("BC_TENANT_ID") or "").strip().lower()
    missing: list[str] = []
    if not _GUID.fullmatch(tenant):
        missing.append("BC tenant id not configured (BC_TENANT_ID)")
    if not _GUID.fullmatch(company):
        missing.append("BC company id not configured (PLX_AUTOPILOT_BC_COMPANY_ID: the "
                       "PLX or Petra Soap company of the write environment)")
    if missing:
        return missing, False, info
    try:
        bearer = (token or (lambda: _bc_token(env)))()
    except Exception:  # noqa: BLE001
        return ["the BC app's token could not be issued (BC_* credentials)"], False, info
    scope_missing: list[str] = []
    if BC_ROLE not in _roles(bearer):
        scope_missing.append(f"Dynamics 365 Business Central application permission {BC_ROLE} "
                             "(admin consent on the BC_* app)")
    url = (f"https://api.businesscentral.dynamics.com/v2.0/{tenant}/{info['environment']}"
           "/api/v2.0/companies")
    try:
        found = (get or (lambda u, t: _bc_get(env, t, u)))(url, bearer)
    except Exception as error:  # noqa: BLE001 - 401/403: the app is not registered in BC
        status = getattr(error, "status", None)
        what = ("the BC_* app is not registered in BC (Microsoft Entra Applications) with "
                "the sales and purchase document edit permission sets"
                if status in (401, 403) else "BC company list unreadable")
        return [*scope_missing, what], True, info
    rows = found.get("value") if isinstance(found, Mapping) else None
    name = next((r.get("name") for r in rows or () if isinstance(r, Mapping)
                 and str(r.get("id") or "").lower() == company), None)
    info["company_name"] = str(name)[:80] if name else None
    if name is None:
        return [*scope_missing, "the configured BC company id is not in this environment"], \
            False, info
    if not company_allowed(name):
        return [*scope_missing, "the configured BC company is not one the autopilot may "
                "write (PLX or Petra Soap only, never PHSI Group)"], False, info
    info["company_verified"] = True
    info["company_id"], info["tenant_id"] = company, tenant
    return scope_missing, bool(scope_missing), info


def _probe_mc(env: Mapping[str, str]) -> tuple[list[str], bool, dict[str, Any]]:
    missing = [f"{n} not configured" for n in ("MC_MCP_API_KEY", "MC_OPERATOR_EMAIL")
               if not str(env.get(n) or "").strip()]
    info = {"task_note": "mc_report_progress (an active checkout of the task is needed)",
            "task_update": "blocked: mc_update_task is never sent (mc_tool_policy.json)",
            "approval_decide": "blocked: MC grants approval.decide only to a human session",
            "verified": "configuration only (no MC call is made by the probe)"}
    return missing, False, info


def probe(state: Path, environ: Mapping[str, str] | None = None, *,
          now: datetime | None = None,
          graph_token: Callable[[], str] | None = None,
          bc_token: Callable[[], str] | None = None,
          bc_get: Callable[[str, str], Any] | None = None) -> dict[str, Any]:
    """Every system's status (module doc); never raises for a system's failure."""
    from operating import autopilot_write as aw
    given = os.environ if environ is None else environ
    env = write_environ(given) if given is os.environ or ENV_FILES_ENV in given \
        else dict(given)
    live = aw.live_systems(state, env)
    now = now or datetime.now(timezone.utc)
    systems: dict[str, Any] = {}
    for system in SYSTEMS:
        info: dict[str, Any] = {}
        try:
            if system == "bc":
                missing, scope, info = _probe_bc(env, bc_token, bc_get)
            elif system == "mc":
                missing, scope, info = _probe_mc(env)
            else:
                missing, scope = _probe_graph(system, env, graph_token)
        except Exception as error:  # noqa: BLE001 - one system's failure never hides others
            missing, scope = [f"probe failed: {type(error).__name__}"], False
        flag = system in live
        if system == "bc" and str(info.get("environment") or "").lower() == "production":
            # Production goes live only with the owner's second file as well
            info["production_flag"] = aw.bc_production_on(state, env)
            flag = flag and info["production_flag"]
        systems[system] = {"status": _status(missing, scope, flag),
                           "missing": missing, "live_flag": flag, **info}
    return {"schema": SCHEMA, "checked_at": now.isoformat(timespec="seconds"),
            "systems": systems,
            "killed": aw.killed(env, Path(state), "") is not None}


def record(state: Path, found: Mapping[str, Any], environ: Mapping[str, str] | None = None
           ) -> Path:
    """Write ``found`` atomically (owner-only) into the control directory."""
    from operating import autopilot_write as aw
    folder = aw.control_dir(Path(state), os.environ if environ is None else environ)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".readiness-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(found, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, folder / READINESS_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return folder / READINESS_FILE


def read(state: Path, environ: Mapping[str, str], now: datetime | None = None
         ) -> dict[str, Any] | None:
    """The last recorded probe, None when absent, unreadable or older than ``MAX_AGE``."""
    from operating import autopilot_write as aw
    path = aw.control_dir(Path(state), environ) / READINESS_FILE
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(found["checked_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(found, dict) or found.get("schema") != SCHEMA or at.tzinfo is None:
        return None
    if (now or datetime.now(timezone.utc)) - at > MAX_AGE:
        return None
    return found


def bc_verified(state: Path, environ: Mapping[str, str], now: datetime | None = None) -> bool:
    """A recent probe found the BC app holding ``BC_ROLE`` and the configured company
    (same id, same environment) to be one the autopilot may write."""
    found = read(state, environ, now)
    bc = ((found or {}).get("systems") or {}).get("bc") or {}
    company = str(environ.get("PLX_AUTOPILOT_BC_COMPANY_ID") or "").strip().lower()
    tenant = str(environ.get("BC_TENANT_ID") or "").strip().lower()
    return (bc.get("status") in ("ready-live", "ready-dry-run")
            and bc.get("company_verified") is True
            and bool(company) and bc.get("company_id") == company
            and bool(tenant) and bc.get("tenant_id") == tenant
            and bc.get("environment") == bc_environment(environ)
            and company_allowed(bc.get("company_name")))


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot_write_readiness")
    p.add_argument("cmd", choices=("probe", "show"))
    p.add_argument("--state", required=True, type=Path)
    a = p.parse_args(argv)
    if a.cmd == "probe":
        found = probe(a.state)
        record(a.state, found)
    else:
        found = read(a.state, os.environ) or {"schema": SCHEMA, "systems": {}}
    rows = {k: {"status": v["status"], "missing": v["missing"]}
            for k, v in (found.get("systems") or {}).items()}
    print(json.dumps({"checked_at": found.get("checked_at"), "systems": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
