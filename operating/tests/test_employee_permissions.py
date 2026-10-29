"""Per-employee entitlements: Portal role ∩ Entra groups ∩ SharePoint item ACL.

Evidence label: ``isolated-fixture``. Portal inputs are Portal-shaped fixtures
(``app_user`` role / access level / scopes / denies / MRP flags as in
portal/prisma/schema.prisma); no network, no credential.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AudienceMap
from ontology.core import Actor
from operating import employee_permissions_portal as portal
from operating.doc_metadata_taxonomy import build_attachment_metadata
from operating.employee_permissions import (
    EMPLOYEE_PREFIX, GROUP_MEMBERSHIP_UNAVAILABLE, POLICY_SCHEMA, EmployeeAccessRefused, EmployeeActorGate,
    EmployeePermissionPolicy, EmployeePermissionResolver, EmployeePrincipal,
    EmployeeRightsUnresolved, EntraDirectoryRead, EntraGroupRead, PortalAccessRead,
    PortalAccessSnapshot, groups_unavailable, live_source_audience_document)

ROOT = Path(__file__).resolve().parents[2]
PORTAL_COMMIT = portal.PORTAL_SOURCE.split("@", 1)[1]
PORTAL_REPO_ENV = "PLX_PORTAL_REPO"


def _has_commit(repo: Path, commit: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_portal_repo(root: Path = ROOT, environ=None, commit: str = PORTAL_COMMIT):
    """A local Portal clone holding ``commit``, wherever this repo is checked out.

    Order: ``$PLX_PORTAL_REPO``; a ``plx-customer-portal`` sibling of this
    checkout or of any of its parents (worktrees live next to the main
    checkout); ``~/plx-customer-portal``. None when no clone has the commit
    (the drift tests then skip and say how to point them at one).
    """
    environ = os.environ if environ is None else environ
    candidates = [Path(environ[PORTAL_REPO_ENV])] if environ.get(PORTAL_REPO_ENV) else []
    candidates += [base / "plx-customer-portal" for base in (root, *root.parents)]
    candidates.append(Path.home() / "plx-customer-portal")
    for candidate in candidates:
        if candidate.is_dir() and _has_commit(candidate, commit):
            return candidate
    return None
TID = "11111111-1111-4111-8111-111111111111"
OTHER_TID = "22222222-2222-4222-8222-222222222222"
OID = "aaaaaaaa-0000-4000-8000-000000000001"
AGENT_OID = "aaaaaaaa-0000-4000-8000-0000000000ff"
QA_GROUP = "bbbbbbbb-0000-4000-8000-0000000000a1"
DEPT_OPS, DEPT_QA = "cmdeptops0001", "cmdeptqa00001"
COMPANY = portal.EXTERNAL_COMPANY_KEY


def rule(capability, data_key, departments, **extra):
    return {"capability": capability, "department_ids": departments, "company_key": COMPANY,
            "data_key": data_key, **extra}


POLICY = {
    "schema": POLICY_SCHEMA, "tenant": "plx", "entra_tenant_id": TID,
    "clearance_by_access_level": {"L1": "internal", "L2": "internal", "L3": "confidential",
                                  "L4": "confidential", "L5": "restricted"},
    "principals": {
        "staff:operations": rule("VIEW_OPERATIONS", "operations.sales", [DEPT_OPS],
                                 mrp_modules=["production"]),
        "staff:customers": rule("VIEW_CUSTOMERS", "crm.customers", [DEPT_OPS]),
        "staff:documents": rule("VIEW_ALL_DOCUMENTS", "documents.controlled", [DEPT_QA]),
        "staff:quality": rule("VIEW_OPERATIONS", "operations.quality", [DEPT_QA],
                              entra_groups=[QA_GROUP]),
        "staff:finance": rule("MANAGE_OPERATIONS", "operations.finance", [DEPT_OPS]),
        "admin:audit": {"capability": "VIEW_AUDIT_LOG"},
    },
    "service_principal_object_ids": [AGENT_OID],
}
ALL_SCOPES = tuple(portal.ScopeRule(t, k, "ALLOW") for t, k in (
    ("COMPANY", COMPANY), ("DATA", "operations.sales"), ("DATA", "crm.customers"),
    ("DATA", "documents.controlled"), ("DATA", "operations.quality"),
    ("DATA", "operations.finance")))


def portal_user(role="STAFF", **overrides):
    """A Portal ``app_user`` read as the provider would return it (Portal-shaped)."""
    values = dict(user_id="cmuser0000001", email="Ricardo@PetraLabX.com", role=role,
                  access_level=portal.LEGACY_ROLE_BOOTSTRAP[role], is_active=True,
                  verified_identity=True, entra_tenant_id=TID, entra_object_id=OID,
                  source="portal:app_user/cmuser0000001@fixture", read_at="2026-10-01T09:00:00Z",
                  mrp_modules=frozenset({"production"}),
                  department_ids=frozenset({DEPT_OPS, DEPT_QA}), scopes=ALL_SCOPES)
    values.update(overrides)
    return PortalAccessRead(**values)


def member(*groups):
    return EntraGroupRead("read", frozenset(groups), source="graph:transitiveMemberOf@fixture",
                          read_at="2026-10-01T09:00:00Z")


class Calls:
    def __init__(self, value):
        self.value, self.count = value, 0

    def __call__(self, principal):
        self.count += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def resolver(user=None, groups=None, policy=None, directory=None):
    portal_reads = user if callable(user) else Calls(
        portal_user() if user is None else user)
    policy = EmployeePermissionPolicy.from_json(policy or POLICY)
    if directory is not None:
        return EmployeePermissionResolver(policy, portal_reads, directory=directory)
    group_reads = groups if callable(groups) else Calls(member(QA_GROUP) if groups is None
                                                          else groups)
    return EmployeePermissionResolver(policy, portal_reads, group_reads)


def held(entitlement):
    return set(entitlement.require_actor().principals) - {"employee:" + OID}


EMPLOYEE = EmployeePrincipal(TID, OID)


def snapshot_user(path, **fields):
    """A Portal access snapshot at ``path`` whose one row is ``EMPLOYEE`` (STAFF)."""
    path.write_text(json.dumps({
        "schema": "plx-portal-access-snapshot/v1", "source": "portal:fixture",
        "read_at": "2026-10-06T09:00:00Z", "users": [{
            "id": "cmuser0000001", "email": "ricardo@petralabx.com", "role": "STAFF",
            "accessLevel": portal.LEGACY_ROLE_BOOTSTRAP["STAFF"], "isActive": True,
            "entraTenantId": TID, "entraObjectId": OID, **fields}]}), "utf-8")


def directory_read(status="read", **fields):
    values = dict(object_id=OID, group_ids=frozenset({QA_GROUP}),
                  source=f"graph:directory/users/{OID}", read_at="2026-10-01T09:00:00Z")
    if status != "read":
        values = dict(object_id=OID)
    return EntraDirectoryRead(status, **{**values, **fields})


def outage_resolvers(tmp):
    """One resolver per permission-provider outage, each else able to resolve EMPLOYEE."""
    from operating.tests.test_employee_permissions_entra import FakeGraph, stack
    unreadable = Path(tmp) / "portal-access.json"
    snapshot_user(unreadable)
    snapshot = PortalAccessSnapshot(unreadable)   # loads; then the export breaks
    unreadable.write_text("{not json", "utf-8")
    missing = Path(tmp) / "portal-access-removed.json"
    snapshot_user(missing)
    removed = PortalAccessSnapshot(missing)
    missing.unlink()
    return {
        "portal_provider_raises": resolver(Calls(RuntimeError("portal down"))),
        "portal_provider_wrong_type": resolver(Calls(directory_read())),
        "portal_snapshot_unparseable": resolver(snapshot),
        "portal_snapshot_missing": resolver(removed),
        "groups_provider_raises": resolver(groups=Calls(RuntimeError("graph 503"))),
        "groups_not_read": resolver(groups=Calls(None)),
        "groups_wrong_type": resolver(groups=Calls(directory_read())),
        "groups_read_5xx": resolver(groups=EntraGroupRead("unavailable",
                                                          reason="unreadable:http_503")),
        "directory_provider_raises": resolver(directory=Calls(RuntimeError("graph down"))),
        "directory_not_read": resolver(directory=Calls(None)),
        "directory_wrong_type": resolver(directory=Calls(member(QA_GROUP))),
        "directory_5xx": resolver(directory=Calls(directory_read(
            "unavailable", reason="unreadable:http_503"))),
        "directory_retried_504": resolver(directory=Calls(directory_read(
            "unavailable", reason="unreadable:retried.http_504"))),
        "directory_transport": resolver(directory=Calls(directory_read(
            "unavailable", reason="unreadable:timeout"))),
        "directory_membership_truncated": resolver(directory=Calls(directory_read(
            "unavailable", reason="membership_truncated"))),
        "directory_consent_missing": resolver(directory=Calls(directory_read(
            "unavailable", reason="graph_permission_missing:User.Read.All"))),
        # Through the real GET-only directory reader (injected Graph transport):
        "graph_user_read_consent_missing": stack(FakeGraph(user_status=403))[1],
        "graph_membership_404_after_user_read": stack(FakeGraph(membership_status=404))[1],
        "graph_membership_refused_and_joined_teams_unreadable": stack(FakeGraph(
            membership_status=403, joined_status=500))[1],
    }


class PortalRoleTests(unittest.TestCase):
    """Each Portal role, with the role's own bootstrap access level, as Portal decides it."""

    EXPECTED = {
        # role: (principals, clearance)
        "STAFF": ({"staff:operations", "staff:customers", "staff:documents", "staff:quality"},
                  "internal"),
        "MANAGER": ({"staff:operations", "staff:customers", "staff:documents", "staff:quality"},
                    "confidential"),
        # ADMIN has VIEW_AUDIT_LOG in the legacy matrix but L4 < L5 canonically.
        "ADMIN": ({"staff:operations", "staff:customers", "staff:documents", "staff:quality",
                   "staff:finance"}, "confidential"),
        "SUPER_ADMIN": ({"staff:operations", "staff:customers", "staff:documents",
                         "staff:quality", "staff:finance", "admin:audit"}, "restricted"),
    }

    def test_each_employee_role(self):
        for role, (principals, clearance) in self.EXPECTED.items():
            with self.subTest(role=role):
                ent = resolver(portal_user(role)).resolve(EMPLOYEE)
                self.assertEqual(held(ent), principals)
                self.assertEqual(ent.require_actor().clearance, clearance)
                self.assertIn(("portal_access", "portal:app_user/cmuser0000001@fixture",
                               "2026-10-01T09:00:00Z"), ent.inputs)
                self.assertEqual(ent.notes, ())

    def test_customer_and_supplier_are_tenants_not_employees(self):
        for role in ("CUSTOMER", "SUPPLIER"):
            with self.subTest(role=role):
                ent = resolver(portal_user(role)).resolve(EMPLOYEE)
                self.assertIsNone(ent.actor)
                self.assertEqual(ent.denied_reason, "portal_role_not_employee")
                with self.assertRaises(EmployeeAccessRefused):
                    ent.require_actor()

    def test_unknown_role_string_denies(self):
        ent = resolver(replace(portal_user(), role="OPERATOR")).resolve(EMPLOYEE)
        self.assertEqual(ent.denied_reason, "portal_role_unknown")

    def test_role_and_access_level_are_intersected(self):
        # A STAFF user given L4 still lacks MANAGE_OPERATIONS in the legacy matrix.
        staff_l4 = resolver(portal_user("STAFF", access_level="L4")).resolve(EMPLOYEE)
        self.assertNotIn("staff:finance", held(staff_l4))
        self.assertEqual(staff_l4.require_actor().clearance, "internal")  # role bootstrap L2
        # An ADMIN downgraded to L2 loses what L2 does not supply canonically.
        admin_l2 = resolver(portal_user("ADMIN", access_level="L2")).resolve(EMPLOYEE)
        self.assertNotIn("staff:finance", held(admin_l2))
        self.assertEqual(admin_l2.require_actor().clearance, "internal")
        decision = {d.principal: d for d in admin_l2.decisions}["staff:finance"]
        self.assertTrue(decision.portal_legacy)
        self.assertEqual(decision.portal_canonical, "CAPABILITY_NOT_GRANTED")

    def test_mrp_module_flags_gate_staff_but_admins_bypass(self):
        staff = resolver(portal_user("STAFF", mrp_modules=frozenset({"warehouse"})))
        self.assertNotIn("staff:operations", held(staff.resolve(EMPLOYEE)))
        admin = resolver(portal_user("ADMIN", mrp_modules=frozenset()))
        self.assertIn("staff:operations", held(admin.resolve(EMPLOYEE)))

    def test_portal_scope_and_policy_denies_are_final(self):
        scopes = ALL_SCOPES + (portal.ScopeRule("DATA", "operations.sales", "DENY"),)
        ent = resolver(portal_user("SUPER_ADMIN", scopes=scopes)).resolve(EMPLOYEE)
        self.assertNotIn("staff:operations", held(ent))
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:operations"]
                         .portal_canonical, "EXPLICIT_DENY")
        deny = portal.PolicyDeny("VIEW_CUSTOMERS", data_key="crm.customers", id="apg1")
        ent = resolver(portal_user("ADMIN", explicit_denies=(deny,))).resolve(EMPLOYEE)
        self.assertNotIn("staff:customers", held(ent))
        revoked = portal.PolicyDeny("VIEW_CUSTOMERS", state="REVOKED", id="apg2")
        ent = resolver(portal_user("ADMIN", explicit_denies=(revoked,))).resolve(EMPLOYEE)
        self.assertIn("staff:customers", held(ent))

    def test_scopes_outside_the_users_departments_and_keys_deny(self):
        ent = resolver(portal_user("ADMIN", department_ids=frozenset({DEPT_OPS}))).resolve(
            EMPLOYEE)
        self.assertNotIn("staff:documents", held(ent))
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:documents"]
                         .portal_canonical, "OUT_OF_SCOPE")
        no_scopes = resolver(portal_user("SUPER_ADMIN", scopes=())).resolve(EMPLOYEE)
        self.assertEqual(held(no_scopes), {"admin:audit"})  # only the global capability

    def test_missing_scope_context_in_policy_denies_like_portal(self):
        policy = json.loads(json.dumps(POLICY))
        policy["principals"]["staff:operations"].pop("department_ids")
        ent = resolver(portal_user("SUPER_ADMIN"), policy=policy).resolve(EMPLOYEE)
        self.assertNotIn("staff:operations", held(ent))
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:operations"]
                         .portal_canonical, "MISSING_REQUIRED_SCOPE_CONTEXT")

    def test_identity_state_denies_everything(self):
        cases = {
            "portal_user_inactive": portal_user(is_active=False),
            "portal_identity_unverified": portal_user(verified_identity=False),
            "portal_access_level_missing": portal_user(access_level=None),
            "portal_identity_mismatch": portal_user(entra_object_id=AGENT_OID.replace("ff", "fe")),
        }
        for reason, user in cases.items():
            with self.subTest(reason=reason):
                ent = resolver(user).resolve(EMPLOYEE)
                self.assertIsNone(ent.actor)
                self.assertEqual(ent.denied_reason, reason)

    def test_portal_input_missing_or_failing_denies(self):
        for value in (None, RuntimeError("down")):
            with self.subTest(value=value):
                ent = resolver(Calls(value)).resolve(EMPLOYEE)
                self.assertEqual(ent.denied_reason, "portal_access_unavailable")

    def test_wrong_tenant_reads_nothing(self):
        reads = Calls(portal_user())
        ent = resolver(reads).resolve(EmployeePrincipal(OTHER_TID, OID))
        self.assertEqual(ent.denied_reason, "tenant_mismatch")
        self.assertEqual(reads.count, 0)


