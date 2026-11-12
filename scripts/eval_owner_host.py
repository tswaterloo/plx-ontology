"""Write the owner-eval host config for the case-card scoreboard (``eval.case_cards_answers``).

The scoreboard must measure the product as a real employee reads it, not as the declared
eval principal (which holds no source audience and so only measures abstention). This tool
derives, from the committed ``examples/host.all-sources.live-check.json`` scope and the
named reader in ``PLX_EVAL_READER`` (the claims JSON: uid, email, entraTenantId,
entraObjectId), one directory holding:

  host.json                  the all-sources host config plus ``employee_policy``,
                             ``portal_access`` and the finance marking grant
  employee_policy.json       the reader's tenant and the staff principals the reader holds
  portal_access.json         one Portal snapshot row: the reader, an active SUPER_ADMIN, L5
  mirror_audience.json       the all-sources mirror audience with the owner principals added
  live_audience.json         the all-sources live audience with the owner principals added,
                             plus mail, Teams, SharePoint and Mission Control, and the code
                             repositories the case-originals registry names
                             (``repo:<owner>/<name>``, exact, one grant per repository)

Only the reader's grant changes. Default deny, per-item ACLs, the people-exclusion guard
(``PLX_EXCLUDED_PEOPLE_OIDS``, read by the host at run time) and the per-reader gating are the
host's own and are untouched. So the audience grant is an upper bound, not what the reader
is served: a SharePoint file still needs its own permission list to name the reader, and a
mailbox the reader does not own stays withheld (``undetermined: shared mailbox``,
``operating.case_runtime._employee_acl_denial``: Graph cannot prove delegated access). The files carry the reader's identity, so the directory is
created 0700 and its files 0600; keep it in a scratch directory, never in the repository.
Never prints a claim.

    python3 -m scripts.eval_owner_host --out DIR      # then PLX_WORKBENCH_HOST_CONFIG=DIR/host.json
    python3 -m scripts.eval_owner_host --out DIR --reader-env PLX_EVAL_READER_VINCE --full-read

``--role purchasing`` (or ``quality``) writes a restricted reader instead: the same named
employee granted only that staff principal and its marking (``ROLE_MARKINGS``:
purchasing prices, or quality-controlled results), never finance and never full read, so the
same questions show what permissions withhold (``scripts.eval_live_ops --reader``). Every
audience still lists the principal: only markings and the reader's own mailbox restrict it.

``--role sales`` holds the order and customer principals and no marking.
``--department-member`` (with ``--role``) makes the reader an ordinary employee of the role's
Portal department (``ROLE_DEPARTMENTS``: a MANAGER at L3 viewing operations, never an admin
role, no Entra directory read), so the briefing routes it by department as it routes any
non-admin employee; without it a role reader is the SUPER_ADMIN the scoreboard measures.

``--full-read`` adds the declared full-read grant (``employee_policy.json`` ``full_read``):
the reader opens every captured mailbox, SharePoint item and Teams conversation and holds
the finance and price markings, still under the people exclusion and the host audiences.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BASE_CONFIG = EXAMPLES / "host.all-sources.live-check.json"
OWNER_PRINCIPALS = ("staff:operations", "staff:purchasing", "staff:warehouse", "staff:quality",
                    "staff:finance", "staff:customers")
# The markings an owner/operator holds: exactly those the live-ops keys grade the owner
# reader as holding (``evals.live_ops.READERS["vince"]``), as the live owner host grants
# them. A host holding fewer hides facts the key calls visible (a QC status, a PO price),
# and the scoreboard counts the forced abstention as the agent's. The others stay unheld.
OWNER_MARKINGS = ("finance", "purchasing_prices", "quality_controlled")
LIVE_PREFIXES = ("mail:*", "sharepoint:*", "teams:*", "mc:*", "bc:*")
ROLE = "SUPER_ADMIN"
DEPARTMENT = "owner-eval-department"  # one id, declared in the policy and the snapshot row


def reader_claims(environ: Mapping[str, str], name: str | None = None) -> dict[str, str]:
    from scripts import eval_real_agent as real
    claims, problem = real.reader_identity(environ, name or real.READER_ENV)
    if claims is None:
        raise ValueError(problem or "reader_identity_missing")
    return dict(claims)


# A restricted reader's principals and the marking it holds, if any (``--role``), and the
# Portal department a ``--department-member`` of that role is confirmed in.
ROLES = {"purchasing": (("staff:purchasing",), "purchasing_prices"),
         "quality": (("staff:quality",), "quality_controlled"),
         "sales": (("staff:operations", "staff:customers"), None)}
ROLE_DEPARTMENTS = {"purchasing": "PROC", "quality": "QUALITY", "sales": "SALES"}
# A department member is an ordinary employee: a Portal MANAGER (L3, below the admin roles
# that are routed everything) who views operations in its scopes.
MEMBER_ROLE, MEMBER_LEVEL, MEMBER_CAPABILITY = "MANAGER", "L3", "VIEW_OPERATIONS"
DATA_KEYS = {"staff:operations": "operations.sales", "staff:purchasing": "operations.procurement",
             "staff:warehouse": "operations.warehouse", "staff:quality": "operations.quality",
             "staff:finance": "operations.finance", "staff:customers": "crm.customers"}

SCOPES = ([{"scopeType": "COMPANY", "scopeKey": "petra-hygienic-plx", "effect": "ALLOW"}]
          + [{"scopeType": "DATA", "scopeKey": key, "effect": "ALLOW"}
             for key in sorted(set(DATA_KEYS.values()))])


def _principal(entry: str, capability: str = "MANAGE_OPERATIONS") -> dict[str, Any]:
    return {"capability": capability, "department_ids": [DEPARTMENT],
            "company_key": "petra-hygienic-plx", "data_key": DATA_KEYS[entry]}


REGISTRY = EXAMPLES / "case_originals.json"


def registry_repositories(registry: Mapping[str, Any]) -> list[str]:
    """The ``repo:<owner>/<name>`` resources of the repository-sourced originals the
    case-originals registry names (each parsed as the serving gate parses it,
    ``operating.captured_originals.source_unit``; a malformed reference names none)."""
    from operating.captured_originals import repository_resource, source_unit
    found = set()
    for row in registry.get("rows") or []:
        source = row.get("source") if isinstance(row, Mapping) else None
        if not isinstance(source, Mapping) or not source.get("repository_ref"):
            continue
        unit, _ = source_unit({"canonical_source": {"repository_ref": source["repository_ref"]}})
        resource = repository_resource(unit) if unit else None
        if resource:
            found.add(resource)
    return sorted(found)


def full_read_grant(claims: Mapping[str, str], granted_at: str,
                    granted_by: str = "workbench owner") -> dict[str, Any]:
    """The declared full-read grant for the named reader: every scope, every mailbox
    (``operating.employee_permissions.FullReadGrant``)."""
    from operating.employee_permissions import FULL_READ_SCOPES
    return {"object_id": claims["entraObjectId"].lower(), "granted_by": granted_by,
            "granted_at": granted_at, "scopes": list(FULL_READ_SCOPES), "mail_scope": "all",
            "reason": "The demo, its live checks and scoreboard read as the named reader, "
                      "who may view everything captured."}


def directory_read(full_read: bool, department_member: bool) -> bool:
    """Whether the eval host reads the reader's Entra directory (``entra_directory``, a live
    Graph read needing ``GRAPH_ENV``). The generated policy declares no Entra-gated
    principal (no ``entra_groups``, ``entra_departments`` or job-title keywords), so the
    directory never changes the reader's principals, clearance or markings; it only adds
    the reader's Entra groups to the identities a per-item ACL is matched against and its
    Entra department to routing. A full-read reader opens every captured item whatever its
    ACL, so its run reads the same without it: off, and the scoreboard runs offline. A
    reader without full read keeps it (a group-shared SharePoint file is gated by the
    groups it reads); a department member is routed by its Portal department alone."""
    return not (full_read or department_member)


def build(claims: Mapping[str, str], *, full_read: bool = False,
          granted_at: str = "2026-10-30",
          granted_by: str = "workbench owner", role: str | None = None,
          department_member: bool = False,
          read_at: str | None = None) -> dict[str, dict[str, Any]]:
    """``read_at``: when the reader's claims were read (default: now, UTC); the snapshot
    states it as an ISO instant, the form ``PortalAccessSnapshot`` accepts."""
    if read_at is None:
        read_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat() \
            .replace("+00:00", "Z")
    if role is not None and (role not in ROLES or full_read):
        raise ValueError("role_unknown" if role not in ROLES else "role_with_full_read")
    if department_member and role is None:
        raise ValueError("department_member_needs_role")
    base = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    mirror = json.loads((EXAMPLES / base["mirror_audience"]).read_text(encoding="utf-8"))
    live = json.loads((EXAMPLES / base["live_audience"]).read_text(encoding="utf-8"))
    owners = list(ROLES[role][0]) if role else list(OWNER_PRINCIPALS)
    for doc in (mirror, live):
        for key, holders in doc["audiences"].items():
            doc["audiences"][key] = sorted({*holders, *owners})
    for key in LIVE_PREFIXES:
        live["audiences"][key] = sorted({*live["audiences"].get(key, []), *owners})
    for key in registry_repositories(json.loads(REGISTRY.read_text(encoding="utf-8"))):
        live["audiences"][key] = sorted({*live["audiences"].get(key, []), *owners})
    live.setdefault("classifications", {}).setdefault("mail:*", "restricted")
    policy = {
        "schema": "plx-employee-permission-policy/v1",
        "note": "Generated by scripts/eval_owner_host.py for the case-card scoreboard: the "
                "named reader as an owner/operator. Never committed.",
        "tenant": base.get("tenant", "plx"),
        "entra_tenant_id": claims["entraTenantId"].lower(),
        "clearance_by_access_level": {"L1": "internal", "L2": "internal", "L3": "confidential",
                                      "L4": "confidential", "L5": "restricted"},
        "principals": {name: _principal(name, MEMBER_CAPABILITY if department_member
                                         else "MANAGE_OPERATIONS") for name in owners},
        "service_principal_object_ids": [],
    }
    if full_read:
        policy["full_read"] = [full_read_grant(claims, granted_at, granted_by)]
    access = {
        "schema": "plx-portal-access-snapshot/v1",
        "source": "scripts/eval_owner_host.py (the named reader's own claims)",
        "read_at": read_at,
        "users": [{"id": claims["uid"], "email": claims["email"],
                   "role": MEMBER_ROLE if department_member else ROLE,
                   "accessLevel": MEMBER_LEVEL if department_member else "L5", "isActive": True,
                   "entraTenantId": claims["entraTenantId"].lower(),
                   "entraObjectId": claims["entraObjectId"].lower(),
                   "departmentIds": [DEPARTMENT], "accessScopes": SCOPES, "mrpWarehouse": True, "mrpProduction": True, "mrpRnD": True}],
    }
    if department_member:
        access["users"][0]["departmentMemberships"] = [
            {"code": ROLE_DEPARTMENTS[role], "provenance": "PORTAL", "departmentActive": True}]
    config = {**base, "capture_audience": "live_audience.json", "live_audience": "live_audience.json",
              "mirror_audience": "mirror_audience.json", "employee_policy": "employee_policy.json",
              "portal_access": "portal_access.json",
              "entra_directory": directory_read(full_read, department_member),
              "marking_grants": (({ROLES[role][1]: owners} if ROLES[role][1] else {})
                                 if role else {name: owners for name in OWNER_MARKINGS})}
    config["note"] = ("Owner-eval host config (scripts/eval_owner_host.py): all-sources scope, "
                      "the named reader granted the owner principals. Generated, never committed.")
    return {"host.json": config, "employee_policy.json": policy, "portal_access.json": access,
            "mirror_audience.json": mirror, "live_audience.json": live}


LIVE_HOST_CONFIG_ENV = "PLX_EVAL_LIVE_HOST_CONFIG"   # the reader's own live host.json


def from_live(config: Path, out: Path, claims: Mapping[str, str], *,
              offline_directory: bool = False) -> Path:
    """The reader's own live host config, for an eval run: ``out/host.json`` (0600 in a
    0700 directory the caller deletes), the live document unchanged (its marking grants,
    audiences, employee policy and Portal snapshot, so the run reads exactly what the
    reader reads) but for its file references, made absolute to the live config's own
    directory so they are read in place, never copied. ``offline_directory``: the Entra
    directory precondition (a live Graph read) is switched off for an offline run; the
    principals still come from the live policy, refused (``offline_directory_changes_
    principals``) when one of its principals is gated by an Entra group, department or
    job title. ValueError (a code) unless the named
    reader is the live config's reader: its tenant is the policy's and its object id is
    in the live Portal snapshot."""
    from operating import host as plx_host
    from operating.employee_permissions import EmployeePermissionPolicy
    source = Path(config).expanduser().resolve()
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("live_host_config_unreadable") from None
    if not isinstance(doc, dict):
        raise ValueError("live_host_config_invalid")
    base = source.parent
    for key in plx_host._PATHS:
        if isinstance(doc.get(key), str):
            doc[key] = str((base / doc[key]).resolve())
    for name in plx_host.SOURCES:
        entry = doc.get(name)
        for key in ("scope", "books"):
            if isinstance(entry, dict) and isinstance(entry.get(key), str):
                entry[key] = str((base / entry[key]).resolve())
    if offline_directory:
        doc["entra_directory"] = False
    try:
        loaded = plx_host.HostConfig.from_file(source)
    except plx_host.HostError as error:
        raise ValueError(f"live_host_config_refused:{error.code}") from None
    if loaded.employee_policy is None or loaded.portal_access is None:
        raise ValueError("live_host_config_has_no_reader")
    try:
        policy = EmployeePermissionPolicy.from_file(loaded.employee_policy)
        users = json.loads(Path(loaded.portal_access).read_text(encoding="utf-8"))["users"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("live_host_config_reader_unreadable") from None
    if offline_directory and any(rule.entra_groups or rule.entra_departments
                                 or rule.entra_job_title_keywords
                                 for rule in policy.rules.values()):
        # Without the directory such a principal would resolve differently: the run would
        # not read with the reader's own grants.
        raise ValueError("offline_directory_changes_principals")
    oid = str(claims.get("entraObjectId") or "").lower()
    if str(claims.get("entraTenantId") or "").lower() != str(policy.entra_tenant_id or "").lower() \
            or not oid or not any(isinstance(u, dict) and str(u.get("entraObjectId") or "")
                                  .lower() == oid for u in users):
        raise ValueError("reader_not_the_live_host_config_reader")
    write(out, {"host.json": doc})
    return out / "host.json"


def write(out: Path, files: Mapping[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out, 0o700)
    for name, doc in files.items():
        path = out / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=1, sort_keys=True)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_owner_host",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reader-env", default=None,
                        help="the variable holding the reader's claims (default PLX_EVAL_READER)")
    parser.add_argument("--full-read", action="store_true",
                        help="write a declared full-read grant for the reader (all scopes, "
                             "every mailbox); the people exclusion still wins")
    parser.add_argument("--role", choices=sorted(ROLES),
                        help="a restricted reader: only this role's staff principals and "
                             "its marking")
    parser.add_argument("--department-member", action="store_true",
                        help="with --role: an ordinary employee of the role's Portal "
                             "department (MANAGER, L3), routed by department, never admin")
    parser.add_argument("--granted-by", default="workbench owner",
                        help="who granted full-read, recorded in the grant")
    args = parser.parse_args(argv)
    try:
        files = build(reader_claims(os.environ if environ is None else environ,
                                    args.reader_env), full_read=args.full_read,
                      granted_by=args.granted_by, role=args.role,
                      department_member=args.department_member)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 2
    write(args.out, files)
    directory = files["host.json"]["entra_directory"]
    print(json.dumps({"host_config": str(args.out / "host.json"), "files": sorted(files),
                      "full_read": args.full_read, "role": args.role,
                      "department_member": args.department_member,
                      "entra_directory": directory}))
    from operating.agent_session import GRAPH_ENV   # presence only, never a value
    env = os.environ if environ is None else environ
    if directory and not all(env.get(name) for name in GRAPH_ENV):
        # Said now, not as a refused host mid-run: the names only, never a value.
        print(json.dumps({"warning": "entra_directory_needs_graph_credentials",
                          "variables": list(GRAPH_ENV),
                          "hint": "this reader's host reads its Entra directory at start "
                                  "(no --full-read): set them, or use --full-read"}),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
