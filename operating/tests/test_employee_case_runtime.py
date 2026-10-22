"""Case reads consult the employee resolver when the actor is an employee.

Evidence label: ``injected-transport`` for the SharePoint reads (the real
GET-only reader over the in-process ``FakeSender``) and the Mission Control capture (the
real capture over the fake MC of ``integrations.tests.test_mc_sources``), and
``isolated-fixture`` for the Portal and Entra inputs. No network, no credential.
"""

from __future__ import annotations

import json
import tempfile
import unittest

from integrations import mc_sources
from integrations.capture_store import CaptureStore
from integrations.tests import test_mc_sources as tmc
from ontology.core import Actor, ValidationError
from operating import decision_evidence as de
from operating import ontology_tools_investigate as oi
from operating.case_originals import OriginalError
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.cases import CaseNotFound, CitationUnresolvable
from operating.employee_permissions import (MAILBOX_ACCESS_UNDETERMINED, EmployeeAccessRefused,
                                            EmployeePermissionPolicy, EmployeePermissionResolver,
                                            groups_unavailable)
from operating.tests.test_case_live_citations import (BODY, DRIVE, LIVE_AUDIENCE, MAILBOX,
                                                      SP_BASE, VINCE_ID, LiveCitationTestCase,
                                                      _extractor, j)
from operating.tests.test_employee_permissions import (OID, POLICY, QA_GROUP, Calls, member,
                                                       portal_user)

EMPLOYEE = Actor("plx", frozenset({"employee:" + OID}))
RICARDO_PERMS = {"value": [{"roles": ["read"], "grantedToV2": {"user": {
    "email": "ricardo@petralabx.com", "id": "r-1"}}}]}
# The case policy for these tests: the shared case audience and the quality folder.
CASE_POLICY = {**POLICY, "principals": {
    "staff:shared": POLICY["principals"]["staff:operations"],
    "staff:quality": POLICY["principals"]["staff:quality"]}}
# For the mail tests: LIVE_AUDIENCE grants mail:vince@... to staff:vince.
MAIL_POLICY = {**CASE_POLICY, "principals": {
    **CASE_POLICY["principals"], "staff:vince": POLICY["principals"]["staff:operations"]}}