DEPT_PROC = "cmdeptproc001"
KEYWORDS = ["purchas", "procure", "buyer", "sourcing"]


class JobTitleTests(unittest.TestCase):
    """staff:purchasing = Portal PROC members OR an Entra job title naming a keyword, never
    an excluded person (supervisor decision 2026-10-27; entra_job_title_keywords)."""

    POLICY = {**POLICY, "principals": {**POLICY["principals"], "staff:purchasing": rule(
        "VIEW_OPERATIONS", "operations.procurement", [DEPT_PROC],
        entra_job_title_keywords=KEYWORDS)}}
    SCOPES = ALL_SCOPES + (portal.ScopeRule("DATA", "operations.procurement", "ALLOW"),)

    def entitlement(self, title=None, *, exclusion="clear", departments=(DEPT_OPS,),
                    directory=True):
        from integrations.people_exclusion import PeopleExclusion
        guard = {"clear": PeopleExclusion(["cccccccc-0000-4000-8000-000000000009"]),
                 "excluded": PeopleExclusion([OID]), "none": None}[exclusion]
        user = portal_user(department_ids=frozenset(departments), scopes=self.SCOPES)
        policy = EmployeePermissionPolicy.from_json(self.POLICY)
        if directory:
            read = directory_read(**({} if title is None else {"job_title": title}))
            res = EmployeePermissionResolver(policy, Calls(user), directory=Calls(read),
                                             people_exclusion=guard)
        else:
            res = EmployeePermissionResolver(policy, Calls(user), Calls(member(QA_GROUP)),
                                             people_exclusion=guard)
        got = res.resolve(EMPLOYEE)
        [decision] = [d for d in got.decisions if d.principal == "staff:purchasing"]
        return got, decision

    def test_a_matching_title_holds_staff_purchasing_without_the_department(self):
        for title in ("Purchasing Manager", "Senior BUYER", "Procurement Lead",
                      "Strategic Sourcing / Planning"):
            with self.subTest(title):
                got, decision = self.entitlement(title)
                self.assertIn("staff:purchasing", held(got))
                self.assertEqual(decision.entra_job_title, "member")
                # Counts and enums only: the title itself is never carried.
                self.assertNotIn(title, json.dumps(got.notes))

    def test_a_title_naming_no_keyword_holds_nothing(self):
        for title in ("Outsourcing Coordinator", "Production Supervisor", "Rebuyer?"):
            with self.subTest(title):
                got, decision = self.entitlement(title)
                self.assertNotIn("staff:purchasing", held(got))
                self.assertEqual(decision.entra_job_title, "not_member")

    def test_an_excluded_person_holds_nothing_by_title_or_department(self):
        for departments in ((DEPT_OPS,), (DEPT_OPS, DEPT_PROC)):
            with self.subTest(departments=departments):
                got, decision = self.entitlement("Buyer", exclusion="excluded",
                                                 departments=departments)
                self.assertNotIn("staff:purchasing", held(got))
                self.assertEqual(decision.entra_job_title, "excluded")
                self.assertIn("staff:operations", held(got))   # other principals stand

    def test_no_title_attribute_or_no_directory_grants_nothing_by_title(self):
        got, decision = self.entitlement("")            # Graph returned no jobTitle
        self.assertNotIn("staff:purchasing", held(got))
        self.assertEqual(decision.entra_job_title, "not_member")
        got, decision = self.entitlement(directory=False)   # no directory read at all
        self.assertNotIn("staff:purchasing", held(got))
        self.assertEqual(decision.entra_job_title, "unavailable")

    def test_without_a_usable_exclusion_only_the_portal_department_grants(self):
        got, decision = self.entitlement("Buyer", exclusion="none")
        self.assertNotIn("staff:purchasing", held(got))
        self.assertEqual(decision.entra_job_title, "exclusion_unavailable")
        got, _ = self.entitlement(None, exclusion="none", departments=(DEPT_PROC,))
        self.assertIn("staff:purchasing", held(got))

    def test_the_title_never_stands_in_for_the_access_level_or_the_data_scope(self):
        from integrations.people_exclusion import PeopleExclusion
        policy = EmployeePermissionPolicy.from_json(self.POLICY)
        guard = PeopleExclusion(["cccccccc-0000-4000-8000-000000000009"])
        for user in (portal_user(department_ids=frozenset({DEPT_OPS}), scopes=ALL_SCOPES),
                     portal_user(access_level="L1", department_ids=frozenset({DEPT_OPS}),
                                 scopes=self.SCOPES)):
            res = EmployeePermissionResolver(
                policy, Calls(user), directory=Calls(directory_read(job_title="Buyer")),
                people_exclusion=guard)
            got = res.resolve(EMPLOYEE)
            self.assertNotIn("staff:purchasing", set(got.actor.principals) if got.actor
                             else set())

    def test_malformed_keywords_fail_closed(self):
        for bad in (["Buyer"], ["buy*"], ["by"], "buyer", [3]):
            with self.subTest(bad), self.assertRaises(ValueError):
                EmployeePermissionPolicy.from_json({**self.POLICY, "principals": {
                    "staff:purchasing": rule("VIEW_OPERATIONS", "operations.procurement",
                                             [DEPT_PROC], entra_job_title_keywords=bad)}})
        with self.assertRaises(ValueError):   # a title only stands in for a department
            EmployeePermissionPolicy.from_json({**self.POLICY, "principals": {
                "staff:purchasing": rule("VIEW_OPERATIONS", "operations.procurement", [],
                                         entra_job_title_keywords=KEYWORDS)}})


