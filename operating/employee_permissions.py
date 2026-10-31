"""Per-employee read entitlements: Portal role ∩ Entra groups ∩ SharePoint item permissions.

Why: the ``plxwb_`` reporting key is one shared DB login with column grants
only, and the Graph/BC app token sees every mailbox, site and company, so no
upstream source trims per employee. The Workbench decides per employee
**before** retrieval, ranking and caching (GOV-09, pack 03 §5).

For one employee principal (Entra ``tid``/``oid``) the resolver derives the
Workbench audience principals (the vocabulary ``source_mirror.AudienceMap``,
the case store, originals and the live-source audiences already use) and a
clearance, from three sources that are **intersected, never unioned**:

(a) **Portal role** -- a cited, cached read of the employee's Portal user
    (``app_user`` role, access level, departments, ``UserAccessScope``,
    ``AccessPolicyGrant`` denies, MRP module flags), decided with the Portal's
    own predicates mirrored exactly in ``employee_permissions_portal``. A
    principal is held only if the Portal would allow its declared capability
    on its declared resource under **both** the legacy role matrix and the
    canonical effective-access resolver (the Workbench cannot see
    ``PLX_RBAC_MODE``, so it takes the stricter of the two).
(b) **Entra groups** -- a cited, cached read of transitive group membership.
    A group can only *restrict*: a principal that declares ``entra_groups``
    additionally needs a completely read membership in one of them. When the
    membership could not be read for the recorded consent blocker (today:
    ``GroupMember.Read.All`` is not consented, pack 06) the note
    ``group_membership_unavailable`` is recorded, group-gated principals are
    withheld and the rest is decided by the Portal role alone. Any other unread
    membership (a provider failure, no read, another reason) is an outage: the
    whole entitlement is denied with ``rights_unresolved``, never narrowed as if
    it were data. Missing group data never widens anything.
    With a **directory provider** (``operating.employee_permissions_entra``
    over the GET-only ``integrations.graph_directory`` reader) the Entra side
    is one cached, cited read of the user object and its complete transitive
    membership, and it is a hard precondition: an unknown, mismatched or
    disabled user, a refused or non-2xx read or truncated paging denies the
    whole entitlement with ``entra_directory:<reason>`` (never a fallback to
    the Portal role alone). Only an unknown, mismatched or disabled user is
    Entra's answer about the employee; every other failure (a 5xx or transport
    failure, a partial or malformed listing, a missing consent, a provider that
    raises) leaves the rights unresolved (``rights_unresolved``).
    One refusal is not a denial: the membership read
    refused for consent (``GroupMember.Read.All``, 403) with the user object
    read and enabled. Then the directory derives Microsoft 365 group
    membership from the user's joined Teams (``group_basis`` ``joined_teams``:
    a team id is its M365 group id, the group a group-connected SharePoint
    site's ACL grants). That evidence is positive only: a listed group is a
    membership (``member``, identity ``group:<id>``), an unlisted one is
    ``unavailable`` (it may be a group that is no team), and an unreadable or
    malformed listing derives nothing (``group_basis`` ``none``): an outage, the
    whole entitlement is denied with ``rights_unresolved``. Portal and MC
    roles and mailbox ACLs name no Entra group membership (the Portal's Entra
    manifest lists *expected* members, an intent, not a state), so they derive
    none. Once the membership read succeeds (consent granted, picked up at the
    provider cache's expiry) the complete transitive membership is used and
    nothing is derived. A principal may also declare ``entra_departments``
    (the user's Entra ``department``, compared case-insensitively); like a
    group it only restricts. A principal may declare ``entra_job_title_keywords``:
    then an Entra ``jobTitle`` with a word starting with one of them stands in for
    membership of the rule's Portal departments (``job_title_matches``; the role,
    access level, scopes and denies still decide), only while the people exclusion
    the resolver is given is usable, and a person it names holds the principal by
    neither path. The decision records an enum (``entra_job_title``), never the title. The directory's UPN and mail join the employee's
    source identities, so a SharePoint grant to that UPN matches.
(c) **SharePoint item permissions** -- read GET-only by the reader for each
    item; ``item_acl_allows`` lets an employee read an original only when the
    item's permission list was read completely and grants one of the
    employee's own identities. Unknown, partial or unrequested -> deny.
(d) **File clearance** -- the Portal's own read list for restricted files,
    mirrored from portal/src/lib/auth/file-clearance.ts@790256c, because the
    app-only token means SharePoint's library permissions restrict nothing
    for the Workbench either. ``file_class_allows`` decides on each SharePoint
    record's security class (``doc_metadata_taxonomy.file_security_class``):
    FINANCE is read by Finance and Executive, BUSINESS_CENTRAL by Finance,
    Executive and IT, SCRATCH by its owner only (the Portal user id of its
    Staff Scratch folder), an unresolved class by nobody. The departments come
    from the Portal's ``app_user_department_membership``, the system of record
    (``PortalAccessRead.clearance_departments``: PORTAL or PROJECTED rows of an
    active department, as ``loadFileClearance`` reads them; a DRIFT row grants
    nothing). The reporting API does not serve that relation
    (services/workbench-api/manifest/allowlist.json@12db891 lists no
    ``app_user*`` or ``app_department*`` relation), so it comes from the Portal
    access snapshot; a read without it confirms no membership and clears
    nothing. The employee's Entra ``department`` (matched to the Portal's
    department catalogue by name) only narrows: the clearance is the
    intersection, so an Entra department the Portal no longer confirms grants
    nothing, and no directory read or an unknown department clears nothing.
    Portal role never widens it.

Agents and service principals never gain employee entitlements: a principal
of kind other than ``employee``, an object id the policy lists as a service
principal, and an actor mixing ``agent:``/``sp_`` principals with an employee
principal are all refused. Customer and supplier Portal roles are tenants, not
employees, and are refused.

The policy file is configuration, not authority: it maps each Workbench
principal to the Portal capability/resource it stands for, or to Portal roles
(``portal_roles``: held only by an employee whose ``app_user`` role is listed and
whose access level supplies that role's ``LEGACY_ROLE_BOOTSTRAP`` level, the stricter
of the two Portal modes again; with a capability as well, both must hold). Pure; no network,
no clock; providers are injected (cached reads, never fetched here).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import functools
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Iterator, Mapping

from ontology.core import LEVELS, Actor

from . import doc_metadata_taxonomy as taxonomy
from . import employee_permissions_portal as portal
from .entitlements import _AGENT_PREFIXES, ACL_BEARING_PATTERNS, EntitlementDenied

POLICY_SCHEMA = "plx-employee-permission-policy/v1"
EMPLOYEE_PREFIX = "employee:"
GROUP_MEMBERSHIP_UNAVAILABLE = "group_membership_unavailable"
# Recorded blocker: the shared Entra app has no
# GroupMember.Read.All / Group.Read.All / Directory.Read.All consent.
GROUP_READ_BLOCKER = "graph_permission_missing:GroupMember.Read.All"
# A mailbox the employee does not own (shared, or another user's with delegated
# FullAccess/ReadPermission). Graph v1.0 exposes no mailbox-permission list, so
# whether the employee may read it cannot be determined: denied, labelled.
MAILBOX_ACCESS_UNDETERMINED = "undetermined: shared mailbox"
# A session host serving live data refuses any actor that carries no employee.
EMPLOYEE_IDENTITY_REQUIRED = "employee_identity_required"
# A declared full-read grant (``EmployeePermissionPolicy.full_read``): one named employee
# reads every captured item of the granted scopes regardless of the item's own permission
# list, the mailbox owner or the file clearance. Never a wildcard; default deny for
# everyone else; the people exclusion always wins and must be usable for it to apply.
FULL_READ_SCOPES = ("finance", "mail", "sharepoint", "teams")
FULL_READ_MAIL_SCOPES = ("all", "shared_only")
# The markings the ``finance`` scope holds (``ontology.store_ddl.MARKINGS``): finance and
# the purchase-order prices; never pii, l5 or quality_controlled.
FULL_READ_MARKINGS = ("finance", "purchasing_prices")
# What the app shows on everything it serves under a full-read grant.
FULL_READ_LABEL = "Viewing with full-read access"
# The item scheme -> the full-read scope that covers it. Mission Control and BC carry no
# item ACL (the host audience alone decides them), so no scope names them.
_FULL_READ_SCHEME_SCOPE = {"graph-mail": "mail", "sharepoint": "sharepoint",
                           "sharepoint-list": "sharepoint", "teams-channel": "teams",
                           "teams-chat": "teams"}
PORTAL_ACCESS_SNAPSHOT_SCHEMA = "plx-portal-access-snapshot/v1"
DIRECTORY_SOURCE = "graph:directory"
GROUP_MEMBERSHIP_DERIVED = "group_membership_derived"
# Directory reasons that are Entra's own answer about the employee (a refusal); any other
# unavailable directory read leaves what the employee may read unknown (unresolved).
ENTRA_REFUSAL_REASONS = frozenset({"user_not_found", "user_mismatch", "account_disabled"})
# What an ``EntraDirectoryRead``'s ``group_ids`` are: the complete transitive
# membership, the M365 groups of the user's joined Teams (positive evidence
# only), or nothing read.
GROUP_BASIS_TRANSITIVE, GROUP_BASIS_JOINED_TEAMS, GROUP_BASIS_NONE = (
    "transitive", "joined_teams", "none")
GROUP_BASES = (GROUP_BASIS_TRANSITIVE, GROUP_BASIS_JOINED_TEAMS, GROUP_BASIS_NONE)
# portal/src/lib/policies/seed-rules.ts@853df2e:19-38 ROUTING_DEPARTMENTS, unchanged since
# 790256c (app_department name -> code); the Entra department is matched by name.
PORTAL_DEPARTMENT_CODES = {name.casefold(): code for name, code in (
    ("Account Management", "ACCTMGMT"), ("Artwork", "ARTWORK"), ("Compounding", "COMPOUND"),
    ("Executive", "EXEC"), ("Finance", "FIN"), ("Human Resources", "HR"), ("IT", "IT"),
    ("Maintenance", "MAINT"), ("Operations", "OPS"), ("Procurement", "PROC"),
    ("Product Development", "RND"), ("Production", "PROD"), ("Quality", "QUALITY"),
    ("Quality Assurance", "QA"), ("Quality Control", "QC"),
    ("Sales & Business Development", "SALES"), ("Shipping", "SHIP"), ("Warehouse", "WH"))}


def clearance_from_department_codes(codes: Iterable[str]) -> frozenset[str]:
    """The restricted classes these department codes read (file-clearance.ts@790256c:69-78
    clearanceFromDepartmentCodes)."""
    held = set(codes)
    return frozenset(cls for cls, readers in taxonomy.RESTRICTED_CLASS_READERS.items()
                     if held & set(readers))

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PRINCIPAL = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[^\s*]{1,160}$")
_KEYWORD = re.compile(r"[a-z]{3,32}")


def job_title_matches(title: Any, keywords: Iterable[str]) -> bool:
    """True when the Entra job title has a word that starts with one of ``keywords``
    (case-insensitive): 'Purchasing Manager' and 'Senior Buyer' match purchas and buyer,
    'Outsourcing Lead' does not match sourcing. No title, or not text: False."""
    if not isinstance(title, str):
        return False
    words = re.findall(r"[a-z]+", title.casefold())
    return any(word.startswith(k) for word in words for k in keywords)
_EMAIL = re.compile(r"^[^\s@*]{1,128}@[a-z0-9.-]{1,190}\.[a-z]{2,}$")


class EmployeeAccessRefused(EntitlementDenied):
    """An actor the employee gate will not serve; ``reason`` is safe to log."""


class EmployeeRightsUnresolved(EmployeeAccessRefused):
    """Refused because a permission input's provider failed (an outage), not because the
    employee was denied: what the employee may read is not known."""


def _guid(value: Any, label: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if _GUID.fullmatch(text) is None:
        raise ValueError(f"{label}_invalid")
    return text


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}_required")
    return value.strip()


# ------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class EmployeePrincipal:
    """Who is asking: an Entra identity and its kind. Only ``employee`` resolves."""

    tenant_id: str
    object_id: str
    kind: str = "employee"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _guid(self.tenant_id, "principal_tenant_id"))
        object.__setattr__(self, "object_id", _guid(self.object_id, "principal_object_id"))
        if self.kind not in ("employee", "agent", "service_principal"):
            raise ValueError("principal_kind_invalid")

    @property
    def principal(self) -> str:
        return EMPLOYEE_PREFIX + self.object_id


@dataclass(frozen=True)
class PortalAccessRead:
    """One cached, cited read of a Portal user and its effective-access state.

    Fields mirror Prisma ``User`` (``app_user``: ``role`` authoritative string,
    ``accessLevel``, ``isActive``, ``entraTenantId``/``entraObjectId``,
    ``email``, ``mrpWarehouse``/``mrpProduction``/``mrpRnD``),
    ``UserDepartmentMembership``, ``UserAccessScope`` and ``AccessPolicyGrant``
    (portal/prisma/schema.prisma at the commit in ``portal.PORTAL_SOURCE``).
    ``source`` cites where it was read; ``read_at`` when. The reporting API
    does not serve these tables today (pack 03 §6), so the provider is a
    blocker input.
    """

    user_id: str
    email: str
    role: str
    access_level: str | None
    is_active: bool
    verified_identity: bool
    entra_tenant_id: str | None
    entra_object_id: str | None
    source: str
    read_at: str
    mrp_modules: frozenset[str] = frozenset()
    department_ids: frozenset[str] = frozenset()
    scopes: tuple[portal.ScopeRule, ...] = ()
    explicit_denies: tuple[portal.PolicyDeny, ...] = ()
    # Department codes whose membership clears restricted files
    # (file-clearance.ts@790256c:124-142 loadFileClearance); None: not read.
    clearance_departments: frozenset[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _text(self.user_id, "portal_user_id"))
        email = self.email.strip().lower() if isinstance(self.email, str) else ""
        if _EMAIL.fullmatch(email) is None:
            raise ValueError("portal_email_invalid")
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "role", _text(self.role, "portal_role"))
        if self.access_level is not None and self.access_level not in portal.ACCESS_LEVELS:
            raise ValueError("portal_access_level_invalid")
        for name in ("is_active", "verified_identity"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"portal_{name}_invalid")
        for name in ("entra_tenant_id", "entra_object_id"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _guid(value, "portal_" + name))
        object.__setattr__(self, "source", _text(self.source, "portal_source"))
        object.__setattr__(self, "read_at", _text(self.read_at, "portal_read_at"))
        modules = frozenset(self.mrp_modules)
        if modules - set(portal.MRP_MODULES):
            raise ValueError("portal_mrp_modules_invalid")
        object.__setattr__(self, "mrp_modules", modules)
        object.__setattr__(self, "department_ids", frozenset(
            _text(d, "portal_department_id") for d in self.department_ids))
        for scope in self.scopes:
            if not isinstance(scope, portal.ScopeRule) or scope.type not in ("COMPANY", "DATA") \
                    or scope.effect not in ("ALLOW", "DENY"):
                raise ValueError("portal_scope_invalid")
        object.__setattr__(self, "scopes", tuple(self.scopes))
        if any(not isinstance(d, portal.PolicyDeny) for d in self.explicit_denies):
            raise ValueError("portal_policy_deny_invalid")
        object.__setattr__(self, "explicit_denies", tuple(self.explicit_denies))
        if self.clearance_departments is not None:
            object.__setattr__(self, "clearance_departments", frozenset(
                _text(c, "portal_department_code") for c in self.clearance_departments))

    def access_state(self) -> portal.AccessState:
        return portal.AccessState(self.user_id, self.is_active, self.verified_identity,
                                  self.access_level, self.department_ids, self.scopes,
                                  self.explicit_denies)


@dataclass(frozen=True)
class EntraGroupRead:
    """One cached, cited read of an employee's transitive Entra group membership.

    ``status`` is ``read`` only when every page was read; anything else
    (``unavailable``, with ``reason``) contributes no group at all.
    """

    status: str
    group_ids: frozenset[str] = frozenset()
    source: str = ""
    read_at: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("read", "unavailable"):
            raise ValueError("group_read_status_invalid")
        object.__setattr__(self, "group_ids", frozenset(
            _guid(g, "group_id") for g in self.group_ids))
        if self.status == "read":
            _text(self.source, "group_read_source")
            _text(self.read_at, "group_read_read_at")
        elif self.group_ids:
            raise ValueError("unavailable_group_read_lists_groups")


@dataclass(frozen=True)
class EntraDirectoryRead:
    """One cited read of an employee's Entra directory facts, complete or nothing.

    ``read``: the user is this enabled object, and ``group_basis`` says what
    ``group_ids`` are: ``transitive`` (every membership page was read),
    ``joined_teams`` (the membership read was refused, ``membership_reason``
    says why; the groups were derived from the user's joined Teams, every page)
    or ``none`` (neither was read; no group). ``unavailable``: ``reason`` says
    why and no fact is carried (``object_id`` may be empty when the user never
    resolved).
    """

    status: str
    object_id: str
    reason: str = ""
    user_principal_name: str = ""
    mail: str = ""
    department: str = ""
    group_ids: frozenset[str] = frozenset()
    source: str = ""
    job_title: str = ""
    read_at: str = ""
    group_basis: str = GROUP_BASIS_TRANSITIVE
    membership_reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("read", "unavailable"):
            raise ValueError("directory_read_status_invalid")
        if self.group_basis not in GROUP_BASES or not isinstance(self.membership_reason, str):
            raise ValueError("directory_group_basis_invalid")
        if self.object_id or self.status == "read":
            object.__setattr__(self, "object_id", _guid(self.object_id, "directory_object_id"))
        object.__setattr__(self, "group_ids", frozenset(
            _guid(g, "group_id") for g in self.group_ids))
        for name in ("user_principal_name", "mail", "department", "job_title"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"directory_{name}_invalid")
        if self.status == "read":
            _text(self.source, "directory_read_source")
            _text(self.read_at, "directory_read_read_at")
            if self.reason:
                raise ValueError("read_directory_carries_reason")
            # Only a membership that is not the complete transitive one says why.
            if (self.group_basis == GROUP_BASIS_TRANSITIVE) == bool(self.membership_reason):
                raise ValueError("directory_membership_reason_invalid")
            if self.group_basis == GROUP_BASIS_NONE and self.group_ids:
                raise ValueError("unread_membership_lists_groups")
        else:
            _text(self.reason, "directory_read_reason")
            if (self.group_ids or self.user_principal_name or self.mail or self.department
                    or self.job_title or self.source or self.membership_reason
                    or self.group_basis != GROUP_BASIS_TRANSITIVE):
                raise ValueError("unavailable_directory_read_carries_facts")


PortalAccessProvider = Callable[[EmployeePrincipal], "PortalAccessRead | None"]
GroupProvider = Callable[[EmployeePrincipal], "EntraGroupRead | None"]
DirectoryProvider = Callable[[EmployeePrincipal], "EntraDirectoryRead | None"]


def groups_unavailable(principal: EmployeePrincipal) -> EntraGroupRead:
    """The provider for today's tenant: the Graph permission to read groups is missing."""
    return EntraGroupRead("unavailable", reason=GROUP_READ_BLOCKER)


class PortalAccessSnapshot:
    """A ``PortalAccessProvider`` over a cited export of Portal user rows.

    ``plx-portal-access-snapshot/v1``: ``{"schema", "source", "read_at", "users": [...]}``;
    each user carries the Prisma ``User`` field names the Portal's own
    ``rbac-adapters.ts`` loads (``id``, ``email``, ``role``, ``accessLevel``,
    ``isActive``, ``entraTenantId``, ``entraObjectId``, ``mrpWarehouse``/
    ``mrpProduction``/``mrpRnD``, ``departmentIds`` from
    ``UserDepartmentMembership``, ``accessScopes`` ``[{scopeType, scopeKey,
    effect}]`` from ``UserAccessScope`` and ``policyDenies`` ``[{id, capability,
    state, departmentId, companyKey, dataKey}]`` from ``AccessPolicyGrant``
    (portal/prisma/schema.prisma, portal/src/lib/auth/rbac-adapters.ts at
    plx-customer-portal@c3ffda3) and ``departmentMemberships`` ``[{code,
    provenance, departmentActive}]`` from ``UserDepartmentMembership`` joined to
    its ``Department`` (schema.prisma@12db891:1138-1161), the file-clearance input:
    absent means not exported, so the row confirms no membership. A listed row is a verified identity, as in
    ``rbac-adapters.ts`` (a found user row is ``verifiedIdentity: true``).

    The file is re-parsed whenever its identity changes (inode, size, mtime or
    ctime in nanoseconds; the stat runs on **every** call), so removing or
    changing a row applies on the next read, and an unchanged file is not
    re-parsed per call. A failed stat or parse is never cached and raises (an
    outage: the resolver denies with ``rights_unresolved``). A row is matched by
    Entra tenant **and** object id; two rows for one identity or no row return
    ``None`` (``portal_access_unavailable``: deny, a refusal). This is the
    Portal input until the reporting API serves the user/RBAC tables;
    it is configuration an operator
    exports from the Portal, never a grant by itself (the policy and the
    item ACLs still intersect).
    """

    _DOC_FIELDS = frozenset({"schema", "source", "read_at", "note", "users"})
    _USER_FIELDS = frozenset({"id", "email", "role", "accessLevel", "isActive",
                              "entraTenantId", "entraObjectId", "mrpWarehouse",
                              "mrpProduction", "mrpRnD", "departmentIds", "accessScopes",
                              "policyDenies", "departmentMemberships"})
    # file-clearance.ts@790256c:131 provenance in [PORTAL, PROJECTED]; DRIFT grants nothing.
    _MEMBERSHIP_PROVENANCE = {"PORTAL": True, "PROJECTED": True, "DRIFT": False}
    _MRP = (("mrpWarehouse", "warehouse"), ("mrpProduction", "production"),
            ("mrpRnD", "rnd"))

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: tuple[tuple[int, ...], list[PortalAccessRead]] | None = None
        # Loud at startup; every later call re-reads and fails closed.
        self._rows()

    def _rows(self) -> list[PortalAccessRead]:
        info = self.path.stat()
        stamp = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        rows = self._load()
        self._cache = (stamp, rows)
        return rows

    def _load(self) -> list[PortalAccessRead]:
        doc = _strict_json(self.path.read_bytes())
        if not isinstance(doc, dict) or doc.get("schema") != PORTAL_ACCESS_SNAPSHOT_SCHEMA \
                or set(doc) - self._DOC_FIELDS or not isinstance(doc.get("users"), list):
            raise ValueError("portal_access_snapshot_invalid")
        source = _text(doc.get("source"), "portal_access_snapshot_source")
        read_at = _text(doc.get("read_at"), "portal_access_snapshot_read_at")
        return [self._row(user, source, read_at) for user in doc["users"]]

    def _row(self, user: Any, source: str, read_at: str) -> PortalAccessRead:
        if not isinstance(user, dict) or set(user) - self._USER_FIELDS:
            raise ValueError("portal_access_snapshot_user_invalid")
        lists = {name: user.get(name, []) for name in ("departmentIds", "accessScopes",
                                                       "policyDenies")}
        if any(not isinstance(value, list) for value in lists.values()):
            raise ValueError("portal_access_snapshot_user_invalid")
        scopes = []
        for scope in lists["accessScopes"]:
            if not isinstance(scope, dict) or set(scope) != {"scopeType", "scopeKey", "effect"}:
                raise ValueError("portal_scope_invalid")
            scopes.append(portal.ScopeRule(scope["scopeType"], _text(scope["scopeKey"],
                                                                    "portal_scope_key"),
                                           scope["effect"]))
        denies = []
        for deny in lists["policyDenies"]:
            if not isinstance(deny, dict) or "capability" not in deny or set(deny) - {
                    "id", "capability", "state", "departmentId", "companyKey", "dataKey"}:
                raise ValueError("portal_policy_deny_invalid")
            denies.append(portal.PolicyDeny(
                _text(deny["capability"], "portal_deny_capability"),
                deny.get("state", "ACTIVE"), deny.get("departmentId"),
                deny.get("companyKey"), deny.get("dataKey"), deny.get("id", "")))
        memberships = user.get("departmentMemberships")
        codes = None
        if memberships is not None:
            if not isinstance(memberships, list):
                raise ValueError("portal_department_membership_invalid")
            codes = set()
            for row in memberships:
                if (not isinstance(row, dict)
                        or set(row) != {"code", "provenance", "departmentActive"}
                        or row["provenance"] not in self._MEMBERSHIP_PROVENANCE
                        or not isinstance(row["departmentActive"], bool)):
                    raise ValueError("portal_department_membership_invalid")
                if self._MEMBERSHIP_PROVENANCE[row["provenance"]] and row["departmentActive"]:
                    codes.add(_text(row["code"], "portal_department_code"))
        flags = [user.get(flag, False) for flag, _ in self._MRP]
        if any(not isinstance(flag, bool) for flag in flags):
            raise ValueError("portal_mrp_flag_invalid")
        user_id = _text(user.get("id"), "portal_user_id")
        return PortalAccessRead(
            user_id=user_id, email=user.get("email"), role=user.get("role"),
            access_level=user.get("accessLevel"), is_active=user.get("isActive"),
            verified_identity=True, entra_tenant_id=user.get("entraTenantId"),
            entra_object_id=user.get("entraObjectId"),
            source=f"{source}#user={user_id}", read_at=read_at,
            mrp_modules=frozenset(m for (_, m), on in zip(self._MRP, flags) if on),
            department_ids=frozenset(lists["departmentIds"]), scopes=tuple(scopes),
            explicit_denies=tuple(denies),
            clearance_departments=None if codes is None else frozenset(codes))

    def __call__(self, principal: EmployeePrincipal) -> PortalAccessRead | None:
        try:
            rows = self._rows()
        except (OSError, ValueError, UnicodeDecodeError, TypeError):
            self._cache = None
            raise   # rights unknown, not "no Portal user": the resolver says unresolved
        found = [row for row in rows if row.entra_object_id == principal.object_id
                 and row.entra_tenant_id == principal.tenant_id]
        return found[0] if len(found) == 1 else None


# ------------------------------------------------------------------- policy

@dataclass(frozen=True)
class PrincipalRule:
    """What holding one Workbench principal means in Portal terms."""

    principal: str
    capability: str | None          # None: held by Portal role alone (``portal_roles``)
    resource: portal.Resource
    mrp_modules: frozenset[str]
    entra_groups: frozenset[str]
    entra_departments: frozenset[str] = frozenset()   # casefolded
    portal_roles: frozenset[str] = frozenset()        # empty: any employee role
    # A second way to hold the principal (``job_title_matches``): an Entra job title naming
    # one of these words stands in for the Portal department scope; never for the role.
    entra_job_title_keywords: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FullReadGrant:
    """One declared, audited full-read grant (``full_read`` in the policy).

    ``object_id`` is the named employee (an Entra object id, never a pattern); ``scopes``
    the sources it covers (``FULL_READ_SCOPES``); ``mail_scope`` ``all`` or ``shared_only``
    (only the mailboxes the policy's ``shared_mailboxes`` names). ``granted_by``,
    ``granted_at`` and ``reason`` are the audit record; all are required."""

    object_id: str
    granted_by: str
    granted_at: str
    reason: str
    scopes: frozenset[str]
    mail_scope: str = "all"

    @classmethod
    def from_json(cls, doc: Any) -> "FullReadGrant":
        from datetime import datetime
        fields = {"object_id", "granted_by", "granted_at", "reason", "scopes", "mail_scope",
                  "note"}
        required = {"object_id", "granted_by", "granted_at", "reason", "scopes"}
        if not isinstance(doc, dict) or set(doc) - fields or required - set(doc):
            raise ValueError("employee_policy_full_read_invalid")
        texts = [doc.get(k) for k in ("granted_by", "granted_at", "reason")]
        if any(not isinstance(t, str) or not t.strip() or "*" in t for t in texts):
            raise ValueError("employee_policy_full_read_invalid")
        try:
            datetime.fromisoformat(texts[1].strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("employee_policy_full_read_granted_at_invalid") from None
        scopes = doc.get("scopes")
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(s, str) or s not in FULL_READ_SCOPES for s in scopes)
                or len(set(scopes)) != len(scopes)):
            raise ValueError("employee_policy_full_read_scopes_invalid")
        mail_scope = doc.get("mail_scope", "all")
        if mail_scope not in FULL_READ_MAIL_SCOPES:
            raise ValueError("employee_policy_full_read_mail_scope_invalid")
        return cls(_guid(doc.get("object_id"), "employee_policy_full_read_object_id"),
                   texts[0].strip(), texts[1].strip(), texts[2].strip(), frozenset(scopes),
                   mail_scope)

    def code(self) -> str:
        """An enum-only summary (scopes, mail scope) for journals and fingerprints."""
        mail = f":mail_{self.mail_scope}" if "mail" in self.scopes else ""
        return "full_read:" + "+".join(sorted(self.scopes)) + mail


