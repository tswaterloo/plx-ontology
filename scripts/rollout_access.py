"""Who gets what when the Workbench opens to readers beyond the two owners (row O-02).

Three steps, each read-only towards every company system, none signing anyone in:

``snapshot``  reads the Portal's people relations (``app_user``, its access scopes and
              department memberships, ``app_department``, ``app_access_policy_grant``)
              through the GET-only reporting client (``integrations.workbench_read_api``),
              falls back per relation to the host's last completed mirror capture
              (``--mirror``, labelled with its capture time) when the live service does not
              serve it, joins each Portal user to an identity and writes a
              ``plx-portal-access-snapshot/v1`` file (``--out``; never over the served host
              config). The reporting service serves no Entra object id for a Portal user,
              and today not the e-mail either (``app_user``: "1 approved column is missing
              or not granted"), so the identity comes from the served ``email`` column when
              it exists, else from ``--identities`` (a file the owner exports: Portal user
              id, e-mail and, if known, the Entra object id; a ``plx-portal-access-snapshot``
              is accepted too), and the Entra object id from that file or, with
              ``--entra-lookup``, from the GET-only Graph directory by exact UPN. A row whose
              identity does not resolve is never written.
``preview``   computes, for every active employee row, what the Workbench would grant
              them under a host config's employee policy, marking grants and mirror
              audience, by running the production resolver
              (``operating.employee_permissions.EmployeePermissionResolver``) over the rows,
              and flags grants that look wrong or over-broad. Rows without a resolved
              identity are previewed by their Portal attributes and reported only in
              aggregate (they cannot be checked against the people exclusion).
``render-policy`` fills the committed example policy's department placeholders from the
              Portal's own department catalogue and carries the served policy's tenant
              and full-read grants over: a proposed policy to review, never installed.

People exclusion (``PLX_EXCLUDED_PEOPLE_OIDS``): both ``snapshot`` and ``preview`` refuse
to run (exit 2) unless the guard is usable, and a person it names is dropped before
anything is written; only the count of dropped rows is reported.

Output files hold names and addresses: they are written 0600 into a 0700 directory the
caller names (``/data/scratch/o02``), never into the repository or the served host config.
Nothing is printed but counts and paths.

Evidence: ``isolated-fixture`` tests (``tests/test_rollout_access.py``); a live run reads the
reporting API and the mirror capture, and is labelled by what it read.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from operating import employee_permissions as ep  # noqa: E402
from operating import employee_permissions_portal as portal  # noqa: E402

USERS, SCOPES, MEMBERS, DEPARTMENTS, GRANTS = (
    "public.app_user", "public.app_user_access_scope",
    "public.app_user_department_membership", "public.app_department",
    "public.app_access_policy_grant")
PEOPLE_RELATIONS = (USERS, SCOPES, MEMBERS, DEPARTMENTS, GRANTS)
IDENTITY_SCHEMA = "plx-rollout-identities/v1"
PREVIEW_SCHEMA = "plx-rollout-access-preview/v1"
SERVED_CONFIG_ROOT = Path.home() / ".config" / "plx"
REGISTRY = ROOT / "ontology" / "source_registry.json"
# The example policy's placeholder -> the Portal department code it stands for
# (examples/employee_permission_policy.example.json; codes: app_department.code).
PLACEHOLDER_CODES = {"<portal-department-id-operations>": "OPS",
                     "<portal-department-id-procurement>": "PROC",
                     "<portal-department-id-warehouse>": "WH",
                     "<portal-department-id-quality>": "QUALITY",
                     "<portal-department-id-finance>": "FIN",
                     "<portal-department-id-sales>": "SALES"}
# Departments whose members may read finance-marked facts and payroll/HR documents
# (the Portal's own Finance clearance: file-clearance.ts, FINANCE read by FIN and EXEC).
FINANCE_DEPARTMENTS = frozenset({"FIN", "EXEC"})
PAYROLL_MARKINGS = ("finance", "pii")   # operating.chunking.sensitive.MARKS


class RolloutError(Exception):
    """A refusal with a code; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ reading the Portal