class EntraGroupTests(unittest.TestCase):
    def test_missing_group_data_never_widens(self):
        with_groups = resolver(portal_user("SUPER_ADMIN")).resolve(EMPLOYEE)
        for provider in (groups_unavailable, Calls(groups_unavailable(EMPLOYEE))):
            with self.subTest(provider=provider):
                ent = resolver(portal_user("SUPER_ADMIN"), groups=provider).resolve(EMPLOYEE)
                self.assertLessEqual(held(ent), held(with_groups))
                self.assertNotIn("staff:quality", held(ent))
                self.assertIn(GROUP_MEMBERSHIP_UNAVAILABLE, ent.notes)
                self.assertEqual(ent.require_actor().clearance,
                                 with_groups.require_actor().clearance)
                self.assertFalse(any(i.startswith("group:") for i in ent.source_identities))
                # Everything not group-gated is still decided by the Portal role.
                self.assertEqual(held(with_groups) - held(ent), {"staff:quality"})
                self.assertEqual({d.principal: d for d in ent.decisions}["staff:quality"]
                                 .entra_group, "unavailable")
        blocked = resolver(groups=groups_unavailable).resolve(EMPLOYEE)
        self.assertIn(GROUP_MEMBERSHIP_UNAVAILABLE + ":graph_permission_missing:"
                      "GroupMember.Read.All", blocked.notes)

    def test_group_only_restricts_never_grants(self):
        # Member of the QA group, but the Portal does not give operations.quality.
        scopes = tuple(s for s in ALL_SCOPES if s.key != "operations.quality")
        ent = resolver(portal_user("SUPER_ADMIN", scopes=scopes)).resolve(EMPLOYEE)
        self.assertNotIn("staff:quality", held(ent))
        # Portal allows it, but not a member.
        ent = resolver(groups=member()).resolve(EMPLOYEE)
        self.assertNotIn("staff:quality", held(ent))
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:quality"]
                         .entra_group, "not_member")

    def test_read_group_membership_is_an_identity_and_cited(self):
        ent = resolver().resolve(EMPLOYEE)
        self.assertIn("group:" + QA_GROUP, ent.source_identities)
        self.assertIn(("entra_groups", "graph:transitiveMemberOf@fixture",
                       "2026-10-01T09:00:00Z"), ent.inputs)

    def test_revocation_applies_on_next_resolve(self):
        groups = Calls(member(QA_GROUP))
        res = resolver(groups=groups)
        self.assertIn("staff:quality", held(res.resolve(EMPLOYEE)))
        groups.value = member()
        self.assertNotIn("staff:quality", held(res.resolve(EMPLOYEE)))


class ProviderOutageTests(unittest.TestCase):
    """A permission provider that fails (an outage) leaves the employee's rights unresolved:
    denied, never narrowed into data, and never mistaken for a refusal of the employee."""

    def test_every_provider_outage_denies_with_rights_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, res in outage_resolvers(tmp).items():
                with self.subTest(name):
                    ent = res.resolve(EMPLOYEE)
                    self.assertIsNone(ent.actor)
                    self.assertTrue(ent.rights_unresolved, ent.denied_reason)
                    self.assertEqual(ent.source_identities, frozenset())
                    with self.assertRaises(EmployeeRightsUnresolved):
                        ent.require_actor()
                    with self.assertRaises(EmployeeRightsUnresolved):
                        EmployeeActorGate(res, "plx")(Actor("plx", frozenset({
                            EMPLOYEE_PREFIX + OID, "staff:quality"})))

    def test_an_answer_about_the_employee_is_a_refusal_not_an_outage(self):
        refusals = {
            "no_portal_row": resolver(Calls(None)),
            "portal_user_inactive": resolver(portal_user(is_active=False)),
            **{reason: resolver(directory=Calls(directory_read("unavailable", reason=reason)))
               for reason in ("user_not_found", "user_mismatch", "account_disabled")},
            "directory_answers_another_user": resolver(directory=Calls(directory_read(
                object_id=AGENT_OID))),
        }
        for name, res in refusals.items():
            with self.subTest(name):
                ent = res.resolve(EMPLOYEE)
                self.assertIsNone(ent.actor)
                self.assertFalse(ent.rights_unresolved)
                with self.assertRaises(EmployeeAccessRefused) as caught:
                    ent.require_actor()
                self.assertNotIsInstance(caught.exception, EmployeeRightsUnresolved)
        # The directory read that succeeds resolves the same employee (the cases above
        # differ from a grant only by the failure).
        self.assertIn("staff:quality", held(resolver(directory=Calls(directory_read()))
                                            .resolve(EMPLOYEE)))

    def test_a_snapshot_outage_is_not_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portal-access.json"
            snapshot_user(path)
            res = resolver(PortalAccessSnapshot(path))
            self.assertIsNotNone(res.resolve(EMPLOYEE).actor)
            path.write_text("{not json", "utf-8")
            self.assertTrue(res.resolve(EMPLOYEE).rights_unresolved)
            snapshot_user(path)
            self.assertIsNotNone(res.resolve(EMPLOYEE).actor)
            snapshot_user(path, isActive=False)   # the export's answer: a refusal
            self.assertFalse(res.resolve(EMPLOYEE).rights_unresolved)


