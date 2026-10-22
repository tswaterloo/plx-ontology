"""Portal RBAC, mirrored exactly: role permissions, capability registry, effective access.

Read-only port of the Portal's authorization predicates so the Workbench can
decide per employee with the Portal's own rules (the ``plxwb_`` key carries no
row or employee scoping). Source, read line by line at
plx-customer-portal@8da33e4571171acbb23f8f63b7a99cee89be955e:

* ``portal/src/lib/auth/roles.ts`` -- ``ROLES``, ``ROLE_LEVEL``, ``PERMISSIONS``,
  ``ROLE_PERMISSIONS``, ``can``, ``hasRole``, ``isAdmin``.
* ``portal/src/lib/auth/rbac-policy.ts`` -- ``ACCESS_LEVELS``,
  ``CAPABILITY_REGISTRY`` (minimum level, required scopes, elevatable),
  ``POLICY_VERSION``, ``LEGACY_ROLE_BOOTSTRAP``, ``levelSuppliesCapability``.
* ``portal/src/lib/auth/rbac-adapters.ts`` -- ``legacyCapabilityAllowed``.
* ``portal/src/lib/auth/effective-access.ts`` -- ``resolveEffectiveAccess``
  (identity checks, explicit scope/policy denies, required scope context,
  actor scopes, base level). **Temporary elevations are not ported**: an
  elevation only ever widens, and the Workbench never widens on an input it
  cannot re-read, so a decision that would need one denies.
* ``portal/src/lib/auth/mrp-access.ts`` -- ``canAccessMrpModule`` and the
  legacy predicate of ``api-auth.ts:requireMrpModule``.
* ``portal/src/lib/auth/data-key-registry.ts`` -- ``RBAC_DATA_KEYS``.

The Portal chooses legacy or canonical per ``PLX_RBAC_MODE``
(``rbac-mode.ts:selectRbacAccess``). The Workbench cannot see that switch, so
callers require **both** (``portal_allows``): never wider than either mode.
Pure; no I/O, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

PORTAL_SOURCE = "plx-customer-portal@8da33e4571171acbb23f8f63b7a99cee89be955e"

# roles.ts ROLE_LEVEL
ROLE_LEVEL = {"CUSTOMER": 0, "SUPPLIER": 0, "STAFF": 1, "MANAGER": 2, "ADMIN": 3,
              "SUPER_ADMIN": 4}
ROLES = tuple(ROLE_LEVEL)
# CUSTOMER and SUPPLIER are external tenants (roles.ts header); never employees.
EMPLOYEE_ROLES = frozenset({"STAFF", "MANAGER", "ADMIN", "SUPER_ADMIN"})

# roles.ts PERMISSIONS, in declaration order
PERMISSIONS = (
    "VIEW_DASHBOARD", "VIEW_PROJECTS", "CREATE_PROJECT", "UPLOAD_DOCUMENT",
    "VIEW_OWN_DOCUMENTS", "SUBMIT_CREDIT_APPLICATION", "VIEW_NOTIFICATIONS",
    "MANAGE_OWN_ACCOUNT",
    "VIEW_ALL_DOCUMENTS", "REVIEW_DOCUMENTS", "BULK_ACTION_DOCUMENTS",
    "VIEW_CUSTOMERS", "EDIT_CUSTOMER", "CREATE_CUSTOMER", "MANAGE_ONBOARDING",
    "VIEW_CREDIT_APPLICATIONS", "REVIEW_CREDIT_APPLICATION",
    "VIEW_USERS", "CREATE_USER", "EDIT_USER_ROLE", "DISABLE_USER",
    "VIEW_AUDIT_LOG", "EXPORT_AUDIT_LOG",
    "VIEW_PROVISIONING", "MANAGE_PROVISIONING",
    "VIEW_SETTINGS", "MANAGE_SETTINGS",
    "VIEW_OPERATIONS", "MANAGE_OPERATIONS",
)

_CUSTOMER = frozenset({"VIEW_DASHBOARD", "VIEW_PROJECTS", "CREATE_PROJECT", "UPLOAD_DOCUMENT",
                       "VIEW_OWN_DOCUMENTS", "SUBMIT_CREDIT_APPLICATION", "VIEW_NOTIFICATIONS",
                       "MANAGE_OWN_ACCOUNT"})
_STAFF = _CUSTOMER | {"VIEW_ALL_DOCUMENTS", "REVIEW_DOCUMENTS", "VIEW_CUSTOMERS",
                      "VIEW_CREDIT_APPLICATIONS", "REVIEW_CREDIT_APPLICATION",
                      "VIEW_PROVISIONING", "VIEW_OPERATIONS"}
_MANAGER = _STAFF | {"MANAGE_PROVISIONING", "MANAGE_ONBOARDING"}
_ADMIN = _STAFF | {"BULK_ACTION_DOCUMENTS", "EDIT_CUSTOMER", "CREATE_CUSTOMER",
                   "MANAGE_ONBOARDING", "VIEW_USERS", "CREATE_USER", "EDIT_USER_ROLE",
                   "DISABLE_USER", "VIEW_AUDIT_LOG", "EXPORT_AUDIT_LOG",
                   "MANAGE_PROVISIONING", "MANAGE_OPERATIONS"}
# roles.ts ROLE_PERMISSIONS (SUPER_ADMIN = every PERMISSIONS value)
ROLE_PERMISSIONS = {
    "CUSTOMER": _CUSTOMER,
    "SUPPLIER": frozenset({"VIEW_DASHBOARD", "VIEW_NOTIFICATIONS", "MANAGE_OWN_ACCOUNT"}),
    "STAFF": frozenset(_STAFF),
    "MANAGER": frozenset(_MANAGER),
    "ADMIN": frozenset(_ADMIN),
    "SUPER_ADMIN": frozenset(PERMISSIONS),
}

# rbac-policy.ts
ACCESS_LEVELS = ("L1", "L2", "L3", "L4", "L5")
POLICY_VERSION = "portal-rbac-2026-08-06.v1"
EXTERNAL_COMPANY_KEY = "petra-hygienic-plx"
LEGACY_ROLE_BOOTSTRAP = {"CUSTOMER": "L1", "SUPPLIER": "L1", "STAFF": "L2", "MANAGER": "L3",
                         "ADMIN": "L4", "SUPER_ADMIN": "L5"}


@dataclass(frozen=True)
class ScopeRequirements:
    department: bool
    company: bool
    data: bool
    owner: bool


@dataclass(frozen=True)
class CapabilityDefinition:
    minimum_level: str
    required_scopes: ScopeRequirements
    elevatable: bool = True


_OWNED = ScopeRequirements(department=False, company=True, data=True, owner=True)
_SCOPED = ScopeRequirements(department=True, company=True, data=True, owner=False)
_GLOBAL = ScopeRequirements(department=False, company=False, data=False, owner=False)


def _cap(level: str, scopes: ScopeRequirements, elevatable: bool = True) -> CapabilityDefinition:
    return CapabilityDefinition(level, scopes, elevatable)


CAPABILITY_REGISTRY = {
    "VIEW_DASHBOARD": _cap("L1", _OWNED),
    "VIEW_PROJECTS": _cap("L1", _OWNED),
    "CREATE_PROJECT": _cap("L1", _OWNED),
    "UPLOAD_DOCUMENT": _cap("L1", _OWNED),
    "VIEW_OWN_DOCUMENTS": _cap("L1", _OWNED),
    "SUBMIT_CREDIT_APPLICATION": _cap("L1", _OWNED),
    "VIEW_NOTIFICATIONS": _cap("L1", _OWNED),
    "MANAGE_OWN_ACCOUNT": _cap("L1", _OWNED),
    "VIEW_ALL_DOCUMENTS": _cap("L2", _SCOPED),
    "REVIEW_DOCUMENTS": _cap("L2", _SCOPED),
    "VIEW_CUSTOMERS": _cap("L2", _SCOPED),
    "VIEW_CREDIT_APPLICATIONS": _cap("L2", _SCOPED),
    "REVIEW_CREDIT_APPLICATION": _cap("L2", _SCOPED),
    "VIEW_PROVISIONING": _cap("L2", _SCOPED),
    "VIEW_OPERATIONS": _cap("L2", _SCOPED),
    "MANAGE_PROVISIONING": _cap("L3", _SCOPED),
    "MANAGE_ONBOARDING": _cap("L3", _SCOPED),
    "BULK_ACTION_DOCUMENTS": _cap("L4", _SCOPED),
    "EDIT_CUSTOMER": _cap("L4", _SCOPED),
    "CREATE_CUSTOMER": _cap("L4", _SCOPED),
    "VIEW_USERS": _cap("L4", _SCOPED),
    "CREATE_USER": _cap("L4", _SCOPED),
    "EDIT_USER_ROLE": _cap("L4", _SCOPED),
    "DISABLE_USER": _cap("L4", _SCOPED),
    "MANAGE_OPERATIONS": _cap("L4", _SCOPED),
    "VIEW_AUDIT_LOG": _cap("L5", _GLOBAL, False),
    "EXPORT_AUDIT_LOG": _cap("L5", _GLOBAL, False),
    "VIEW_SETTINGS": _cap("L5", _GLOBAL, False),
    "MANAGE_SETTINGS": _cap("L5", _GLOBAL, False),
    "MANAGE_GLOBAL_POLICY": _cap("L5", _GLOBAL, False),
    "MANAGE_ENTRA": _cap("L5", _GLOBAL, False),
    "MANAGE_TEMPORARY_ELEVATION": _cap("L5", _GLOBAL, False),
}

# data-key-registry.ts RBAC_DATA_KEYS (identity.user-administration from rbac-policy.ts)
RBAC_DATA_KEYS = frozenset({
    "identity.user-administration", "identity.provisioning", "operations.warehouse",
    "operations.manufacturing", "operations.rnd", "operations.procurement",
    "operations.finance", "operations.sales", "operations.project-development",
    "operations.quality", "platform.governance", "platform.preferences", "crm.customers",
    "documents.controlled", "customer.form-requests"})

MRP_MODULES = ("warehouse", "production", "rnd")  # mrp-access.ts MrpModule


# ------------------------------------------------------------- legacy (roles.ts)

def can(role: str, permission: str) -> bool:
    """roles.ts ``can``: unknown role -> False."""
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def has_role(role: str, required: str) -> bool:
    """roles.ts ``hasRole``: level >= required level; unknown either side -> False."""
    if role not in ROLE_LEVEL or required not in ROLE_LEVEL:
        return False
    return ROLE_LEVEL[role] >= ROLE_LEVEL[required]


def is_admin(role: str) -> bool:
    return has_role(role, "ADMIN")


def level_supplies_capability(level: str, capability: str) -> bool:
    """rbac-policy.ts ``levelSuppliesCapability``."""
    return (ACCESS_LEVELS.index(level)
            >= ACCESS_LEVELS.index(CAPABILITY_REGISTRY[capability].minimum_level))


def legacy_capability_allowed(role: str, capability: str) -> bool:
    """rbac-adapters.ts ``legacyCapabilityAllowed``."""
    if capability in PERMISSIONS:
        return can(role, capability)
    return (role in ROLE_LEVEL and capability in CAPABILITY_REGISTRY
            and level_supplies_capability(LEGACY_ROLE_BOOTSTRAP[role], capability))


def can_access_mrp_module(role: str, modules: Iterable[str], module: str) -> bool:
    """mrp-access.ts ``canAccessMrpModule``: admins bypass; else the user's module flag."""
    if is_admin(role):
        return True
    return module in MRP_MODULES and module in set(modules)


