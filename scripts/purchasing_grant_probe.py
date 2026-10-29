"""Live check ``permissions.purchasing_prices_grant``: who may hold ``purchasing_prices``
at PLX, counted from both sources the grant rests on (counts only, never a name).

Owner decision (Tanush, 2026-10-27): purchasing staff see PO line prices. The committed live
host config (``examples/host.live-check.json``) grants the marking to ``staff:purchasing``
only. The employee policy (``examples/employee_permission_policy.example.json``) gives an
employee ``staff:purchasing`` through either of two paths (``operating.employee_permissions``):

* the Portal's Procurement department (code ``PROC``), or
* an Entra job title with a word that starts with one of the rule's
  ``entra_job_title_keywords`` (purchas, procure, buyer, sourcing), never for a person the
  people exclusion (``PLX_EXCLUDED_PEOPLE_OIDS`` / ``_FILE``) names.

The probe checks both committed files offline (exit 1 when the grant no longer rests on
them), then counts:

* Portal: the distinct members of the active ``PROC`` department, from
  ``public.app_department`` and ``public.app_user_department_membership`` through the
  GET-only Workbench reporting API client;
* Entra: the enabled users whose job title matches, and how many of them the exclusion
  names, from one complete ``GET /users`` listing through the GET-only Graph client
  (``integrations.graph_directory.count_job_titles``).

The two sets may overlap (no identity join here): ``holders_at_least`` is the larger count,
``holders_at_most`` their sum. ``blocked_reason`` names the first gap: ``portal_api_unset``,
``graph_unset``, an exclusion code (``excluded_people_oids_absent`` ...) or
``purchasing_holders_none`` (both sources read, nobody holds the grant).

Evidence: isolated-fixture tests (``tests.test_purchasing_po_prices``); live-unverified until
a passing result of ``devloop/live-checks/permissions.purchasing_prices_grant.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
HOST_CONFIG = ROOT / "examples" / "host.live-check.json"
POLICY = ROOT / "examples" / "employee_permission_policy.example.json"
MARKING = "purchasing_prices"
PRINCIPAL = "staff:purchasing"
DEPARTMENT_CODE = "PROC"
DATA_KEY = "operations.procurement"
KEYWORDS = ("buyer", "procure", "purchas", "sourcing")
GRAPH_ENV = ("MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
             "MICROSOFT_GRAPH_CLIENT_SECRET")


def grant_basis(host_config: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """The committed grant and the role it rests on, or ValueError naming what breaks it."""
    holders = (host_config.get("marking_grants") or {}).get(MARKING)
    if holders != [PRINCIPAL]:
        raise ValueError("grant_not_purchasing_only")
    rule = (policy.get("principals") or {}).get(PRINCIPAL)
    if not isinstance(rule, Mapping) or rule.get("data_key") != DATA_KEY \
            or not rule.get("department_ids") or rule.get("entra_departments"):
        raise ValueError("principal_not_tied_to_procurement")
    if tuple(sorted(rule.get("entra_job_title_keywords") or ())) != KEYWORDS:
        raise ValueError("job_title_keywords_differ")
    return {"marking": MARKING, "granted_to": [PRINCIPAL], "department_code": DEPARTMENT_CODE,
            "job_title_keywords": list(KEYWORDS)}


def count_members(departments: Iterable[Mapping[str, Any]],
                  memberships: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Distinct members of the active ``PROC`` department(s)."""
    ids = {str(d.get("id")) for d in departments
           if str(d.get("code") or "").strip().upper() == DEPARTMENT_CODE
           and d.get("is_active") in (True, "true", "t", 1, "1")}
    members = {str(m.get("user_id")) for m in memberships
               if str(m.get("department_id")) in ids and m.get("user_id") not in (None, "")}
    return {"department_found": bool(ids), "departments": len(ids), "members": len(members)}


def run(environ: Mapping[str, str], client: Any = None, graph: Any = None,
        exclusion: Any = None) -> dict[str, Any]:
    basis = grant_basis(json.loads(HOST_CONFIG.read_text("utf-8")),
                        json.loads(POLICY.read_text("utf-8")))
    out: dict[str, Any] = {**basis, "adapter_status": "live-unverified",
                           "department_found": False, "departments": 0, "members": 0,
                           "entra_job_title_counted": False, "enabled_users": 0,
                           "job_title_matches": 0, "job_title_excluded": 0,
                           "job_title_members": 0}
    gaps: list[str] = []
    if client is None:
        url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
        if url and key:
            from integrations.workbench_read_api import WorkbenchReadApiClient
            client = WorkbenchReadApiClient(url, key)
        else:
            gaps.append("portal_api_unset")
    if client is not None:
        departments = client.read_relation("public.app_department",
                                           columns=["id", "code", "is_active"])["rows"]
        memberships = client.read_relation("public.app_user_department_membership",
                                           columns=["id", "department_id", "user_id"])["rows"]
        out.update(count_members(departments, memberships))
    if exclusion is None:
        from integrations.people_exclusion import PeopleExclusion
        exclusion = PeopleExclusion.from_env(environ)
    if graph is None and all(environ.get(k) for k in GRAPH_ENV):
        from integrations.graph_read import build_graph
        graph, _ = build_graph(environ)
    if graph is None:
        gaps.append("graph_unset")
    elif exclusion.unavailable:
        gaps.append(exclusion.unavailable)
    else:
        from integrations.graph_directory import count_job_titles
        out.update(count_job_titles(graph, KEYWORDS, exclusion), entra_job_title_counted=True)
    out["holders_at_least"] = max(out["members"], out["job_title_members"])
    out["holders_at_most"] = out["members"] + out["job_title_members"]
    if not gaps and not out["holders_at_most"]:
        gaps.append("purchasing_holders_none")
    if gaps:
        out["blocked_reason"] = gaps[0]
    return out


def main() -> int:
    try:
        out = run(os.environ)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