def read_live(client: Any, relations: Sequence[str] = PEOPLE_RELATIONS
              ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Each relation's rows through the GET-only client, every served column; a relation
    the service does not serve is left out with its upstream reason (never guessed)."""
    from integrations.workbench_read_api import RelationUnavailable, served_entry
    from integrations.http import SourceUnavailable

    tables: dict[str, list[dict[str, Any]]] = {}
    basis: dict[str, dict[str, Any]] = {}
    health = client.health()
    catalog = client.relations()
    now = _now()
    for name in relations:
        try:
            entry = served_entry(catalog, name, health.get("unavailable_relations"))
            read = client.read_relation(name, columns=list(entry["columns"]))
        except RelationUnavailable as gap:
            basis[name] = {"basis": "unavailable", "reason": _reason(gap)}
            continue
        except SourceUnavailable as failure:
            basis[name] = {"basis": "unavailable", "reason": _reason(failure)}
            continue
        tables[name] = [dict(row) for row in read["rows"]]
        basis[name] = {"basis": "live", "read_at": now, "rows": len(tables[name]),
                       "columns": sorted(entry["columns"])}
    return tables, basis


def _reason(error: Exception) -> str:
    text = str(getattr(error, "reason", "") or error)
    return text[:160]


def read_mirror(path: Path, relations: Sequence[str]
                ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """The last completed capture of each relation in the host's source mirror
    (``row_current`` rows still present upstream), read-only, with its capture time."""
    if not path.is_file():
        raise RolloutError("mirror_absent")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        tables: dict[str, list[dict[str, Any]]] = {}
        basis: dict[str, dict[str, Any]] = {}
        for name in relations:
            state = conn.execute(
                "select columns_json, last_full_traversal_at, last_refresh_at "
                "from relation_state where relation = ?", (name,)).fetchone()
            if state is None:
                basis[name] = {"basis": "unavailable", "reason": "not in the mirror"}
                continue
            rows = [json.loads(r[0]) for r in conn.execute(
                "select row_json from row_current where relation = ? "
                "and missing_from_source_since is null and deleted_in_source is null "
                "order by key_json, run_index", (name,))]
            tables[name] = rows
            basis[name] = {"basis": "mirror", "captured_at": state[1],
                           "refreshed_at": state[2], "rows": len(rows),
                           "columns": sorted(json.loads(state[0]))}
        return tables, basis
    finally:
        conn.close()


def merge(live: tuple[dict, dict], mirror: tuple[dict, dict] | None
          ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Live wins per relation; the mirror fills only what live could not serve."""
    tables, basis = dict(live[0]), {k: dict(v) for k, v in live[1].items()}
    if mirror is not None:
        for name, rows in mirror[0].items():
            if name not in tables:
                tables[name] = rows
                basis[name] = {**mirror[1][name],
                               "live": basis.get(name, {}).get("reason", "not read")}
    for name in PEOPLE_RELATIONS:
        basis.setdefault(name, {"basis": "unavailable", "reason": "not read"})
    return tables, basis


# ------------------------------------------------------------------ identities

def load_identities(path: Path | None) -> dict[str, dict[str, str]]:
    """Portal user id -> {email, entraObjectId?} from an owner export: either
    ``plx-rollout-identities/v1`` ``{"schema", "users": [{"portalUserId", "email",
    "entraObjectId"?}]}`` or a ``plx-portal-access-snapshot/v1`` (its ``id``, ``email``,
    ``entraObjectId``). A malformed file or a user id listed twice refuses the run."""
    if path is None:
        return {}
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        raise RolloutError("identities_unreadable") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("users"), list):
        raise RolloutError("identities_invalid")
    keys = {IDENTITY_SCHEMA: ("portalUserId", "email", "entraObjectId"),
            ep.PORTAL_ACCESS_SNAPSHOT_SCHEMA: ("id", "email", "entraObjectId")}.get(
                doc.get("schema"))
    if keys is None:
        raise RolloutError("identities_invalid")
    out: dict[str, dict[str, str]] = {}
    for user in doc["users"]:
        if not isinstance(user, dict):
            raise RolloutError("identities_invalid")
        uid, email, oid = (user.get(k) for k in keys)
        if not isinstance(uid, str) or not uid.strip() or not isinstance(email, str) \
                or ep._EMAIL.fullmatch(email.strip().lower()) is None:
            raise RolloutError("identities_invalid")
        if oid is not None and (not isinstance(oid, str) or not ep._GUID.fullmatch(
                oid.strip().lower())):
            raise RolloutError("identities_invalid")
        if uid in out:
            raise RolloutError("identities_duplicate_user")
        out[uid] = {"email": email.strip().lower()}
        if oid:
            out[uid]["entraObjectId"] = oid.strip().lower()
    return out


# ------------------------------------------------------------------ Portal rows

def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise RolloutError("portal_flag_invalid")


