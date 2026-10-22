"""Session identity: no live data without a real employee.

Evidence label: ``isolated-fixture``. Portal user rows are Portal-shaped fixtures
(the Prisma ``User`` fields ``rbac-adapters.ts`` loads), tickets are minted here
with a test secret; no network, no credential. The case runtimes are offline (no
Workbench client, no live reader).
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ontology.core import Actor
from operating import employee_permissions_portal as portal
from operating import session_identity as si
from operating.case_runtime import CaseRuntime
from operating.employee_permissions import (EMPLOYEE_IDENTITY_REQUIRED, POLICY_SCHEMA,
                                            PORTAL_ACCESS_SNAPSHOT_SCHEMA,
                                            EmployeeAccessRefused, EmployeeActorGate,
                                            EmployeePermissionPolicy,
                                            EmployeePermissionResolver, EmployeePrincipal,
                                            PortalAccessSnapshot)
from operating.mcp_server import MCPServer, local_client

ROOT = Path(__file__).resolve().parents[2]
AUDIENCE = ROOT / "examples" / "ao_case_audience.example.json"
TID = "11111111-1111-4111-8111-111111111111"
OTHER_TID = "22222222-2222-4222-8222-222222222222"
OID_A = "aaaaaaaa-0000-4000-8000-000000000001"
OID_B = "aaaaaaaa-0000-4000-8000-000000000002"
OID_GONE = "aaaaaaaa-0000-4000-8000-000000000009"
EMAIL_A, EMAIL_B = "ricardo@petralabx.com", "vince@petralabx.com"
DEPTS = {"ops": "cmdeptops0001", "qa": "cmdeptqa00001"}
COMPANY = portal.EXTERNAL_COMPANY_KEY
SECRET = "test-session-secret-" + "x" * 32
RULES = {"staff:operations": ("VIEW_OPERATIONS", "operations.sales", "ops"),
         "staff:quality": ("VIEW_OPERATIONS", "operations.quality", "qa")}


def user_row(oid, email, role="STAFF", departments=("ops", "qa"), **extra):
    """One Portal user as the snapshot carries it (Prisma ``User`` field names)."""
    data_keys = sorted({dk for _, dk, _ in RULES.values()})
    return {"id": "cmuser" + oid[-7:], "email": email, "role": role,
            "accessLevel": portal.LEGACY_ROLE_BOOTSTRAP[role], "isActive": True,
            "entraTenantId": TID, "entraObjectId": oid,
            "departmentIds": [DEPTS[d] for d in departments],
            "accessScopes": [{"scopeType": "COMPANY", "scopeKey": COMPANY, "effect": "ALLOW"}]
            + [{"scopeType": "DATA", "scopeKey": key, "effect": "ALLOW"} for key in data_keys],
            **extra}


def identity_files(directory, rules=None, *, role="STAFF", users=None):
    """Write an employee policy and a Portal access snapshot; return their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    policy = {"schema": POLICY_SCHEMA, "tenant": "plx", "entra_tenant_id": TID,
              "clearance_by_access_level": {"L1": "internal", "L2": "internal",
                                            "L3": "confidential", "L4": "confidential",
                                            "L5": "restricted"},
              "principals": {name: {"capability": cap, "department_ids": [DEPTS[dept]],
                                    "company_key": COMPANY, "data_key": data_key}
                             for name, (cap, data_key, dept) in (rules or RULES).items()}}
    snapshot = {"schema": PORTAL_ACCESS_SNAPSHOT_SCHEMA, "source": "portal:app_user@fixture",
                "read_at": "2026-10-02T09:00:00Z",
                "users": users if users is not None else [user_row(OID_A, EMAIL_A, role)]}
    (directory / "policy.json").write_text(json.dumps(policy), "utf-8")
    (directory / "portal.json").write_text(json.dumps(snapshot), "utf-8")
    return directory / "policy.json", directory / "portal.json"


TICKET_ENV = "PLX_TEST_SESSION_TICKET"


def employee_host_args(directory, principals, *, role="STAFF"):
    """``mcp_server`` arguments and environment for one authenticated employee holding
    ``principals`` (each a VIEW_OPERATIONS rule on the ops department): a fresh agent
    start-up ticket (single use) in ``TICKET_ENV``."""
    policy, snapshot = identity_files(directory, {
        name: ("VIEW_OPERATIONS", "operations.sales", "ops") for name in principals},
        role=role)
    return (["--employee-policy", str(policy), "--portal-access", str(snapshot),
             "--session-ticket-env", TICKET_ENV],
            {TICKET_ENV: Fixture.ticket(author=si.AUTHOR_AGENT),
             si.SESSION_TICKET_SECRET_ENV: SECRET})


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.users = [user_row(OID_A, EMAIL_A, "MANAGER"),
                      user_row(OID_B, EMAIL_B, "STAFF", departments=("ops",))]
        self.policy_path, self.portal_path = identity_files(self.dir / "id", users=self.users)

    def resolver(self):
        return EmployeePermissionResolver(EmployeePermissionPolicy.from_file(self.policy_path),
                                          PortalAccessSnapshot(self.portal_path))

    def write_users(self, users):
        self.portal_path.write_text(json.dumps({
            "schema": PORTAL_ACCESS_SNAPSHOT_SCHEMA, "source": "portal:app_user@fixture",
            "read_at": "2026-10-02T09:00:00Z", "users": users}), "utf-8")

    def runtime(self, *, require_employee=True, resolver=None):
        resolver = resolver or self.resolver()
        runtime = CaseRuntime(self.dir / "state", client=None, audience=AUDIENCE, tenant="plx",
                              employee_permissions=resolver, require_employee=require_employee)
        self.addCleanup(runtime.close)
        return runtime

    @staticmethod
    def ticket(oid=OID_A, email=EMAIL_A, author=si.AUTHOR_HUMAN, tenant=TID, **kw):
        return si.mint_session_ticket(uid="cmuser1", email=email, tenant_id=tenant,
                                      object_id=oid, author=author,
                                      secret=kw.pop("secret", SECRET), **kw)


def _resign(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"),
                                                  sort_keys=True).encode()).rstrip(b"=").decode()
    return f"v1.{payload}.{si._b64(si._sign(SECRET, 'v1.' + payload))}"


class TicketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_round_trip_and_refusals(self):
        ticket = Fixture.ticket(now=1000)
        claims = si.verify_session_ticket(ticket, SECRET, now=1001)
        self.assertEqual((claims["entraObjectId"], claims["author"]), (OID_A, "human"))
        version, payload, mac = ticket.split(".")
        forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        forged["entraObjectId"] = OID_B
        forged_payload = base64.urlsafe_b64encode(
            json.dumps(forged).encode()).rstrip(b"=").decode()
        cases = {"session_ticket_invalid": [
                     f"{version}.{forged_payload}.{mac}",          # payload swapped
                     ticket[:-2] + ("AA" if ticket[-2:] != "AA" else "BB"),  # MAC changed
                     "v2." + payload + "." + mac, "", "x" * 5000, None],
                 "session_ticket_expired": [Fixture.ticket(now=1000, ttl_seconds=60)]}
        for reason, tickets in cases.items():
            for bad in tickets:
                with self.assertRaises(si.SessionIdentityRefused) as caught:
                    si.verify_session_ticket(bad, SECRET, now=1061)
                self.assertEqual(caught.exception.reason, reason)
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.verify_session_ticket(ticket, "another-secret-" + "y" * 32, now=1001)
        self.assertEqual(caught.exception.reason, "session_ticket_invalid")
        for secret in (None, "", "short"):
            with self.assertRaises(si.SessionIdentityRefused) as caught:
                si.verify_session_ticket(ticket, secret, now=1001)
            self.assertEqual(caught.exception.reason, "session_secret_unavailable")
        # Issued in the future (beyond clock skew) is not a ticket either.
        with self.assertRaises(si.SessionIdentityRefused):
            si.verify_session_ticket(Fixture.ticket(now=5000), SECRET, now=1000)
        with self.assertRaises(ValueError):
            Fixture.ticket(ttl_seconds=si.SESSION_TICKET_TTL_SECONDS + 1)

    def test_ticket_lives_at_most_two_hours_and_carries_a_jti(self):
        self.assertEqual(si.SESSION_TICKET_TTL_SECONDS, 2 * 60 * 60)
        with self.assertRaises(ValueError):
            Fixture.ticket(ttl_seconds=3 * 60 * 60)
        # A correctly signed 3h ticket (an old 12h minter) is refused by the verifier.
        claims = si.verify_session_ticket(Fixture.ticket(now=1000), SECRET, now=1001)
        self.assertEqual(claims["exp"] - claims["iat"], 2 * 60 * 60)
        self.assertRegex(claims["jti"], r"^[A-Za-z0-9_-]{16,64}$")
        self.assertNotEqual(claims["jti"], si.verify_session_ticket(
            Fixture.ticket(now=1000), SECRET, now=1001)["jti"])
        payload = {**claims, "exp": claims["iat"] + 3 * 60 * 60}
        long = _resign(payload)
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.verify_session_ticket(long, SECRET, now=1001)
        self.assertEqual(caught.exception.reason, "session_ticket_invalid")
        no_jti = {k: v for k, v in claims.items() if k != "jti"}
        with self.assertRaises(si.SessionIdentityRefused):
            si.verify_session_ticket(_resign(no_jti), SECRET, now=1001)
        with self.assertRaises(ValueError):
            Fixture.ticket(jti="short")

    def test_replay_guard_refuses_a_reused_or_revoked_jti_within_its_ttl(self):
        guard = si.TicketReplayGuard(self.dir / "jti.db")
        ticket = Fixture.ticket(now=1000)
        claims = si.verify_session_ticket(ticket, SECRET, now=1001)
        guard.check(claims, consume=True, now=1001)
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            guard.check(claims, consume=True, now=1002)
        self.assertEqual(caught.exception.reason, "session_ticket_replayed")
        guard.check(claims, now=1002)  # a per-request check does not consume
        other = si.verify_session_ticket(Fixture.ticket(now=1000), SECRET, now=1001)
        guard.check(other, consume=True, now=1001)  # another ticket is a new jti
        guard.revoke(other)
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            guard.check(other, now=1003)
        self.assertEqual(caught.exception.reason, "session_ticket_revoked")
        # Entries die with the ticket: the guard stays small and the ticket is expired anyway.
        guard.check({"jti": "z" * 16, "exp": 5000}, consume=True, now=1004)
        guard.check({"jti": "y" * 16, "exp": 19000}, consume=True, now=9000)
        with sqlite3.connect(guard.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ticket_jti").fetchone()[0], 1)

    def test_replay_guard_survives_a_restart_within_the_ticket_ttl(self):
        # Identity tickets are bearer credentials: a host restart within the 2 h TTL
        # must not make a consumed or revoked jti usable again.
        first = si.verify_session_ticket(Fixture.ticket(now=1000), SECRET, now=1001)
        revoked = si.verify_session_ticket(Fixture.ticket(now=1000), SECRET, now=1001)
        guard = si.TicketReplayGuard(self.dir / "jti.db")
        guard.check(first, consume=True, now=1001)
        guard.revoke(revoked)
        restarted = si.TicketReplayGuard(self.dir / "jti.db")
        for claims, reason in ((first, "session_ticket_replayed"),
                               (revoked, "session_ticket_revoked")):
            with self.subTest(reason=reason), \
                    self.assertRaises(si.SessionIdentityRefused) as caught:
                restarted.check(claims, consume=True, now=1002)
            self.assertEqual(caught.exception.reason, reason)
        with sqlite3.connect(restarted.path) as db:   # only digests are kept, never a jti
            stored = {row[0] for row in db.execute("SELECT jti_sha256 FROM ticket_jti")}
        self.assertFalse({first["jti"], revoked["jti"]} & stored)

    def test_secret_has_no_fallback(self):
        self.assertIsNone(si.session_secret({}))
        self.assertIsNone(si.session_secret({"NEXTAUTH_SECRET": SECRET}))
        self.assertEqual(si.session_secret({si.SESSION_TICKET_SECRET_ENV: SECRET}), SECRET)


class EmployeeSessionTests(Fixture):
    def test_configured_employee_for_mcp_is_verified_by_the_resolver(self):
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        self.assertEqual((session.proof, session.author_kind, session.fixture),
                         (si.PROOF_CONFIGURED, si.AUTHOR_AGENT, False))
        self.assertEqual(session.actor, Actor("plx", frozenset({"employee:" + OID_A})))
        self.assertEqual(session.describe()["employee"], "employee:" + OID_A)
        refusals = {OID_GONE: "portal_access_unavailable", "not-a-guid": "employee_principal_invalid"}
        for oid, reason in refusals.items():
            with self.assertRaises(si.SessionIdentityRefused) as caught:
                si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=oid)
            self.assertEqual(caught.exception.reason, reason)
        self.write_users([user_row(OID_A, EMAIL_A, isActive=False)])
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        self.assertEqual(caught.exception.reason, "portal_user_inactive")
        self.write_users([user_row(OID_A, EMAIL_A, role="CUSTOMER")])
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        self.assertEqual(caught.exception.reason, "portal_role_not_employee")

    def test_console_identity_comes_only_from_a_ticket_of_the_right_author(self):
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE, object_id=OID_A)
        self.assertEqual(caught.exception.reason, "console_identity_requires_session_ticket")
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                      ticket=self.ticket(), secret=SECRET)
        self.assertEqual((session.proof, session.author_kind, session.email),
                         (si.PROOF_TICKET, si.AUTHOR_HUMAN, EMAIL_A))
        self.assertIsNotNone(session.expires_at)
        refusals = {
            "session_ticket_author_mismatch": self.ticket(author=si.AUTHOR_AGENT),
            "tenant_mismatch": self.ticket(tenant=OTHER_TID),
            "session_ticket_identity_mismatch": self.ticket(email=EMAIL_B),
            "portal_access_unavailable": self.ticket(oid=OID_GONE),
        }
        for reason, ticket in refusals.items():
            with self.assertRaises(si.SessionIdentityRefused) as caught:
                si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE, ticket=ticket,
                                    secret=SECRET)
            self.assertEqual(caught.exception.reason, reason)
        # An agent ticket opens an MCP session (author agent), never a console one.
        mcp = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP,
                                  ticket=self.ticket(author=si.AUTHOR_AGENT), secret=SECRET)
        self.assertEqual(mcp.author_kind, si.AUTHOR_AGENT)
        with self.assertRaises(si.SessionIdentityRefused):
            si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A,
                                ticket=self.ticket(author=si.AUTHOR_AGENT), secret=SECRET)
        with self.assertRaises(si.SessionIdentityRefused):
            si.employee_session(None, channel=si.CHANNEL_MCP, object_id=OID_A)

    def test_fixture_identity_is_explicit_and_cannot_impersonate(self):
        session = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_CONSOLE)
        self.assertTrue(session.fixture)
        self.assertEqual(session.author_kind, si.AUTHOR_HUMAN)
        for principals in ([], ["*"], ["employee:" + OID_A], ["agent:claude"], ["sp_mcp"]):
            with self.assertRaises(si.SessionIdentityRefused):
                si.fixture_session("plx", principals, channel=si.CHANNEL_MCP)
        with self.assertRaises(ValueError):  # author kind follows the channel
            si.SessionIdentity("plx", si.CHANNEL_MCP, si.AUTHOR_HUMAN, si.PROOF_FIXTURE, None,
                               frozenset({"staff:operations"}))
        with self.assertRaises(ValueError):  # an employee session is exactly the employee
            si.SessionIdentity("plx", si.CHANNEL_MCP, si.AUTHOR_AGENT, si.PROOF_CONFIGURED,
                               EmployeePrincipal(TID, OID_A),
                               frozenset({"employee:" + OID_A, "staff:finance"}))

    def test_state_directories_never_mix_fixture_and_live(self):
        fresh = self.dir / "fresh"
        self.assertEqual(si.bind_state_dir(fresh, fixture=True), "fixture")
        self.assertEqual(si.bind_state_dir(fresh, fixture=True), "fixture")
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.bind_state_dir(fresh, fixture=False)
        self.assertEqual(caught.exception.reason, "live_session_on_fixture_state")
        old = self.dir / "old"
        (old / "case-runtime").mkdir(parents=True)  # unlabelled case state counts as live
        with self.assertRaises(si.SessionIdentityRefused) as caught:
            si.bind_state_dir(old, fixture=True)
        self.assertEqual(caught.exception.reason, "fixture_session_on_live_state")
        self.assertEqual(si.bind_state_dir(old, fixture=False), "live")


