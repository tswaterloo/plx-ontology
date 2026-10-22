"""Per-employee entitlements from Entra directory facts, end to end.

Evidence label: ``injected-transport``. A fake Graph sender sits behind the
real ``GuardedTransport``/``SourceApi`` (integrations/live_source_guard.py), so
every request passes the closed grammar and the GET-only check; the directory
reader (integrations/graph_directory.py) feeds the expiring provider
(operating/employee_permissions_entra.py) and the one resolver
(operating/employee_permissions.py). No network, no credential.
"""

from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from urllib.parse import parse_qs, unquote, urlsplit

from verify import live_verify
from integrations import graph_directory
from integrations.graph_directory import GraphDirectoryReader
from integrations.graph_read import build_graph
from integrations.live_source_guard import check_request
from integrations import live_source_grammar as grammar
from operating.employee_permissions import (EmployeePermissionPolicy, EmployeePermissionResolver,
                                            EntraDirectoryRead, live_source_audience_document)
from operating.employee_permissions_entra import EntraDirectoryProvider
from operating.tests.test_employee_permissions import (EMPLOYEE, OID, POLICY, QA_GROUP, held,
                                                       member, portal_user, rule)

ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": "11111111-2222-4333-8444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak"}
UPN = "ricardo@petralabx.com"
OTHER_GROUP = "bbbbbbbb-0000-4000-8000-0000000000b2"
OTHER_OID = "aaaaaaaa-0000-4000-8000-000000000002"
MEMBERSHIP = f"/v1.0/users/{OID}/transitiveMemberOf/microsoft.graph.group"
JOINED = f"/v1.0/users/{OID}/joinedTeams"
GRAPH = "https://graph.microsoft.com"

# staff:quality is gated on QA_GROUP (POLICY); staff:qa-records additionally on
# the Entra department.
DEPARTMENT_POLICY = copy.deepcopy(POLICY)
DEPARTMENT_POLICY["principals"]["staff:qa-records"] = rule(
    "VIEW_ALL_DOCUMENTS", "documents.controlled", ["cmdeptqa00001"],
    entra_departments=["Quality Assurance"])


def j(status, value):
    return status, {}, json.dumps(value).encode()


