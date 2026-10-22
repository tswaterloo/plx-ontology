"""Shared permission rules every Workbench source path uses.

The per-employee resolver is ``operating.employee_permissions`` (Portal role ∩
Entra groups ∩ SharePoint item permissions, mirrored from the Portal's own
RBAC; docs/reference/permissions.md). It replaced the earlier entitlement map here,
which **added** principals from Entra groups and explicit grants to the
Portal-role principals (a union across sources) and used Workbench-invented
role ceilings instead of the Portal's role and capability matrix.

What stays here are the rules other modules import without pulling in the
resolver: agents are never principals, which sources carry an item ACL, and
the denial type.
"""

from __future__ import annotations

# Agents and service principals are never principals of an audience, a case or
# an original (operating/case_originals.py imports this one rule).
_AGENT_PREFIXES = ("agent:", "sp_")
# A source ACL is always required where the source exposes one (mail owner,
# SharePoint item permissions). BC and the Portal mirror expose none.
ACL_BEARING_PATTERNS = ("mail:*", "sharepoint:*")


class EntitlementDenied(PermissionError):
    """No actor for this caller; ``reason`` is safe to log (no identity values)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


__all__ = ["ACL_BEARING_PATTERNS", "EntitlementDenied"]