def _people_excluded(guard: Any, identities: Iterable[Any]) -> bool:
    """``ontology_tools_evidence.people_excluded``'s rule (an object id always, an address
    only by a bound guard; a missing, unusable or raising guard withholds)."""
    try:
        from .ontology_tools_evidence import people_excluded
        return people_excluded(guard, identities)
    except Exception:  # noqa: BLE001 - an undecidable guard withholds
        return True


@dataclass(frozen=True)
class FullReadAccess:
    """A full-read grant as one resolve applied it: the grant, the people exclusion it is
    held under (re-checked on every use: unusable now, nothing is granted) and the shared
    mailboxes ``mail_scope`` ``shared_only`` admits."""

    grant: FullReadGrant
    exclusion: Any = field(compare=False, repr=False)
    shared_mailboxes: frozenset[str] = frozenset()

    def guard(self) -> Any:
        """The people exclusion now (a ``ReloadingExclusion`` re-reads its list)."""
        current = getattr(self.exclusion, "current", None)
        return current() if callable(current) else self.exclusion

    def usable(self) -> bool:
        try:
            guard = self.guard()
            return guard is not None and not guard.unavailable \
                and not guard.is_excluded(self.grant.object_id)
        except Exception:  # noqa: BLE001 - an undecidable guard grants nothing
            return False

    def covers(self, scope: str) -> bool:
        return scope in self.grant.scopes and self.usable()

    def allows_item(self, scheme: str, source_acl: Any, record: Any = None) -> bool:
        """The item of ``scheme`` with ``source_acl`` under this grant. Mail needs the
        derived mailbox-owner ACL naming the mailbox's object id (the exclusion decides on
        it), and with ``shared_only`` a mailbox the policy names as shared; Teams needs the
        captured membership list; SharePoint a completely read permission list, else (one
        never or partly read) a record whose creator and last modifier are both recorded and
        decided clear by the exclusion (never opened blind). A person the exclusion names
        (any identity of the item, as it decides them) is never read."""
        scope = _FULL_READ_SCHEME_SCOPE.get(scheme)
        if scope is None or not self.covers(scope) or not isinstance(source_acl, Mapping):
            return False
        identities = source_acl.get("identities")
        guard = self.guard()
        if scope == "sharepoint" and source_acl.get("status") != "read":
            people = [record.get(k) if isinstance(record, Mapping) else None
                      for k in ("created_by", "last_modified_by")]
            if not all(isinstance(v, str) and v.strip() for v in people):
                return False
            try:
                from integrations.people_exclusion import is_object_id
                decidable = all(is_object_id(v.strip()) or guard.bound for v in people)
            except Exception:  # noqa: BLE001 - an undecidable guard opens nothing
                return False
            if not decidable or _people_excluded(guard, people):
                return False
            identities = identities if isinstance(identities, (list, tuple)) else []
        if not isinstance(identities, (list, tuple)) \
                or any(not isinstance(i, str) for i in identities):
            return False
        folded = {i.strip().lower() for i in identities if i.strip()}
        if scope == "mail":
            if (source_acl.get("status") != "derived"
                    or source_acl.get("kind") != "mailbox_owner"
                    or not any(_GUID.fullmatch(i) for i in folded)):
                return False
            if self.grant.mail_scope == "shared_only" and not folded & self.shared_mailboxes:
                return False
        elif scope == "teams" and not folded:
            return False
        return not _people_excluded(guard, sorted(folded))