class IntentIdentityTests(Fixture):
    """Decision g2-1: an intent's ``sub``/``act`` come only from the verified session."""

    def test_an_employee_session_names_its_subject_by_object_id_and_portal_email(self):
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        identity = session.intent_identity(session.actor)
        self.assertEqual(identity, {
            "schema": "plx-intent-identity/v2",
            "sub": {"principal": "employee:" + OID_A, "kind": "employee",
                    "proof": si.PROOF_CONFIGURED, "identities": sorted({OID_A, EMAIL_A}),
                    "oid": OID_A},
            "act": [{"kind": si.AUTHOR_AGENT, "channel": si.CHANNEL_MCP,
                     "client": "plx-workbench-mcp", "session": session.session_ref}]})
        # A configured session's act.session is a per-session nonce digest: two sessions
        # of the same employee are two actors' sessions, never one.
        again = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        self.assertNotEqual(again.session_ref, session.session_ref)
        ticket = self.ticket(jti="jti-of-this-ticket-01")
        console = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                      ticket=ticket, secret=SECRET, client="portal-console-1")
        identity = si.intent_identity(console, console.actor)
        self.assertEqual((identity["sub"]["identities"], identity["act"]),
                         (sorted({OID_A, EMAIL_A}),
                          [{"kind": si.AUTHOR_HUMAN, "channel": si.CHANNEL_CONSOLE,
                            "client": "portal-console-1",
                            "session": hashlib.sha256(b"jti-of-this-ticket-01").hexdigest()}]))
        # act is attribution only: the session's actor holds the employee principal alone.
        self.assertEqual(console.actor.principals, {"employee:" + OID_A})

    def test_only_the_sessions_own_current_actor_gets_its_subject(self):
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        other = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_B)
        old = int(time.time()) - si.SESSION_TICKET_TTL_SECONDS - 10
        expired = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE, now=old,
                                      ticket=self.ticket(now=old), secret=SECRET)
        forged = {"schema": si.INTENT_IDENTITY_SCHEMA, "act": None,
                  "sub": {"principal": "employee:" + OID_B, "identities": [EMAIL_B]}}
        for given, actor, reason in ((session, other.actor, "session_actor_mismatch"),
                                     (expired, expired.actor, "session_expired"),
                                     (forged, session.actor, "session_identity_required"),
                                     # No session: refused, never an unknown ``act``.
                                     (None, session.actor, "session_required")):
            with self.subTest(reason=reason), \
                    self.assertRaises(si.SessionIdentityRefused) as caught:
                si.intent_identity(given, actor)
            self.assertEqual(caught.exception.reason, reason)

    def test_a_fixture_subject_is_host_configuration_and_otherwise_unknown(self):
        bare = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_MCP)
        self.assertIsNone(bare.intent_identity(bare.actor)["sub"])
        named = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_MCP,
                                   subject_email=" Ricardo@PetraLabX.com ")
        self.assertEqual(named.intent_identity(named.actor)["sub"],
                         {"principal": None, "kind": "fixture", "proof": si.PROOF_FIXTURE,
                          "identities": [EMAIL_A], "oid": None})
        for bad in ("agent:claude", "group:ops", "not-an-email", ""):
            with self.subTest(bad=bad), self.assertRaises(si.SessionIdentityRefused):
                si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_MCP,
                                   subject_email=bad)
        with self.assertRaises(ValueError):
            si.SessionIdentity(tenant="plx", channel=si.CHANNEL_MCP,
                               author_kind=si.AUTHOR_AGENT, proof=si.PROOF_FIXTURE,
                               employee=None, principals=frozenset({"ops"}),
                               identities=frozenset({"agent:claude"}))


class RequiredIdentityTests(Fixture):
    """A runtime built for a live session refuses every actor without an employee."""

    def test_gate_requires_a_resolver_and_refuses_non_employees(self):
        with self.assertRaises(ValueError):
            EmployeeActorGate(None, "plx", require_employee=True)
        gate = EmployeeActorGate(self.resolver(), "plx", require_employee=True)
        for actor in (Actor("plx", frozenset({"staff:operations"})), None, "eng"):
            with self.assertRaises(EmployeeAccessRefused) as caught:
                gate(actor)
            self.assertEqual(caught.exception.reason, EMPLOYEE_IDENTITY_REQUIRED)
        resolved = gate(Actor("plx", frozenset({"employee:" + OID_A})))
        self.assertIn("staff:quality", resolved.principals)

    def test_missing_identity_is_refused_on_every_gated_read_path(self):
        runtime = self.runtime()
        host_actor = Actor("plx", frozenset({"staff:operations", "staff:quality"}),
                           "confidential")
        checked = []
        for target in (runtime, runtime.mirror, runtime.originals, runtime.cases):
            for name in dir(type(target)):
                if name.startswith("_"):
                    continue
                method = getattr(target, name)
                if not callable(method) or not hasattr(method, "__wrapped__"):
                    continue
                params = inspect.signature(method.__wrapped__).parameters
                if "actor" not in params:
                    continue
                kwargs = {p: None for p, spec in params.items()
                          if p != "actor" and spec.default is inspect.Parameter.empty
                          and spec.kind in (spec.POSITIONAL_OR_KEYWORD, spec.KEYWORD_ONLY)}
                with self.assertRaises(EmployeeAccessRefused, msg=name) as caught:
                    method(actor=host_actor, **kwargs)
                self.assertEqual(caught.exception.reason, EMPLOYEE_IDENTITY_REQUIRED, name)
                checked.append(f"{type(target).__name__}.{name}")
        # The read surface the hosts serve is covered, not a token subset.
        for expected in ("CaseStore.list", "CaseStore.get", "CaseStore.open",
                         "CaseRuntime.resolve_citation", "CaseRuntime.citation_status",
                         "OriginalStore.read_pdf"):
            self.assertIn(expected, checked)
        self.assertGreater(len(checked), 15)

    def test_mcp_session_without_identity_is_refused_and_with_one_reads(self):
        runtime = self.runtime()
        fixture = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_MCP)
        with self.assertRaises(si.SessionIdentityRefused):
            MCPServer(case_runtime=runtime, actor=fixture.actor, session=fixture)
        # No session object: the runtime itself still refuses the host actor.
        bare = local_client(MCPServer(case_runtime=runtime, actor=fixture.actor), "t")
        with self.assertRaisesRegex(RuntimeError, EMPLOYEE_IDENTITY_REQUIRED):
            bare("plx.case_get", {})
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        with self.assertRaises(ValueError):
            MCPServer(case_runtime=runtime, actor=Actor("plx", frozenset({"staff:quality"})),
                      session=session)
        server = MCPServer(case_runtime=runtime, actor=session.actor, session=session)
        self.assertEqual(server.session.author_kind, si.AUTHOR_AGENT)
        call = local_client(server, "t")
        self.assertEqual(call("plx.case_get", {})["cases"], [])

    def test_test_host_capabilities_never_ride_an_employee_session(self):
        # Test-host capabilities (hosts: ["test"], the action path's
        # FIXTURE_CAPABILITIES among them) need a fixture session or none.
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        with self.assertRaisesRegex(ValueError, "test_capabilities_need_a_fixture_session"):
            MCPServer(case_runtime=self.runtime(), actor=session.actor, session=session,
                      test_capabilities=True)
        loose = CaseRuntime(self.dir / "loose-test", client=None, audience=AUDIENCE,
                            tenant="plx")
        self.addCleanup(loose.close)
        fixture = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_MCP)
        server = MCPServer(case_runtime=loose, actor=fixture.actor, session=fixture,
                           test_capabilities=True)
        self.assertTrue(server.test_capabilities)

    def test_an_expired_ticket_ends_the_mcp_session(self):
        runtime = self.runtime()
        session = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP,
                                      ticket=self.ticket(author=si.AUTHOR_AGENT, now=1000,
                                                         ttl_seconds=60),
                                      secret=SECRET, now=1001)
        call = local_client(MCPServer(case_runtime=runtime, actor=session.actor,
                                      session=session), "t")
        with self.assertRaisesRegex(RuntimeError, "session_expired"):
            call("plx.case_get", {})

    def test_a_fixture_session_never_runs_on_a_live_runtime_nor_the_reverse(self):
        fixture = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_MCP)
        with self.assertRaises(si.SessionIdentityRefused):
            si.check_runtime(self.runtime(), fixture)
        loose = CaseRuntime(self.dir / "loose", client=None, audience=AUDIENCE, tenant="plx")
        self.addCleanup(loose.close)
        si.check_runtime(loose, fixture)
        employee = si.employee_session(self.resolver(), channel=si.CHANNEL_MCP, object_id=OID_A)
        with self.assertRaises(si.SessionIdentityRefused):
            si.check_runtime(loose, employee)