class EmployeeCaseRuntimeTests(LiveCitationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.perms = RICARDO_PERMS
        self.sender.routes[f"{SP_BASE}/items/AO1/permissions"] = lambda url: j(200, self.perms)
        self.portal = Calls(portal_user("MANAGER"))  # L3: confidential, as the drive
        self.groups = Calls(member(QA_GROUP))

    def resolver(self) -> EmployeePermissionResolver:
        return EmployeePermissionResolver(EmployeePermissionPolicy.from_json(CASE_POLICY),
                                          self.portal, self.groups)

    def employee_runtime(self, live: LiveReaders | None = None,
                         resolver: EmployeePermissionResolver | None = None) -> CaseRuntime:
        runtime = CaseRuntime(self.state, client=None, audience=self.mirror_audience,
                              tenant="plx", originals_options={"extractor": _extractor},
                              live=live, employee_permissions=resolver or self.resolver())
        self.addCleanup(runtime.close)
        return runtime

    def live_acl(self, identities=("ricardo@petralabx.com",), required=True) -> LiveReaders:
        return self.live(LIVE_AUDIENCE, identities={"staff:quality": list(identities)},
                         source_acl_required=[f"sharepoint:{DRIVE}:*"] if required else [])

    # ------------------------------------------------------------ wiring

    def test_employee_actor_is_refused_without_a_resolver(self):
        runtime = self.runtime()
        with self.assertRaises(EmployeeAccessRefused):
            runtime.cases.open(EMPLOYEE, "AO-26005B")
        with self.assertRaises(EmployeeAccessRefused):
            runtime.cases.list(EMPLOYEE)
        # A host-configured (non-employee) actor is unaffected.
        case_id = runtime.cases.open(Actor("plx", frozenset({"staff:shared"})), "AO-26005B")
        self.assertTrue(case_id.startswith("case-"))

    def test_case_audience_comes_from_the_resolver(self):
        runtime = self.employee_runtime()
        case_id = runtime.cases.open(EMPLOYEE, "AO-26005B")
        view = runtime.cases.get(EMPLOYEE, case_id)
        self.assertEqual(set(view["audience"]),
                         {"employee:" + OID, "staff:shared", "staff:quality"})
        self.assertEqual(view["classification"], "confidential")  # MANAGER -> L3
        # A presented principal the employee does not hold is not used.
        with self.assertRaises(ValidationError):
            runtime.cases.open(Actor("plx", frozenset({"employee:" + OID, "staff:finance"})),
                               "x", audience=["staff:finance"])

    def test_mcp_case_tools_read_through_the_resolver(self):
        runtime = self.employee_runtime(self.live_acl())
        server = self.server(runtime, frozenset({"employee:" + OID}))
        case_id = self.ok(server, "plx.case_open", {"question": "AO-26005B",
                                                    "audience": ["staff:quality"]})["case_id"]
        self.assertEqual(self.ok(server, "plx.case_get", {"case_id": case_id})["case_id"],
                         case_id)
        self.groups.value = member()
        self.assertTrue(self.call(server, "plx.case_get", {"case_id": case_id})["isError"])

    def test_revocation_hides_the_case_on_the_next_read(self):
        runtime = self.employee_runtime()
        case_id = runtime.cases.open(EMPLOYEE, "AO-26005B", audience=["staff:quality"])
        self.assertEqual(runtime.cases.get(EMPLOYEE, case_id)["case_id"], case_id)
        self.groups.value = member()  # removed from the QA group upstream
        with self.assertRaises(CaseNotFound):
            runtime.cases.get(EMPLOYEE, case_id)
        self.assertEqual(runtime.cases.list(EMPLOYEE), [])
        self.groups.value = member(QA_GROUP)
        self.portal.value = portal_user("CUSTOMER")  # Portal role changed
        with self.assertRaises(EmployeeAccessRefused):
            runtime.cases.get(EMPLOYEE, case_id)

    def test_missing_group_data_never_widens_case_reads(self):
        runtime = self.employee_runtime(resolver=EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json(CASE_POLICY), self.portal, groups_unavailable))
        case_id = runtime.cases.open(EMPLOYEE, "shared question")
        self.assertEqual(set(runtime.cases.get(EMPLOYEE, case_id)["audience"]),
                         {"employee:" + OID, "staff:shared"})
        with self.assertRaises(ValidationError):
            runtime.cases.open(EMPLOYEE, "quality question", audience=["staff:quality"])

    def test_agent_principal_is_refused(self):
        runtime = self.employee_runtime()
        agent = Actor("plx", frozenset({"employee:" + OID, "agent:claude"}))
        with self.assertRaises(EmployeeAccessRefused):
            runtime.cases.list(agent)
        with self.assertRaises(EmployeeAccessRefused):
            runtime.read_original("original://x", agent)

    # ------------------------------------------------------ SharePoint ACL

    def test_employee_reads_an_original_their_item_acl_grants(self):
        runtime = self.employee_runtime(self.live_acl())
        stored = runtime.attach_sharepoint_original(EMPLOYEE, DRIVE, "AO1")
        self.assertTrue(runtime.read_original(stored["citation"], EMPLOYEE)["ok"])
        self.assertTrue(runtime.read_live(stored["source_metadata_citation"], EMPLOYEE)["ok"])

    def test_item_acl_is_intersected_with_the_employees_own_identity(self):
        # The host's static identities let the reader pass (Vince is named for
        # staff:quality), but the item grants Vince, not this employee.
        runtime = self.employee_runtime(self.live_acl(
            identities=("ricardo@petralabx.com", "vince@petralabx.com")))
        stored = runtime.attach_sharepoint_original(EMPLOYEE, DRIVE, "AO1")
        self.perms = {"value": [{"roles": ["read"], "grantedToV2": {"user": {
            "email": "vince@petralabx.com", "id": "v-1"}}}]}
        self.assertFalse(runtime.read_original(stored["citation"], EMPLOYEE)["ok"])
        self.assertFalse(runtime.read_live(stored["source_metadata_citation"], EMPLOYEE)["ok"])
        host = Actor("plx", frozenset({"staff:quality", "staff:shared"}), "restricted")
        self.assertTrue(runtime.read_original(stored["citation"], host)["ok"])
        with self.assertRaises(OriginalError):
            runtime.attach_sharepoint_original(EMPLOYEE, DRIVE, "AO1")

    def test_unknown_item_permissions_deny_the_employee(self):
        # The audience does not require the item ACL, so it is never read
        # ("not_requested"): the host actor may read, the employee may not.
        runtime = self.employee_runtime(self.live_acl(required=False))
        before = len(self.downloads())
        with self.assertRaises(OriginalError) as caught:
            runtime.attach_sharepoint_original(EMPLOYEE, DRIVE, "AO1")
        self.assertEqual(caught.exception.code, "sharepoint_item_unavailable")
        self.assertEqual(len(self.downloads()), before)  # refused before any byte
        listing = runtime.list_live(EMPLOYEE, "sharepoint", drive_id=DRIVE,
                                    path="/AOs/2026 - AOs")
        self.assertEqual(listing["items"], [])
        self.assertEqual(listing["employee_acl_withheld"], 1)
        # Withheld for permission reasons is never "complete" (counts only).
        completeness = listing["completeness"]
        self.assertFalse(completeness["complete"])
        self.assertTrue(completeness["withheld"])
        self.assertEqual((completeness["employee_acl_withheld"], completeness["withheld_count"],
                          completeness["items_returned"]), (1, 1, 0))
        self.assertEqual(listing["employee_acl_withheld_reasons"], {"employee_acl_denied": 1})
        self.assertNotIn("AO1", json.dumps({k: v for k, v in listing.items()
                                            if k != "path"}))
        host = Actor("plx", frozenset({"staff:quality"}), "restricted")
        self.assertTrue(runtime.attach_sharepoint_original(host, DRIVE, "AO1")["original_id"])

    def test_listing_with_nothing_withheld_stays_complete(self):
        runtime = self.employee_runtime(self.live_acl())
        listing = runtime.list_live(EMPLOYEE, "sharepoint", drive_id=DRIVE,
                                    path="/AOs/2026 - AOs")
        self.assertEqual(len(listing["items"]), 1)
        self.assertTrue(listing["completeness"]["complete"])
        self.assertNotIn("employee_acl_withheld", listing)

    # ------------------------------------------------------------- mail

    def mail_runtime(self) -> CaseRuntime:
        return self.employee_runtime(self.live(), EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json(MAIL_POLICY), self.portal, self.groups))

    def message_reads(self) -> list[str]:
        return [c[1] for c in self.sender.calls if "/messages" in c[1]]

    def test_mailbox_permission_is_checked_before_any_message_read(self):
        # Ricardo does not own Vince's mailbox: decided on the directory lookup,
        # labelled undetermined (shared/delegated access is not readable), and
        # no message, metadata or body of it is fetched.
        runtime = self.mail_runtime()
        host = Actor("plx", frozenset({"staff:vince", "staff:shared"}), "restricted")
        metadata = runtime.mail_message(host, MAILBOX, "m1")["metadata_citation"]
        before = len(self.message_reads())
        with self.assertRaises(CitationUnresolvable) as caught:
            runtime.mail_message(EMPLOYEE, MAILBOX, "m1")
        self.assertEqual(str(caught.exception), MAILBOX_ACCESS_UNDETERMINED)
        listing = runtime.list_live(EMPLOYEE, "mail", mailbox=MAILBOX)
        self.assertEqual(listing["items"], [])
        self.assertFalse(listing["completeness"]["complete"])
        self.assertEqual(listing["completeness"]["withheld_reason"], MAILBOX_ACCESS_UNDETERMINED)
        self.assertNotIn(VINCE_ID, json.dumps(listing))
        self.assertNotIn(MAILBOX, json.dumps(listing))
        refused = runtime.read_live(metadata, EMPLOYEE)
        self.assertEqual((refused["ok"], refused["reason"]),
                         (False, MAILBOX_ACCESS_UNDETERMINED))
        self.assertEqual(len(self.message_reads()), before)
        # A mailbox the host cannot see at all still reads as not visible.
        with self.assertRaises(CitationUnresolvable) as caught:
            runtime.mail_message(EMPLOYEE, "nobody@petralabx.com", "m1")
        self.assertEqual(str(caught.exception), "live_citation_unresolvable")

    def test_mailbox_owner_reads_their_own_message_body(self):
        self.portal.value = portal_user("MANAGER", email="Vince@PetraLabX.com")
        runtime = self.mail_runtime()
        read = runtime.mail_message(EMPLOYEE, MAILBOX, "m1")
        self.assertEqual(read["body_text"], BODY)
        self.assertEqual(len(self.message_reads()), 1)  # one message read, with the body
        listing = runtime.list_live(EMPLOYEE, "mail", mailbox=MAILBOX)
        self.assertEqual(len(listing["items"]), 1)
        self.assertTrue(listing["completeness"]["complete"])
        self.assertTrue(runtime.read_live(read["citation"], EMPLOYEE)["ok"])

    def test_unreadable_item_permissions_deny_the_stored_original(self):
        runtime = self.employee_runtime(self.live_acl())
        stored = runtime.attach_sharepoint_original(EMPLOYEE, DRIVE, "AO1")
        self.sender.routes[f"{SP_BASE}/items/AO1/permissions"] = j(403, {})
        self.assertFalse(runtime.read_original(stored["citation"], EMPLOYEE)["ok"])

    # ---------------------------------------------------------- Mission Control

    def mc_gate(self) -> str:
        """Capture the fake MC into this host's state directory; the citation of gate
        apg_one (TASK-2, project PRJ-OPS: approved by vince 2026-10-01 12:00Z)."""
        path = mc_sources.store_path(self.state)
        path.parent.mkdir(parents=True)
        with CaptureStore(path, tmc.audience(), clock=tmc.Clock()) as store:
            mc_sources.McSourcesCapture(store, tmc.mc_client(tmc.FakeMc())).capture(
                mc_sources.CaptureSpec("ops", ()))
            gates = store.records(tmc.OPS, mc_sources.KIND,
                                  mc_sources.unit_of("ops", "approvals"))["items"]
        [handle] = [g["citation"] for g in gates if g["key"]["id"] == "apg_one"]
        return handle

    def decision(self, runtime: CaseRuntime, handle: str, actor: Actor) -> dict:
        """Decision evidence (F11) over the gate, its time re-read through the host's own
        resolver (``citation_rows``: ``CaseRuntime.claim_row``), never a test resolver."""
        def entry(name, field, value):
            return {"name": name, "kind": "property", "value": value,
                    "object_id": 'MCApproval:mc.approvals:{"id":"ops/apg_one"}',
                    "citation": {"l1_handle": handle, "field_path": f"row.{field}"}}
        evidence = {"at": [entry("at_1", "decidedAt", "2026-10-01T12:00:00Z")],
                    "by": [entry("by_1", "decidedBy", "vince@petrasoap.com")],
                    "status": [entry("status_1", "status", "approved")]}
        return de.plan_run({"records": [{"label": "mc_approval", "time": ["at"],
                                         "actor": ["by"], "outcome": ["status"]}]},
                           evidence, [], items={}, name="decisions", as_of="2026-10-06",
                           resolver=oi.citation_rows(runtime), actor=actor, complete=True,
                           covers=lambda link: True)

    def test_an_mc_approval_decision_is_verified_through_the_host_resolver(self):
        handle = self.mc_gate()
        # The live audience grants the approvals collection and the gate's project to the
        # QA group's principal; the host actor holds neither.
        runtime = self.employee_runtime(self.live({
            **LIVE_AUDIENCE, "mc:ops:approvals": ["staff:quality"],
            "mc:ops:project:PRJ-OPS": ["staff:quality"]}))
        view = runtime.resolve_citation(handle, EMPLOYEE)
        self.assertEqual((view["source"], view["status"], view["row"]["decidedAt"]),
                         ("mc", "current", "2026-10-01T12:00:00Z"))
        out = self.decision(runtime, handle, EMPLOYEE)
        self.assertEqual([(e["name"], e["value"]) for e in out["entries"]],
                         [("decisions_1", "2026-10-01T12:00:00Z"), ("decisions", 1)])
        self.assertNotIn("decision_time_unverified", out["reasons"])
        # Without MC access the same decision is never listed on its unchecked time.
        host = Actor("plx", frozenset({"staff:vince", "staff:shared"}), "restricted")
        for actor, why in ((host, "no MC grant"), (EMPLOYEE, "removed from the QA group")):
            if actor is EMPLOYEE:
                self.groups.value = member()
            with self.assertRaises(CitationUnresolvable, msg=why):
                runtime.resolve_citation(handle, actor)
            self.assertFalse(runtime.citation_status(handle, actor)["ok"], why)
            out = self.decision(runtime, handle, actor)
            self.assertIn("decision_time_unverified", out["reasons"], why)
            self.assertEqual(out["entries"], [], why)  # nothing listed, nothing counted
        # The collection alone is not enough: the gate's project needs its own grant.
        self.groups.value = member(QA_GROUP)
        collection_only = self.employee_runtime(self.live({
            **LIVE_AUDIENCE, "mc:ops:approvals": ["staff:quality"]}))
        with self.assertRaises(CitationUnresolvable):
            collection_only.resolve_citation(handle, EMPLOYEE)

    def test_mc_citations_fail_closed_without_the_hosts_mc_capture(self):
        handle = self.mc_gate()
        granted = {**LIVE_AUDIENCE, "mc:ops:*": ["staff:quality"],
                   "mc:ops:project:PRJ-OPS": ["staff:quality"]}
        runtime = self.employee_runtime(self.live(granted))
        body, _, tail = handle.rpartition("@")
        generation, _, capture_id = tail.rpartition("-")
        for bad in (body, f"{body}@{generation}-{int(capture_id) + 999}",
                    f"{body}@{'0' * len(generation)}-{capture_id}",
                    handle.replace("/apg_one@", "/apg_two@"),
                    handle.replace("mc://ops/approvals/", "mc://ops/nonsense/"), handle + "#x"):
            with self.assertRaises(CitationUnresolvable, msg=bad):
                runtime.resolve_citation(bad, EMPLOYEE)
        # No live audience, or no MC capture in the state directory: source_not_configured.
        with tempfile.TemporaryDirectory() as empty:
            others = (self.employee_runtime(None),
                      CaseRuntime(empty, client=None, audience=self.mirror_audience,
                                  tenant="plx", live=self.live(granted),
                                  employee_permissions=self.resolver()))
            for other in others:
                status = other.citation_status(handle, EMPLOYEE)
                self.assertEqual((status["ok"], status["error"], status["source"]),
                                 (False, "source_not_configured", "mc"))
                with self.assertRaises(CitationUnresolvable):
                    other.resolve_citation(handle, EMPLOYEE)
            others[1].close()


if __name__ == "__main__":
    unittest.main()