def _strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError("employee_policy_duplicate_key")
            out[key] = value
        return out
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


@dataclass(frozen=True)
class EmployeePermissionPolicy:
    """``plx-employee-permission-policy/v1``; see examples/employee_permission_policy.example.json."""

    tenant: str
    entra_tenant_id: str
    clearance_by_access_level: Mapping[str, str]
    rules: Mapping[str, PrincipalRule]
    service_principal_object_ids: frozenset[str] = frozenset()
    digest: str = field(default="", compare=False)
    # SharePoint drive id -> security class of that library (the PLX site's
    # Finance, Business Central and Staff Scratch libraries).
    library_security_classes: Mapping[str, str] = field(default_factory=dict)
    # Declared full-read grants by employee object id (``FullReadGrant``); none: nobody.
    full_read: Mapping[str, FullReadGrant] = field(default_factory=dict)
    # The shared/functional mailboxes (object ids or addresses, lower case) a grant with
    # ``mail_scope`` ``shared_only`` reads; none declared: such a grant reads no mailbox.
    shared_mailboxes: frozenset[str] = frozenset()

    _FIELDS = frozenset({"schema", "note", "tenant", "entra_tenant_id",
                         "clearance_by_access_level", "principals",
                         "service_principal_object_ids", "library_security_classes",
                         "full_read", "shared_mailboxes"})
    _RULE_FIELDS = frozenset({"capability", "resource_type", "department_ids", "company_key",
                              "data_key", "mrp_modules", "entra_groups",
                              "entra_departments", "entra_job_title_keywords",
                              "portal_roles", "note"})
    # A rule without a capability names no Portal resource.
    _RESOURCE_FIELDS = frozenset({"resource_type", "department_ids", "company_key",
                                  "data_key", "mrp_modules"})

    @classmethod
    def from_json(cls, doc: Any) -> "EmployeePermissionPolicy":
        if not isinstance(doc, dict) or doc.get("schema") != POLICY_SCHEMA \
                or set(doc) - cls._FIELDS:
            raise ValueError("employee_policy_document_invalid")
        tenant = doc.get("tenant")
        if not isinstance(tenant, str) or not tenant.strip() or tenant != tenant.strip():
            raise ValueError("employee_policy_tenant_invalid")
        entra = _guid(doc.get("entra_tenant_id"), "employee_policy_entra_tenant_id")
        levels = doc.get("clearance_by_access_level")
        if not isinstance(levels, dict) or set(levels) != set(portal.ACCESS_LEVELS) \
                or any(v not in LEVELS for v in levels.values()):
            raise ValueError("employee_policy_clearance_by_access_level_invalid")
        rules: dict[str, PrincipalRule] = {}
        principals = doc.get("principals")
        if not isinstance(principals, dict):
            raise ValueError("employee_policy_principals_invalid")
        for name, rule in principals.items():
            if (not isinstance(name, str) or _PRINCIPAL.fullmatch(name) is None
                    or name.startswith(_AGENT_PREFIXES) or name.startswith(EMPLOYEE_PREFIX)):
                raise ValueError("employee_policy_principal_invalid")
            if not isinstance(rule, dict) or set(rule) - cls._RULE_FIELDS:
                raise ValueError("employee_policy_rule_invalid")
            roles = rule.get("portal_roles")
            if roles is not None and (not isinstance(roles, list) or not roles):
                raise ValueError("employee_policy_rule_invalid")
            if roles and any(not isinstance(r, str) or r not in portal.EMPLOYEE_ROLES
                             for r in roles):
                # CUSTOMER/SUPPLIER are tenants; an unknown role matches nobody.
                raise ValueError("employee_policy_portal_role_unknown")
            capability = rule.get("capability")
            if capability is None and roles:
                if set(rule) & cls._RESOURCE_FIELDS:
                    raise ValueError("employee_policy_rule_invalid")
            elif capability not in portal.CAPABILITY_REGISTRY:
                raise ValueError("employee_policy_capability_unknown")
            elif portal.CAPABILITY_REGISTRY[capability].required_scopes.owner:
                # Owner-scoped capabilities are about the user's own records,
                # never a shared Workbench audience.
                raise ValueError("employee_policy_owner_scoped_capability")
            data_key = rule.get("data_key")
            if data_key is not None and data_key not in portal.RBAC_DATA_KEYS:
                raise ValueError("employee_policy_data_key_unknown")
            departments = rule.get("department_ids", [])
            modules = rule.get("mrp_modules", [])
            groups = rule.get("entra_groups", [])
            entra_departments = rule.get("entra_departments", [])
            keywords = rule.get("entra_job_title_keywords", [])
            for value in (departments, modules, groups, entra_departments, keywords):
                if not isinstance(value, list):
                    raise ValueError("employee_policy_rule_invalid")
            if any(not isinstance(k, str) or _KEYWORD.fullmatch(k) is None for k in keywords) \
                    or (keywords and (capability is None or not departments)):
                # Lower-case letters only; a title only stands in for a department scope.
                raise ValueError("employee_policy_job_title_keywords_invalid")
            if set(modules) - set(portal.MRP_MODULES):
                raise ValueError("employee_policy_mrp_module_unknown")
            company = rule.get("company_key")
            if company is not None and not isinstance(company, str):
                raise ValueError("employee_policy_rule_invalid")
            resource = portal.Resource(
                type=_text(rule.get("resource_type", "workbench-audience"), "resource_type"),
                department_ids=tuple(_text(d, "department_id") for d in departments),
                company_key=company, data_key=data_key)
            rules[name] = PrincipalRule(
                name, capability, resource, frozenset(modules),
                frozenset(_guid(g, "entra_group") for g in groups),
                frozenset(_text(d, "entra_department").casefold() for d in entra_departments),
                frozenset(roles or ()), frozenset(keywords))
        services = doc.get("service_principal_object_ids", [])
        if not isinstance(services, list):
            raise ValueError("employee_policy_service_principals_invalid")
        libraries = doc.get("library_security_classes", {})
        if not isinstance(libraries, dict) or any(
                not isinstance(drive, str) or not drive.strip()
                or value not in taxonomy.SECURITY_CLASSES for drive, value in libraries.items()):
            raise ValueError("employee_policy_library_security_classes_invalid")
        service_ids = frozenset(_guid(s, "service_principal_object_id") for s in services)
        grants_doc = doc.get("full_read", [])
        if not isinstance(grants_doc, list):
            raise ValueError("employee_policy_full_read_invalid")
        full_read: dict[str, FullReadGrant] = {}
        for entry in grants_doc:
            grant = FullReadGrant.from_json(entry)
            if grant.object_id in full_read or grant.object_id in service_ids:
                # One grant per employee; a service principal never holds one.
                raise ValueError("employee_policy_full_read_invalid")
            full_read[grant.object_id] = grant
        shared = doc.get("shared_mailboxes", [])
        if not isinstance(shared, list) or any(
                not isinstance(v, str) or (_GUID.fullmatch(v.strip().lower()) is None
                                           and _EMAIL.fullmatch(v.strip().lower()) is None)
                for v in shared):
            raise ValueError("employee_policy_shared_mailboxes_invalid")
        return cls(tenant, entra, dict(levels), rules, service_ids,
                   sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest(),
                   dict(libraries), full_read,
                   frozenset(v.strip().lower() for v in shared))

    @classmethod
    def from_file(cls, path: str | Path) -> "EmployeePermissionPolicy":
        return cls.from_json(_strict_json(Path(path).read_bytes()))