def portal_rows(tables: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One ``plx-portal-access-snapshot/v1`` user per Portal user, without identity
    (``email``/``entraTenantId``/``entraObjectId`` are joined later); the department
    memberships, scopes and denies attached by user id. A row the reader cannot
    interpret refuses the whole build (``portal_flag_invalid``)."""
    if USERS not in tables:
        raise RolloutError("portal_users_unavailable")
    departments = {d["id"]: d for d in tables.get(DEPARTMENTS, [])}
    members: dict[str, list[dict[str, Any]]] = {}
    for row in tables.get(MEMBERS, []):
        members.setdefault(row["user_id"], []).append(row)
    scopes: dict[str, list[dict[str, Any]]] = {}
    for row in tables.get(SCOPES, []):
        scopes.setdefault(row["user_id"], []).append(row)
    denies: dict[str, list[dict[str, Any]]] = {}
    for row in tables.get(GRANTS, []):
        if str(row.get("effect") or "").upper() == "DENY" and row.get("subject_id"):
            denies.setdefault(row["subject_id"], []).append(row)
    out = []
    for user in sorted(tables[USERS], key=lambda u: str(u.get("id"))):
        uid = user.get("id")
        if not isinstance(uid, str) or not uid.strip():
            raise RolloutError("portal_user_id_invalid")
        level = user.get("access_level")
        row: dict[str, Any] = {
            "id": uid, "role": user.get("role"), "accessLevel": level or None,
            "isActive": _flag(user.get("is_active")),
            "mrpWarehouse": _flag(user.get("mrp_warehouse") or False),
            "mrpProduction": _flag(user.get("mrp_production") or False),
            "mrpRnD": _flag(user.get("mrp_rnd") or False),
            "departmentIds": sorted({m["department_id"] for m in members.get(uid, [])}),
            "accessScopes": [{"scopeType": s["scope_type"], "scopeKey": s["scope_key"],
                              "effect": s["effect"]}
                             for s in sorted(scopes.get(uid, []), key=lambda s: str(s["id"]))],
            "policyDenies": [{"id": d["id"], "capability": d["capability"],
                              "state": "REVOKED" if d.get("revoked_at") else
                              (d.get("state") or "ACTIVE"),
                              "departmentId": d.get("department_id"),
                              "companyKey": d.get("company_key"),
                              "dataKey": d.get("data_key")}
                             for d in sorted(denies.get(uid, []), key=lambda d: str(d["id"]))],
        }
        if MEMBERS in tables and DEPARTMENTS in tables:
            memberships = []
            for m in members.get(uid, []):
                dept = departments.get(m["department_id"])
                if dept is None:
                    continue    # a department the catalogue does not list confirms nothing
                memberships.append({"code": dept["code"], "provenance": m["provenance"],
                                    "departmentActive": _flag(dept.get("is_active"))})
            row["departmentMemberships"] = sorted(memberships, key=lambda m: m["code"])
        if isinstance(user.get("email"), str) and user["email"].strip():
            row["_served_email"] = user["email"].strip().lower()
        out.append(row)
    return out


def department_codes(row: Mapping[str, Any]) -> list[str]:
    return sorted({m["code"] for m in row.get("departmentMemberships") or ()
                   if m["provenance"] in ("PORTAL", "PROJECTED") and m["departmentActive"]})


def join_identities(rows: Iterable[dict[str, Any]], identities: Mapping[str, Mapping[str, str]],
                    tenant_id: str, *, lookup: Callable[[str], str | None] | None = None
                    ) -> list[dict[str, Any]]:
    """Each row with ``email``/``entraTenantId``/``entraObjectId`` set when its identity
    resolves, and ``_identity`` saying how: ``portal_email`` (served column),
    ``owner_export`` (the identities file) or ``unresolved``; ``_entra`` says where the
    object id came from (``owner_export``, ``graph_upn`` or ``unresolved``). A served
    e-mail that disagrees with the owner export leaves the row unresolved."""
    out = []
    for row in rows:
        row = dict(row)
        served = row.pop("_served_email", None)
        exported = identities.get(row["id"], {})
        email = served or exported.get("email")
        if served and exported.get("email") and exported["email"] != served:
            email = None
            row["_identity"] = "conflict"
        else:
            row["_identity"] = ("portal_email" if served else
                                "owner_export" if email else "unresolved")
        oid = exported.get("entraObjectId") if email else None
        row["_entra"] = "owner_export" if oid else "unresolved"
        if email and not oid and lookup is not None:
            oid = lookup(email)
            row["_entra"] = "graph_upn" if oid else "unresolved"
        if email and oid:
            row.update(email=email, entraTenantId=tenant_id, entraObjectId=oid)
        else:
            row["_identity"] = row["_identity"] if row["_identity"] == "conflict" \
                else ("unresolved" if not email else row["_identity"])
        out.append(row)
    return out


def resolved(row: Mapping[str, Any]) -> bool:
    return bool(row.get("entraObjectId")) and bool(row.get("email"))


def exclusion_guard(environ: Mapping[str, str]) -> Any:
    """The people exclusion as the hosts build it; refuses unless usable."""
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    guard = PeopleExclusion.from_env(environ_with_exclusion(environ))
    if guard.unavailable:
        raise RolloutError("people_exclusion_unavailable")
    return guard


def drop_excluded(rows: Iterable[dict[str, Any]], guard: Any) -> tuple[list[dict], int]:
    """Rows without any person the exclusion names (by object id; by address only when
    the guard can decide one). A value the guard fails on drops the row (fail closed). A row
    with neither value cannot be checked: it is kept for the aggregate preview only and is
    never written to a snapshot, so it cannot sign in."""
    kept, dropped = [], 0
    for row in rows:
        values = [v for v in (row.get("entraObjectId"), row.get("email")) if v]
        try:
            excluded = any(guard.is_excluded(v) for v in values if guard.decides(v))
        except Exception:  # noqa: BLE001 - an undecidable person is withheld
            excluded = True
        if excluded:
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def snapshot_document(rows: Iterable[Mapping[str, Any]], basis: Mapping[str, Any],
                      source: str) -> dict[str, Any]:
    """The ``plx-portal-access-snapshot/v1`` document of the resolved rows. ``read_at`` is
    the oldest input's time (a mirror capture's, if one filled a relation), so the
    snapshot's age bound measures the oldest fact in it. The Portal's DENY rules and access
    scopes must have been read (live or from the mirror): without them every user would
    be written with none, and the hosts would grant what the Portal denies
    (``portal_denies_unavailable``)."""
    for relation in (GRANTS, SCOPES):
        if (basis.get(relation) or {}).get("basis") not in ("live", "mirror"):
            raise RolloutError("portal_denies_unavailable")
    times = [v.get("read_at") or v.get("captured_at") for v in basis.values()
             if v.get("basis") in ("live", "mirror")]
    read_at = min(times) if times else _now()
    users = [{k: v for k, v in row.items() if not k.startswith("_")}
             for row in rows if resolved(row)]
    return {"schema": ep.PORTAL_ACCESS_SNAPSHOT_SCHEMA, "source": source,
            "read_at": _iso(ep.parse_read_at(read_at)),
            "note": "Written by scripts/rollout_access.py snapshot; inputs per relation: " +
                    ", ".join(f"{k}={v.get('basis')}" for k, v in sorted(basis.items())),
            "users": users}


# ------------------------------------------------------------------ preview

def _placeholder(uid: str) -> tuple[str, str]:
    oid = str(uuid.uuid5(uuid.NAMESPACE_URL, "plx-rollout-preview:" + uid))
    return f"unresolved-{oid[:8]}@preview.invalid", oid


def _people_relations() -> frozenset[str]:
    """The Portal relations of the ``people-access`` domain (the source registry)."""
    try:
        doc = json.loads(REGISTRY.read_text("utf-8"))
    except (OSError, ValueError):
        return frozenset(PEOPLE_RELATIONS)
    entries = doc if isinstance(doc, list) else doc.get("entries") or doc.get("sources") or []
    found = {e.get("name") for e in entries if isinstance(e, dict)
             and e.get("system") == "portal-reporting-api" and e.get("kind") == "relation"
             and e.get("domain") == "people-access"}
    return frozenset(n for n in found if isinstance(n, str)) or frozenset(PEOPLE_RELATIONS)


def preview(rows: Sequence[Mapping[str, Any]], *, policy: Any, tenant_id: str,
            marking_grants: Mapping[str, Iterable[str]] | None = None,
            mirror_audience: Any = None, relations: Iterable[str] = (),
            guard: Any = None, owners: Iterable[str] = (),
            team_readers: Iterable[str] = (), senders: Iterable[str] = (),
            autopilot_members: Iterable[str] = ()) -> list[dict[str, Any]]:
    """What the Workbench grants each active employee row, via the production resolver.

    ``rows``: snapshot users (resolved or not); an unresolved row is decided with a
    placeholder identity that holds no grant of its own. ``policy``: an
    ``EmployeePermissionPolicy``. ``owners``: object ids of the two owner readers (a
    full-read grant to anyone else is flagged). The Entra directory is not read: a host
    with ``entra_directory`` also needs an enabled Entra user, and only an Entra
    department the Portal confirms clears restricted files, so file clearance here is
    the Portal side alone (an upper bound)."""
    relations = sorted(set(relations))
    people = _people_relations()
    owners = {o.lower() for o in owners}
    team = {t.strip().lower() for t in team_readers}
    send = {s.strip().lower() for s in senders}
    auto = {a.strip().lower() for a in autopilot_members}
    grants = {m: frozenset(p) for m, p in (marking_grants or {}).items()}
    users = []
    placeholders: dict[str, tuple[str, str]] = {}
    for row in rows:
        user = {k: v for k, v in row.items() if not k.startswith("_")}
        if not resolved(row):
            email, oid = _placeholder(row["id"])
            placeholders[row["id"]] = (email, oid)
            user.update(email=email, entraTenantId=tenant_id, entraObjectId=oid)
        users.append(user)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.json"
        path.write_text(json.dumps({"schema": ep.PORTAL_ACCESS_SNAPSHOT_SCHEMA,
                                    "source": "rollout-preview", "read_at": _now(),
                                    "users": users}), "utf-8")
        snapshot = ep.PortalAccessSnapshot(path)
        resolver = ep.EmployeePermissionResolver(policy, snapshot, people_exclusion=guard)
        out = []
        for row, user in zip(rows, users):
            if row.get("role") not in portal.EMPLOYEE_ROLES or not row.get("isActive"):
                continue
            principal = ep.EmployeePrincipal(tenant_id, user["entraObjectId"])
            ent = resolver.resolve(principal)
            out.append(_describe(row, ent, grants, mirror_audience, relations, people,
                                 owners, team, send, auto))
    return out


def _describe(row: Mapping[str, Any], ent: Any, grants: Mapping[str, frozenset[str]],
              audience: Any, relations: Sequence[str], people: frozenset[str],
              owners: set[str], team: set[str], send: set[str], auto: set[str]
              ) -> dict[str, Any]:
    known = resolved(row)
    oid = str(row.get("entraObjectId") or "").lower()
    email = str(row.get("email") or "").lower()
    depts = department_codes(row)
    entry: dict[str, Any] = {
        "portal_user": row["id"], "email": email if known else None,
        "identity": row.get("_identity", "portal_email" if known else "unresolved"),
        "entra_object_id_from": row.get("_entra", "unresolved"),
        "role": row.get("role"), "access_level": row.get("accessLevel"),
        "departments": depts, "flags": []}
    flags: list[str] = entry["flags"]
    if not known:
        flags.append("identity unresolved: no e-mail/Entra id for this Portal user; "
                     "it cannot sign in and was not checked against the people exclusion")
    elif entry["entra_object_id_from"] == "graph_upn":
        flags.append("Entra object id found by e-mail (UPN), not the Portal's own link")
    if ent.actor is None:
        entry.update(granted=False, denied_reason=ent.denied_reason)
        return entry
    principals = sorted(p for p in ent.actor.principals if not p.startswith("employee:"))
    held_markings = ep.full_read_marking_grants(grants, ent)
    mine = set(ent.actor.principals)
    markings = sorted(m for m, holders in held_markings.items() if holders & mine)
    full_read = ent.full_read_code()
    unrestricted = ent.unrestricted()
    readable = ([r for r in relations if audience.allows(ent.actor, r)]
                if audience is not None and relations else [])
    owner = known and oid in owners
    entry.update(
        granted=True, principals=principals, clearance=ent.actor.clearance,
        full_read=full_read, unrestricted=unrestricted, markings=markings,
        payroll_hr_documents=all(m in markings for m in PAYROLL_MARKINGS),
        file_clearance=sorted(ent.file_clearance),
        qc_documents=bool(ent.qc_document_reader or unrestricted),
        mail=("every captured mailbox" if ent.full_read_covers("mail")
              and ent.full_read.grant.mail_scope == "all" else
              "own mailbox and the shared mailboxes the policy names"
              if ent.full_read_covers("mail") else
              "own mailbox only (shared and other people's mailboxes withheld)"),
        teams=("every captured conversation" if ent.full_read_covers("teams")
               else "chats and channels they are a member of"),
        sharepoint=("every captured file" if ent.full_read_covers("sharepoint")
                    else "files whose permission list names them, within file clearance"),
        portal_relations=f"{len(readable)} of {len(relations)}" if relations else None,
        people_tables=sorted(r for r in readable if r in people),
        owner_pages=bool(unrestricted),
        team_view=bool(unrestricted or (known and (oid in team or email in team))),
        card_send=bool(known and email in send),
        autopilot=("member of an autopilot role" if known and (oid in auto or email in auto)
                   else "off (in no autopilot role; opt-in only)"),
    )
    if full_read and not owner:
        flags.append("full-read grant held by someone other than the two owners")
    if entry["payroll_hr_documents"] and not owner:
        flags.append("sees payroll and HR documents (holds the finance and pii markings)")
    if "finance" in markings and not owner and not set(depts) & FINANCE_DEPARTMENTS:
        flags.append("holds the finance marking without a Finance or Executive department")
    if entry["people_tables"] and not owner:
        flags.append("reads the Portal's people/access tables: " +
                     ", ".join(entry["people_tables"]))
    if ent.actor.clearance == "restricted" and not owner:
        flags.append("clearance restricted (L5) beyond the owners")
    if not principals and not unrestricted:
        flags.append("holds no staff principal: sees only their own mail and files")
    if row.get("role") in ("ADMIN", "SUPER_ADMIN") and not owner and len(principals) > 3:
        flags.append(f"admin role holds {len(principals)} staff principals")
    if entry["card_send"] and not owner:
        flags.append("listed as a live sender (PLX_ACTIONS_SENDERS)")
    if entry["autopilot"].startswith("member") and not owner:
        flags.append("already in an autopilot role before opting in")
    return entry


def _outcome(e: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """(granted or the denial, principals, markings, payroll/HR) of one entry."""
    if not e.get("granted"):
        return f"no ({e.get('denied_reason')})", "-", "-", "-"
    return ("yes", ", ".join(p.split(":", 1)[-1] for p in e["principals"]) or "-",
            ", ".join(e["markings"]) or "-", "yes" if e["payroll_hr_documents"] else "no")


def _unresolved(e: Mapping[str, Any]) -> bool:
    return e["identity"] in ("unresolved", "conflict")


def summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts only; unresolved rows grouped by their Portal attributes and outcome."""
    groups = Counter((e["role"], e.get("access_level") or "-",
                      "+".join(e["departments"]) or "-", *_outcome(e),
                      "; ".join(f for f in e["flags"] if not f.startswith("identity "))
                      or "-")
                     for e in entries if _unresolved(e))
    names = ("role", "access_level", "departments", "granted", "principals", "markings",
             "payroll_hr", "flags")
    return {"active_employees": len(entries),
            "identity_resolved": sum(not _unresolved(e) for e in entries),
            "granted": sum(bool(e.get("granted")) for e in entries),
            "flagged": sum(any(not f.startswith("identity ") for f in e["flags"])
                           for e in entries),
            "unresolved_by_attributes": [dict(zip(names, key), count=n)
                                         for key, n in sorted(groups.items())]}