class SharePointItemAclTests(unittest.TestCase):
    def test_item_acl_must_be_read_and_name_the_employee(self):
        ent = resolver().resolve(EMPLOYEE)
        self.assertTrue(ent.item_acl_allows({"status": "read",
                                             "identities": ["ricardo@petralabx.com"]}))
        self.assertTrue(ent.item_acl_allows({"status": "read", "identities": [OID.upper()]}))
        self.assertTrue(ent.item_acl_allows({"status": "read",
                                             "identities": ["group:" + QA_GROUP]}))
        self.assertFalse(ent.item_acl_allows({"status": "read",
                                              "identities": ["vince@petralabx.com"]}))

    def test_identity_case_and_whitespace_follow_entra_without_widening(self):
        # Entra object ids, UPNs and SMTP addresses are case-insensitive; the
        # match stays exact otherwise (no suffix, domain or prefix matches).
        ent = resolver().resolve(EMPLOYEE)
        for granted in ("RICARDO@PetraLabX.com", " ricardo@petralabx.com ", OID.upper()):
            with self.subTest(granted=granted):
                self.assertTrue(ent.item_acl_allows({"status": "read", "identities": [granted]}))
        for granted in ("ricardo@petralabx.com.evil.example", "petralabx.com", "ricardo",
                        "x" + OID, OID[:-1], "group:" + QA_GROUP.upper() + "x"):
            with self.subTest(granted=granted):
                self.assertFalse(ent.item_acl_allows({"status": "read",
                                                      "identities": [granted]}))

    def test_unknown_permissions_deny(self):
        ent = resolver().resolve(EMPLOYEE)
        mine = ["ricardo@petralabx.com"]
        for acl in ({"status": "partial", "identities": mine},
                    {"status": "unreadable", "identities": []},
                    {"status": "not_requested", "identities": []},
                    {"status": "not_read_bound", "identities": []},
                    {"identities": mine}, None, {"status": "read"},
                    {"status": "read", "identities": ["link-scope:organization"]}):
            with self.subTest(acl=acl):
                self.assertFalse(ent.item_acl_allows(acl))

    def test_group_grant_needs_a_read_membership(self):
        ent = resolver(groups=groups_unavailable).resolve(EMPLOYEE)
        self.assertFalse(ent.item_acl_allows({"status": "read",
                                              "identities": ["group:" + QA_GROUP]}))

    def test_denied_employee_reads_no_item(self):
        ent = resolver(portal_user("CUSTOMER")).resolve(EMPLOYEE)
        self.assertFalse(ent.item_acl_allows({"status": "read",
                                              "identities": ["ricardo@petralabx.com"]}))


def columns_drive():
    """The capture fake plus each file's listItem (DocumentCategory from ``categories``),
    read one file at a time or as the library's list items in pages of ``page_size``."""
    from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
    from integrations.tests.test_capture_sharepoint import SITE_ID, FakeDrive, j

    class ColumnsDrive(FakeDrive):
        categories = {"01TAX": "TAX_FORM", "01COA": "COA"}
        list_id = "4f0c1d2e-0000-4000-8000-0000000000aa"
        page_size = 25
        page_etags: dict = {}       # item id -> the eTag a list page reports (edited since)
        refused: set = set()        # item ids whose own listItem read answers 403
        pages_status = None         # the HTTP status every list items page answers

        def raw(self, drive, iid):
            body = super().raw(drive, iid)
            body["sharepointIds"] = {"siteId": "s", "listId": "l", "listItemId": iid}
            return body

        def list_page(self, url, drive):
            if self.pages_status is not None:
                return j(self.pages_status, {"error": {"code": "invalidRequest"}})
            query = [(k, v) for k, v in parse_qsl(urlsplit(url).query) if k != "$skiptoken"]
            start = int(dict(parse_qsl(urlsplit(url).query)).get("$skiptoken", "0"))
            rows = [item for key, item in sorted(self.items.items())
                    if self.drive_of[key] == drive and not item.get("root")]
            value = [{"id": item["id"], "eTag": '"li"',
                      "contentType": {"id": "0x0120" if item["folder"] else "0x0101"},
                      "parentReference": {"id": self.list_id, "siteId": SITE_ID},
                      "fields": ({"DocumentCategory": self.categories[item["id"]]}
                                 if item["id"] in self.categories else {}),
                      "driveItem": {"id": item["id"], "eTag": self.page_etags.get(
                          item["id"], item.get("etag"))}}
                     for item in rows[start:start + self.page_size]]
            body = {"value": value}
            if start + self.page_size < len(rows):
                body["@odata.nextLink"] = url.split("?", 1)[0] + "?" + urlencode(
                    [*query, ("$skiptoken", str(start + self.page_size))])
            return j(200, body)

        def __call__(self, method, url, headers, body, timeout, max_bytes):
            path = unquote(urlsplit(url).path)
            for drive in set(self.drive_of.values()):
                if path in (f"/v1.0/drives/{drive}/list", f"/v1.0/drives/{drive}/list/items"):
                    self.calls.append((method, url))
                    if path.endswith("/items"):
                        return self.list_page(url, drive)
                    return j(200, {"id": self.list_id, "parentReference": {"siteId": SITE_ID}})
            if "/listItem" in url and "login.microsoftonline.com" not in url:
                self.calls.append((method, url))
                iid = url.split("/items/", 1)[1].split("/", 1)[0]
                if iid in self.refused:
                    return j(403, {"error": {"code": "accessDenied"}})
                fields = {"DocumentCategory": self.categories[iid]} \
                    if iid in self.categories else {}
                return j(200, {"id": iid, "fields": fields, "eTag": '"li"',
                               "sharepointIds": {"siteId": "s", "listId": "l",
                                                 "listItemId": iid}})
            return super().__call__(method, url, headers, body, timeout, max_bytes)

    return ColumnsDrive()


def column_reads(drive) -> dict[str, int]:
    """The DocumentCategory requests a check sent: list (and list items pages), and
    per-file listItem reads (each with its two metadata reads and its permissions)."""
    from urllib.parse import unquote, urlsplit
    paths = [unquote(urlsplit(url).path) for _method, url in drive.calls]
    return {"list": sum(p.endswith("/list") for p in paths),
            "pages": sum(p.endswith("/list/items") for p in paths),
            "per_file": sum(p.endswith("/listItem") for p in paths)}


def run_clearance_check(drive, audiences=None):
    """``operating.file_clearance_check`` over ``drive``'s ``/Customers`` folder."""
    from integrations.tests.test_capture_sharepoint import DRIVE, ENV
    from operating import file_clearance_check
    with tempfile.TemporaryDirectory() as tmp:
        scope_file, audience_file = Path(tmp) / "scope.json", Path(tmp) / "audience.json"
        scope_file.write_text(json.dumps({
            "schema": "plx-sharepoint-capture-scope/v1", "targets": [
                {"id": "customers", "drive_id": DRIVE, "folders": ["/Customers"],
                 "max_depth": 2, "max_folders": 10}]}), encoding="utf-8")
        audience_file.write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": audiences or {"sharepoint:*": ["svc:sharepoint-capture"]}}),
            encoding="utf-8")
        return file_clearance_check.run(argparse.Namespace(
            state_dir=str(Path(tmp) / "state"), scope=str(scope_file),
            live_audience=str(audience_file), principal="svc:sharepoint-capture"),
            environ=ENV, sender=drive, sleep=lambda s: None)