# ------------------------------------------------------------------- result

@dataclass(frozen=True)
class PrincipalDecision:
    """Why one Workbench principal is or is not held; every source must say yes."""

    principal: str
    allowed: bool
    portal_legacy: bool
    portal_canonical: str       # effective-access decisive rule
    entra_group: str            # not_required | member | not_member | unavailable
    entra_department: str = "not_required"   # same vocabulary
    # not_required | member (title names a keyword) | not_member | unavailable (no title
    # read) | excluded (a person the exclusion names) | exclusion_unavailable
    entra_job_title: str = "not_required"


@dataclass(frozen=True)
class EmployeeEntitlement:
    """What one employee may read now. ``actor`` is None when denied."""

    principal: EmployeePrincipal
    actor: Actor | None
    denied_reason: str | None
    decisions: tuple[PrincipalDecision, ...] = ()
    source_identities: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()
    inputs: tuple[tuple[str, str, str], ...] = ()   # (kind, cited source, read_at)
    fingerprint: str = ""
    portal_user_id: str = ""
    file_clearance: frozenset[str] = frozenset()    # restricted classes readable
    library_security_classes: Mapping[str, str] = field(default_factory=dict)
    rights_unresolved: bool = False   # denied because a provider failed, not a refusal
    qc_document_reader: bool = False  # reads Batches QC documents (admin or QC department)
    # The declared full-read grant this employee holds (``FullReadAccess``), else None.
    full_read: FullReadAccess | None = None

    def full_read_code(self) -> str | None:
        """The full-read grant in effect now as an enum code (``FullReadGrant.code``), or
        None: no grant, a denied entitlement, or a people exclusion not usable now."""
        if self.actor is None or self.full_read is None or not self.full_read.usable():
            return None
        return self.full_read.grant.code()

    def full_read_covers(self, scope: str) -> bool:
        return self.actor is not None and self.full_read is not None \
            and self.full_read.covers(scope)

    def full_read_allows(self, scheme: str, source_acl: Any, record: Any = None) -> bool:
        """Whether this employee's full-read grant opens one item of ``scheme``
        (``FullReadAccess.allows_item``; ``record``: the item, for SharePoint's creator and
        last modifier); no grant: False."""
        return self.actor is not None and self.full_read is not None \
            and self.full_read.allows_item(scheme, source_acl, record)

    def require_actor(self) -> Actor:
        if self.actor is None:
            raise (EmployeeRightsUnresolved if self.rights_unresolved
                   else EmployeeAccessRefused)(self.denied_reason or "denied")
        return self.actor

    def readable_relations(self, audience_map: Any, relations: Iterable[str]) -> list[str]:
        """Relations the mirror's own audience check would show this employee."""
        if self.actor is None:
            return []
        return sorted(r for r in relations if audience_map.allows(self.actor, r))

    def item_acl_allows(self, source_acl: Any, *, statuses: Iterable[str] = ("read",)) -> bool:
        """Source item permissions ∩ this employee's own identities.

        Only a completely read list counts (``read``; mail owner ACLs are
        ``derived``); ``partial``, ``unreadable``, ``not_requested`` or a
        missing ACL deny. Sharing-link scopes never match a person.
        """
        if self.actor is None or not isinstance(source_acl, Mapping):
            return False
        if source_acl.get("status") not in tuple(statuses):
            return False
        granted = source_acl.get("identities")
        if not isinstance(granted, (list, tuple)):
            return False
        # Entra object ids (GUIDs), UPNs and SMTP addresses compare
        # case-insensitively at the identity provider; exact match otherwise
        # (no prefix, domain or wildcard matching).
        return bool({str(v).strip().lower() for v in granted} & self.source_identities)

    def file_class_allows(self, record: Any) -> bool:
        """The Portal's file clearance on one SharePoint drive-item record.

        canReadAttachment (portal/src/lib/attachments/access.ts@790256c:63-89) with
        clearanceAllows (file-clearance.ts@790256c:84-90): SCRATCH only to its owner,
        FINANCE and BUSINESS_CENTRAL only with that clearance, an unresolved class
        to nobody, every other class to every employee. Re-read at f3e3a53
        (access.ts:78-86, 66d08fb TASK-3075): the Portal now also lets a staff uploader
        read their own Finance or Business Central file. Not adopted: the capture holds no
        Portal ``uploadedById`` for a drive item, so this side only narrows.

        QC documents (access.ts@1d3eaf2:88-93, e2728afe6 TASK-1929): a record that may be
        a BATCH COA or WAIVER (``taxonomy.may_be_qc_document``: in a Batches folder, with no
        column but BATCH_RECORD, as a waiver's is OTHER) is read, on top of its class, only by a
        ``qc_document_reader``: an admin in both Portal modes, or a member of a QUALITY,
        QA or QC department the Portal confirms and the Entra department names. An unread
        membership or directory, or an eval principal, reads none.
        """
        if self.actor is None:
            return False
        security_class, owner = taxonomy.file_security_class(
            record, self.library_security_classes)
        qc_ok = self.qc_document_reader or not taxonomy.may_be_qc_document(record)
        if self.full_read_covers("sharepoint"):
            # A full-read grant reads every class; Finance, Business Central and an
            # unresolved class (which may be either) only with its ``finance`` scope too.
            # Quality is not granted: a Batches QC document still needs a QC reader.
            restricted = security_class in taxonomy.RESTRICTED_CLASS_READERS \
                or security_class == taxonomy.UNRESOLVED_CLASS
            if not restricted or self.full_read_covers("finance"):
                return qc_ok
        if security_class == "SCRATCH":
            return owner is not None and bool(self.portal_user_id) \
                and owner == self.portal_user_id
        if security_class == taxonomy.UNRESOLVED_CLASS:
            return False
        if security_class in taxonomy.RESTRICTED_CLASS_READERS \
                and security_class not in self.file_clearance:
            return False
        return self.qc_document_reader or not taxonomy.may_be_qc_document(record)