class TwoEmployeesTests(Fixture):
    """Different grants, different results; a narrower employee never sees more."""

    def test_grants_differ_and_the_narrower_employee_sees_a_subset(self):
        runtime = self.runtime()
        a = Actor("plx", frozenset({"employee:" + OID_A}))
        b = Actor("plx", frozenset({"employee:" + OID_B}))
        gate = runtime.employee_gate
        held_a, held_b = gate(a).principals, gate(b).principals
        self.assertEqual(held_a - {"employee:" + OID_A}, {"staff:operations", "staff:quality"})
        self.assertEqual(held_b - {"employee:" + OID_B}, {"staff:operations"})
        self.assertEqual((gate(a).clearance, gate(b).clearance), ("confidential", "internal"))
        only_a = runtime.cases.open(a, "Quality question for A")       # confidential
        shared = runtime.cases.open(b, "Operations question from B")   # internal, ops
        quality = runtime.cases.open(a, "Quality-only question", audience=["staff:quality"])
        def ids(actor):
            return {case["case_id"] for case in runtime.cases.list(actor)}

        seen = {"a": ids(a), "b": ids(b)}
        self.assertEqual(seen["a"], {only_a, shared, quality})
        self.assertEqual(seen["b"], {shared})
        self.assertLessEqual(seen["b"], seen["a"])
        # Narrowing the session (extra presented principals) only narrows.
        narrowed = Actor("plx", frozenset({"employee:" + OID_A, "staff:operations"}))
        self.assertLessEqual(ids(narrowed), seen["a"])
        self.assertNotIn(quality, ids(narrowed))
        # Revoking A's QA department in the Portal applies on the next read.
        self.write_users([user_row(OID_A, EMAIL_A, "MANAGER", departments=("ops",)),
                          self.users[1]])
        self.assertNotIn(quality, ids(a))