def legacy_mrp_allowed(role: str, modules: Iterable[str], required: Iterable[str],
                       minimum_role: str = "STAFF") -> bool:
    """api-auth.ts ``requireMrpModule`` legacy predicate: role floor and any one module."""
    held = set(modules)
    return has_role(role, minimum_role) and any(
        can_access_mrp_module(role, held, m) for m in required)


# -------------------------------------------------- canonical (effective-access.ts)

@dataclass(frozen=True)
class ScopeRule:
    """``UserAccessScope`` row: type COMPANY|DATA, key, effect ALLOW|DENY."""

    type: str
    key: str
    effect: str


@dataclass(frozen=True)
class PolicyDeny:
    """``AccessPolicyGrant`` row (DENY-only); a null scope column matches every value."""

    capability: str
    state: str = "ACTIVE"
    department_id: str | None = None
    company_key: str | None = None
    data_key: str | None = None
    id: str = ""


@dataclass(frozen=True)
class AccessState:
    """``EffectiveAccessState`` without elevations (see module docstring)."""

    user_id: str
    active: bool
    verified_identity: bool
    access_level: str | None
    department_ids: frozenset[str] = frozenset()
    scopes: tuple[ScopeRule, ...] = ()
    explicit_denies: tuple[PolicyDeny, ...] = ()