class FakeGraph:
    """A directory: one user object and its membership pages; records every call."""

    def __init__(self, **fields):
        self.user = {"id": OID, "accountEnabled": True, "userPrincipalName": UPN.title(),
                     "mail": UPN, "department": "Quality Assurance"}
        self.user_status = 200
        # Pages of transitive groups; page i links to i+1 with $skiptoken=i+1.
        self.pages = [[{"@odata.type": "#microsoft.graph.group", "id": OTHER_GROUP}],
                      [{"@odata.type": "#microsoft.graph.group", "id": QA_GROUP.upper()}]]
        self.membership_status = 200
        self.next_link = None          # override of the nextLink on page 0
        # joinedTeams (read only after the membership read's 403): the user is in
        # the M365 group behind QA_GROUP's team.
        self.joined_pages = [[{"id": QA_GROUP}]]
        self.joined_status = 200
        self.joined_next_link = None
        self.calls = []
        for name, value in fields.items():
            setattr(self, name, value)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.token-do-not-leak.sig", "expires_in": 3599})
        path, query = unquote(parts.path), parse_qs(parts.query)
        if path == "/v1.0/users":
            return j(200, {"value": [{"id": OTHER_OID, "accountEnabled": False},
                                     {"id": OID, "accountEnabled": True}]})
        if path.lower() in (f"/v1.0/users/{OID}", f"/v1.0/users/{UPN}"):
            if self.user_status != 200:
                return j(self.user_status, {"error": {"code": "Authorization_RequestDenied"}})
            return j(200, {"@odata.context": f"{GRAPH}/v1.0/$metadata#users", **self.user})
        if path == MEMBERSHIP:
            if self.membership_status != 200:
                return j(self.membership_status, {"error": {"code": "Request_Forbidden"}})
            index = int(query.get("$skiptoken", ["0"])[0])
            page = {"value": self.pages[index]}
            if index + 1 < len(self.pages):
                page["@odata.nextLink"] = (self.next_link if index == 0 and self.next_link
                                           else f"{GRAPH}{MEMBERSHIP}?$select=id&$top=999"
                                                f"&$skiptoken={index + 1}")
            return j(200, page)
        if path == JOINED:
            if self.joined_status != 200:
                return j(self.joined_status, {"error": {"code": "Forbidden"}})
            index = int(query.get("$skiptoken", ["0"])[0])
            page = {"value": self.joined_pages[index]}
            if index + 1 < len(self.joined_pages):
                page["@odata.nextLink"] = (self.joined_next_link or
                                           f"{GRAPH}{JOINED}?$select=id&$skiptoken={index + 1}")
            return j(200, page)
        return j(404, {"error": {"code": "ResourceNotFound"}})

    def gets(self):
        return [url for method, url in self.calls if method == "GET"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def stack(graph=None, *, policy=None, ttl=300, clock=None, max_pages=5, role="STAFF"):
    graph = graph or FakeGraph()
    api, _transport = build_graph(ENV, sender=graph)
    provider = EntraDirectoryProvider(GraphDirectoryReader(api, max_pages=max_pages),
                                      ttl_seconds=ttl, clock=clock or Clock())
    resolver = EmployeePermissionResolver(
        EmployeePermissionPolicy.from_json(policy or DEPARTMENT_POLICY),
        lambda principal: portal_user(role), directory=provider)
    return graph, resolver


class MembershipToEntitlementTests(unittest.TestCase):

    def test_transitive_membership_across_pages_grants_the_group_gated_principal(self):
        graph, resolver = stack()
        ent = resolver.resolve(EMPLOYEE)
        # QA_GROUP is on the second page (upper-case id): it still counts.
        self.assertIn("staff:quality", held(ent))
        self.assertIn("staff:qa-records", held(ent))
        decisions = {d.principal: d for d in ent.decisions}
        self.assertEqual(decisions["staff:quality"].entra_group, "member")
        self.assertEqual(decisions["staff:qa-records"].entra_department, "member")
        self.assertEqual(ent.notes, ())
        self.assertIn(("entra_directory", f"graph:directory/users/{OID}"),
                      [i[:2] for i in ent.inputs])
        # The directory's UPN and every group join the employee's source identities.
        self.assertTrue({OID, UPN, "group:" + QA_GROUP, "group:" + OTHER_GROUP}
                        <= ent.source_identities)
        self.assertTrue(ent.item_acl_allows({"status": "read", "identities": [UPN.upper()]}))
        self.assertEqual(len([u for u in graph.gets() if MEMBERSHIP in u]), 2)

    def test_groups_and_departments_only_restrict(self):
        graph = FakeGraph()
        graph.pages = [[{"id": OTHER_GROUP}]]
        graph.user["department"] = "Sales"
        _, resolver = stack(graph)
        ent = resolver.resolve(EMPLOYEE)
        self.assertNotIn("staff:quality", held(ent))
        self.assertNotIn("staff:qa-records", held(ent))
        # Principals gated on neither are still decided by the Portal role alone.
        self.assertIn("staff:operations", held(ent))
        decisions = {d.principal: d for d in ent.decisions}
        self.assertEqual(decisions["staff:quality"].entra_group, "not_member")
        self.assertEqual(decisions["staff:qa-records"].entra_department, "not_member")

    def test_a_group_never_adds_what_the_portal_role_denies(self):
        _, resolver = stack(role="CUSTOMER")
        self.assertEqual(resolver.resolve(EMPLOYEE).denied_reason, "portal_role_not_employee")


class FailClosedTests(unittest.TestCase):
    """Every incomplete or refused directory read denies the whole entitlement."""

    def denied(self, graph, reason, *, unresolved=True, **kwargs):
        _, resolver = stack(graph, **kwargs)
        ent = resolver.resolve(EMPLOYEE)
        self.assertIsNone(ent.actor)
        self.assertEqual(ent.denied_reason, reason)
        # Only Entra's answer about the employee is a refusal; every failure is an outage.
        self.assertEqual(ent.rights_unresolved, unresolved)
        # Never a fallback to the Portal role: nothing at all is held.
        self.assertEqual(ent.decisions, ())
        self.assertEqual(ent.source_identities, frozenset())
        return ent

    def test_incomplete_paging_denies(self):
        # Two pages, one allowed: page cap with a continuation left.
        self.denied(FakeGraph(), "entra_directory:membership_truncated", max_pages=1)

    def test_a_nextlink_leaving_the_collection_denies(self):
        graph = FakeGraph()
        graph.next_link = f"{GRAPH}/v1.0/users/{OTHER_OID}/transitiveMemberOf/" \
                          "microsoft.graph.group?$skiptoken=1"
        self.denied(graph, "entra_directory:unreadable:nextlink_escaped")

    def test_malformed_or_non_group_membership_items_deny(self):
        for item in ({"id": "not-a-guid"}, {}, {"@odata.type": "#microsoft.graph.directoryRole",
                                                "id": OTHER_GROUP}):
            with self.subTest(item=item):
                graph = FakeGraph()
                graph.pages = [[{"id": QA_GROUP}, item]]
                self.denied(graph, "entra_directory:membership_item_invalid")

    def test_unknown_user_denies(self):
        graph = FakeGraph()
        graph.user_status = 404
        self.denied(graph, "entra_directory:user_not_found", unresolved=False)

    def test_disabled_or_unflagged_account_denies(self):
        for enabled in (False, None, "true"):
            with self.subTest(enabled=enabled):
                graph = FakeGraph()
                graph.user["accountEnabled"] = enabled
                self.denied(graph, "entra_directory:account_disabled", unresolved=False)
                self.assertFalse(any(MEMBERSHIP in u for u in graph.gets()))

    def test_another_directory_object_denies(self):
        graph = FakeGraph()
        graph.user["id"] = OTHER_OID
        self.denied(graph, "entra_directory:user_mismatch", unresolved=False)

    def test_directory_403_404_and_500_deny_with_reason(self):
        # (A membership 403 is the consent blocker, not a denial: GroupFallbackTests.) A 404
        # on the membership after the user object was read is no answer about the user.
        cases = (("user_status", 403, "entra_directory:graph_permission_missing:User.Read.All"),
                 ("user_status", 500, "entra_directory:unreadable:http_500"),
                 ("membership_status", 404, "entra_directory:unreadable:http_404"),
                 ("membership_status", 503, "entra_directory:unreadable:http_503"))
        for field, status, reason in cases:
            with self.subTest(field=field, status=status):
                graph = FakeGraph()
                setattr(graph, field, status)
                self.denied(graph, reason)

    def test_a_failing_provider_denies(self):
        def broken(principal):
            raise RuntimeError("boom")
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_json(POLICY),
                                              lambda principal: portal_user(), directory=broken)
        self.assertEqual(resolver.resolve(EMPLOYEE).denied_reason,
                         "entra_directory:provider_failed")

    def test_one_entra_input_only(self):
        with self.assertRaises(ValueError):
            EmployeePermissionResolver(EmployeePermissionPolicy.from_json(POLICY),
                                       lambda principal: portal_user(),
                                       lambda principal: member(QA_GROUP),
                                       directory=lambda principal: None)

    def test_an_unavailable_read_carries_no_facts(self):
        with self.assertRaises(ValueError):
            EntraDirectoryRead("unavailable", OID, reason="user_not_found",
                               group_ids=frozenset({QA_GROUP}))