class ConsoleHttpTests(Fixture):
    """The live console takes each request's identity from its bearer session ticket."""

    def serve(self, *, guarded=False, entra=None):
        """``guarded``: as ``case_console`` serves it, with the state directory's replay
        guard and session journal. ``entra``: the console's Entra resource (sign-in)."""
        from http.client import HTTPConnection
        from queue import Queue
        import threading

        from operator_ui.case_console import TicketSessions
        from operator_ui.case_workspace import CaseWorkspace
        from operator_ui.server import make_server
        from operator_ui.session_journal import SessionJournal

        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                runtime = CaseRuntime(self.dir / "state", client=None, audience=AUDIENCE,
                                      tenant="plx", employee_permissions=self.resolver(),
                                      require_employee=True)
                a = Actor("plx", frozenset({"employee:" + OID_A}))
                b = Actor("plx", frozenset({"employee:" + OID_B}))
                self.only_a = runtime.cases.open(a, "Quality question for A")
                self.shared = runtime.cases.open(b, "Operations question from B")
                state = self.dir / "state"
                sessions = TicketSessions(
                    runtime.employee_gate.resolver, SECRET,
                    lambda session: CaseWorkspace(runtime, session.actor),
                    replay_guard=si.replay_guard_for(state) if guarded else None,
                    entra_resource=entra)
                server = make_server(ticket_sessions=sessions, journal=SessionJournal
                                     .for_state_dir(state) if guarded else None)
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                runtime.close()
            except BaseException as error:
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        def stop():
            server.shutdown()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        self.assertIsNone(server.operator_access_token)
        port = server.server_address[1]

        def request(path, bearer=None, *, raw=False):
            conn = HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("GET", path, headers={"Authorization": "Bearer " + bearer}
                         if bearer else {})
            response = conn.getresponse()
            body = response.read()
            value = response.status, body if raw else json.loads(body)
            conn.close()
            return value
        # The console journals a request after it answered it: stop the server (every
        # handler done) before reading the journal.
        request.stop = stop
        return request

    def test_missing_or_invalid_identity_is_refused_and_employees_differ(self):
        request = self.serve()
        for bearer, detail in ((None, "session_ticket_required"),
                               ("not-a-ticket", "session_ticket_invalid"),
                               (self.ticket(author=si.AUTHOR_AGENT),
                                "session_ticket_author_mismatch"),
                               (self.ticket(oid=OID_GONE), "portal_access_unavailable")):
            for path in ("/api/session", "/api/cases", f"/api/case/{self.shared}"):
                status, body = request(path, bearer)
                self.assertEqual((status, body["detail"]), (401, detail), (path, bearer))
        status, session = request("/api/session", self.ticket())
        self.assertEqual(status, 200)
        self.assertEqual(session["identity"]["author_kind"], "human")
        self.assertEqual(session["identity"]["employee"], "employee:" + OID_A)
        self.assertFalse(session["identity"]["fixture"])
        seen = {}
        for name, ticket in (("a", self.ticket()), ("b", self.ticket(oid=OID_B, email=EMAIL_B))):
            status, listed = request("/api/cases", ticket)
            self.assertEqual(status, 200, listed)
            seen[name] = {item["case_id"] for item in listed["cases"]}
        self.assertEqual(seen, {"a": {self.only_a, self.shared}, "b": {self.shared}})
        status, _ = request(f"/api/case/{self.only_a}", self.ticket(oid=OID_B, email=EMAIL_B))
        self.assertEqual(status, 404)

    def test_an_employee_signed_in_with_entra_is_the_session_the_journal_records(self):
        """A fixture-signed Entra access token (isolated-fixture; generated key, no
        Entra, no network) opens a console session whose identity is the token's subject;
        an expired, wrong-audience, foreign-tenant, unsigned or foreign-key token is
        refused; a console without the Entra resource refuses every token."""
        from unittest import mock

        from operating.oidc_auth import EntraResource, StaticJwks
        from operating.tests import test_mcp_http_host as door
        from operator_ui.case_console import TicketSessions
        from operator_ui import session_journal
        from operator_ui.session_journal import SessionJournal

        audience = "api://plx-console-test"
        resource = EntraResource(TID, frozenset({audience}), door.SCOPE,
                                 "http://127.0.0.1:8765/", StaticJwks(door.jwks(door.KEY)))

        def token(oid=OID_A, key=door.KEY, header=None, **override):
            return door.token(key, door.claims(oid, aud=audience, **override), header)

        request = self.serve(guarded=True, entra=resource)
        signed = token()
        status, session = request("/api/session", signed)
        self.assertEqual(status, 200, session)
        self.assertEqual((session["identity"]["employee"], session["identity"]["author_kind"],
                          session["identity"]["proof"], session["identity"]["fixture"]),
                         ("employee:" + OID_A, "human", si.PROOF_ENTRA, False))
        status, listed = request("/api/cases", signed)
        self.assertEqual({item["case_id"] for item in listed["cases"]},
                         {self.only_a, self.shared})
        # The subject decides who reads: B's token sees B's cases only.
        status, listed = request("/api/cases", token(OID_B))
        self.assertEqual({item["case_id"] for item in listed["cases"]}, {self.shared})
        now = int(time.time())
        head = door._b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": door.KID}).encode())
        body = door._b64(json.dumps(door.claims(OID_A, aud=audience)).encode())
        none = door._b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        for name, bearer, detail in (
                ("expired", token(iat=now - 7200, nbf=now - 7200, exp=now - 60),
                 "token_expired"),
                ("wrong audience", door.token(door.KEY, door.claims(OID_A)),
                 "token_audience_mismatch"),
                ("another tenant", token(tenant=OTHER_TID), "token_tenant_mismatch"),
                ("unsigned", f"{none}.{body}.", "token_malformed"),
                ("alg none", f"{none}.{body}.{door._b64(b'x')}", "token_algorithm_refused"),
                ("signature stripped", f"{head}.{body}.{door._b64(b'x' * 256)}",
                 "token_signature_invalid"),
                ("another key", token(key=door.OTHER_KEY), "token_signature_invalid"),
                ("no Portal row", token(OID_GONE), "portal_access_unavailable")):
            with self.subTest(name):
                status, refused = request("/api/cases", bearer)
                self.assertEqual((status, refused["detail"]), (401, detail))
        # A session ticket still works beside the sign-in.
        self.assertEqual(request("/api/session", self.ticket())[1]["identity"]["proof"],
                         si.PROOF_TICKET)
        request.stop()

        # The journal records the signed-in employee and how they were proven; the
        # check counts that session as signed in (no view or citations here: it does
        # not qualify) and the ticket session as neither signed in nor host-minted.
        state = self.dir / "state"
        summary = SessionJournal.for_state_dir(state).summary()
        proofs = sorted(tuple(entry["proofs"]) for entry in summary["sessions"].values())
        self.assertEqual(proofs, [(si.PROOF_ENTRA,), (si.PROOF_ENTRA,), (si.PROOF_TICKET,)])
        self.assertEqual(sum(summary["unauthenticated"].values()), 8)
        for value in (signed, OID_A, EMAIL_A, TID):
            self.assertNotIn(value, json.dumps(summary))
        resource_file = self.dir / "entra-console.json"
        (self.dir / "jwks.json").write_text(json.dumps(door.jwks(door.KEY)), "utf-8")
        resource_file.write_text(json.dumps({
            "schema": "plx-entra-resource/v1", "tenant_id": TID, "audiences": [audience],
            "required_scope": door.SCOPE, "resource": "http://127.0.0.1:8765/",
            "jwks_file": "jwks.json"}), "utf-8")
        eval_reader = json.dumps({"entraTenantId": TID, "entraObjectId": OID_B})
        measured = session_journal.employee_recorded(state, {
            "PLX_CONSOLE_ENTRA_RESOURCE": str(resource_file), "PLX_EVAL_READER": eval_reader})
        self.assertEqual({k: measured[k] for k in ("blocked_reason", "sessions", "signed_in",
                                                    "employee_identity", "host_minted",
                                                    "qualifying_sessions")},
                         {"blocked_reason": None, "sessions": 3, "signed_in": 2,
                          "employee_identity": 1, "host_minted": 0,
                          "qualifying_sessions": 0})

        # Without the Entra resource the console takes tickets only.
        self.dir = self.dir / "plain"
        self.dir.mkdir()
        self.policy_path, self.portal_path = identity_files(self.dir / "id", users=self.users)
        request = self.serve(guarded=True)
        status, refused = request("/api/session", token())
        self.assertEqual((status, refused["detail"]), (401, "session_ticket_invalid"))
        # Sign-in needs the durable replay guard and the policy's own tenant.
        for kwargs, reason in (({}, "console_entra_sign_in_requires_replay_guard"),
                               ({"replay_guard": si.replay_guard_for(self.dir / "g")}, None)):
            with self.subTest(reason or "tenant"):
                other = EntraResource(OTHER_TID, frozenset({audience}), door.SCOPE,
                                      "http://127.0.0.1:8765/", StaticJwks(door.jwks(door.KEY)))
                with self.assertRaises(ValueError) as raised:
                    TicketSessions(self.resolver(), SECRET, mock.Mock(),
                                   entra_resource=resource if reason else other, **kwargs)
                self.assertEqual(str(raised.exception), reason or
                                 "entra_resource_tenant_must_equal_the_policy_tenant")

    def test_the_loopback_operator_console_takes_no_entra_token(self):
        """A fixture (loopback operator) console serves its one fixture session behind its
        process token: an Entra token is not a credential there, and its session names no
        employee, so nothing it does is recorded as one."""
        import threading
        from http.client import HTTPConnection

        from operating.tests import test_mcp_http_host as door
        from operator_ui.case_workspace import CaseWorkspace
        from operator_ui.server import make_server

        runtime = self.runtime(require_employee=False)
        session = si.fixture_session("plx", ["staff:operations"], channel=si.CHANNEL_CONSOLE)
        server = make_server(case_workspace=CaseWorkspace(runtime, session.actor),
                             session=session)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(bearer):
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            conn.request("GET", "/api/session", headers={"Authorization": "Bearer " + bearer})
            response = conn.getresponse()
            value = response.status, json.loads(response.read())
            conn.close()
            return value

        status, refused = get(door.token(door.KEY, door.claims(OID_A)))
        self.assertEqual((status, refused["detail"]), (401, "access_token_required"))
        status, body = get(server.operator_access_token)
        self.assertEqual((status, body["identity"]["employee"], body["identity"]["proof"]),
                         (200, None, si.PROOF_FIXTURE))
        with self.assertRaises(ValueError):   # no journal: there is no employee to record
            make_server(case_workspace=CaseWorkspace(runtime, session.actor), session=session,
                        journal=mock_journal())

    def mint(self, out, *, state=None, reader=None, secret=SECRET, extra=()):
        """``console_ticket mint`` as the host runs it: ``(exit code, printed JSON)``."""
        import contextlib
        import io

        from operator_ui import console_ticket

        reader = {"uid": "cmuser1", "email": EMAIL_A, "entraTenantId": TID,
                  "entraObjectId": OID_A} if reader is None else reader
        environ = {"PLX_EMPLOYEE_READER": reader if isinstance(reader, str) else json.dumps(reader)}
        if secret is not None:
            environ[si.SESSION_TICKET_SECRET_ENV] = secret
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = console_ticket.main(
                ["mint", "--state-dir", str(state or self.dir / "state"), "--employee-policy",
                 str(self.policy_path), "--portal-access", str(self.portal_path),
                 "--out", str(out), *extra], environ=environ)
        return code, printed.getvalue()

    def test_a_minted_console_ticket_opens_one_employee_session_until_revoked(self):
        import contextlib
        import io

        from operator_ui import console_ticket
        from operator_ui.session_journal import SessionJournal

        request = self.serve(guarded=True)
        out = self.dir / "console-url"
        code, printed = self.mint(out, extra=["--ttl-minutes", "30"])
        self.assertEqual(code, 0, printed)
        url = out.read_text("ascii").strip()
        ticket = url.split("#access=", 1)[1]
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(ticket, printed)   # the bearer credential is never printed
        minted = json.loads(printed)
        self.assertEqual((minted["status"], minted["employee"]), ("minted", "employee:" + OID_A))
        self.assertEqual(si.verify_session_ticket(ticket, SECRET)["exp"]
                         - si.verify_session_ticket(ticket, SECRET)["iat"], 1800)
        status, session = request("/api/session", ticket)
        self.assertEqual((status, session["identity"]["employee"],
                          session["identity"]["author_kind"]),
                         (200, "employee:" + OID_A, "human"))
        status, listed = request("/api/cases", ticket)
        self.assertEqual({item["case_id"] for item in listed["cases"]},
                         {self.only_a, self.shared})
        status, case_view = request(f"/api/case/{self.shared}", ticket)
        self.assertEqual(status, 200, case_view)
        # Opening an original's bytes is journaled like any answered request (the store
        # is stubbed here: its audience checks are tests.test_case_workspace_http's).
        from unittest import mock

        from operator_ui.case_workspace import CaseWorkspace
        with mock.patch.object(CaseWorkspace, "original_pdf",
                               return_value={"content": b"%PDF-1.4 stub"}):
            self.assertEqual(request("/api/case-original-pdf?handle=original%3A%2F%2Forig-"
                                     + "0" * 32, ticket, raw=True), (200, b"%PDF-1.4 stub"))
        # The file is never overwritten; a second mint for the same file is refused.
        self.assertEqual(json.loads(self.mint(out)[1])["reason"], "out_exists")

        revoked = io.StringIO()
        with contextlib.redirect_stdout(revoked):
            self.assertEqual(console_ticket.main(
                ["revoke", "--state-dir", str(self.dir / "state"), "--ticket-file", str(out)],
                environ={si.SESSION_TICKET_SECRET_ENV: SECRET}), 0)
        printed = json.loads(revoked.getvalue())
        self.assertEqual((printed["status"], printed["session"]),
                         ("revoked", minted["session"]))
        self.assertFalse(out.exists())
        status, body = request("/api/cases", ticket)
        self.assertEqual((status, body["detail"]), (401, "session_ticket_revoked"))

        # The journal holds the session as counts and enums: no case, e-mail or object id.
        request.stop()
        summary = SessionJournal.for_state_dir(self.dir / "state").summary()
        recorded = summary["sessions"][minted["session"]]
        self.assertEqual((recorded["requests"], recorded["by_route"]),
                         (4, {"GET /api/session 200": 1, "GET /api/cases 200": 1,
                              "GET /api/case/ 200": 1, "GET /api/case-original-pdf 200": 1}))
        # A case view is journaled as the items its panels showed, never a case id.
        self.assertEqual(recorded["panel_items"], {name: len(case_view[name]) for name in (
            "timeline", "originals", "gaps", "conflicts")})
        self.assertEqual(summary["unauthenticated"], {"401:session_ticket_revoked": 1})
        dumped = json.dumps(summary)
        for value in (self.only_a, self.shared, EMAIL_A, OID_A, ticket):
            self.assertNotIn(value, dumped)
        # An object id's type is journaled only when it names a schema object type, and
        # each write keeps only the newest KEEP_ROWS rows.
        import operator_ui.session_journal as journal_module
        probe = SessionJournal(self.dir / "probe" / "journal.sqlite")
        who = type("Session", (), {"session_ref": "a" * 64, "employee": type(
            "Employee", (), {"principal": "employee:" + OID_A})()})()
        with mock.patch.object(journal_module, "KEEP_ROWS", 3):
            for object_id in ("PurchaseOrder:PO-1", "purchaseorder:PO-1", "Ricardo Smith:x",
                              "PurchaseOrder:PO-2", "NotAType:PO-3"):
                probe.record(method="GET", path="/api/object", status=200, session=who,
                             object_id=object_id, value={"status": "read", "gaps": []})
        opened = probe.summary()["sessions"]["a" * 64]
        self.assertEqual((opened["requests"], opened["objects_opened"]),
                         (3, {"other": 2, "PurchaseOrder": 1}))
        self.assertNotIn("Ricardo", json.dumps(probe.summary()))

    def test_console_ticket_mint_refuses_what_the_console_would_refuse(self):
        out = self.dir / "console-url"
        fixture_state = self.dir / "fixture-state"
        si.bind_state_dir(fixture_state, fixture=True)
        si.bind_state_dir(self.dir / "state", fixture=False)
        a = {"uid": "cmuser1", "email": EMAIL_A, "entraTenantId": TID, "entraObjectId": OID_A}
        for kwargs, reason in (
                ({"reader": dict(a, entraObjectId=OID_GONE)}, "portal_access_unavailable"),
                ({"reader": dict(a, email=EMAIL_B)}, "session_ticket_identity_mismatch"),
                ({"reader": dict(a, entraTenantId=OTHER_TID)}, "tenant_mismatch"),
                ({"reader": {"uid": "cmuser1", "email": EMAIL_A}}, "reader_identity_invalid"),
                ({"reader": "not json"}, "reader_identity_invalid"),
                ({"reader": ""}, "no_employee_identity"),
                ({"secret": None}, "session_secret_unavailable"),
                ({"secret": "short"}, "session_secret_unavailable"),
                ({"state": fixture_state}, "state_not_live:fixture"),
                ({"state": self.dir / "never-written"}, "state_not_live:new")):
            code, printed = self.mint(out, **kwargs)
            self.assertEqual((code, json.loads(printed)), (2, {"status": "refused",
                                                                "reason": reason}), kwargs)
            self.assertFalse(out.exists(), kwargs)
            self.assertEqual(self.audit_rows(), [], kwargs)
        self.assertFalse((self.dir / "never-written").exists())

    def audit_rows(self, state=None):
        path = (state or self.dir / "state") / "console-ticket-audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def test_a_mint_and_its_revoke_each_leave_one_audit_row_without_the_ticket(self):
        import contextlib
        import io

        from operator_ui import console_ticket

        si.bind_state_dir(self.dir / "state", fixture=False)
        out = self.dir / "console-url"
        code, printed = self.mint(out, extra=["--ttl-minutes", "30"])
        self.assertEqual(code, 0, printed)
        minted = json.loads(printed)
        ticket = out.read_text("ascii").strip().split("#access=", 1)[1]
        audit = self.dir / "state" / "console-ticket-audit.jsonl"
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), {"event", "at", "session_ref", "employee", "expires_at",
                                    "ttl", "os_user", "minted_by"})
        self.assertEqual((row["event"], row["session_ref"], row["employee"], row["minted_by"],
                          row["expires_at"], row["ttl"]),
                         ("mint", minted["session"], "employee:" + OID_A, "host",
                          minted["expires_at"], 1800))
        self.assertTrue(row["os_user"])
        claims = si.verify_session_ticket(ticket, SECRET)
        raw = audit.read_text("utf-8")
        for value in (ticket, ticket.split(".")[1], ticket.split(".")[2], claims["jti"],
                      EMAIL_A, SECRET, "@"):
            self.assertNotIn(value, raw)
        # A refused mint (the URL file exists) appends nothing.
        self.assertEqual(json.loads(self.mint(out)[1])["reason"], "out_exists")
        self.assertEqual(len(self.audit_rows()), 1)

        revoked = io.StringIO()
        with contextlib.redirect_stdout(revoked):
            self.assertEqual(console_ticket.main(
                ["revoke", "--state-dir", str(self.dir / "state"), "--ticket-file", str(out)],
                environ={si.SESSION_TICKET_SECRET_ENV: SECRET}), 0)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("minted_by", rows[1])
        self.assertEqual((rows[1]["event"], rows[1]["session_ref"], rows[1]["employee"],
                          rows[1]["revoked_by"], rows[1]["expires_at"], rows[1]["ttl"]),
                         ("revoke", minted["session"], "employee:" + OID_A, "host",
                          minted["expires_at"], 1800))
        raw = audit.read_text("utf-8")
        for value in (ticket, claims["jti"], EMAIL_A, SECRET, "@"):
            self.assertNotIn(value, raw)
        # A revoke whose row cannot be written still revokes, and says so (exit 2).
        code, printed = self.mint(out)
        self.assertEqual(code, 0, printed)
        second = json.loads(printed)
        audit.rename(self.dir / "kept-audit.jsonl")
        audit.mkdir()
        revoked = io.StringIO()
        with contextlib.redirect_stdout(revoked):
            self.assertEqual(console_ticket.main(
                ["revoke", "--state-dir", str(self.dir / "state"), "--ticket-file", str(out)],
                environ={si.SESSION_TICKET_SECRET_ENV: SECRET}), 2)
        self.assertEqual(json.loads(revoked.getvalue())["audit"], "audit_unwritable")
        self.assertEqual(json.loads(revoked.getvalue())["session"], second["session"])
        self.assertFalse(out.exists())

    def test_mint_refuses_with_audit_unwritable_and_writes_no_url(self):
        si.bind_state_dir(self.dir / "state", fixture=False)
        audit = self.dir / "state" / "console-ticket-audit.jsonl"
        out = self.dir / "console-url"
        elsewhere = self.dir / "elsewhere.jsonl"

        def make_directory():
            audit.mkdir()

        def make_symlink():   # an audit path that is a link is never followed
            elsewhere.write_text("", "utf-8")
            audit.symlink_to(elsewhere)

        def make_read_only():
            audit.write_text("", "utf-8")
            audit.chmod(0o400)
        cases = [make_directory, make_symlink]
        if os.geteuid() != 0:   # root writes through file modes
            cases.append(make_read_only)
        for make in cases:
            make()
            code, printed = self.mint(out)
            self.assertEqual((code, json.loads(printed)),
                             (2, {"status": "refused", "reason": "audit_unwritable"}),
                             make.__name__)
            self.assertFalse(out.exists(), make.__name__)
            if audit.is_dir() and not audit.is_symlink():
                audit.rmdir()
            else:
                audit.chmod(0o600) if not audit.is_symlink() else None
                audit.unlink()
        self.assertEqual(elsewhere.read_text("utf-8"), "")
        # A URL that cannot be written after its row (a full disk) leaves no URL file, so
        # the next mint is not blocked by out_exists.
        from unittest import mock

        from operator_ui import console_ticket

        real_fdopen = console_ticket.os.fdopen
        opened = []

        def no_handle(descriptor, *args, **kwargs):   # the descriptor is closed by mint
            opened.append(descriptor)
            raise OSError(24, "Too many open files")

        class FullDisk:
            def __init__(self, descriptor, *args, **kwargs):
                self.handle = real_fdopen(descriptor, *args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def write(self, text):
                raise OSError(28, "No space left on device")
        for broken in (FullDisk, no_handle):
            with mock.patch.object(console_ticket.os, "fdopen", broken):
                code, printed = self.mint(out)
            self.assertEqual((code, json.loads(printed)),
                             (2, {"status": "refused", "reason": "out_unwritable"}))
            self.assertFalse(out.exists())
        with self.assertRaises(OSError):   # mint closed the descriptor fdopen refused
            os.fstat(opened[0])
        self.assertEqual(self.mint(out)[0], 0)
        out.unlink()   # the read-only case below needs a free --out path
        # A state directory that is read-only as a whole (a non-root host user).
        if os.geteuid() != 0:
            state = self.dir / "state"
            # An audit file already there stays appendable; the case is a first mint.
            (state / "console-ticket-audit.jsonl").unlink(missing_ok=True)
            state.chmod(0o500)
            try:
                code, printed = self.mint(out)
            finally:
                state.chmod(0o700)
            self.assertEqual(json.loads(printed)["reason"], "audit_unwritable")
            self.assertFalse(out.exists())

    def test_session_journal_summary_reports_who_minted_each_session(self):
        import contextlib
        import io

        from unittest import mock

        from operator_ui import session_journal
        from operator_ui.session_journal import SessionJournal

        si.bind_state_dir(self.dir / "state", fixture=False)
        code, printed = self.mint(self.dir / "console-url")
        self.assertEqual(code, 0, printed)
        minted = json.loads(printed)
        journal = SessionJournal.for_state_dir(self.dir / "state")
        for ref in (minted["session"], "b" * 64):
            who = type("Session", (), {"session_ref": ref, "employee": type(
                "Employee", (), {"principal": "employee:" + OID_A})()})()
            journal.record(method="GET", path="/api/session", status=200, session=who)
        # A malformed or foreign audit line never names a minter.
        with open(self.dir / "state" / "console-ticket-audit.jsonl", "a",
                  encoding="utf-8") as handle:
            handle.write("not json\n" + json.dumps({"event": "mint", "session_ref": "b" * 64,
                                                    "minted_by": "portal; drop"}) + "\n")
        sessions = journal.summary()["sessions"]
        self.assertEqual(sessions[minted["session"]]["minted_by"], "host")
        self.assertEqual(sessions["b" * 64]["minted_by"], "other")
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            self.assertEqual(session_journal.main(
                ["summary", "--state-dir", str(self.dir / "state"),
                 "--session", minted["session"]]), 0)
        self.assertEqual(json.loads(shown.getvalue())["sessions"][minted["session"]]
                         ["minted_by"], "host")
        probe = SessionJournal(self.dir / "probe" / "journal.sqlite")
        probe.record(method="GET", path="/api/session", status=200, session=type(
            "Session", (), {"session_ref": "c" * 64, "employee": None})())
        self.assertEqual(probe.summary()["sessions"]["c" * 64]["minted_by"], "unrecorded")
        # Past MAX_AUDIT_BYTES the newest rows are read (the oldest are dropped).
        with mock.patch.object(session_journal, "MAX_AUDIT_BYTES", 300):
            with open(self.dir / "state" / "console-ticket-audit.jsonl", "a",
                      encoding="utf-8") as handle:
                handle.write(("x" * 120 + "\n") * 4 + json.dumps(
                    {"event": "mint", "session_ref": "b" * 64, "minted_by": "host"}) + "\n")
            sessions = journal.summary()["sessions"]
        self.assertEqual(sessions["b" * 64]["minted_by"], "host")
        self.assertEqual(sessions[minted["session"]]["minted_by"], "unrecorded")
        # A window that starts exactly on a row keeps that row whole.
        row = json.dumps({"event": "mint", "session_ref": "d" * 64, "minted_by": "host"})
        with open(self.dir / "state" / "console-ticket-audit.jsonl", "a",
                  encoding="utf-8") as handle:
            handle.write(row + "\n")
        with mock.patch.object(session_journal, "MAX_AUDIT_BYTES", len(row) + 1):
            self.assertEqual(journal.minted_by(), {"d" * 64: "host"})

    def test_a_live_workspace_is_never_bound_to_a_process_token(self):
        from operator_ui.case_workspace import CaseWorkspace
        from operator_ui.server import make_server

        runtime = self.runtime()
        employee = si.employee_session(self.resolver(), channel=si.CHANNEL_CONSOLE,
                                       ticket=self.ticket(), secret=SECRET)
        workspace = CaseWorkspace(runtime, employee.actor)
        for kwargs in ({}, {"session": employee},
                       {"session": si.fixture_session("plx", ["staff:operations"],
                                                      channel=si.CHANNEL_MCP)}):
            with self.assertRaises(ValueError):
                make_server(case_workspace=workspace, **kwargs)


def mock_journal():
    from unittest import mock
    return mock.Mock(spec=["record"])


def _env():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PLX_", "MICROSOFT_GRAPH_", "SHAREPOINT_", "BC_",
                                  "MC_", "VMC_", "BRAIN_", "AWS_", "AZURE_"))}
    env["PYTHONPATH"] = str(ROOT)
    return env