def _portal_decision(rule: PrincipalRule, access: PortalAccessRead,
                     state: portal.AccessState) -> tuple[bool, portal.Decision]:
    """One rule under both Portal modes (legacy, canonical); the caller requires both.

    Legacy: the role matrix on the capability, the MRP flags, and the role among
    ``portal_roles``. Canonical: effective access on the capability, and an access level
    that supplies the bootstrap level of a listed role (the canonical resolver knows no
    roles, only levels). A rule without a capability is decided by the roles alone."""
    legacy = rule.capability is None or portal.legacy_capability_allowed(access.role,
                                                                         rule.capability)
    if rule.mrp_modules:
        legacy = legacy and portal.legacy_mrp_allowed(access.role, access.mrp_modules,
                                                      rule.mrp_modules)
    if rule.portal_roles:
        legacy = legacy and access.role in rule.portal_roles
        floor = min(portal.ACCESS_LEVELS.index(portal.LEGACY_ROLE_BOOTSTRAP[r])
                    for r in rule.portal_roles)
        if portal.ACCESS_LEVELS.index(state.access_level) < floor:
            return legacy, portal.Decision(False, "ROLE_LEVEL_NOT_SUPPLIED",
                                           f"{state.access_level} is below every listed role")
    if rule.capability is None:
        return legacy, portal.Decision(True, "ROLE_LEVEL_SUPPLIED",
                                       f"{state.access_level} supplies a listed role")
    return legacy, portal.resolve_effective_access(access.user_id, rule.capability,
                                                   rule.resource, state)