class FileClearanceTests(unittest.TestCase):
    """The Portal's file clearance (portal/src/lib/auth/file-clearance.ts@790256c) from
    the Portal's department membership, narrowed by the Entra department; the host
    paths are in test_case_host_sharepoint."""

    BC_DRIVE = "b!business-central"

    def entitlement(self, department, role="STAFF", portal_access=None, *, codes=None):
        from operating.employee_permissions import PORTAL_DEPARTMENT_CODES, EntraDirectoryRead
        if codes is None:   # the Portal confirms the department Entra names
            codes = {PORTAL_DEPARTMENT_CODES.get(department.strip().casefold(), "NONE")}
        policy = {**POLICY, "library_security_classes": {self.BC_DRIVE: "BUSINESS_CENTRAL"}}
        return EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json(policy),
            portal_access or Calls(portal_user(role, clearance_departments=codes)),
            directory=lambda principal: EntraDirectoryRead(
                "read", OID, department=department, source="graph:directory@fixture",
                read_at="2026-10-01T09:00:00Z")).resolve(EMPLOYEE)

    def test_class_readers_follow_the_department_never_the_role(self):
        def record(drive="b!quality", category=None, etag='"{F},1"', snapshot_etag=None):
            out = {"drive_id": drive, "path": "/Customers/Acme/credit.pdf", "etag": etag}
            if category is not None:
                out["list_item_fields"] = {"source": "sharepoint-listitem-fields/v1",
                                           "drive_etag": snapshot_etag or etag,
                                           "fields": {"DocumentCategory": category}}
            return out
        finance, bc = record(category="Tax Form"), record(drive=self.BC_DRIVE)
        stale = record(category="COA", snapshot_etag='"{F},0"')
        cases = {("Finance", "STAFF"): (True, True), ("executive", "STAFF"): (True, True),
                 ("IT", "STAFF"): (False, True), ("Operations", "SUPER_ADMIN"): (False, False),
                 ("", "ADMIN"): (False, False), ("Finance Team", "STAFF"): (False, False),
                 # SALES (Entra-group mapped since Portal 2cb3760 TASK-2622) reads neither class.
                 ("Sales & Business Development", "STAFF"): (False, False)}
        for (department, role), expected in cases.items():
            with self.subTest(department=department, role=role):
                ent = self.entitlement(department, role)
                self.assertEqual((ent.file_class_allows(finance), ent.file_class_allows(bc)),
                                 expected)
                self.assertTrue(ent.file_class_allows(record(category="COA")))
                self.assertFalse(ent.file_class_allows(stale))   # unresolved: nobody
        # access.ts@f3e3a53:78-86 (TASK-3075) lets a staff uploader read their own
        # Finance file; the Workbench does not adopt it (no capture field states the
        # Portal uploader), so an uncleared reader named as the file's author stays refused.
        ops = self.entitlement("Operations")
        self.assertTrue(ops.portal_user_id)
        authored = {**finance, "created_by": {"portal_user_id": ops.portal_user_id},
                    "last_modified_by": {"portal_user_id": ops.portal_user_id}}
        self.assertFalse(ops.file_class_allows(authored))
        # No directory read: the department is unknown, so nothing restricted clears.
        self.assertFalse(resolver(portal_user(clearance_departments={"FIN"})).resolve(
            EMPLOYEE).file_class_allows(finance))
        # Entra only narrows: Finance in Entra, IT at the Portal clears BC alone.
        ent = self.entitlement("Finance", codes={"IT"})
        self.assertEqual((ent.file_class_allows(finance), ent.file_class_allows(bc)),
                         (False, True))
        # A department change changes the cache key.
        self.assertNotEqual(self.entitlement("Finance").fingerprint,
                            self.entitlement("Operations").fingerprint)

    def test_a_batch_qc_document_is_read_by_admins_and_quality_staff_only(self):
        # access.ts@1d3eaf2:88-93 with qc-document-categories.ts@1d3eaf2:8-10,27-32: a
        # BATCH COA or WAIVER (QUALITY library, Batches/{year}/{ref}) is read by an admin
        # or by a QUALITY/QA/QC department member; everyone else is refused. The fixtures
        # carry the DocumentCategory column the Portal writes (build_attachment_metadata).
        def record(category, path="/Batches/2026/B26-0042/result.pdf", snapshot=True):
            out = {"drive_id": "b!quality", "path": path, "etag": '"{Q},1"'}
            if snapshot:
                out["list_item_fields"] = {"source": "sharepoint-listitem-fields/v1",
                                           "drive_etag": '"{Q},1"',
                                           "fields": {"DocumentCategory": category}}
            return out
        tokens = {"ref": "B26-0042", "year": "2026"}
        column = {category: build_attachment_metadata("BATCH", category, tokens)
                  ["DocumentCategory"] for category in ("WAIVER", "COA", "BATCH_RECORD")}
        # A Portal waiver's column is OTHER (document-types.ts@1d3eaf2:28,93), shared with
        # open Batches files (QC_RESULT, LAB_RESULT), so OTHER fails closed; so do a column
        # no Portal route files into Batches and an unrecognised value.
        qc = [record(column["WAIVER"], "/Batches/2026/B26-0042/B26-0042_WVR_v1.pdf"),
              record(column["COA"]), record(" Coa "), record("other"), record("SDS"),
              record("WAIVER"), record("COA", "/Quality/batches/2026/B26-0042/coa.pdf"),
              record(None, snapshot=False)]   # no category read: it may be a QC document
        plain = [record(column["BATCH_RECORD"]), record(" batch_record "),
                 record("COA", "/Materials/RM-1/coa.pdf"), record("OTHER", "/Unfiled/x.pdf"),
                 record("COA", "/Batches.pdf")]
        readers = {("Quality", "STAFF"): True, ("Quality Assurance", "STAFF"): True,
                   ("quality control", "MANAGER"): True, ("Operations", "ADMIN"): True,
                   ("Operations", "SUPER_ADMIN"): True, ("Operations", "MANAGER"): False,
                   ("Operations", "STAFF"): False, ("Finance", "STAFF"): False,
                   ("", "STAFF"): False}
        for (department, role), expected in readers.items():
            with self.subTest(department=department, role=role):
                ent = self.entitlement(department, role)
                self.assertEqual([ent.file_class_allows(r) for r in qc], [expected] * len(qc))
                self.assertTrue(all(ent.file_class_allows(r) for r in plain))

        def quality(user=None, codes=None, directory=True):
            if not directory:
                return resolver(user).resolve(EMPLOYEE)
            return self.entitlement("Quality", codes=codes,
                                    portal_access=None if user is None else Calls(user))
        unknown = {
            # Entra says Quality; the Portal membership is unread, removed or elsewhere.
            "portal_unread": quality(portal_user(clearance_departments=None)),
            "portal_removed": quality(codes=set()),
            "portal_elsewhere": quality(codes={"OPS"}),
            # No directory read: the Entra department is unknown.
            "no_directory": quality(portal_user(clearance_departments={"QA"}), directory=False),
            # An ADMIN role whose access level is a staff one is not an admin in both modes.
            "admin_role_staff_level": quality(portal_user("ADMIN", access_level="L2",
                                                          clearance_departments=set())),
        }
        for name, ent in unknown.items():
            with self.subTest(refused=name):
                self.assertIsNotNone(ent.actor)
                self.assertEqual([ent.file_class_allows(r) for r in qc], [False] * len(qc))
                self.assertTrue(all(ent.file_class_allows(r) for r in plain))
        # A Finance-library file is still refused to an uncleared QC member.
        self.assertFalse(self.entitlement("Quality").file_class_allows(
            {**record("COA"), "library_security_class": "FINANCE"}))
        # A QC membership change changes the cache key (same role, same Entra department).
        self.assertNotEqual(quality(codes={"QUALITY"}).fingerprint,
                            quality(codes={"OPS"}).fingerprint)

    def test_portal_membership_decides_and_an_unconfirmed_one_clears_nothing(self):
        from operating.employee_permissions import PortalAccessSnapshot
        staff = {"drive_id": "b!quality", "path": "/note.pdf", "etag": '"{N},1"'}
        finance = {**staff, "library_security_class": "FINANCE"}   # as the capture serves it
        bc = {**staff, "drive_id": self.BC_DRIVE}                  # classified by the policy
        user = {"id": "cmuser0000001", "email": "ricardo@petralabx.com", "role": "STAFF",
                "accessLevel": portal.LEGACY_ROLE_BOOTSTRAP["STAFF"], "isActive": True,
                "entraTenantId": TID, "entraObjectId": OID}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portal-access.json"

            def snapshot(**fields):
                path.write_text(json.dumps({
                    "schema": "plx-portal-access-snapshot/v1", "source": "portal:fixture",
                    "read_at": "2026-10-06T09:00:00Z", "users": [{**user, **fields}]}))

            def member(code, provenance="PORTAL", active=True):
                return {"code": code, "provenance": provenance, "departmentActive": active}

            snapshot(departmentMemberships=[member("FIN")])
            provider = PortalAccessSnapshot(path)
            # Entra says Finance throughout; only the Portal's rows change.
            cases = [([member("FIN")], (True, True)),
                     ([member("EXEC", "PROJECTED")], (True, True)),
                     ([], (False, False)),                              # removed at the Portal
                     ([member("FIN", "DRIFT")], (False, False)),        # Entra-only row
                     ([member("FIN", active=False)], (False, False)),   # inactive department
                     (None, (False, False))]                            # not exported
            for rows, expected in cases:
                with self.subTest(rows=rows):
                    snapshot(**({} if rows is None else {"departmentMemberships": rows}))
                    ent = self.entitlement("Finance", portal_access=provider)
                    self.assertIsNotNone(ent.actor)
                    self.assertEqual((ent.file_class_allows(finance),
                                      ent.file_class_allows(bc)), expected)
                    self.assertTrue(ent.file_class_allows(staff))   # STANDARD stays open
            # An unknown provenance is an invalid read: the employee is refused outright.
            snapshot(departmentMemberships=[member("FIN", "ENTRA")])
            self.assertIsNone(self.entitlement("Finance", portal_access=provider).actor)

    def test_capture_classifies_named_targets_and_keeps_document_category(self):
        from integrations import graph_read
        from integrations.capture_sharepoint import (CaptureScope, SharePointCapture,
                                                     SharePointCaptureStore)
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_capture_sharepoint import (CAPTURE, DRIVE, ENV, RICARDO,
                                                                capture_audience)
        from operating import doc_metadata_taxonomy as taxonomy
        drive = columns_drive()
        drive.library_names[DRIVE] = "Business Central"
        drive.folder("01CUST", "Customers")
        drive.file("01TAX", "tax.pdf", "01CUST", acl=(RICARDO,))
        drive.file("01NONE", "note.pdf", "01CUST", acl=(RICARDO,))
        target = {"id": "named", "drive_id": DRIVE, "folders": ["/Customers"]}
        with tempfile.TemporaryDirectory() as tmp:
            store = SharePointCaptureStore(Path(tmp) / "state")
            api, transport = graph_read.build_graph(ENV, sender=drive, retry=BoundedBackoff(
                sleep=lambda s: None, jitter=lambda: 0.0))
            capture = SharePointCapture(store, lambda d: SharePointReader(
                api, capture_audience(), d, transport=transport, read_permissions=True), CAPTURE)
            try:
                # Named by drive id, its library called Business Central: refused unread.
                run = capture.run(CaptureScope([target]))
                self.assertEqual(run["targets"][0]["status"], "restricted_library_unclassified")
                self.assertEqual((run["complete_bound"], run["bound_total"]), (0, 1))
                self.assertIsNone(store.current(DRIVE, "01TAX"))
                self.assertFalse([u for _m, u in drive.calls if "/items/" in u])
                # Classified as STANDARD by drive id: walked, DocumentCategory kept per
                # version, so the Finance category still classifies the tax form.
                run = capture.run(CaptureScope(
                    [target], library_security_classes=[{"drive_id": DRIVE, "class": "STANDARD"}],
                    list_item_columns=["DocumentCategory"]))
                self.assertTrue(run["complete"], run)
                self.assertEqual(run["targets"][0]["roots"][0]["counts"]["list_items_read"], 2)
                audience = capture_audience()
                rows = {iid: store.resolve(store.current(DRIVE, iid)["l1_handle"], CAPTURE,
                                           audience, text=False)["row"]
                        for iid in ("01TAX", "01NONE")}
            finally:
                store.close()
        self.assertEqual(rows["01TAX"]["list_item_fields"]["fields"],
                         {"DocumentCategory": "TAX_FORM"})
        self.assertEqual({iid: taxonomy.file_security_class(row)[0]
                          for iid, row in rows.items()},
                         {"01TAX": "FINANCE", "01NONE": "STANDARD"})
        uncleared = self.entitlement("Operations")
        self.assertEqual([uncleared.file_class_allows(rows[i]) for i in ("01TAX", "01NONE")],
                         [False, True])
        self.assertTrue(all(method == "GET" for method, _ in drive.calls))

    def test_live_check_reports_classes_and_hides_restricted_files_from_the_uncleared(self):
        from integrations.tests.test_capture_sharepoint import RICARDO

        drive = columns_drive()
        drive.folder("01CUST", "Customers")
        drive.file("01TAX", "tax.pdf", "01CUST", acl=(RICARDO,))
        drive.file("01COA", "coa.pdf", "01CUST", acl=(RICARDO,))
        drive.file("01NONE", "note.pdf", "01CUST", acl=(RICARDO,))
        report = run_clearance_check(drive)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual((report["files_classified"], report["listitems_read"]), (3, 3))
        self.assertEqual(report["class_counts"]["FINANCE"], 1)
        self.assertEqual(report["class_counts"]["STANDARD"], 2)
        self.assertEqual((report["restricted_total"], report["restricted_visible_to_uncleared"],
                          report["restricted_visible_to_unconfirmed"],
                          report["finance_visible_to_cleared"]), (1, 0, 0, 1))
        from devloop import live_verify as lv
        spec = json.loads((ROOT / "devloop/live-checks/sharepoint.restricted_class.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(lv.check_expectations(spec, 0, report), [])
        leaked = {**report, "ok": False, "restricted_visible_to_unconfirmed": 1}
        self.assertTrue(lv.check_expectations(spec, 1, leaked))
        self.assertTrue(all(method == "GET" for method, _ in drive.calls))
        for secret in ("tax.pdf", "TAX_FORM", "01TAX", "Customers"):
            self.assertNotIn(secret, json.dumps(report))
        # DocumentCategory came from the library's list items, not one read per file.
        self.assertEqual(column_reads(drive), {"list": 1, "pages": 1, "per_file": 0})

    def test_live_check_reads_document_category_in_library_pages(self):
        from integrations.tests.test_capture_sharepoint import DRIVE, RICARDO
        for n in (1, 10, 60):
            with self.subTest(files=n):
                drive = columns_drive()
                drive.folder("01CUST", "Customers")
                for i in range(n):
                    drive.file(f"01F{i:03d}", f"f{i}.pdf", "01CUST", acl=(RICARDO,))
                drive.categories = {"01F000": "TAX_FORM"}
                report = run_clearance_check(drive)
                self.assertTrue(report["ok"], report)
                self.assertEqual((report["files_classified"], report["listitems_read"],
                                  report["class_counts"]["FINANCE"]), (n, n, 1))
                # The folder is a list item too: 1 + n rows in pages of 25.
                self.assertEqual(column_reads(drive), {"list": 1, "pages": -(-(n + 1) // 25),
                                                       "per_file": 0})
        # A grant that does not cover the whole library never reads its list: each
        # captured file's own listItem is read through its audience and ACL check.
        drive = columns_drive()
        drive.folder("01CUST", "Customers")
        drive.file("01TAX", "tax.pdf", "01CUST", acl=(RICARDO,))
        drive.file("01NONE", "note.pdf", "01CUST", acl=(RICARDO,))
        report = run_clearance_check(drive, {"sharepoint:*": ["svc:sharepoint-capture"],
                                             f"sharepoint:{DRIVE}:/Payroll/*": ["staff:hr"]})
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["listitems_read"], report["class_counts"]["FINANCE"]), (2, 1))
        self.assertEqual(column_reads(drive), {"list": 0, "pages": 0, "per_file": 2})

    def test_live_check_counts_each_unread_file_by_cause_and_never_clears_it(self):
        from devloop import live_verify as lv
        from integrations.tests.test_capture_sharepoint import RICARDO
        spec = json.loads((ROOT / "devloop/live-checks/sharepoint.restricted_class.json")
                          .read_text(encoding="utf-8"))

        def drive_with(**setup):
            drive = columns_drive()
            drive.folder("01CUST", "Customers")
            for iid in ("01TAX", "01COA", "01NONE"):
                drive.file(iid, f"{iid}.pdf", "01CUST", acl=(RICARDO,))
            for name, value in setup.items():
                setattr(drive, name, value)
            return drive

        # The page read refused (400): every file falls back to its own read; the refused
        # one (403) is counted by cause, with why the page read did not cover it.
        drive = drive_with(pages_status=400, refused={"01TAX"})
        report = run_clearance_check(drive)
        self.assertFalse(report["ok"])
        self.assertEqual(report["listitem_failures"], {"SourceUnavailable.http_403": 1,
                                                       "pages.SourceUnavailable.http_400": 1})
        self.assertEqual((report["files_classified"], report["listitems_read"]), (2, 2))
        self.assertEqual(sum(report["class_counts"].values()), 2)   # never classified unread
        self.assertEqual(report["class_counts"]["FINANCE"], 0)
        self.assertEqual(column_reads(drive)["per_file"], 3)
        self.assertIn("listitem_failures not empty", lv.check_expectations(spec, 1, report))
        # A file edited since the capture never takes its page row: its own read decides.
        drive = drive_with(page_etags={"01COA": '"{01COA},9"'}, refused={"01COA"})
        report = run_clearance_check(drive)
        self.assertFalse(report["ok"])
        self.assertEqual(report["listitem_failures"], {"SourceUnavailable.http_403": 1,
                                                       "pages.unbound": 1})
        self.assertEqual(column_reads(drive), {"list": 1, "pages": 1, "per_file": 1})
        self.assertEqual((report["files_classified"], report["class_counts"]["FINANCE"]), (2, 1))


class AgentRefusalTests(unittest.TestCase):
    def test_agent_and_service_principals_are_refused_before_any_read(self):
        reads = Calls(portal_user("SUPER_ADMIN"))
        res = resolver(reads)
        for principal in (EmployeePrincipal(TID, OID, kind="agent"),
                          EmployeePrincipal(TID, OID, kind="service_principal"),
                          EmployeePrincipal(TID, AGENT_OID)):
            with self.subTest(principal=principal):
                ent = res.resolve(principal)
                self.assertIsNone(ent.actor)
                self.assertEqual(ent.denied_reason, "agent_principal_refused")
        self.assertEqual(reads.count, 0)

    def test_policy_rejects_agent_and_employee_principals(self):
        for name in ("agent:claude", "sp_mcp:workbench", "employee:" + OID):
            with self.subTest(name=name):
                bad = json.loads(json.dumps(POLICY))
                bad["principals"][name] = {"capability": "VIEW_AUDIT_LOG"}
                with self.assertRaises(ValueError):
                    EmployeePermissionPolicy.from_json(bad)

    def test_gate_refuses_an_actor_mixing_agent_and_employee(self):
        gate = EmployeeActorGate(resolver(portal_user("SUPER_ADMIN")), "plx")
        for extra in ("agent:claude", "sp_mcp_cursor"):
            with self.subTest(extra=extra), self.assertRaises(EmployeeAccessRefused) as caught:
                gate(Actor("plx", frozenset({"employee:" + OID, extra})))
            self.assertEqual(caught.exception.reason, "agent_principal_refused")


class GateTests(unittest.TestCase):
    def test_non_employee_actor_passes_unchanged(self):
        actor = Actor("plx", frozenset({"staff:quality"}), "restricted")
        self.assertIs(EmployeeActorGate(None, "plx")(actor), actor)
        self.assertIs(EmployeeActorGate(resolver(), "plx")(actor), actor)

    def test_employee_without_resolver_is_refused(self):
        with self.assertRaises(EmployeeAccessRefused) as caught:
            EmployeeActorGate(None, "plx")(Actor("plx", frozenset({"employee:" + OID})))
        self.assertEqual(caught.exception.reason, "employee_entitlements_not_configured")

    def test_bare_employee_becomes_resolved_actor_and_extras_only_narrow(self):
        gate = EmployeeActorGate(resolver(), "plx")
        resolved = gate(Actor("plx", frozenset({"employee:" + OID})))
        self.assertEqual(set(resolved.principals) - {"employee:" + OID},
                         PortalRoleTests.EXPECTED["STAFF"][0])
        narrowed = gate(Actor("plx", frozenset({"employee:" + OID, "staff:quality",
                                                "staff:finance"}), "restricted"))
        self.assertEqual(set(narrowed.principals), {"employee:" + OID, "staff:quality"})
        self.assertEqual(narrowed.clearance, "internal")
        self.assertEqual(gate(narrowed), narrowed)  # idempotent

    def test_employee_prefix_and_object_id_are_case_insensitive(self):
        # A case variant of the employee principal is still an employee: refused
        # without a resolver, never passed through as a plain host actor.
        for variant in ("Employee:" + OID.upper(), "EMPLOYEE:" + OID, "employee:" + OID.upper()):
            with self.subTest(variant=variant):
                with self.assertRaises(EmployeeAccessRefused) as caught:
                    EmployeeActorGate(None, "plx")(Actor("plx", frozenset({variant})))
                self.assertEqual(caught.exception.reason, "employee_entitlements_not_configured")
                gate = EmployeeActorGate(resolver(), "plx")
                canonical = gate(Actor("plx", frozenset({EMPLOYEE_PREFIX + OID})))
                self.assertEqual(gate(Actor("plx", frozenset({variant}))), canonical)
                # Extras still only narrow; the variant and the canonical form are one.
                narrowed = gate(Actor("plx", frozenset({variant, "employee:" + OID,
                                                        "staff:quality", "Staff:Finance"})))
                self.assertEqual(set(narrowed.principals), {"employee:" + OID, "staff:quality"})
        gate = EmployeeActorGate(resolver(portal_user("SUPER_ADMIN")), "plx")
        for extra in ("Agent:claude", "SP_mcp_cursor"):
            with self.subTest(extra=extra), self.assertRaises(EmployeeAccessRefused) as caught:
                gate(Actor("plx", frozenset({"Employee:" + OID, extra})))
            self.assertEqual(caught.exception.reason, "agent_principal_refused")
        with self.assertRaises(EmployeeAccessRefused) as caught:
            gate(Actor("plx", frozenset({"Employee:" + OID, "employee:" + AGENT_OID})))
        self.assertEqual(caught.exception.reason, "several_employee_principals")

    def test_gate_refusals(self):
        gate = EmployeeActorGate(resolver(portal_user("CUSTOMER")), "plx")
        with self.assertRaises(EmployeeAccessRefused):
            gate(Actor("plx", frozenset({"employee:" + OID})))
        gate = EmployeeActorGate(resolver(), "plx")
        for actor in (Actor("plx", frozenset({"employee:" + OID, "employee:" + AGENT_OID})),
                      Actor("other", frozenset({"employee:" + OID})),
                      Actor("plx", frozenset({"employee:not-a-guid"}))):
            with self.subTest(actor=actor), self.assertRaises(EmployeeAccessRefused):
                gate(actor)
        with self.assertRaises(ValueError):
            EmployeeActorGate(resolver(), "other-tenant")


class AudienceIntegrationTests(unittest.TestCase):
    def test_mirror_relations_follow_the_resolved_actor(self):
        mirror = AudienceMap({"public.app_sales_order": ["staff:operations"],
                              "public.app_purchase_order": ["staff:purchasing"],
                              "sage.*": ["staff:finance"]}, tenant="plx",
                             classifications={"sage.*": "confidential"})
        relations = ["public.app_sales_order", "public.app_purchase_order", "sage.ar_customer"]
        staff = resolver(portal_user("STAFF")).resolve(EMPLOYEE)
        admin = resolver(portal_user("ADMIN")).resolve(EMPLOYEE)
        self.assertEqual(staff.readable_relations(mirror, relations), ["public.app_sales_order"])
        self.assertEqual(admin.readable_relations(mirror, relations),
                         ["public.app_sales_order", "sage.ar_customer"])
        denied = resolver(portal_user("CUSTOMER")).resolve(EMPLOYEE)
        self.assertEqual(denied.readable_relations(mirror, relations), [])

    def test_live_audience_document_uses_only_the_employees_identities(self):
        base = {"schema": "plx-live-source-audience/v1", "tenant": "plx",
                "default_classification": "internal",
                "audiences": {"sharepoint:d:/AOs/*": ["staff:quality"]},
                "identities": {"staff:quality": ["someone-else@petralabx.com"]}}
        ent = resolver().resolve(EMPLOYEE)
        doc = live_source_audience_document(base, ent)
        self.assertEqual(doc["identities"], {"employee:" + OID: sorted(ent.source_identities)})
        self.assertTrue({"mail:*", "sharepoint:*"} <= set(doc["source_acl_required"]))
        live = LiveSourceAudience.from_json(doc)
        actor = ent.require_actor()
        keys = ["sharepoint:d:/AOs/2026 - AOs/AO-26005B.pdf"]
        self.assertTrue(live.visible_all(actor, keys, {"status": "read",
                                                       "identities": ["group:" + QA_GROUP]}))
        self.assertFalse(live.visible_all(actor, keys, {
            "status": "read", "identities": ["someone-else@petralabx.com"]}))
        with self.assertRaises(EmployeeAccessRefused):
            live_source_audience_document(dict(base, tenant="other"), ent)

    def test_example_policy_loads_and_covers_example_audience_principals(self):
        policy = EmployeePermissionPolicy.from_file(
            ROOT / "examples" / "employee_permission_policy.example.json")
        self.assertEqual(policy.tenant, "plx")
        mirror = json.loads((ROOT / "examples" / "source_mirror_audience.example.json")
                            .read_text())
        used = set().union(*map(set, mirror["audiences"].values()))
        self.assertLessEqual(used, set(policy.rules), used - set(policy.rules))

    def test_policy_validation(self):
        for mutate in (lambda d: d.update(schema="x"),
                       lambda d: d.update(extra=1),
                       lambda d: d["clearance_by_access_level"].pop("L5"),
                       lambda d: d["principals"]["staff:operations"].update(capability="NOPE"),
                       lambda d: d["principals"]["staff:operations"].update(
                           data_key="operations.everything"),
                       lambda d: d["principals"]["staff:operations"].update(
                           capability="VIEW_OWN_DOCUMENTS"),
                       lambda d: d["principals"]["staff:operations"].update(
                           mrp_modules=["payroll"]),
                       lambda d: d["principals"]["staff:quality"].update(
                           entra_groups=["not-a-guid"]),
                       # A role grant names employee roles only and no Portal resource.
                       *(lambda d, r=r: d["principals"].update({"review:identity": r})
                         for r in ({"portal_roles": ["OWNER"]}, {"portal_roles": ["CUSTOMER"]},
                                   {"portal_roles": []}, {"portal_roles": "ADMIN"},
                                   {"portal_roles": ["ADMIN"], "data_key": "crm.customers"}))):
            doc = json.loads(json.dumps(POLICY))
            mutate(doc)
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                EmployeePermissionPolicy.from_json(doc)


class PortalMirrorDriftTests(unittest.TestCase):
    """The mirrored matrices equal the Portal source at the pinned commit."""

    def portal_file(self, path: str) -> str:
        repo = find_portal_repo()
        if repo is None:
            self.skipTest(f"no Portal clone with {PORTAL_COMMIT[:12]}: set ${PORTAL_REPO_ENV}")
        try:
            return subprocess.run(["git", "-C", str(repo), "show",
                                   f"{PORTAL_COMMIT}:{path}"], check=True,
                                  capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("Portal checkout at the pinned commit is not available")

    def test_portal_clone_is_found_wherever_the_repo_is_checked_out(self):
        real = find_portal_repo()
        if real is None:
            self.skipTest(f"no Portal clone with {PORTAL_COMMIT[:12]}: set ${PORTAL_REPO_ENV}")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            # A worktree next to the Portal clone, at any depth.
            (base / "plx-customer-portal").symlink_to(real, target_is_directory=True)
            nested = base / "wt-task" / "OntologyProject"
            nested.mkdir(parents=True)
            with mock.patch.object(Path, "home", return_value=base / "nowhere"):
                self.assertEqual(find_portal_repo(nested, {}), base / "plx-customer-portal")
                # An explicit location wins, and a clone without the commit is skipped.
                (base / "moved").symlink_to(real, target_is_directory=True)
                self.assertEqual(find_portal_repo(nested, {PORTAL_REPO_ENV: str(base / "moved")}),
                                 base / "moved")
                (base / "empty").mkdir()
                self.assertEqual(find_portal_repo(nested, {PORTAL_REPO_ENV: str(base / "empty")}),
                                 base / "plx-customer-portal")
                with tempfile.TemporaryDirectory() as alone:
                    self.assertIsNone(find_portal_repo(Path(alone) / "x", {}))

    def test_role_permission_matrix_matches_roles_ts(self):
        source = self.portal_file("portal/src/lib/auth/roles.ts")
        block = source.split("const ROLE_PERMISSIONS", 1)[1].split("\n};", 1)[0]
        found = {}
        for role, body in re.findall(r"\n  (\w+): new Set\(\[(.*?)\]\)", block, re.S):
            body = re.sub(r"//[^\n]*", "", body)
            found[role] = (set(portal.PERMISSIONS) if "Object.values(PERMISSIONS)" in body
                           else set(re.findall(r"PERMISSIONS\.(\w+)", body)))
        self.assertEqual(found, {r: set(p) for r, p in portal.ROLE_PERMISSIONS.items()})
        declared = re.findall(r"^  (\w+): \"\1\",$",
                              source.split("export const PERMISSIONS", 1)[1].split("} as const",
                                                                                    1)[0], re.M)
        self.assertEqual(tuple(declared), portal.PERMISSIONS)
        levels = dict(re.findall(r"^  (\w+): (\d),$", source.split("const ROLE_LEVEL", 1)[1]
                                 .split("};", 1)[0], re.M))
        self.assertEqual({k: int(v) for k, v in levels.items()}, portal.ROLE_LEVEL)

    def test_capability_registry_and_bootstrap_match_rbac_policy_ts(self):
        source = self.portal_file("portal/src/lib/auth/rbac-policy.ts")
        presets = {"OWNED_SCOPES": portal.ScopeRequirements(False, True, True, True),
                   "SCOPED": portal.ScopeRequirements(True, True, True, False),
                   "GLOBAL": portal.ScopeRequirements(False, False, False, False)}
        found = {name: portal.CapabilityDefinition(level, presets[preset], not no_elevate)
                 for name, level, preset, no_elevate in re.findall(
                     r"^  (\w+): capability\(\"(L\d)\", (\w+)(, false)?\),$", source, re.M)}
        self.assertEqual(found, portal.CAPABILITY_REGISTRY)
        bootstrap = dict(re.findall(r"^  (\w+): \"(L\d)\",$", source.split(
            "LEGACY_ROLE_BOOTSTRAP", 1)[1].split("} as const", 1)[0], re.M))
        self.assertEqual(bootstrap, portal.LEGACY_ROLE_BOOTSTRAP)
        self.assertIn(f'POLICY_VERSION = "{portal.POLICY_VERSION}"', source)
        for preset, flags in (("OWNED_SCOPES", "false, true, true, true"),
                              ("SCOPED", "true, true, true, false"),
                              ("GLOBAL", "false, false, false, false")):
            body = source.split(f"const {preset} = {{", 1)[1].split("}", 1)[0]
            values = ", ".join(re.findall(r": (true|false)", body))
            self.assertEqual(values, flags, preset)

    def test_data_keys_match_registry(self):
        source = self.portal_file("portal/src/lib/auth/data-key-registry.ts")
        block = source.split("export const RBAC_DATA_KEYS", 1)[1].split("} as const", 1)[0]
        keys = set(re.findall(r": \"([a-z.-]+)\",", block)) | {"identity.user-administration"}
        self.assertEqual(keys, portal.RBAC_DATA_KEYS)


class EffectiveAccessPortTests(unittest.TestCase):
    """Rule order of effective-access.ts:resolveEffectiveAccess."""

    def state(self, **overrides):
        values = dict(user_id="u1", active=True, verified_identity=True, access_level="L2",
                      department_ids=frozenset({"d1"}),
                      scopes=(portal.ScopeRule("COMPANY", "c1", "ALLOW"),
                              portal.ScopeRule("DATA", "k1", "ALLOW")))
        values.update(overrides)
        return portal.AccessState(**values)

    RES = portal.Resource("x", ("d1",), "c1", "k1")

    def test_rule_order(self):
        decide = portal.resolve_effective_access
        self.assertEqual(decide("u2", "VIEW_OPERATIONS", self.RES, self.state()).rule,
                         "INVALID_IDENTITY")
        self.assertEqual(decide("u1", "VIEW_OPERATIONS", self.RES,
                                self.state(active=False)).rule, "INACTIVE_IDENTITY")
        self.assertEqual(decide("u1", "VIEW_OPERATIONS", self.RES,
                                self.state(access_level=None)).rule, "MISSING_ACCESS_LEVEL")
        self.assertEqual(decide("u1", "KH_VIEW", self.RES, self.state()).rule,
                         "CAPABILITY_NOT_GRANTED")
        self.assertEqual(decide("u1", "VIEW_OPERATIONS", portal.Resource("x", (), "c1", "k1"),
                                self.state()).rule, "MISSING_REQUIRED_SCOPE_CONTEXT")
        self.assertEqual(decide("u1", "VIEW_OPERATIONS", portal.Resource("x", ("d2",), "c1",
                                                                          "k1"),
                                self.state()).rule, "OUT_OF_SCOPE")
        self.assertTrue(decide("u1", "VIEW_OPERATIONS", self.RES, self.state()).allowed)
        self.assertEqual(decide("u1", "MANAGE_OPERATIONS", self.RES, self.state()).rule,
                         "CAPABILITY_NOT_GRANTED")
        self.assertTrue(decide("u1", "VIEW_AUDIT_LOG", portal.Resource("x"),
                               self.state(access_level="L5")).allowed)


if __name__ == "__main__":
    unittest.main()