class McpHostTests(Fixture):
    """``python3 -m operating.mcp_server``: no default principal, fixture only by flag."""

    MESSAGES = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "plx.case_get", "arguments": {}}}]

    def host(self, *arguments, env=None, state="state"):
        return subprocess.run(
            [sys.executable, "-m", "operating.mcp_server", "--state-dir", str(self.dir / state),
             "--source-mirror-audience", str(AUDIENCE), *arguments], cwd=ROOT,
            env={**_env(), **(env or {})}, capture_output=True, text=True, timeout=120,
            input="".join(json.dumps(m) + "\n" for m in self.MESSAGES))

    def test_live_host_requires_a_verified_employee(self):
        identity = ["--employee-policy", str(self.policy_path),
                    "--portal-access", str(self.portal_path)]
        dev = [*identity, "--dev-configured-employee", "--employee-oid"]
        refused = {
            (): "exactly one of --session-ticket-env or --dev-configured-employee",
            ("--principal", "staff:operations"): "--principal and --clearance name a fixture",
            # A command-line employee is never a live identity: dev flag only.
            (*identity, "--employee-oid", OID_A): "--employee-oid is development only",
            ("--dev-configured-employee", "--employee-oid", OID_A): "exactly one of",
            (*dev, OID_GONE): "portal_access_unavailable",
            ("--employee-policy", str(self.policy_path), "--dev-configured-employee",
             "--employee-oid", OID_A): "portal_access_unavailable",  # no Portal input
            (*dev, OID_A, "--tenant", "other"): "--tenant must equal",
            # ... and never reads a real source or a capture.
            (*dev, OID_A, "--live-source-audience", "x"):
                "--dev-configured-employee cannot be combined with --live-source-audience",
            (*dev, OID_A, "--ingest-sources", "x"):
                "--dev-configured-employee cannot be combined with --ingest-sources",
            ("--fixture", "--principal", "staff:operations", "--live-source-audience", "x"):
                "--fixture cannot be combined",
            ("--fixture",): "--fixture needs --principal",
        }
        for arguments, message in refused.items():
            done = self.host(*arguments)
            self.assertEqual(done.returncode, 2, (arguments, done.stderr[-300:]))
            self.assertIn(message, done.stderr, arguments)
            self.assertEqual(done.stdout, "")
        done = self.host(*dev, OID_A, state="dev")
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        reply = json.loads(done.stdout.splitlines()[1])["result"]
        self.assertFalse(reply["isError"], reply)
        # State a dev session wrote is never opened by a fixture or an authenticated one.
        done = self.host("--fixture", "--principal", "staff:operations", state="dev")
        self.assertEqual(done.returncode, 2)
        self.assertIn("fixture_session_on_dev_state", done.stderr)
        agent = {"T": self.ticket(author=si.AUTHOR_AGENT), si.SESSION_TICKET_SECRET_ENV: SECRET}
        done = self.host(*identity, "--session-ticket-env", "T", env=agent, state="dev")
        self.assertEqual(done.returncode, 2)
        self.assertIn("live_session_on_dev_state", done.stderr)
        # An authenticated session's (live) state refuses fixture and dev sessions.
        done = self.host(*identity, "--session-ticket-env", "T", env=agent)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        for arguments, reason in ((("--fixture", "--principal", "staff:operations"),
                                   "fixture_session_on_live_state"),
                                  ((*dev, OID_A), "dev_session_on_live_state")):
            done = self.host(*arguments)
            self.assertEqual(done.returncode, 2)
            self.assertIn(reason, done.stderr)

    def test_ticket_identity_needs_an_agent_ticket_and_the_secret(self):
        identity = ["--employee-policy", str(self.policy_path),
                    "--portal-access", str(self.portal_path), "--session-ticket-env", "T"]
        agent = self.ticket(author=si.AUTHOR_AGENT)
        cases = {"session_secret_unavailable": {"T": agent},
                 "session_ticket_author_mismatch": {"T": self.ticket(),
                                                    si.SESSION_TICKET_SECRET_ENV: SECRET},
                 "session_ticket_invalid": {si.SESSION_TICKET_SECRET_ENV: SECRET}}
        for reason, env in cases.items():
            done = self.host(*identity, env=env)
            self.assertEqual(done.returncode, 2, done.stderr[-300:])
            self.assertIn(reason, done.stderr)
        done = self.host(*identity, env={"T": agent, si.SESSION_TICKET_SECRET_ENV: SECRET})
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertFalse(json.loads(done.stdout.splitlines()[1])["result"]["isError"])

    def test_fixture_host_works_only_with_the_flag(self):
        done = self.host("--fixture", "--principal", "staff:operations", state="demo")
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertFalse(json.loads(done.stdout.splitlines()[1])["result"]["isError"])
        done = self.host("--principal", "staff:operations", state="demo")
        self.assertEqual(done.returncode, 2)
        done = self.host("--employee-policy", str(self.policy_path), "--portal-access",
                         str(self.portal_path), "--dev-configured-employee", "--employee-oid",
                         OID_A, state="demo")
        self.assertEqual(done.returncode, 2)
        self.assertIn("dev_session_on_fixture_state", done.stderr)