# ----------------------------------------------------------------- resolver

class EmployeePermissionResolver:
    """``resolve(principal)`` derives one employee's entitlement from cited inputs."""

    def __init__(self, policy: EmployeePermissionPolicy, portal_access: PortalAccessProvider,
                 groups: GroupProvider = groups_unavailable, *,
                 directory: DirectoryProvider | None = None,
                 people_exclusion: Any = None) -> None:
        if not isinstance(policy, EmployeePermissionPolicy):
            raise TypeError("employee permission policy required")
        if not callable(portal_access) or not callable(groups) or not (
                directory is None or callable(directory)):
            raise TypeError("providers must be callable")
        if directory is not None and groups is not groups_unavailable:
            # One Entra input: the directory read carries the membership.
            raise ValueError("directory_replaces_group_provider")
        self.policy = policy
        self._portal = portal_access
        self._groups = groups
        self._directory = directory
        # integrations.people_exclusion.PeopleExclusion: a person it names never holds a
        # principal whose rule declares entra_job_title_keywords; without a usable one no
        # job title grants anything (the Portal department path still decides).
        self._exclusion = people_exclusion

    def _title_exclusion(self, principal: EmployeePrincipal) -> str:
        """``excluded``, ``exclusion_unavailable`` or ``clear`` for this employee."""
        guard = self._exclusion
        try:
            current = getattr(guard, "current", None)
            guard = current() if callable(current) else guard
            if guard is None or guard.unavailable:
                return "exclusion_unavailable"
            return "excluded" if guard.is_excluded(principal.object_id) else "clear"
        except Exception:  # noqa: BLE001 - an undecidable guard grants nothing
            return "exclusion_unavailable"

    def resolve(self, principal: EmployeePrincipal) -> EmployeeEntitlement:
        if not isinstance(principal, EmployeePrincipal):
            raise TypeError("EmployeePrincipal required")
        p = self.policy

        def denied(reason: str, inputs: tuple = (), *,
                   unresolved: bool = False) -> EmployeeEntitlement:
            return EmployeeEntitlement(principal, None, reason, inputs=inputs,
                                       fingerprint=self._fingerprint(None, frozenset(), reason),
                                       rights_unresolved=unresolved)

        # Agents and service principals never gain employee entitlements, and
        # nothing is read for them.
        if principal.kind != "employee":
            return denied("agent_principal_refused")
        if principal.object_id in p.service_principal_object_ids:
            return denied("agent_principal_refused")
        if principal.tenant_id != p.entra_tenant_id:
            return denied("tenant_mismatch")
        try:
            access = self._portal(principal)
        except Exception:  # provider outage: no Portal input -> nothing
            return denied("portal_access_unavailable", unresolved=True)
        if access is None:
            return denied("portal_access_unavailable")
        if not isinstance(access, PortalAccessRead):
            return denied("portal_access_invalid", unresolved=True)
        inputs: tuple[tuple[str, str, str], ...] = (("portal_access", access.source,
                                                     access.read_at),)
        if access.entra_object_id != principal.object_id \
                or access.entra_tenant_id != principal.tenant_id:
            return denied("portal_identity_mismatch", inputs)
        if access.role not in portal.ROLE_LEVEL:
            return denied("portal_role_unknown", inputs)
        if access.role not in portal.EMPLOYEE_ROLES:
            return denied("portal_role_not_employee", inputs)
        if not access.is_active:
            return denied("portal_user_inactive", inputs)
        if not access.verified_identity:
            return denied("portal_identity_unverified", inputs)
        if access.access_level is None:
            return denied("portal_access_level_missing", inputs)

        notes: list[str] = []
        department: str | None = None
        job_title: str | None = None
        identities = {principal.object_id, access.email}
        derived: frozenset[str] = frozenset()     # positive evidence only
        if self._directory is not None:
            try:
                directory = self._directory(principal)
            except Exception:  # provider outage: no Entra input -> nothing
                return denied("entra_directory:provider_failed", inputs, unresolved=True)
            if not isinstance(directory, EntraDirectoryRead):
                return denied("entra_directory:provider_failed", inputs, unresolved=True)
            if directory.status != "read":
                return denied("entra_directory:" + directory.reason, inputs,
                              unresolved=directory.reason not in ENTRA_REFUSAL_REASONS)
            if directory.object_id != principal.object_id:
                return denied("entra_directory:user_mismatch", inputs)
            inputs += (("entra_directory", directory.source, directory.read_at),)
            if directory.group_basis == GROUP_BASIS_NONE:
                # Membership refused and no joined Teams read either: an outage, never
                # narrowed to the Portal role alone.
                return denied(f"entra_directory:{GROUP_MEMBERSHIP_UNAVAILABLE}:"
                              f"{directory.membership_reason}", inputs, unresolved=True)
            member_of: frozenset[str] | None = None
            if directory.group_basis == GROUP_BASIS_TRANSITIVE:
                member_of = directory.group_ids
            else:
                derived = directory.group_ids
                notes += [f"{GROUP_MEMBERSHIP_DERIVED}:{GROUP_BASIS_JOINED_TEAMS}",
                          f"{GROUP_MEMBERSHIP_UNAVAILABLE}:{directory.membership_reason}"]
            department = directory.department.casefold()
            job_title = directory.job_title
            identities |= {v for v in (directory.user_principal_name, directory.mail) if v}
        else:
            try:
                groups = self._groups(principal)
            except Exception:  # provider outage: rights unknown, never narrowed
                return denied("entra_groups:provider_failed", inputs, unresolved=True)
            if not isinstance(groups, EntraGroupRead):
                return denied("entra_groups:provider_failed", inputs, unresolved=True)
            if groups.status == "read":
                member_of = groups.group_ids
                inputs += (("entra_groups", groups.source, groups.read_at),)
            elif groups.reason == GROUP_READ_BLOCKER:
                # The recorded consent blocker, a standing state: group-gated principals
                # are withheld, the rest decided by the Portal role.
                notes += [GROUP_MEMBERSHIP_UNAVAILABLE,
                          f"{GROUP_MEMBERSHIP_UNAVAILABLE}:{GROUP_READ_BLOCKER}"]
                member_of = None
            else:   # any other unread membership is an outage
                return denied("entra_groups:unavailable", inputs, unresolved=True)

        state = access.access_state()
        decisions = []
        for name in sorted(p.rules):
            rule = p.rules[name]
            legacy, canonical = _portal_decision(rule, access, state)
            if not rule.entra_groups:
                group = "not_required"
            elif member_of is not None:
                group = "member" if rule.entra_groups & member_of else "not_member"
            else:   # a derived membership proves presence, never absence
                group = "member" if rule.entra_groups & derived else "unavailable"
            if not rule.entra_departments:
                in_department = "not_required"
            elif department is None:
                in_department = "unavailable"
            else:
                in_department = ("member" if department in rule.entra_departments
                                 else "not_member")
            entra_ok = (group in ("not_required", "member")
                        and in_department in ("not_required", "member"))
            allowed = legacy and canonical.allowed and entra_ok
            title = "not_required"
            if rule.entra_job_title_keywords:
                # Second path: the Entra job title stands in for membership of the rule's
                # Portal departments; the role, access level, scopes and denies still decide.
                title = self._title_exclusion(principal)
                if title == "clear":
                    title = ("unavailable" if job_title is None else
                             "member" if job_title_matches(job_title,
                                                           rule.entra_job_title_keywords)
                             else "not_member")
                if title == "member" and not allowed:
                    as_member = replace(state, department_ids=frozenset(state.department_ids)
                                        | set(rule.resource.department_ids))
                    t_legacy, t_canonical = _portal_decision(rule, access, as_member)
                    allowed = t_legacy and t_canonical.allowed and entra_ok
                    if allowed:
                        legacy, canonical = t_legacy, t_canonical
                if title == "excluded":
                    allowed = False
                notes.append(f"entra_job_title:{name}:{title}")   # an enum, never the title
            decisions.append(PrincipalDecision(name, allowed, legacy, canonical.rule, group,
                                               in_department, title))

        # The Portal's membership decides; the Entra department only narrows it. An
        # unconfirmed membership, no directory read or an unknown department: nothing.
        code = None if department is None else PORTAL_DEPARTMENT_CODES.get(department.strip())
        file_clearance = (clearance_from_department_codes([code] if code else [])
                          & clearance_from_department_codes(access.clearance_departments or ()))
        # QC documents: an admin under both modes (role and access level), else the same
        # Portal-confirmed, Entra-narrowed membership in a QUALITY/QA/QC department.
        qc_departments = set(taxonomy.QC_READER_DEPARTMENTS)
        admin_level = portal.ACCESS_LEVELS.index(portal.LEGACY_ROLE_BOOTSTRAP["ADMIN"])
        qc_reader = ((portal.is_admin(access.role) and access.access_level in portal.ACCESS_LEVELS
                      and portal.ACCESS_LEVELS.index(access.access_level) >= admin_level)
                     or (code in qc_departments
                         and bool(qc_departments & set(access.clearance_departments or ()))))
        held = frozenset(d.principal for d in decisions if d.allowed) | {principal.principal}
        clearance = min(p.clearance_by_access_level[access.access_level],
                        p.clearance_by_access_level[portal.LEGACY_ROLE_BOOTSTRAP[access.role]],
                        key=LEVELS.__getitem__)
        identities |= {"group:" + g for g in (derived if member_of is None else member_of)}
        actor = Actor(p.tenant, held, clearance)
        full_read = None
        grant = p.full_read.get(principal.object_id)
        if grant is not None:
            # The people exclusion must be usable and must not name the grantee; else the
            # grant applies to nothing (the employee keeps their own entitlement).
            state = self._title_exclusion(principal)
            if state == "clear":
                full_read = FullReadAccess(grant, self._exclusion, p.shared_mailboxes)
                inputs += (("full_read_grant", f"employee_policy:{p.digest[:16]}#full_read",
                            grant.granted_at),)
                notes.append(grant.code())
            else:
                notes.append(f"full_read:{state}")
        return EmployeeEntitlement(principal, actor, None, tuple(decisions),
                                   frozenset(identities), tuple(notes), inputs,
                                   self._fingerprint(actor, frozenset(identities), None,
                                                     access.user_id, file_clearance, qc_reader,
                                                     None if full_read is None
                                                     else full_read.grant.code()),
                                   access.user_id, file_clearance,
                                   dict(p.library_security_classes),
                                   qc_document_reader=qc_reader, full_read=full_read)

    def _fingerprint(self, actor: Actor | None, identities: frozenset[str],
                     reason: str | None, user_id: str = "",
                     file_clearance: frozenset[str] = frozenset(),
                     qc_document_reader: bool = False, full_read: str | None = None) -> str:
        """Cache-key material: any change in access changes it (never the shared key)."""
        body = {"policy": self.policy.digest, "reason": reason, "identities": sorted(identities),
                "actor": None if actor is None else
                [actor.tenant, sorted(actor.principals), actor.clearance],
                "portal_user": user_id, "file_clearance": sorted(file_clearance),
                "qc_document_reader": qc_document_reader}
        if full_read is not None:   # absent otherwise: every existing fingerprint unchanged
            body["full_read"] = full_read
        return sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------- gate