SITE_ACL = {"status": "read", "identities": ["group:" + QA_GROUP]}   # a site's ACL grants the group


def without_consent(**fields):
    return FakeGraph(membership_status=403, **fields)


class GroupFallbackTests(unittest.TestCase):
    """Without GroupMember.Read.All the groups come from the user's joined Teams."""

    def test_without_consent_a_site_acl_group_member_gets_the_group_and_a_non_member_does_not(self):
        graph, resolver = stack(without_consent())
        ent = resolver.resolve(EMPLOYEE)
        self.assertIn("staff:quality", held(ent))
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:quality"].entra_group,
                         "member")
        self.assertTrue(ent.item_acl_allows(SITE_ACL))
        self.assertIn("group:" + QA_GROUP, live_source_audience_document(
            {"tenant": ent.actor.tenant}, ent)["identities"][ent.principal.principal])
        self.assertEqual(ent.notes, ("group_membership_derived:joined_teams",
                                     "group_membership_unavailable:"
                                     "graph_permission_missing:GroupMember.Read.All"))
        self.assertTrue(any(JOINED in u for u in graph.gets()))

        _, resolver = stack(without_consent(joined_pages=[[{"id": OTHER_GROUP}]]))
        ent = resolver.resolve(EMPLOYEE)
        self.assertNotIn("staff:quality", held(ent))
        # Absence from joinedTeams proves nothing (the group may be no team).
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:quality"].entra_group,
                         "unavailable")
        self.assertFalse(ent.item_acl_allows(SITE_ACL))
        self.assertTrue(ent.item_acl_allows({"status": "read",
                                             "identities": ["group:" + OTHER_GROUP]}))
        self.assertIn("staff:operations", held(ent))       # Portal role alone, as before

    def test_with_consent_the_graph_path_wins_from_the_next_read(self):
        clock = Clock()
        graph, resolver = stack(without_consent(), clock=clock, ttl=60)
        self.assertIn("staff:quality", held(resolver.resolve(EMPLOYEE)))
        graph.membership_status = 200                       # consent granted
        graph.pages = [[{"id": OTHER_GROUP}]]               # QA_GROUP only in joinedTeams
        joined = len([u for u in graph.gets() if JOINED in u])
        clock.now += 60
        ent = resolver.resolve(EMPLOYEE)
        self.assertNotIn("staff:quality", held(ent))
        self.assertEqual({d.principal: d for d in ent.decisions}["staff:quality"].entra_group,
                         "not_member")
        self.assertNotIn("group:" + QA_GROUP, ent.source_identities)
        self.assertEqual(ent.notes, ())
        self.assertEqual(len([u for u in graph.gets() if JOINED in u]), joined)

    def test_unreadable_joined_teams_leave_the_rights_unresolved(self):
        two_pages = [[{"id": OTHER_GROUP}], [{"id": QA_GROUP}]]
        escaped = f"{GRAPH}/v1.0/users/{OTHER_OID}/joinedTeams?$select=id&$skiptoken=1"
        cases = {"refused": dict(joined_status=403), "outage": dict(joined_status=500),
                 "not_found": dict(joined_status=404),
                 "truncated": dict(joined_pages=two_pages),
                 "nextlink_escaped": dict(joined_pages=two_pages, joined_next_link=escaped),
                 "malformed": dict(joined_pages=[[{"id": QA_GROUP}, {"id": "not-a-guid"}]]),
                 "duplicate": dict(joined_pages=[[{"id": QA_GROUP}, {"id": QA_GROUP.upper()}]]),
                 "not_a_team": dict(joined_pages=[[{"@odata.type": "#microsoft.graph.group",
                                                    "id": QA_GROUP}]])}
        for name, fields in cases.items():
            with self.subTest(name):
                _, resolver = stack(without_consent(**fields),
                                    max_pages=1 if name == "truncated" else 5)
                ent = resolver.resolve(EMPLOYEE)
                # Groups neither read nor derived: never narrowed to the Portal role alone.
                self.assertIsNone(ent.actor)
                self.assertTrue(ent.rights_unresolved)
                self.assertTrue(ent.denied_reason.startswith(
                    "entra_directory:group_membership_unavailable:"
                    "graph_permission_missing:GroupMember.Read.All,joined_teams:"))
                self.assertEqual(ent.source_identities, frozenset())
                self.assertFalse(ent.item_acl_allows(SITE_ACL))
        graph, resolver = stack(without_consent(joined_status=500))
        self.assertIsNone(resolver.resolve(EMPLOYEE).actor)
        graph.joined_status = 200          # the outage ends: retried now, not at expiry
        self.assertIn("staff:quality", held(resolver.resolve(EMPLOYEE)))

    def test_every_joined_team_is_derived_once_at_any_cardinality(self):
        for count in (1, 10, 60):
            with self.subTest(count=count):
                teams = [f"cccccccc-0000-4000-8000-{n:012x}" for n in range(count)]
                pages = [[{"id": t} for t in teams[i:i + 20]] for i in range(0, count, 20)]
                _, resolver = stack(without_consent(joined_pages=pages))
                ent = resolver.resolve(EMPLOYEE)
                self.assertEqual({i for i in ent.source_identities if i.startswith("group:")},
                                 {"group:" + t for t in teams})