class CommittedEntraResourceTests(unittest.TestCase):
    """The console's committed ``plx-entra-resource/v1`` file (operating/entra_resource.json):
    identifiers only, each cited from committed repo content or a typed unset naming its
    later ask. Isolated-fixture: tokens are signed here with a generated key."""

    PATH = ROOT / "operating" / "entra_resource.json"

    def test_the_committed_file_passes_the_schema_check_and_names_what_is_unset(self):
        from operating import oidc_auth
        checked = oidc_auth.check_resource_file(self.PATH)
        self.assertEqual(checked, {
            "schema": oidc_auth.RESOURCE_SCHEMA,
            "unset": ["resource", "jwks_file"],
            "later_asks": ["console-entra-app-registration"]})
        document = json.loads(self.PATH.read_text("utf-8"))
        # Every committed value is cited, and the cited files state it.
        cited = set(oidc_auth.RESOURCE_FIELDS) & set(document) - set(checked["unset"])
        self.assertEqual(set(document["sources"]), cited)
        for field in cited:
            paths = re.findall(r"[\w./-]+\.md", document["sources"][field])
            self.assertTrue(paths, field)
            for path in paths:
                self.assertIn(document[field], (ROOT / path).read_text("utf-8"), path)
        # An unset file never verifies a token: the console and the MCP host refuse it,
        # naming the fields (an injected key set drops only jwks_file).
        with self.assertRaises(oidc_auth.EntraResourceUnset) as raised:
            oidc_auth.EntraResource.from_file(self.PATH)
        self.assertEqual(list(raised.exception.unset), checked["unset"])
        with self.assertRaises(oidc_auth.EntraResourceUnset) as raised:
            oidc_auth.EntraResource.from_file(self.PATH, jwks=oidc_auth.StaticJwks({"keys": []}))
        self.assertNotIn("jwks_file", raised.exception.unset)
        # An unexpected shape fails closed as invalid, never as unset.
        with tempfile.TemporaryDirectory() as temp:
            for name, change, reason in (
                    ("null", {"client_id": None}, "entra_resource_client_id_invalid"),
                    ("empty ask", {"required_scope": {"unset": ""}},
                     "entra_resource_scope_invalid"),
                    ("unset with a value", {"resource": {"unset": "console-entra-app-registration",
                                                        "value": "https://x/"}},
                     "entra_resource_uri_invalid"),
                    ("client id not a GUID", {"client_id": "plx-console"},
                     "entra_resource_client_id_invalid"),
                    ("app id uri scheme", {"app_id_uri": "http://plx"},
                     "entra_resource_app_id_uri_invalid"),
                    ("scope of another audience", {"required_scope": "api://other/Workbench.Use"},
                     "entra_resource_scope_invalid"),
                    ("tenant unset as a string", {"tenant_id": "unset"},
                     "entra_resource_tenant_invalid"),
                    ("unknown field", {"client_secret": "x"}, "entra_resource_invalid"),
                    ("uncited source", {"sources": {"secret": "docs/reference/permissions.md"}},
                     "entra_resource_sources_invalid")):
                with self.subTest(name):
                    path = Path(temp) / "resource.json"
                    path.write_text(json.dumps({**document, **change}), "utf-8")
                    with self.assertRaises(ValueError) as raised:
                        oidc_auth.check_resource_file(path)
                    self.assertNotIsInstance(raised.exception, oidc_auth.EntraResourceUnset)
                    self.assertEqual(str(raised.exception), reason)

    def test_the_committed_registration_verifies_its_own_tokens_and_refuses_others(self):
        """The committed tenant, client id, Application ID URI and scope with fixture values
        for the two unset fields: a token for the client id or the Application ID URI with
        ``scp`` Workbench.Use verifies; another audience, tenant or scope is refused with its
        typed reason."""
        from operating.oidc_auth import EntraResource, EntraTokenRefused, verify_access_token
        from operating.tests import test_mcp_http_host as door
        document = json.loads(self.PATH.read_text("utf-8"))
        tenant, client, uri = (document[k] for k in ("tenant_id", "client_id", "app_id_uri"))
        scp = "openid Workbench.Use"
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "jwks.json").write_text(json.dumps(door.jwks(door.KEY)), "utf-8")
            path = Path(temp) / "entra_resource.json"
            path.write_text(json.dumps({**document, "resource": "http://127.0.0.1:8765/",
                                        "jwks_file": "jwks.json"}), "utf-8")
            resource = EntraResource.from_file(path)
            self.assertEqual((resource.tenant_id, resource.audiences, resource.scope_claim),
                             (tenant, frozenset({client, uri}), "Workbench.Use"))
            self.assertEqual(resource.metadata()["scopes_supported"],
                             [document["required_scope"]])
            for aud in (client, uri):
                claims = verify_access_token(door.token(door.KEY, door.claims(
                    OID_A, tenant=tenant, aud=aud, scp=scp)), resource)
                self.assertEqual((claims["tid"], claims["aud"]), (tenant, aud))
            for name, claims, refusal in (
                    ("another audience", door.claims(OID_A, tenant=tenant, scp=scp),
                     ("token_audience_mismatch", "invalid_token")),
                    ("another tenant", door.claims(OID_A, tenant=OTHER_TID, aud=client, scp=scp),
                     ("token_tenant_mismatch", "invalid_token")),
                    ("the test tenant", door.claims(OID_A, tenant=TID, aud=client, scp=scp),
                     ("token_tenant_mismatch", "invalid_token")),
                    ("issuer of another tenant", door.claims(
                        OID_A, tenant=tenant, aud=client, scp=scp,
                        iss=f"https://login.microsoftonline.com/{OTHER_TID}/v2.0"),
                     ("token_issuer_mismatch", "invalid_token")),
                    ("another scope", door.claims(OID_A, tenant=tenant, aud=client),
                     ("token_scope_missing", "insufficient_scope")),
                    ("the full scope string as scp", door.claims(
                        OID_A, tenant=tenant, aud=client, scp=document["required_scope"]),
                     ("token_scope_missing", "insufficient_scope")),
                    ("app-only", door.claims(OID_A, tenant=tenant, aud=client, scp=None,
                                             roles=["Workbench.Use"], idtyp="app"),
                     ("token_not_delegated", "insufficient_scope"))):
                with self.subTest(name), self.assertRaises(EntraTokenRefused) as refused:
                    verify_access_token(door.token(door.KEY, claims), resource)
                self.assertEqual((refused.exception.reason, refused.exception.error), refusal)