def _folded(principal: Any) -> str:
    return principal.strip().lower() if isinstance(principal, str) else ""


def employee_principals(actor: Actor) -> list[str]:
    """Every principal that names an employee, in canonical form (``employee:<oid>``).

    The prefix is matched case- and whitespace-insensitively and the Entra
    object id is a GUID (case-insensitive at Entra), so ``Employee:<OID>`` or
    `` employee:<oid>`` is routed to the resolver like ``employee:<oid>``
    instead of passing as a plain host actor. Matching is otherwise exact.
    """
    return sorted({_folded(p) for p in actor.principals
                   if _folded(p).startswith(EMPLOYEE_PREFIX)})


def _is_agent_principal(principal: Any) -> bool:
    return _folded(principal).startswith(_AGENT_PREFIXES)


# (gate, gated actor, its entitlement) of the gated call in progress in this context
# (``EmployeeActorGate.holding``): that call's own reads reuse the entitlement, nothing else.
_HELD: ContextVar[tuple[Any, Actor, EmployeeEntitlement] | None] = ContextVar(
    "employee_gate_held", default=None)


class EmployeeActorGate:
    """Applied to every actor a case read receives.

    * No ``employee:`` principal: refused with ``employee_identity_required``
      when the gate was built with ``require_employee=True`` (every session
      host serving live data: ``operating.session_identity``); otherwise the
      actor passes unchanged (in-process harnesses and fixture sessions).
    * An ``employee:`` principal: the resolver decides. No resolver
      configured, several employees, a mixed agent/service principal, another
      tenant or a denied entitlement -> ``EmployeeAccessRefused``. Otherwise
      the actor used is the resolved one; if the caller presented more than
      the bare employee principal, only the intersection (and the lower
      clearance) is used, so a caller can narrow but never widen.

    A gated method (``gate_actor_methods``) resolves the employee once per call
    (``holding``): inside that call ``entitlement`` of the gated actor, and the gate
    applied to it again, return that resolve; every other call resolves anew.
    """

    def __init__(self, resolver: EmployeePermissionResolver | None, tenant: str, *,
                 require_employee: bool = False) -> None:
        if resolver is not None and not isinstance(resolver, EmployeePermissionResolver):
            raise TypeError("EmployeePermissionResolver required")
        if resolver is not None and resolver.policy.tenant != tenant:
            raise ValueError("employee_policy_tenant_mismatch")
        if require_employee is not False and require_employee is not True:
            raise TypeError("require_employee must be a bool")
        if require_employee and resolver is None:
            # Requiring an employee without a resolver would refuse everything;
            # say so at construction instead of on the first read.
            raise ValueError("employee_entitlements_not_configured")
        self.resolver = resolver
        self.tenant = tenant
        self.require_employee = require_employee

    def _held(self, actor: Any) -> EmployeeEntitlement | None:
        held = _HELD.get()
        if held is not None and held[0] is self and held[1] is actor:
            return held[2]
        return None

    def entitlement(self, actor: Any) -> EmployeeEntitlement | None:
        held = self._held(actor)
        return held if held is not None else self._resolve(actor)

    def _resolve(self, actor: Any) -> EmployeeEntitlement | None:
        if not isinstance(actor, Actor):
            if self.require_employee:
                raise EmployeeAccessRefused(EMPLOYEE_IDENTITY_REQUIRED)
            return None
        employees = employee_principals(actor)
        if not employees:
            if self.require_employee:
                raise EmployeeAccessRefused(EMPLOYEE_IDENTITY_REQUIRED)
            return None
        if self.resolver is None:
            raise EmployeeAccessRefused("employee_entitlements_not_configured")
        if len(employees) > 1:
            raise EmployeeAccessRefused("several_employee_principals")
        if any(_is_agent_principal(p) for p in actor.principals):
            raise EmployeeAccessRefused("agent_principal_refused")
        if actor.tenant != self.tenant:
            raise EmployeeAccessRefused("tenant_mismatch")
        try:
            principal = EmployeePrincipal(self.resolver.policy.entra_tenant_id,
                                          employees[0][len(EMPLOYEE_PREFIX):])
        except ValueError:
            raise EmployeeAccessRefused("employee_principal_invalid") from None
        entitlement = self.resolver.resolve(principal)
        entitlement.require_actor()
        return entitlement

    def __call__(self, actor: Any) -> Any:
        return self._gate(actor)[0]

    def _gate(self, actor: Any) -> tuple[Any, EmployeeEntitlement | None]:
        """(the actor a read uses, the entitlement that decided it; None: no employee)."""
        held = self._held(actor)
        if held is not None:
            return actor, held
        entitlement = self._resolve(actor)
        if entitlement is None:
            return actor, None
        resolved = entitlement.require_actor()
        # The employee principal is compared in canonical form; every other
        # presented principal stays exact, so a variant can only narrow.
        presented = frozenset(
            entitlement.principal.principal if _folded(p).startswith(EMPLOYEE_PREFIX) else p
            for p in actor.principals)
        if presented != {entitlement.principal.principal}:
            clearance = min(actor.clearance, resolved.clearance, key=LEVELS.__getitem__)
            resolved = Actor(resolved.tenant, presented & resolved.principals, clearance)
        return resolved, entitlement

    @contextmanager
    def holding(self, actor: Any) -> Iterator[Any]:
        """``actor`` gated for one call: inside the block the gated actor's entitlement
        is this resolve (one per call), so the call's own employee checks never resolve
        the employee again; it is released when the call returns."""
        gated, entitlement = self._gate(actor)
        if entitlement is None:
            yield gated
            return
        token = _HELD.set((self, gated, entitlement))
        try:
            yield gated
        finally:
            _HELD.reset(token)