class ExpiryTests(unittest.TestCase):

    def test_a_revoked_membership_takes_effect_at_expiry(self):
        clock = Clock()
        graph, resolver = stack(clock=clock, ttl=300)
        self.assertIn("staff:quality", held(resolver.resolve(EMPLOYEE)))
        sent = len(graph.gets())
        graph.pages = [[{"id": OTHER_GROUP}]]                 # removed in Entra
        clock.now += 299
        self.assertIn("staff:quality", held(resolver.resolve(EMPLOYEE)))
        self.assertEqual(len(graph.gets()), sent)              # served from the cache
        clock.now += 1
        self.assertNotIn("staff:quality", held(resolver.resolve(EMPLOYEE)))
        self.assertGreater(len(graph.gets()), sent)

    def test_a_disabled_account_takes_effect_at_expiry_and_failures_are_not_cached(self):
        clock = Clock()
        graph, resolver = stack(clock=clock, ttl=60)
        resolver.resolve(EMPLOYEE).require_actor()
        graph.user["accountEnabled"] = False
        clock.now += 60
        self.assertEqual(resolver.resolve(EMPLOYEE).denied_reason,
                         "entra_directory:account_disabled")
        graph.user["accountEnabled"] = True                    # re-enabled: read again now
        resolver.resolve(EMPLOYEE).require_actor()

    def test_a_clock_running_backwards_expires_the_entry(self):
        clock = Clock()
        graph, resolver = stack(clock=clock)
        resolver.resolve(EMPLOYEE)
        sent = len(graph.gets())
        clock.now -= 1
        resolver.resolve(EMPLOYEE)
        self.assertGreater(len(graph.gets()), sent)