def table(entries: Sequence[Mapping[str, Any]], title: str) -> str:
    """A Markdown table of the resolved rows (names only here), then the unresolved
    ones grouped by their Portal attributes (never one line per unnamed person)."""
    head = ("| {first} | role / level | departments | granted | principals | markings | "
            "payroll/HR | {mid}flags |")
    lines = [f"## {title}", "",
             head.format(first="person", mid="mail | full read | "),
             "|---|---|---|---|---|---|---|---|---|---|"]
    for e in entries:
        if _unresolved(e):
            continue
        granted, principals, markings, payroll = _outcome(e)
        lines.append("| " + " | ".join([
            e["email"], f"{e['role']} {e.get('access_level') or '-'}",
            ", ".join(e["departments"]) or "-", granted, principals, markings, payroll,
            e.get("mail", "-"), e.get("full_read") or "no",
            "; ".join(e["flags"]) or "-"]) + " |")
    agg = summary(entries)["unresolved_by_attributes"]
    if agg:
        lines += ["", "Portal users whose identity is not resolved yet (they cannot sign in "
                      "until it is; grouped, not listed):", "",
                  head.format(first="count", mid="").replace("| role / level |",
                                                             "| role / level |"),
                  "|---|---|---|---|---|---|---|---|"]
        lines += ["| " + " | ".join([str(a["count"]), f"{a['role']} {a['access_level']}",
                                     a["departments"], a["granted"], a["principals"],
                                     a["markings"], a["payroll_hr"], a["flags"]]) + " |"
                  for a in agg]
    return "\n".join(lines) + "\n"


