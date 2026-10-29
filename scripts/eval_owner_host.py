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
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BASE_CONFIG = EXAMPLES / "host.all-sources.live-check.json"
OWNER_PRINCIPALS = ("staff:operations", "staff:purchasing", "staff:warehouse", "staff:quality",
                    "staff:finance", "staff:customers")
# The markings an owner/operator holds; the others stay unheld.
OWNER_MARKINGS = ("finance",)
LIVE_PREFIXES = ("mail:*", "sharepoint:*", "teams:*", "mc:*", "bc:*")
ROLE = "SUPER_ADMIN"
DEPARTMENT = "owner-eval-department"  # one id, declared in the policy and the snapshot row


def reader_claims(environ: Mapping[str, str]) -> dict[str, str]:
    from scripts import eval_real_agent as real
    claims, problem = real.reader_identity(environ, real.READER_ENV)
    if claims is None:
        raise ValueError(problem or "reader_identity_missing")
    return dict(claims)


DATA_KEYS = {"staff:operations": "operations.sales", "staff:purchasing": "operations.procurement",
             "staff:warehouse": "operations.warehouse", "staff:quality": "operations.quality",
             "staff:finance": "operations.finance", "staff:customers": "crm.customers"}

SCOPES = ([{"scopeType": "COMPANY", "scopeKey": "petra-hygienic-plx", "effect": "ALLOW"}]
          + [{"scopeType": "DATA", "scopeKey": key, "effect": "ALLOW"}
             for key in sorted(set(DATA_KEYS.values()))])


def _principal(entry: str) -> dict[str, Any]:
    return {"capability": "MANAGE_OPERATIONS", "department_ids": [DEPARTMENT],
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


def build(claims: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    base = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    mirror = json.loads((EXAMPLES / base["mirror_audience"]).read_text(encoding="utf-8"))
    live = json.loads((EXAMPLES / base["live_audience"]).read_text(encoding="utf-8"))
    owners = list(OWNER_PRINCIPALS)
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
        "principals": {name: _principal(name) for name in owners},
        "service_principal_object_ids": [],
    }
    access = {
        "schema": "plx-portal-access-snapshot/v1",
        "source": "scripts/eval_owner_host.py (the named reader's own claims)",
        "read_at": "generated at run time",
        "users": [{"id": claims["uid"], "email": claims["email"], "role": ROLE,
                   "accessLevel": "L5", "isActive": True,
                   "entraTenantId": claims["entraTenantId"].lower(),
                   "entraObjectId": claims["entraObjectId"].lower(),
                   "departmentIds": [DEPARTMENT], "accessScopes": SCOPES, "mrpWarehouse": True, "mrpProduction": True, "mrpRnD": True}],
    }
    config = {**base, "capture_audience": "live_audience.json", "live_audience": "live_audience.json",
              "mirror_audience": "mirror_audience.json", "employee_policy": "employee_policy.json",
              "portal_access": "portal_access.json",
              # The reader's source identities and groups come from the Entra directory, as
              # on the product host: mail and SharePoint items are gated by their own ACL.
              "entra_directory": True,
              "marking_grants": {name: owners for name in OWNER_MARKINGS}}
    config["note"] = ("Owner-eval host config (scripts/eval_owner_host.py): all-sources scope, "
                      "the named reader granted the owner principals. Generated, never committed.")
    return {"host.json": config, "employee_policy.json": policy, "portal_access.json": access,
            "mirror_audience.json": mirror, "live_audience.json": live}


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
    args = parser.parse_args(argv)
    try:
        files = build(reader_claims(os.environ if environ is None else environ))
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 2
    write(args.out, files)
    print(json.dumps({"host_config": str(args.out / "host.json"), "files": sorted(files)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