class GetOnlyTests(unittest.TestCase):

    def test_every_request_is_a_guarded_get_of_the_directory_grammar(self):
        graph, resolver = stack()
        resolver.resolve(EMPLOYEE)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(graph_directory.main(["probe"], environ=ENV, sender=graph), 0)
        methods = {(m, urlsplit(u).hostname) for m, u in graph.calls}
        self.assertEqual(methods, {("POST", "login.microsoftonline.com"),
                                   ("GET", "graph.microsoft.com")})
        ops = set()
        for url in graph.gets():
            check_request("GET", url, environ={})
            ops.add(grammar.match("graph.microsoft.com", urlsplit(url).path))
        self.assertEqual(ops, {"graph.user", "graph.user_transitive_groups", "graph.users"})
        graph.membership_status = 403                        # the fallback is GET-only too
        with redirect_stdout(io.StringIO()):
            graph_directory.main(["probe"], environ=ENV, sender=graph)
        for url in graph.gets():
            check_request("GET", url, environ={})
            ops.add(grammar.match("graph.microsoft.com", urlsplit(url).path))
        self.assertIn("graph.user_joined_teams", ops)
        self.assertEqual({m for m, _ in graph.calls}, {"GET", "POST"})

    def test_the_reader_only_takes_the_guarded_client(self):
        with self.assertRaises(TypeError):
            GraphDirectoryReader(object())

    def test_probe_reports_counts_and_enums_never_identities(self):
        graph = FakeGraph()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(graph_directory.main(["probe"], environ=ENV, sender=graph), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual((report["directory_status"], report["directory_reason"],
                          report["group_count"], report["enabled_users_in_page"],
                          report["group_basis"]), ("read", "none", 2, 1, "transitive"))
        text = out.getvalue().lower()
        for value in (OID, OTHER_OID, UPN, QA_GROUP, OTHER_GROUP, "quality", "do-not-leak"):
            self.assertNotIn(value, text)
        # The live check fails group_basis none (every employee then denied), not only an
        # unread directory; injected output is never the live-read label.
        spec = json.loads((live_verify.ROOT / live_verify.SPECS_DIR_NAME
                           / "graph.directory_entitlements.json").read_text("utf-8"))
        for fields, basis, failures in (
                ({}, "transitive", []),
                ({"membership_status": 403}, "joined_teams", []),
                ({"membership_status": 403, "joined_status": 403}, "none",
                 ["group_listing_read < 1"])):
            with self.subTest(basis=basis):
                out = io.StringIO()
                with redirect_stdout(out):
                    graph_directory.main(["probe"], environ=ENV, sender=FakeGraph(**fields))
                report = json.loads(out.getvalue())
                self.assertEqual((report["directory_status"], report["group_basis"]),
                                 ("read", basis))
                self.assertEqual(live_verify.check_expectations(spec, 0, report),
                                 ["evidence_label != expected", *failures])

    def test_lookup_by_upn_resolves_the_same_object(self):
        graph = FakeGraph()
        api, _ = build_graph(ENV, sender=graph)
        read = GraphDirectoryReader(api).read(UPN)
        self.assertEqual((read.status, read.object_id), ("read", OID))
        graph.user["userPrincipalName"] = "someone.else@petralabx.com"
        self.assertEqual(GraphDirectoryReader(api).read(UPN).reason, "user_mismatch")


if __name__ == "__main__":
    unittest.main()