# Proposed marking grants for readers beyond the owners: each marking to the principal
# whose work needs it (Purchasing staff see PO line prices).
# pii and l5 go to nobody: the owners hold every marking through their unrestricted
# full-read grant (``employee_permissions.full_read_marking_grants``).
def render_policy(example: Mapping[str, Any], served: Mapping[str, Any],
                  departments: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The example policy with each department placeholder replaced by the Portal's id of
    that department code, the served policy's tenant, full-read grants and shared
    mailboxes, and example-only Entra group ids removed (a placeholder group would
    withhold the principal from everyone). Refuses when a code is missing or inactive."""
    by_code = {d["code"]: d for d in departments}
    doc = json.loads(json.dumps(dict(example)))
    for rule in doc["principals"].values():
        ids = []
        for value in rule.get("department_ids", []):
            if value in PLACEHOLDER_CODES:
                dept = by_code.get(PLACEHOLDER_CODES[value])
                if dept is None or not _flag(dept.get("is_active")):
                    raise RolloutError("department_unavailable:" + PLACEHOLDER_CODES[value])
                value = dept["id"]
            elif value.startswith("<"):
                raise RolloutError("department_placeholder_unknown")
            ids.append(value)
        if "department_ids" in rule:
            rule["department_ids"] = ids
        groups = [g for g in rule.get("entra_groups", [])
                  if not g.startswith("00000000-0000-0000-0000-")]
        if groups:
            rule["entra_groups"] = groups
        else:
            rule.pop("entra_groups", None)
    doc["entra_tenant_id"] = served["entra_tenant_id"]
    for key in ("full_read", "shared_mailboxes", "library_security_classes"):
        if served.get(key):
            doc[key] = served[key]
    doc["note"] = ("Proposed rollout policy, rendered by scripts/rollout_access.py "
                   "render-policy from examples/employee_permission_policy.example.json: "
                   "department ids from the Portal's app_department by code; tenant and "
                   "full-read grants from the served policy. Review before installing.")
    ep.EmployeePermissionPolicy.from_json(doc)     # refuses anything the hosts would
    return doc


# Proposed marking grants for readers beyond the owners (see the comment above).
ROLLOUT_MARKING_GRANTS = {"finance": ["staff:finance"],
                          "purchasing_prices": ["staff:purchasing", "staff:finance"],
                          "quality_controlled": ["staff:quality"]}


def render_host(served: Mapping[str, Any], served_dir: Path, mirror_audience: Mapping[str, Any],
                owners: Iterable[str], people: Iterable[str]) -> tuple[dict, dict]:
    """The served host config re-pointed at the rollout files (policy, snapshot, mirror
    audience in the output directory; every other file at its served path), with
    ``ROLLOUT_MARKING_GRANTS``, a 36-hour snapshot age bound, and a mirror audience whose
    people-access relations are readable by the owners' own principals only."""
    host = json.loads(json.dumps(dict(served)))
    for key in ("capture_audience", "live_audience", "knowledge_namespaces"):
        if isinstance(host.get(key), str):
            host[key] = str((served_dir / host[key]).resolve())
    host.update(employee_policy="employee_policy.rollout.json",
                portal_access="portal_access.json",
                mirror_audience="mirror_audience.rollout.json",
                portal_access_max_age_hours=36,
                marking_grants=ROLLOUT_MARKING_GRANTS)
    host["note"] = ("Proposed rollout host config (scripts/rollout_access.py render-policy): "
                    "the served config with the live Portal snapshot, the rendered policy, "
                    "narrowed marking grants and the people tables closed to all but the "
                    "owners. Not installed.")
    audience = json.loads(json.dumps(dict(mirror_audience)))
    holders = sorted(ep.EMPLOYEE_PREFIX + o.lower() for o in owners)
    for relation in sorted(people):
        audience["audiences"][relation] = holders
    from integrations.source_mirror import AudienceMap
    AudienceMap.from_json(audience)
    return host, audience


# ------------------------------------------------------------------ files

def _now() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def out_dir(path: Path) -> Path:
    """A private output directory outside the repository and the served host config."""
    target = path.expanduser().resolve()
    for forbidden in (ROOT.resolve(), SERVED_CONFIG_ROOT.resolve()):
        if target == forbidden or forbidden in target.parents:
            raise RolloutError("out_dir_forbidden")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    return target


def write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _host_inputs(config: Path) -> dict[str, Any]:
    """The host config's employee policy, marking grants and mirror audience."""
    from integrations.source_mirror import AudienceMap
    doc = json.loads(config.read_text("utf-8"))
    base = config.parent
    policy_path = base / doc["employee_policy"]
    audience = doc.get("mirror_audience")
    return {"policy": ep.EmployeePermissionPolicy.from_file(policy_path),
            "policy_doc": json.loads(policy_path.read_text("utf-8")),
            "marking_grants": doc.get("marking_grants") or {},
            "mirror_audience": AudienceMap.from_file(base / audience) if audience else None}


def _environ() -> dict[str, str]:
    return dict(os.environ)


def main(argv: Sequence[str] | None = None, *, client: Any = None,
         environ: Mapping[str, str] | None = None,
         lookup: Callable[[str], str | None] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rollout_access", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="read the Portal people relations, write a snapshot")
    snap.add_argument("--out", type=Path, required=True)
    snap.add_argument("--mirror", type=Path, help="the host's source-mirror.sqlite (fallback)")
    snap.add_argument("--no-live", action="store_true", help="read the mirror only")
    snap.add_argument("--identities", type=Path)
    snap.add_argument("--tenant-config", type=Path, required=True,
                      help="the served host config (its employee policy names the tenant)")
    snap.add_argument("--entra-lookup", action="store_true",
                      help="resolve a missing Entra object id by exact UPN (GET-only Graph)")
    prev = sub.add_parser("preview", help="per-person grants under a host config")
    prev.add_argument("--out", type=Path, required=True)
    prev.add_argument("--rows", type=Path, required=True, help="rows.json from snapshot")
    prev.add_argument("--host-config", action="append", required=True,
                      help="label=PATH; repeatable (e.g. served=..., proposed=...)")
    prev.add_argument("--relations", type=Path, help="the source mirror (relation list)")
    rend = sub.add_parser("render-policy", help="fill the example policy's departments")
    rend.add_argument("--out", type=Path, required=True)
    rend.add_argument("--rows", type=Path, required=True)
    rend.add_argument("--served-host", type=Path, required=True,
                      help="the served host config (its policy, audiences and grants)")
    args = parser.parse_args(argv)
    env = _environ() if environ is None else dict(environ)
    try:
        return _run(args, env, client, lookup)
    except RolloutError as refusal:
        print(json.dumps({"refused": refusal.code}), file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, env: Mapping[str, str], client: Any,
         lookup: Callable[[str], str | None] | None) -> int:
    target = out_dir(args.out)
    if args.command == "snapshot":
        guard = exclusion_guard(env)
        served = _host_inputs(args.tenant_config)["policy"]
        live: tuple[dict, dict] = ({}, {})
        if not args.no_live:
            if client is None:
                from integrations.workbench_read_api import WorkbenchReadApiClient
                key, url = env.get("PLX_WORKBENCH_API_KEY"), env.get("PLX_WORKBENCH_API_URL")
                if not key or not url:
                    raise RolloutError("portal_credentials_unset")
                client = WorkbenchReadApiClient(url, key)
            live = read_live(client)
        mirror = read_mirror(args.mirror, PEOPLE_RELATIONS) if args.mirror else None
        tables, basis = merge(live, mirror)
        if args.entra_lookup and lookup is None:
            lookup = _graph_lookup(env)
        rows = join_identities(portal_rows(tables), load_identities(args.identities),
                               served.entra_tenant_id, lookup=lookup)
        rows, dropped = drop_excluded(rows, guard)
        source = "portal-reporting-api people relations (" + ", ".join(
            f"{k.split('.', 1)[1]}: {v['basis']}" for k, v in sorted(basis.items())) + ")"
        doc = snapshot_document(rows, basis, source)
        with tempfile.TemporaryDirectory() as tmp:     # the hosts' own parser accepts it
            check = Path(tmp) / "check.json"
            check.write_text(json.dumps(doc), "utf-8")
            ep.PortalAccessSnapshot(check)
        write_private(target / "portal_access.json", json.dumps(doc, indent=1) + "\n")
        write_private(target / "rows.json", json.dumps(
            {"basis": basis, "departments": tables.get(DEPARTMENTS, []), "rows": rows},
            indent=1, default=str) + "\n")
        print(json.dumps({"portal_users": len(rows) + dropped,
                          "written_to_snapshot": len(doc["users"]),
                          "identity_unresolved": sum(not resolved(r) for r in rows),
                          "dropped_by_exclusion": dropped,
                          "inputs": {k: v["basis"] for k, v in sorted(basis.items())},
                          "out": str(target)}, sort_keys=True))
        return 0
    if args.command == "render-policy":
        data = json.loads(args.rows.read_text("utf-8"))
        example = json.loads((ROOT / "examples" /
                              "employee_permission_policy.example.json").read_text("utf-8"))
        host_doc = json.loads(args.served_host.read_text("utf-8"))
        base = args.served_host.resolve().parent
        served = json.loads((base / host_doc["employee_policy"]).read_text("utf-8"))
        doc = render_policy(example, served, data["departments"])
        host, audience = render_host(
            host_doc, base, json.loads((base / host_doc["mirror_audience"]).read_text("utf-8")),
            [g["object_id"] for g in served.get("full_read", [])], _people_relations())
        write_private(target / "employee_policy.rollout.json", json.dumps(doc, indent=1) + "\n")
        write_private(target / "host.rollout.json", json.dumps(host, indent=1) + "\n")
        write_private(target / "mirror_audience.rollout.json",
                      json.dumps(audience, indent=1) + "\n")
        print(json.dumps({"principals": len(doc["principals"]),
                          "full_read": len(doc.get("full_read", [])), "out": str(target)}))
        return 0
    guard = exclusion_guard(env)
    data = json.loads(args.rows.read_text("utf-8"))
    rows, dropped = drop_excluded(data["rows"], guard)
    relations: list[str] = []
    if args.relations:
        conn = sqlite3.connect(f"file:{args.relations}?mode=ro", uri=True, timeout=30)
        try:
            relations = [r[0] for r in conn.execute("select relation from relation_state")]
        finally:
            conn.close()
    out: dict[str, Any] = {"schema": PREVIEW_SCHEMA, "written_at": _now(),
                           "inputs": data.get("basis"), "dropped_by_exclusion": dropped,
                           "configs": {}}
    text = ["# Per-person access preview", "",
            f"Written {out['written_at']} by scripts/rollout_access.py preview. Nobody was "
            "signed in. People the exclusion names are omitted.", ""]
    for spec in args.host_config:
        label, _, path = spec.partition("=")
        if not path:
            raise RolloutError("host_config_label_required")
        inputs = _host_inputs(Path(path))
        policy = inputs["policy"]
        owners = [g["object_id"] for g in inputs["policy_doc"].get("full_read", [])]
        entries = preview(rows, policy=policy, tenant_id=policy.entra_tenant_id,
                          marking_grants=inputs["marking_grants"],
                          mirror_audience=inputs["mirror_audience"], relations=relations,
                          guard=guard, owners=owners,
                          team_readers=_split(env.get("PLX_TEAM_VIEW_READERS")),
                          senders=_split(env.get("PLX_ACTIONS_SENDERS")))
        out["configs"][label] = {"host_config": path, "summary": summary(entries),
                                 "people": entries}
        text.append(table(entries, f"Under the {label} host config"))
    write_private(target / "access-preview.json", json.dumps(out, indent=1) + "\n")
    write_private(target / "access-preview.md", "\n".join(text))
    print(json.dumps({label: c["summary"] | {"unresolved_by_attributes": len(
        c["summary"]["unresolved_by_attributes"])} for label, c in out["configs"].items()},
        sort_keys=True))
    return 0


def _split(value: str | None) -> list[str]:
    return [v for v in (value or "").replace(";", ",").replace(" ", ",").split(",") if v]


def _graph_lookup(env: Mapping[str, str]) -> Callable[[str], str | None]:
    from integrations.graph_directory import GraphDirectoryReader
    from integrations.graph_read import build_graph
    api, _transport = build_graph(env)
    reader = GraphDirectoryReader(api)
    return lambda email: reader.object_id(email)[0]


if __name__ == "__main__":
    raise SystemExit(main())