@dataclass(frozen=True)
class Resource:
    """``EffectiveAccessResource``."""

    type: str
    department_ids: tuple[str, ...] = ()
    company_key: str | None = None
    data_key: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str
    reason: str
    policy_version: str = field(default=POLICY_VERSION)


def _norm(value: str | None) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def resolve_effective_access(actor_id: str, capability: str, resource: Resource,
                             state: AccessState) -> Decision:
    """effective-access.ts ``resolveEffectiveAccess``, same rule order, no elevation."""
    if not state.verified_identity or state.user_id != actor_id:
        return Decision(False, "INVALID_IDENTITY",
                        "actor identity is absent, mismatched, or unverified")
    if not state.active:
        return Decision(False, "INACTIVE_IDENTITY", "actor identity is inactive")
    if not state.access_level:
        return Decision(False, "MISSING_ACCESS_LEVEL", "actor has no canonical access level")
    if capability not in CAPABILITY_REGISTRY:
        return Decision(False, "CAPABILITY_NOT_GRANTED",
                        "capability is not present in the canonical registry")

    requested_departments = sorted({d for d in map(_norm, resource.department_ids) if d})
    requested_company = _norm(resource.company_key)
    requested_data = _norm(resource.data_key)
    requested_owner = _norm(resource.owner_id)

    # explicitScopeDenyMatches
    for scope in state.scopes:
        if scope.effect == "DENY" and scope.type == "COMPANY" \
                and _norm(scope.key) == requested_company:
            return Decision(False, "EXPLICIT_DENY", "company scope is explicitly denied")
    for scope in state.scopes:
        if scope.effect == "DENY" and scope.type == "DATA" and _norm(scope.key) == requested_data:
            return Decision(False, "EXPLICIT_DENY", "data scope is explicitly denied")
    for deny in state.explicit_denies:
        if deny.state != "ACTIVE" or deny.capability != capability:
            continue
        dep, com, dat = _norm(deny.department_id), _norm(deny.company_key), _norm(deny.data_key)
        if ((not dep or dep in requested_departments)
                and (not com or requested_company == com)
                and (not dat or requested_data == dat)):
            return Decision(False, "EXPLICIT_DENY", f"policy deny {deny.id}")

    required = CAPABILITY_REGISTRY[capability].required_scopes
    missing = [name for name, needed, have in (
        ("department", required.department, bool(requested_departments)),
        ("company", required.company, bool(requested_company)),
        ("data", required.data, bool(requested_data)),
        ("owner", required.owner, bool(requested_owner))) if needed and not have]
    if missing:
        return Decision(False, "MISSING_REQUIRED_SCOPE_CONTEXT",
                        "missing required scope context: " + ", ".join(missing))

    departments = {d for d in map(_norm, state.department_ids) if d}
    companies = {_norm(s.key) for s in state.scopes if s.type == "COMPANY" and s.effect == "ALLOW"}
    data_keys = {_norm(s.key) for s in state.scopes if s.type == "DATA" and s.effect == "ALLOW"}
    scopes_allow = ((not required.department
                     or all(d in departments for d in requested_departments))
                    and (not required.company or requested_company in companies)
                    and (not required.data or requested_data in data_keys)
                    and (not required.owner or requested_owner == actor_id))
    if not scopes_allow:
        return Decision(False, "OUT_OF_SCOPE",
                        "requested department, company, data, or owner is outside actor scope")
    if level_supplies_capability(state.access_level, capability):
        return Decision(True, "BASE_LEVEL_CAPABILITY",
                        f"{state.access_level} supplies {capability} within scope")
    return Decision(False, "CAPABILITY_NOT_GRANTED",
                    f"{state.access_level} does not supply {capability} and no valid "
                    "elevation applies")


__all__ = ["ACCESS_LEVELS", "AccessState", "CAPABILITY_REGISTRY", "CapabilityDefinition",
           "Decision", "EMPLOYEE_ROLES", "EXTERNAL_COMPANY_KEY", "LEGACY_ROLE_BOOTSTRAP",
           "MRP_MODULES", "PERMISSIONS", "POLICY_VERSION", "PORTAL_SOURCE", "PolicyDeny",
           "RBAC_DATA_KEYS", "ROLES", "ROLE_LEVEL", "ROLE_PERMISSIONS", "Resource",
           "ScopeRule", "ScopeRequirements", "can", "can_access_mrp_module", "has_role",
           "is_admin", "legacy_capability_allowed", "legacy_mrp_allowed",
           "level_supplies_capability", "resolve_effective_access"]