def gate_actor_methods(target: Any, gate: Callable[[Any], Any]) -> Any:
    """Route the ``actor`` argument of every public method of ``target`` through ``gate``.

    Installs instance attributes that shadow the class methods, so
    ``isinstance`` and every other attribute are unchanged; the gate is
    idempotent, so internal calls that pass a gated actor again are harmless. A gate
    with ``holding`` (``EmployeeActorGate``) holds its decision for the call.
    """
    holding = getattr(gate, "holding", None)
    for name in dir(type(target)):
        if name.startswith("_"):
            continue
        method = getattr(target, name)
        if not inspect.ismethod(method):
            continue
        params = list(inspect.signature(method).parameters)
        if "actor" not in params:
            continue
        index = params.index("actor")

        def wrap(bound: Callable[..., Any], position: int) -> Callable[..., Any]:
            @functools.wraps(bound)
            def gated(*args: Any, **kwargs: Any) -> Any:
                if "actor" in kwargs:
                    actor = kwargs["actor"]
                elif len(args) > position:
                    actor = args[position]
                else:
                    return bound(*args, **kwargs)
                with (holding(actor) if holding else nullcontext(gate(actor))) as actor:
                    if "actor" in kwargs:
                        kwargs["actor"] = actor
                    else:
                        args = args[:position] + (actor,) + args[position + 1:]
                    return bound(*args, **kwargs)
            return gated

        setattr(target, name, wrap(method, index))
    return target


# ------------------------------------------------------------------ adapter

def full_read_marking_grants(marking_grants: Mapping[str, Iterable[str]] | None,
                             entitlement: EmployeeEntitlement | None
                             ) -> dict[str, frozenset[str]]:
    """The host's ``marking_grants`` with this employee's principal added to every
    ``FULL_READ_MARKINGS`` marking when their full-read grant covers ``finance`` now.
    Anyone else, or a grant without that scope: the host's grants unchanged."""
    out = {m: frozenset(p) for m, p in (marking_grants or {}).items()}
    if entitlement is None or not entitlement.full_read_covers("finance"):
        return out
    for marking in FULL_READ_MARKINGS:
        out[marking] = out.get(marking, frozenset()) | {entitlement.principal.principal}
    return out


def live_source_audience_document(base: Mapping[str, Any],
                                  entitlement: EmployeeEntitlement) -> dict[str, Any]:
    """A per-request ``plx-live-source-audience/v1`` document for this employee.

    Keeps the base file's grants and classifications; replaces its static
    ``identities`` with exactly this employee's identities (object id, Portal
    email, ``group:<id>`` only for a completely read or derived membership) and always
    requires the source ACL for mail and SharePoint.
    """
    actor = entitlement.require_actor()
    if not isinstance(base, Mapping) or base.get("tenant") != actor.tenant:
        raise EmployeeAccessRefused("live_audience_tenant_mismatch")
    doc = json.loads(json.dumps(dict(base)))
    doc["identities"] = {entitlement.principal.principal: sorted(entitlement.source_identities)}
    required = list(doc.get("source_acl_required") or [])
    doc["source_acl_required"] = sorted(set(required) | set(ACL_BEARING_PATTERNS))
    return doc


__all__ = ["DIRECTORY_SOURCE", "EMPLOYEE_IDENTITY_REQUIRED", "EMPLOYEE_PREFIX",
           "FULL_READ_LABEL", "FULL_READ_MAIL_SCOPES", "FULL_READ_MARKINGS",
           "FULL_READ_SCOPES", "FullReadAccess", "FullReadGrant",
           "full_read_marking_grants",
           "ENTRA_REFUSAL_REASONS",
           "GROUP_BASES", "GROUP_BASIS_JOINED_TEAMS", "GROUP_BASIS_NONE",
           "GROUP_BASIS_TRANSITIVE", "GROUP_MEMBERSHIP_DERIVED", "PORTAL_DEPARTMENT_CODES",
           "clearance_from_department_codes",
           "EmployeeAccessRefused", "EmployeeActorGate", "EmployeeRightsUnresolved", "EmployeeEntitlement",
           "EntraDirectoryRead", "EmployeePermissionPolicy", "EmployeePermissionResolver",
           "EmployeePrincipal", "EntraGroupRead", "GROUP_MEMBERSHIP_UNAVAILABLE",
           "GROUP_READ_BLOCKER", "MAILBOX_ACCESS_UNDETERMINED", "POLICY_SCHEMA",
           "PORTAL_ACCESS_SNAPSHOT_SCHEMA", "PortalAccessRead", "PortalAccessSnapshot",
           "PrincipalDecision", "PrincipalRule", "employee_principals",
           "gate_actor_methods", "groups_unavailable", "live_source_audience_document"]