class PortalSnapshotTests(Fixture):
    def test_snapshot_is_parsed_once_per_file_state_and_reread_on_change(self):
        provider = PortalAccessSnapshot(self.portal_path)
        calls = []
        original = provider._load
        provider._load = lambda: calls.append(1) or original()
        for _ in range(3):
            self.assertIsNotNone(provider(EmployeePrincipal(TID, OID_A)))
        self.assertEqual(len(calls), 0)  # the constructor already parsed it
        # A change (here: the row is removed, so the size and mtime move) applies next read.
        self.write_users([self.users[1]])
        self.assertIsNone(provider(EmployeePrincipal(TID, OID_A)))
        self.assertEqual(len(calls), 1)
        # A same-size rewrite is seen too (ctime/mtime in nanoseconds are in the stamp).
        self.write_users([self.users[0]])
        self.assertIsNotNone(provider(EmployeePrincipal(TID, OID_A)))
        self.portal_path.unlink()
        with self.assertRaises(OSError):   # an outage, never a stale hit or "no user"
            provider(EmployeePrincipal(TID, OID_A))

    def test_snapshot_rows_mirror_the_portal_user_and_fail_closed(self):
        read = PortalAccessSnapshot(self.portal_path)(EmployeePrincipal(TID, OID_A))
        self.assertEqual((read.role, read.access_level, read.verified_identity),
                         ("MANAGER", "L3", True))
        self.assertEqual(read.department_ids, frozenset(DEPTS.values()))
        self.assertTrue(read.source.startswith("portal:app_user@fixture#user="))
        provider = PortalAccessSnapshot(self.portal_path)
        self.assertIsNone(provider(EmployeePrincipal(OTHER_TID, OID_A)))  # other tenant
        self.write_users([self.users[0], self.users[0]])  # duplicate identity -> none
        self.assertIsNone(provider(EmployeePrincipal(TID, OID_A)))
        self.portal_path.write_text("{not json", "utf-8")
        with self.assertRaises(ValueError):   # unreadable: an outage, not "no user"
            provider(EmployeePrincipal(TID, OID_A))
        with self.assertRaises(ValueError):
            PortalAccessSnapshot(self.portal_path)
        self.write_users([{**self.users[0], "isAdmin": True}])  # unknown field
        with self.assertRaises(ValueError):
            PortalAccessSnapshot(self.portal_path)
        # An explicit Portal deny is honoured (AccessPolicyGrant DENY).
        self.write_users([user_row(OID_A, EMAIL_A, "MANAGER", policyDenies=[
            {"id": "d1", "capability": "VIEW_OPERATIONS", "departmentId": DEPTS["qa"]}])])
        entitlement = self.resolver().resolve(EmployeePrincipal(TID, OID_A))
        self.assertNotIn("staff:quality", entitlement.require_actor().principals)
        self.assertIn("staff:operations", entitlement.require_actor().principals)


if __name__ == "__main__":
    unittest.main()
