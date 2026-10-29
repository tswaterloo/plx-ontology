"""Goal-2 action path on fixtures (devloop/GOAL2-ACTIONS.md).

Evidence label: ``isolated-fixture`` for dispatch and read-back,
``injected-transport`` for MC: the real ``MissionControlMcpClient`` reads and
its opted-in write door (``call_write_tool``) writes a fake MC through an injected sender,
so ``approval_decision`` and request semantics are the production ones. No
network I/O.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import asdict
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from integrations.live_source_guard import ReadOnlyGuardError
from integrations.mc import MissionControlMcpClient
from integrations.plx_hub_guard import MC_WRITE_TOOLS, check_mcp_body
from integrations.live_source_guard import Secret
from integrations.plx_hub_guard import McConfig
from integrations.tests.test_mc_brain_guard import (MC, MC_KEY, OPERATOR, FakeSender, hub,
                                                     mc_config, ok_json, tool_result)
from ontology.core import Actor
from operating import action_adapters as adapters
from operating import action_path as ap
from operating import goal2_exec as gx
from operating.cases import (CITATION_CONTENT_CHANGED, CaseClosed, CaseNotFound, CaseStore,
                             CitationError)
from integrations.mc import WriteAuthorizationLedger
from operating.action_catalog import ActionCatalog
from operating.goal2_mc_approval import (APPROVER_ROSTER_SCHEMA, ApproverRoster,
                                         load_approver_roster)
from operating.session_identity import CHANNEL_CONSOLE, CHANNEL_MCP, fixture_session

OPS = Actor("plx", frozenset({"ops"}))
OUTSIDER = Actor("plx", frozenset({"finance"}))
VINCE = "vince@petrasoap.com"
STEPHEN = "stephen@petrasoap.com"
# The employee the actions are for (each intent's sub): never a decider here.
EMPLOYEE = "requesting.employee@petrasoap.com"
DECIDER = "mc.owner@petrasoap.com"
# Entra object ids (fixture GUIDs): the roster names each MC member's, a session its subject's.
VINCE_OID = "0000b000-0000-4000-8000-00000000000a"
DECIDER_OID = "0000b000-0000-4000-8000-00000000000d"
RITA, RITA_OID = "rita@petrasoap.com", "0000b000-0000-4000-8000-0000000000aa"
EMPLOYEE_OID = "0000b000-0000-4000-8000-00000000000e"
SESSION = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP, subject_email=EMPLOYEE,
                          subject_oid=EMPLOYEE_OID)
EMPLOYEE_IDS = sorted([EMPLOYEE_OID, EMPLOYEE])   # the subject's identities, as recorded
# A host session that names no subject employee: its intents' subject is unknown.
ANONYMOUS = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP)
# Every catalog action type: what an owner of the hand-built rosters below approves.
ALL_TYPES = tuple(ActionCatalog.builtin().ids())
# VINCE approves the calls; DECIDER, another owner, decides the gates (four-eyes: the
# call's approver never decides its gate).
ROSTER = ApproverRoster({VINCE: {"role": "owner", "action_types": ALL_TYPES, "oid": VINCE_OID},
                         DECIDER: {"role": "owner", "action_types": ALL_TYPES,
                                   "oid": DECIDER_OID},
                         STEPHEN: {"role": "member", "action_types": (), "oid": None}},
                        source_ref="MC mc_users as listed by a person (test)")
REL = "fixture.sales_order"
EFFECT = {"relation": REL, "key": {"id": 3}, "fields": ["po_reference"], "equals": "PO-7788"}
API_ORDER = "api://public.app_sales_order/3@g1-c1"
# Cited evidence holding the proposed customer PO (the catalog traces a Portal
# proposal's value to a field of a cited record).
API_EVIDENCE = "api://public.order_entry/9@g1-c1"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(microseconds=1)
        return self.now


class FakeMc:
    """Task store behind a fake MC (injected transport). The real, opted-in
    ``MissionControlMcpClient`` raises gates through ``call_write_tool``
    (``mc_request_approval``, as PLX_MC ``requestApprovalGate``) and reads them
    through ``mc_get_task``; a test 'human' decides. ``read_only`` is the same
    MC through a client that has not opted in to any write."""

    def __init__(self, clock, ledger_path) -> None:
        self.tasks = {"TASK-9": {"id": "TASK-9", "title": "Customer PO on SO 3",
                                 "stage": "progress", "accountableOwner": VINCE,
                                 "approvalGates": []}}
        # mc_self_check fields as PLX_MC src/lib/mcp/actions.ts:65-76 (top level, no data).
        self.health = {"ok": True, "operator": OPERATOR, "dataSource": "live",
                       "boringGateMet": True, "lastSweep": "2026-10-01T11:55:00Z"}
        self.requests = []
        self.lose_next_answer = False
        self.clock = clock
        tools = {"mc_get_task": self._get_task,
                 "mc_self_check": lambda args: dict(self.health),
                 "mc_request_approval": self._request_approval}

        def respond(method, url, headers, body):
            request = json.loads(body)
            payload = tools[request["params"]["name"]](request["params"].get("arguments", {}))
            return ok_json(tool_result(payload, request_id=request["id"],
                                       is_error="error" in payload))

        def send(method, url, headers, body):
            answer = respond(method, url, headers, body)
            if self.lose_next_answer:
                self.lose_next_answer = False
                raise TimeoutError("answer lost after MC wrote the gate")
            return answer

        self.sender = FakeSender(fn=send)
        transport, _ = hub(self.sender, clock=clock)
        self.ledger = WriteAuthorizationLedger(ledger_path)
        self.client = MissionControlMcpClient(
            mc_config(), hub=transport, write_tools={"mc_request_approval"},
            write_approvers={VINCE}, write_ledger=self.ledger)
        self.read_only = MissionControlMcpClient(mc_config(), hub=transport)

    def _get_task(self, args):
        if args["id"] not in self.tasks:  # restricted or unknown: MC says not_found
            return {"error": {"code": "not_found", "message": f"unknown task {args['id']}"}}
        return {"data": {"taskId": args["id"], "task": copy.deepcopy(self.tasks[args["id"]]),
                         "events": [], "checkouts": []}, "meta": {}}

    def _request_approval(self, args):
        task_id, reason = args["taskId"], args["reason"]
        self.requests.append((task_id, reason))
        gate_id = f"apg_{len(self.requests)}"
        self.tasks[task_id]["approvalGates"].append({
            "id": gate_id, "status": "pending", "requestedBy": OPERATOR, "reason": reason,
            # MC stamps the time it writes the gate (PLX_MC approvals.ts @ 1fba083).
            "requestedAt": self._stamp()})
        return {"data": {"taskId": task_id, "gateId": gate_id, "status": "pending",
                         "inputRequired": True, "link": "https://mc.plxcustomer.io/tasks/x"}}

    def _stamp(self):
        return self.clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # True: a decided gate is never decided again, as real MC (``gate_already_decided``).
    final_decisions = False

    def decide(self, gate_id, status="approved", decided_by=DECIDER, decided_at=None):
        decided_at = decided_at or self._stamp()  # MC stamps the decision time too
        for gate in self.tasks["TASK-9"]["approvalGates"]:
            if gate["id"] == gate_id:
                if self.final_decisions and gate["status"] != "pending":
                    raise ValueError("gate_already_decided")
                gate.update(status=status, decidedBy=decided_by, decidedAt=decided_at)

    def tools_called(self):
        return [json.loads(call[3])["params"]["name"] for call in self.sender.calls]


class ActionPathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fx = ap.FixtureSystem()
        self.handle = self.fx.seed(REL, {"id": 3}, {"id": 3, "po_reference": None})
        # A sales-order row as the mirror serves it: its PO fields read and empty, so the
        # capability guards ActionPath.register enforces can read them.
        self.api_rows = {API_ORDER: {"relation": "public.app_sales_order", "key": {"id": 3},
                                     "row": {"id": 3, "order_number": "SO-3",
                                             "po_reference": None, "custom_fields": None}},
                         API_EVIDENCE: {"relation": "public.order_entry",
                                        "key": {"id": 9},
                                        "row": {"id": 9, "custpocode": "PO-7788"}}}
        self.cases = CaseStore(self.dir / "cases.db", citation_resolver=self.resolve)
        self.clock = Clock()
        self.path = self.new_path()
        # Opened and proposed through the action path with the employee's session: the
        # opener and every proposal's author are recorded subjects (``open_case``).
        self.case_id = self.path.open_case(
            OPS, "Set the customer PO on SO 3 to PO-7788 or PO-9999?", roots=["SO-3"],
            objective_author_kind="human", session=SESSION)
        self.mc = FakeMc(self.clock, self.dir / "mc-write-ledger.jsonl")
        self.registry = ap.DispatcherRegistry()
        self.registry.register(ap.FIXTURE_CAPABILITY, ap.FixtureDispatcher(self.fx))

    def new_path(self, **kwargs):
        options = {"call_approvers": frozenset({VINCE}), "mc_operator_email": OPERATOR,
                   "approver_roster": ROSTER, "clock": self.clock, **kwargs}
        return ap.ActionPath(self.dir / "actions.db", self.cases, **options)

    def resolve(self, handle, actor):
        if "ops" not in actor.principals:
            raise CitationError("not visible")
        if handle in self.api_rows:
            return copy.deepcopy(self.api_rows[handle])
        return self.fx.resolve(handle, actor)

    def propose(self, capability=ap.FIXTURE_CAPABILITY, citation=None, effect=None,
                preconditions=None):
        handle = citation or self.handle
        if preconditions is None:
            preconditions = [{"check": "field_empty", "citation": handle,
                              "field": "po_reference"},
                             "a person at PLX confirms the PO belongs to this order"]
        effect = effect or EFFECT
        citations = [handle]
        if capability == ap.FIXTURE_CAPABILITY:
            payload = {"field": effect["fields"][0], "value": effect["equals"]}
        else:   # the catalog's typed Portal payload, its value held in cited evidence
            payload = {"proposed_customer_po": effect["equals"],
                       "evidence_citation": API_EVIDENCE, "evidence_field": "custpocode"}
            citations.append(API_EVIDENCE)
        receipt = self.path.record_proposal(
            OPS, self.case_id, capability, payload,
            preconditions, citations=citations, expected_effect=effect, session=SESSION)
        return receipt["proposal_id"]

    def approve(self, iid, record, path=None):
        """Prepare the MC approval request, then record the human's per-call approval."""
        path = path or self.path
        if path.get(OPS, iid)["state"] != "gate_prepared":
            path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        return path.approve_call(OPS, iid, "TASK-9", record, session=SESSION)

    def call_record(self, intent, **overrides):
        # Each new human approval gets its own approval second: the MC write
        # ledger (like the action path) treats approver + time + call as one use.
        self.approvals_made = getattr(self, "approvals_made", -1) + 1
        record = {"schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
                  "arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"]},
                  "approved_by": VINCE,
                  "approved_at": f"2026-10-01T11:00:{self.approvals_made:02d}Z",
                  "expires_at": "2026-10-02T11:00:00Z",
                  "evidence_ref": "advisory record 2026-10-01 (test)"}
        record.update(overrides)
        return record

    def authorized(self, capability=ap.FIXTURE_CAPABILITY, citation=None, effect=None,
                   decided_by=DECIDER, past_prepare_guard=False):
        """``past_prepare_guard``: simulate a production intent that got registered and
        an MC gate anyway (e.g. recorded before the registration capability guards and
        the prepare-time guard) to prove dispatch refuses it on its own."""
        proposal = self.propose(capability, citation, effect)
        if past_prepare_guard:
            with mock.patch.object(ap.capability_guards, "check",
                                   return_value={"passed": True, "failures": []}):
                intent = self.path.register(OPS, self.case_id, proposal, session=SESSION)
        else:
            intent = self.path.register(OPS, self.case_id, proposal, session=SESSION)
        iid = intent["intent_id"]
        if past_prepare_guard:
            with mock.patch.object(ap, "production_target_refusal", return_value=None):
                self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        gate_id = self.path.get(OPS, iid)["gate"]["gate_id"]
        self.mc.decide(gate_id, decided_by=decided_by)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        return iid

    def kinds(self, iid):
        return [item["kind"] for item in self.path.get(OPS, iid)["history"]]


class RegistrationGuardTests(ActionPathTestCase):
    """Review wh-2 minor 2: ActionPath.register is the
    one place a registration enforces the catalog's capability guards, so every caller
    (plx.action_propose, goal2_exec, evals/injection) gets them."""

    EFFECT = {"relation": "public.app_sales_order", "key": {"id": 3},
              "fields": ["po_reference"], "equals": "PO-7788"}

    def test_register_refuses_a_proposal_whose_target_already_holds_a_po(self):
        # Recorded straight on the case store, past plx.case_propose's own guard check.
        self.api_rows[API_ORDER]["row"]["po_reference"] = "PO-5501"
        proposal_id = self.propose("portal.sales_order.set_customer_po", API_ORDER,
                                   self.EFFECT, preconditions=[
                                       {"check": "field_equals", "citation": API_ORDER,
                                        "field": "order_number", "value": "SO-3"}])
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(OPS, self.case_id, proposal_id, session=SESSION)
        self.assertIn("capability_guard_failed:target_has_no_po", caught.exception.code)
        self.assertEqual(self.path.list_for_case(OPS, self.case_id)["intents"], [])

    def test_register_uses_the_hosts_reader_when_given(self):
        proposal_id = self.propose("portal.sales_order.set_customer_po", API_ORDER,
                                   self.EFFECT)
        rows = {handle: dict(found, row=dict(found["row"]))
                for handle, found in self.api_rows.items()}
        rows[API_ORDER]["row"]["po_reference"] = "PO-5501"  # the row as it is now
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(OPS, self.case_id, proposal_id, read=rows.get, session=SESSION)
        self.assertIn("target_has_no_po", caught.exception.code)
        # The same proposal on the cited content registers.
        self.assertFalse(self.path.register(OPS, self.case_id, proposal_id,
                                            session=SESSION)["duplicate"])


class LifecycleTests(ActionPathTestCase):
    def test_full_lifecycle_on_fixtures(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.assertEqual(self.path.get(OPS, iid)["state"], "proposed")
        self.assertTrue(intent["idempotency_key"].startswith("wb-"))
        self.assertLessEqual(len(intent["gate_reason"]), 500)
        self.assertIn('set po_reference = "PO-7788"', intent["gate_reason"])

        self.approve(iid, self.call_record(intent))
        self.assertEqual(self.path.get(OPS, iid)["state"], "call_approved")
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.mc.requests, [("TASK-9", intent["gate_reason"])])
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_requested")

        # Pending in MC: recorded, still waiting, nothing dispatchable.
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_requested")
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)

        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        view = self.path.get(OPS, iid)
        self.assertEqual(view["state"], "gate_authorized")
        self.assertEqual((view["decisions"][-1]["decided_by"],
                          view["decisions"][-1]["reason_code"]), (DECIDER, "authorized"))

        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(result["state"], "dispatched")
        self.assertTrue(result["receipt"].startswith("fixture-receipt:"))
        self.assertEqual(self.fx.calls, 1)
        self.assertEqual(self.fx.rows[(REL, '{"id":3}')]["po_reference"], "PO-7788")

        checked = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.assertEqual((checked["state"], checked["outcome"], checked["reconciled_by"]),
                         ("verified", "effect_observed", None))
        self.assertEqual(checked["attribution"], "not_established")
        case = self.cases.get(OPS, self.case_id)
        [verification] = case["verifications"]
        self.assertEqual((verification["outcome"], verification["detail"]["intent_id"],
                          verification["detail"]["evidence"]),
                         ("effect_observed", iid, "isolated-fixture"))
        self.assertEqual(self.kinds(iid), [
            "intent.registered", "gate.prepared", "call.approved", "gate.request_started",
            "gate.requested", "gate.decision",
            "gate.decision", "dispatch.started", "dispatch.finished", "readback.recorded"])
        self.assertTrue(self.path.get(OPS, iid)["chain_ok"])
        # MC: reads, plus exactly the one human-approved mc_request_approval.
        self.assertEqual(set(self.mc.tools_called()),
                         {"mc_self_check", "mc_get_task", "mc_request_approval"})
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), 1)

    def test_refusal_without_approval(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        for step in ("proposed", "call_approved", "gate_requested"):
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.dispatch(OPS, iid, self.registry, session=SESSION)
            self.assertEqual(caught.exception.code, "gate_not_authorized", step)
            if step == "proposed":
                self.approve(iid, self.call_record(intent))
            elif step == "call_approved":
                self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.fx.calls, 0)
        self.assertNotIn("dispatch.started", self.kinds(iid))

    def test_per_call_approval_must_name_the_exact_call_by_a_configured_human(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        bad = [
            ({"tool": "mc_create_task"}, "call_approval_names_another_call"),
            ({"arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"] + " "}},
             "call_approval_arguments_mismatch"),
            ({"arguments": {"taskId": "TASK-8", "reason": intent["gate_reason"]}},
             "call_approval_arguments_mismatch"),
            ({"approved_by": "agent:cos"}, "call_approver_not_allowed"),
            ({"approved_by": "sp_mcp_cursor"}, "call_approver_not_allowed"),
            ({"approved_by": OPERATOR}, "call_approver_not_allowed"),
            ({"expires_at": "2026-10-01T11:30:00Z"}, "call_approval_not_current"),
            ({"approved_at": "2026-10-01T13:00:00Z"}, "call_approval_not_current"),
            ({"evidence_ref": ""}, "call_approval_evidence_missing"),
        ]
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.approve_call(OPS, iid, "TASK-9", self.call_record(intent), session=SESSION)
        self.assertEqual(caught.exception.code, "gate_request_not_prepared")
        prepared = self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client,
                                          session=SESSION)["request"]
        self.assertEqual(prepared["arguments"], {"taskId": "TASK-9",
                                                 "reason": intent["gate_reason"]})
        for overrides, code in bad:
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.approve_call(OPS, iid, "TASK-9", self.call_record(intent, **overrides),
                                       session=SESSION)
            self.assertEqual(caught.exception.code, code, overrides)
        for path, code in ((self.new_path(call_approvers=frozenset(), approver_roster=None),
                            "call_approvers_not_configured"),
                           (self.new_path(mc_operator_email=None), "mc_operator_not_configured"),
                           (self.new_path(call_approvers=frozenset({VINCE, OPERATOR})),
                            "call_approver_not_allowed")):
            record = (self.call_record(intent, approved_by=OPERATOR)
                      if code == "call_approver_not_allowed" else self.call_record(intent))
            with self.assertRaises(ap.ActionPathError) as caught:
                path.approve_call(OPS, iid, "TASK-9", record, session=SESSION)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_prepared")
        # One record authorizes one call: after the request it is consumed.
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        with self.assertRaises(ap.ActionPathError):
            self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(len(self.mc.requests), 1)

    def test_self_approved_or_agent_approved_gate_never_authorizes(self):
        for decided_by, code in ((OPERATOR, "self_decided"), ("COS@petrasoap.com", "self_decided"),
                                 ("agent:cos", "decider_not_human"),
                                 ("sp_mcp_cursor", "decider_not_human")):
            with self.subTest(decided_by=decided_by):
                self.setUp()
                iid = self.authorized(decided_by=decided_by)
                view = self.path.get(OPS, iid)
                self.assertEqual((view["state"], view["decisions"][-1]["reason_code"]),
                                 ("gate_not_authorizing", code))
                with self.assertRaises(ap.ActionPathError):
                    self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual(self.fx.calls, 0)

    def test_rejected_gate_and_foreign_gate_dispatch_nothing(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide("apg_1", status="rejected")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_rejected")
        # A gate someone else raised with our reason is not ours (a fresh approval).
        self.approve(iid, self.call_record(
            intent, evidence_ref="advisory record 2026-10-01 #2 (test)"))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.tasks["TASK-9"]["approvalGates"][-1]["requestedBy"] = "stephen@petrasoap.com"
        self.mc.decide("apg_2")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        view = self.path.get(OPS, iid)
        self.assertEqual((view["state"], view["decisions"][-1]["reason_code"]),
                         ("gate_not_authorizing", "foreign_requester"))
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(self.fx.calls, 0)

    def test_stale_approval_dispatches_nothing(self):
        iid = self.authorized()
        self.fx.rows[(REL, '{"id":3}')]["po_reference"] = "changed by someone"
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("dispatch_refused", "precondition_citation_changed"))
        self.assertEqual(self.fx.calls, 0)
        self.assertNotIn("dispatch.started", self.kinds(iid))

    def test_an_actor_who_cannot_see_the_case_cannot_see_or_move_the_intent(self):
        iid = self.authorized()
        with self.assertRaises(ap.IntentNotFound):
            self.path.get(OUTSIDER, iid)
        with self.assertRaises(ap.IntentNotFound):
            self.path.dispatch(OUTSIDER, iid, self.registry, session=fixture_session(
                "plx", OUTSIDER.principals, channel=CHANNEL_MCP))
        with self.assertRaises(ap.IntentNotFound):
            self.path.get(OPS, "act_unknown")
        self.assertEqual(self.fx.calls, 0)


class ApprovalScopeTests(ActionPathTestCase):
    """Review 790c10e: single use, expiry at send time, decision age, typed MC."""

    def test_a_call_approval_is_used_once_even_after_a_rejected_gate(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        record = self.call_record(intent)
        self.approve(iid, record)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide("apg_1", status="rejected")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_rejected")
        with self.assertRaises(ap.ActionPathError) as caught:
            self.approve(iid, record)
        self.assertEqual(caught.exception.code, "call_approval_already_used")
        # Same human approval (approver, time, evidence) on another intent: refused too.
        other = self.path.register(OPS, self.case_id, self.propose(effect={
            **EFFECT, "equals": "PO-9999"}), session=SESSION)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.approve(other["intent_id"],
                         self.call_record(other, approved_at="2026-10-01T11:00:00+00:00"))
        self.assertEqual(caught.exception.code, "call_approval_already_used")
        # A restarted process sees the consumed approval as well.
        with self.assertRaises(ap.ActionPathError):
            self.new_path().approve_call(OPS, iid, "TASK-9", record, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_prepared")
        self.assertEqual(len(self.mc.requests), 1)
        db = sqlite3.connect(self.dir / "actions.db")
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM consumed_call_approval")

    def test_an_approval_that_expired_before_the_call_sends_nothing(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.clock.now += timedelta(days=30)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        view = self.path.get(OPS, iid)
        self.assertEqual(view["state"], "gate_request_refused")
        self.assertEqual(view["history"][-1]["kind"], "gate.request_refused")
        self.assertEqual(self.mc.requests, [])
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(self.fx.calls, 0)

    def test_a_decision_older_than_the_max_age_dispatches_nothing(self):
        iid = self.authorized()
        self.clock.now += timedelta(hours=25)
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("approval_expired", "decision_too_old"))
        self.assertEqual(self.fx.calls, 0)
        self.assertNotIn("dispatch.started", self.kinds(iid))
        # Re-deciding the same gate cannot help; a new approval and gate can.
        with self.assertRaises(ap.ActionPathError):
            self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        intent = self.path.get(OPS, iid)["intent"]
        self.approve(iid, self.call_record(
            intent, approved_at="2026-10-02T12:00:00Z", expires_at="2026-10-03T12:00:00Z",
            evidence_ref="advisory record 2026-10-02 (test)"))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide("apg_2", decided_at="2026-10-02T13:00:00Z")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        self.assertEqual(self.fx.calls, 1)

    def test_a_refused_dispatch_is_not_retried_on_an_old_decision(self):
        effect = {"relation": "public.app_sales_order", "key": {"id": 3},
                  "fields": ["po_reference"], "equals": "PO-7788"}
        iid = self.authorized("portal.sales_order.set_customer_po", API_ORDER, effect,
                              past_prepare_guard=True)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["state"],
                         "dispatch_refused")
        self.clock.now += timedelta(days=365)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["state"],
                         "approval_expired")

    def test_record_decision_takes_only_the_mc_client_and_rechecks_the_decider(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        reason_sha = hashlib.sha256(intent["gate_reason"].encode()).hexdigest()

        def answer(decided_by, gate_id="apg_1"):
            return {"task_id": "TASK-9", "gate_id": gate_id, "status": "approved",
                    "decided_by": decided_by, "decided_at": "2026-10-01T11:30:00Z",
                    "reason_sha256": reason_sha, "authorizes": True,
                    "reason_code": "authorized"}

        class NotMc:
            def approval_decision(self, *args, **kwargs):
                return answer(VINCE)

        with self.assertRaises(TypeError):
            self.path.record_decision(OPS, iid, NotMc(), session=SESSION)
        for decided_by, code in (("agent:x", "decider_not_human"),
                                 ("sp_mcp_cursor", "decider_not_human"),
                                 ("COS@petrasoap.com", "self_decided")):
            lying = mock.patch.object(MissionControlMcpClient, "approval_decision",
                                      lambda client, task_id, gate_id, _d=decided_by, **k:
                                      answer(_d, gate_id))
            with lying:
                self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
            last = self.path.get(OPS, iid)["decisions"][-1]
            self.assertEqual((last["authorizes"], last["reason_code"]), (False, code))
            self.assertEqual(self.path.get(OPS, iid)["state"], "gate_not_authorizing")
            self.approve(iid, self.call_record(
                intent, evidence_ref=f"advisory record for {code} {decided_by} (test)"))
            self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        other_operator = MissionControlMcpClient(
            McConfig(MC, Secret(MC_KEY), "ops@petrasoap.com"), hub=hub(self.mc.sender)[0])
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.record_decision(OPS, iid, other_operator, session=SESSION)
        self.assertEqual(caught.exception.code, "mc_operator_mismatch")
        self.assertEqual(self.fx.calls, 0)


class UncertaintyAndReplayTests(ActionPathTestCase):
    def test_lost_response_is_dispatched_unknown_and_reconciled_only_by_read_back(self):
        iid = self.authorized()
        self.fx.lose_next_response = True
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"]), ("dispatched_unknown",
                                                               "TimeoutError"))
        self.assertEqual(self.fx.calls, 1)
        again = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((again["state"], again["reconcile_required"]),
                         ("dispatched_unknown", True))
        self.assertEqual(self.fx.calls, 1)  # never re-sent
        checked = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.assertEqual((checked["state"], checked["reconciled_by"]), ("verified", "read_back"))
        self.assertEqual(self.path.get(OPS, iid)["state"], "verified")
        self.assertEqual(self.fx.calls, 1)

    def test_unknown_without_observed_effect_stays_unknown_and_goes_to_a_human(self):
        iid = self.authorized()
        self.fx.fail_next_before_send = True
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["state"],
                         "dispatched_unknown")
        checked = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.assertEqual((checked["state"], checked["outcome"]),
                         ("dispatched_unknown", "effect_not_observed"))
        self.assertEqual(self.path.get(OPS, iid)["state"], "dispatched_unknown")
        self.path.escalate(OPS, iid, "Outcome unknown; please check SO 3 in the owning system.",
                           session=SESSION)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["reconcile_required"], True)
        self.assertEqual(self.fx.calls, 1)
        # The human does it in the owning system; a later read-back sees it.
        self.fx.rows[(REL, '{"id":3}')]["po_reference"] = "PO-7788"
        checked = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.assertEqual((checked["state"], checked["reconciled_by"]), ("verified", "read_back"))
        self.assertEqual(self.fx.calls, 1)

    def test_a_dispatch_answering_after_escalation_is_recorded_and_never_re_sent(self):
        # The intent is escalated while its dispatch is in flight. Before: the late answer
        # overwrote ``escalated`` (a refusal read dispatchable, an acknowledgement read
        # dispatched) and the next dispatch wrote to the owner a second time.
        apply = ap.FixtureSystem.apply
        for late, outcome in (("refused", "not_dispatched"), ("acknowledged", "dispatched")):
            with self.subTest(late=late):
                iid = self.authorized()
                calls = self.fx.calls

                def in_flight(fixture, *args, _iid=iid, _late=late):
                    self.path.escalate(OPS, _iid, "Outcome unknown; a person checks SO 3.",
                                     session=SESSION)
                    if _late == "refused":
                        fixture.calls += 1
                        raise ap.DispatchRefused("fixture_target_absent")
                    return apply(fixture, *args)

                with mock.patch.object(ap.FixtureSystem, "apply", in_flight):
                    self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                                        session=SESSION)["state"],
                                     "escalated")
                again = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((again["state"], again["reconcile_required"]),
                                 ("escalated", True))
                self.assertEqual(self.fx.calls, calls + 1)   # one owner call, never re-sent
                state = self.path.get(OPS, iid)
                self.assertEqual(state["dispatches"][-1]["outcome"], outcome)
                self.assertEqual((state["recovery"][-1]["trigger"],
                                  state["recovery"][-1]["resend_allowed"]),
                                 ("late_dispatch_answer", False))
                step = self.path.view(OPS, iid)["next_safe_step"]
                self.assertEqual((step["step"], step["resend_allowed"]),
                                 ("reconcile_in_owning_system", False))

    def test_process_death_after_the_intent_reads_as_dispatched_unknown(self):
        iid = self.authorized()

        def die(fixture, *args, **kwargs):
            fixture.calls += 1
            raise KeyboardInterrupt  # not an Exception: no outcome gets written

        with mock.patch.object(ap.FixtureSystem, "apply", die), \
                self.assertRaises(KeyboardInterrupt):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        restarted = self.new_path()
        view = restarted.get(OPS, iid)
        self.assertEqual((view["state"], view["dispatches"][-1]["reason"]),
                         ("dispatched_unknown", "no_outcome_recorded"))
        self.assertTrue(restarted.dispatch(OPS, iid, self.registry,
                                           session=SESSION)["reconcile_required"])
        self.assertEqual(self.fx.calls, 1)

    def test_replay_is_idempotent(self):
        pid = self.propose()
        first = self.path.register(OPS, self.case_id, pid, session=SESSION)
        again = self.path.register(OPS, self.case_id, pid, session=SESSION)
        self.assertEqual((first["duplicate"], again["duplicate"]), (False, True))
        self.assertEqual(first["intent_id"], again["intent_id"])
        iid = first["intent_id"]
        self.approve(iid, self.call_record(first))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        done = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        events = len(self.path.get(OPS, iid)["history"])
        replay = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((replay["state"], replay["replayed"], replay["dispatch"]["receipt"]),
                         ("dispatched", True, done["receipt"]))
        self.assertEqual(len(self.path.get(OPS, iid)["history"]), events)
        self.assertEqual(self.fx.calls, 1)
        # The fixture owning system is idempotent on the key as well.
        self.assertEqual(self.fx.apply(first["idempotency_key"], REL, {"id": 3},
                                       "po_reference", "PO-7788"), done["receipt"])
        self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "verified")


class RealSystemsAreClosedTests(ActionPathTestCase):
    def test_real_capability_is_refused_even_with_an_authorizing_gate(self):
        effect = {"relation": "public.app_sales_order", "key": {"id": 3},
                  "fields": ["po_reference"], "equals": "PO-7788"}
        iid = self.authorized("portal.sales_order.set_customer_po", API_ORDER, effect,
                              past_prepare_guard=True)
        view = self.path.get(OPS, iid)
        self.assertEqual((view["state"], view["intent"]["owner_system"]),
                         ("gate_authorized", "portal"))
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"], result["business_system_writes"]),
                         ("dispatch_refused", "production_target_refused", 0))
        self.assertNotIn("dispatch.started", self.kinds(iid))
        self.assertEqual(self.path.get(OPS, iid)["dispatches"][-1]["system"], "portal")
        with self.assertRaises(ap.ActionPathError):
            self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)

    def test_registry_accepts_only_fixture_and_refusing_dispatchers(self):
        registry = ap.DispatcherRegistry()
        for capability in ap.REAL_CAPABILITIES:
            with self.assertRaises(ap.ActionPathError):
                registry.register(capability, ap.FixtureDispatcher(self.fx))
        with self.assertRaises(ap.ActionPathError):
            registry.register("bc.purchase_order.post", ap.FixtureDispatcher(self.fx))

        class Writer(ap.FixtureDispatcher):
            pass

        for impostor in (Writer(self.fx), object()):
            with self.assertRaises(ap.ActionPathError):
                registry.register(ap.FIXTURE_CAPABILITY, impostor)

        class Apply:
            def apply(self, *args):
                return "receipt"

        class SubSystem(ap.FixtureSystem):
            pass

        for writer in (Apply(), SubSystem()):
            with self.assertRaises(ap.ActionPathError):
                ap.FixtureDispatcher(writer)
        refusing, fixture = ap.RefusingDispatcher("portal"), ap.FixtureDispatcher(self.fx)
        for sealed, name in ((refusing, "dispatch"), (refusing, "refusal"),
                             (refusing, "reason"), (fixture, "fixture"), (fixture, "dispatch")):
            with self.assertRaises(AttributeError):
                setattr(sealed, name, lambda *a: None)
        unknown = registry.for_capability("bc.purchase_order.post")
        self.assertIs(type(unknown), ap.RefusingDispatcher)
        self.assertEqual((unknown.system, unknown.reason), ("bc", "capability_not_registered"))
        # The fixture dispatcher refuses a real relation even under its capability.
        intent = ap.ActionIntent("act_x", "c", "P1", ap.FIXTURE_CAPABILITY, "fixture", "0",
                                 {**EFFECT, "relation": "public.app_sales_order"}, "wb-x", "r")
        self.assertEqual(ap.FixtureDispatcher(self.fx).refusal(intent),
                         "fixture_dispatcher_refuses_non_fixture_relation")
        with self.assertRaises(ValueError):
            self.fx.seed("public.app_sales_order", {"id": 1}, {})

    def test_gate_request_through_a_client_not_opted_in_is_refused_before_io(self):
        self.assertIn("mc_request_approval", MC_WRITE_TOOLS)
        with self.assertRaises(ReadOnlyGuardError):
            check_mcp_body({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "mc_request_approval",
                                       "arguments": {"taskId": "TASK-9", "reason": "x"}}})
        self.assertFalse(hasattr(self.mc.client, "request_approval"))
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        reads = len(self.mc.sender.calls)  # prepare_gate's MC reads
        self.path.request_gate(OPS, iid, self.mc.read_only,
                               session=SESSION)  # write_tools empty (default)
        view = self.path.get(OPS, iid)
        self.assertEqual(view["state"], "gate_request_refused")
        self.assertEqual([h["kind"] for h in view["history"]][-2:],
                         ["gate.request_started", "gate.request_refused"])
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((self.mc.sender.calls[reads:], self.fx.calls), ([], 0))
        self.assertNotIn("mc_request_approval", self.mc.tools_called())

    def test_unknown_gate_request_is_reconciled_by_reading_mc_only(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))

        class NotMc:
            def call_write_tool(self, *args, **kwargs):
                raise TimeoutError

        with self.assertRaises(TypeError):  # only the guarded MC client is accepted
            self.path.request_gate(OPS, iid, NotMc(), session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "call_approved")
        self.mc.lose_next_answer = True  # MC writes the gate; the answer is lost
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_request_unknown")
        with self.assertRaises(ap.ActionPathError):
            self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.mc.tasks["TASK-9"]["approvalGates"].append(
            {**self.mc.tasks["TASK-9"]["approvalGates"][0], "id": "apg_dup"})
        self.assertEqual(self.path.reconcile_gate(OPS, iid, self.mc.client,
                                                  session=SESSION)["matching_gates"], 2)
        self.mc.tasks["TASK-9"]["approvalGates"].pop()
        self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        view = self.path.get(OPS, iid)
        self.assertEqual((view["state"], view["gate"]["gate_id"], view["gate"]["reconciled"]),
                         ("gate_requested", "apg_1", True))
        self.assertEqual(len(self.mc.requests), 1)
        # Sent once (retry=False, never re-sent); everything after it was a read.
        called = self.mc.tools_called()
        sent = called.index("mc_request_approval")
        self.assertEqual((set(called[:sent]), set(called[sent + 1:])),
                         ({"mc_self_check", "mc_get_task"}, {"mc_get_task"}))

    def lose_requests_before_mc_writes(self):
        real = self.mc.sender.fn

        def lost_before_write(method, url, headers, body):
            if json.loads(body)["params"]["name"] == "mc_request_approval":
                raise TimeoutError("lost before MC wrote anything")
            return real(method, url, headers, body)

        self.mc.sender.fn = lost_before_write
        return real

    def test_unknown_request_records_f7_and_a_settled_absence_is_recoverable(self):
        """Minor (a): gate_request_unknown with no gate is not a dead end."""
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        real = self.lose_requests_before_mc_writes()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        view = self.path.get(OPS, iid)
        self.assertEqual(view["state"], "gate_request_unknown")
        # The unknown outcome itself carries an F7 record whose one safe step is a read.
        [unknown] = view["recovery"]
        self.assertEqual((unknown["trigger"], unknown["attempt_id"], unknown["task_id"],
                          unknown["resend_allowed"], unknown["new_request_allowed"]),
                         ("gate_request_unknown", f"{iid}-g1", "TASK-9", False, False))
        self.assertIn("reconcile_gate", unknown["next_step"])
        self.assertIn("never re-send", unknown["next_step"])

        # Answered "unknown" (a timeout): MC may still be writing, so right away an
        # absent gate is no evidence and nothing is recorded (review of ef97a62).
        result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["state"], result["matching_gates"], result["reason"],
                          result["send_in_flight"]),
                         ("gate_request_unknown", 0, "send_in_flight", True))
        self.assertEqual(len(self.path.get(OPS, iid)["recovery"]), 1)
        self.assertNotIn("gate.request_lost", self.kinds(iid))
        # Only once the settle window has passed since the send started: lost, once.
        self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
        result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["state"], result["matching_gates"], result["reason"]),
                         ("gate_request_lost", 0, "no_matching_gate"))
        self.path.reconcile_gate(OPS, iid, self.mc.client,
                                 session=SESSION)  # repeat: one record per attempt
        view = self.path.get(OPS, iid)
        self.assertEqual(view["state"], "gate_request_lost")
        absent = [r for r in view["recovery"] if r["trigger"] == "gate_absent_after_request"]
        self.assertEqual(len(absent), 1)
        self.assertEqual((absent[0]["resend_allowed"], absent[0]["new_request_allowed"],
                          absent[0]["reason"]),
                         (False, True, "send_answered_unknown_past_settle_window"))
        self.assertEqual(self.kinds(iid).count("gate.request_lost"), 1)
        with self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)

        # Recoverable: a human approves a *new* request; MC is read again first and
        # the request record names the lost attempt it supersedes. Never a resend
        # of the old approval: that one was consumed.
        self.mc.sender.fn = real
        prepared = self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client,
                                          session=SESSION)["request"]
        self.assertEqual(prepared["supersedes_attempt"], f"{iid}-g1")
        self.assertIn(f"attempt {iid}-g1", prepared["human_preconditions"][-1])
        self.path.approve_call(OPS, iid, "TASK-9",
                               self.call_record(intent, evidence_ref="second approval (test)"),
                                   session=SESSION)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_requested")
        self.assertEqual(self.mc.requests, [("TASK-9", intent["gate_reason"])])
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), 2)
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        self.assertEqual(self.path.read_back(OPS, iid, self.fx.read_back,
                                             session=SESSION)["state"], "verified")
        self.assertTrue(self.path.get(OPS, iid)["chain_ok"])

    def test_a_lost_request_whose_gate_lands_late_is_adopted_not_duplicated(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        real = self.lose_requests_before_mc_writes()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
        self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_request_lost")
        # The lost request lands after all (MC wrote it late).
        self.mc.tasks["TASK-9"]["approvalGates"].append({
            "id": "apg_late", "status": "pending", "requestedBy": OPERATOR,
            "reason": intent["gate_reason"], "requestedAt": "2026-10-01T12:00:00Z"})
        # A new request is refused: MC already holds an untracked gate for this action.
        self.mc.sender.fn = real
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.assertEqual(caught.exception.code, "mc_untracked_gate_for_this_action")
        adopted = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((adopted["state"], adopted["gate_id"]), ("gate_requested", "apg_late"))
        self.assertEqual(self.mc.requests, [])

    def test_escalated_unknown_request_still_ends_in_a_human_hand_off(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.lose_requests_before_mc_writes()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.path.escalate(OPS, iid, "MC shows no gate on TASK-9; a person will raise one.",
                           session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "escalated")
        self.assertEqual(self.mc.requests, [])
        self.assertTrue(self.path.get(OPS, iid)["chain_ok"])

    def test_escalated_before_dispatch_refuses_dispatch_and_read_back_cleanly(self):
        """Minor (c): no IndexError; a clean refusal with a reason, nothing sent."""
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.lose_requests_before_mc_writes()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.path.escalate(OPS, iid, "Unknown gate request; a person checks TASK-9.",
                           session=SESSION)
        before = self.kinds(iid)
        for step in (lambda: self.path.dispatch(OPS, iid, self.registry, session=SESSION),
                     lambda: self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)):
            with self.assertRaises(ap.ActionPathError) as caught:
                step()
            self.assertEqual(caught.exception.code, "escalated_before_dispatch")
        self.assertEqual((self.kinds(iid), self.fx.calls), (before, 0))
        self.assertEqual(self.path.reconcile_gate(OPS, iid, self.mc.client,
                                                  session=SESSION)["reason"],
                         "nothing_to_reconcile")

    def test_reconcile_is_safe_in_every_state_and_reads_nothing_without_cause(self):
        """Minor (b): a defined answer before (and after) any gate outcome."""
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]

        def check(expected_state):
            kinds, reads = self.kinds(iid), len(self.mc.sender.calls)
            result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
            if expected_state == "gate_requested":
                # With a gate tracked it reads the task for duplicates (none here).
                self.assertEqual((result["state"], result["reconciled"], result["reason"],
                                  result["untracked_gates"]),
                                 (expected_state, False, "gate_already_known", []))
                self.assertEqual((self.kinds(iid), self.mc.tools_called()[reads:]),
                                 (kinds, ["mc_get_task"]))
                return
            self.assertEqual((result["state"], result["reconciled"], result["reason"]),
                             (expected_state, False, "nothing_to_reconcile"))
            self.assertEqual((self.kinds(iid), len(self.mc.sender.calls)), (kinds, reads))

        check("proposed")
        self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        check("gate_prepared")
        self.path.approve_call(OPS, iid, "TASK-9", self.call_record(intent), session=SESSION)
        check("call_approved")
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        check("gate_requested")
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        check("gate_authorized")
        self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        check("dispatched")
        self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        check("verified")
        with self.assertRaises(TypeError):  # still only the guarded MC client
            self.path.reconcile_gate(OPS, iid, object(), session=SESSION)

    def test_reconcile_during_an_in_flight_send_records_nothing_false(self):
        """Minor (d): an absent gate mid-send is not evidence; ordering holds."""
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        real, seen = self.mc.sender.fn, []

        def reconcile_then_write(method, url, headers, body):
            if json.loads(body)["params"]["name"] == "mc_request_approval" and not seen:
                seen.append(self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION))
            return real(method, url, headers, body)

        self.mc.sender.fn = reconcile_then_write
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        [mid] = seen
        self.assertEqual((mid["state"], mid["matching_gates"], mid["send_in_flight"],
                          mid["reason"]), ("gate_request_unknown", 0, True, "send_in_flight"))
        view = self.path.get(OPS, iid)
        self.assertEqual((view["state"], view["gate"]["gate_id"]), ("gate_requested", "apg_1"))
        self.assertEqual(view["recovery"], [])  # no false "no gate" / escalation note
        self.assertNotIn("gate.request_lost", self.kinds(iid))

    def test_an_interrupted_send_is_judged_lost_only_after_the_settle_window(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        with mock.patch.object(ap, "send_request", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)  # dies mid-send
        self.assertEqual(self.kinds(iid)[-1], "gate.request_started")
        early = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((early["state"], early.get("send_in_flight")),
                         ("gate_request_unknown", True))
        self.assertEqual(self.path.get(OPS, iid)["recovery"], [])
        self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
        late = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(late["state"], "gate_request_lost")
        [absent] = self.path.get(OPS, iid)["recovery"]
        self.assertEqual((absent["trigger"], absent["reason"]),
                         ("gate_absent_after_request", "send_unanswered_past_settle_window"))
        self.assertEqual(self.mc.requests, [])

    def test_several_matching_gates_ask_a_human_once_and_adopt_nothing(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.mc.lose_next_answer = True
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.tasks["TASK-9"]["approvalGates"].append(
            {**self.mc.tasks["TASK-9"]["approvalGates"][0], "id": "apg_dup"})
        for _ in range(2):
            result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
            self.assertEqual((result["state"], result["matching_gates"]),
                             ("gate_request_unknown", 2))
        ambiguous = [r for r in self.path.get(OPS, iid)["recovery"]
                     if r["trigger"] == "gate_ambiguous"]
        self.assertEqual(len(ambiguous), 1)
        self.assertFalse(ambiguous[0]["new_request_allowed"])
        with self.assertRaises(ap.ActionPathError):  # no new request while gates are unresolved
            self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.assertEqual(len(self.mc.requests), 1)


    def test_request_gate_returns_the_adopted_state_when_reconcile_got_there_first(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        real, adopted = self.mc.sender.fn, []

        def adopt_after_write(method, url, headers, body):
            answer = real(method, url, headers, body)
            if json.loads(body)["params"]["name"] == "mc_request_approval" and not adopted:
                adopted.append(self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION))
            return answer

        self.mc.sender.fn = adopt_after_write
        result = self.path.request_gate(OPS, iid, self.mc.client,
                                        session=SESSION)  # must not raise
        self.assertEqual((result["state"], result["gate_id"]), ("gate_requested", "apg_1"))
        view = self.path.get(OPS, iid)
        self.assertEqual((view["state"], view["gate"]["gate_id"], view["gate"]["reconciled"]),
                         ("gate_requested", "apg_1", True))
        self.assertEqual(len(self.mc.requests), 1)
        self.assertEqual(view["gate_ids"], ["apg_1"])

    def test_journal_is_append_only_and_the_module_has_no_network_imports(self):
        iid = self.authorized()
        db = sqlite3.connect(self.dir / "actions.db")
        self.addCleanup(db.close)
        for statement in ("UPDATE action_event SET kind='x'", "DELETE FROM action_event"):
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute(statement)
        self.assertTrue(self.path.get(OPS, iid)["chain_ok"])
        tree = ast.parse(Path(ap.__file__).read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imported & {"urllib", "http", "socket", "ssl", "requests", "httpx"})



PORTAL_CAPABILITY = "portal.sales_order.set_customer_po"
PORTAL_EFFECT = {"relation": "public.app_sales_order", "key": {"id": 3},
                 "fields": ["po_reference"], "equals": "PO-7788"}


class FakePortal:
    """The Portal's sales-order routes behind an injected transport (GET/PATCH
    ``/api/mrp/sales-orders/{id}`` as plx-customer-portal@790256c route.ts; a header edit
    only while ``canEditSalesOrderUntilShipped``, else 409 ``ORDER_NOT_EDITABLE``, as
    updateSalesOrderHeader@eb964b99). Records every request; ``lose_next_answer`` applies a
    write and then loses its answer; ``ship_after_read`` ships part of the order right
    after the next GET (a person at the Portal, between the adapter's pre-write read and
    its PATCH)."""

    evidence = adapters.EVIDENCE_LABEL
    LOCKED = "Order edits are locked after the first shipment or cancel"
    # status.ts@eb964b99:20-33; normalizeSalesOrderStatus (:8-12) maps Open and READY to
    # statuses outside this set, so it never changes the answer.
    EDIT_LOCKED = frozenset({"PARTIALLY_SHIPPED", "SHIPPED", "COMPLETE", "CANCELLED"})

    def __init__(self) -> None:
        self.rows = {"3": {"id": "3", "orderNumber": "SO-3", "status": "DRAFT",
                           "poReference": None}}
        self.requests = []
        self.lose_next_answer = False
        self.ship_after_read = False

    @classmethod
    def editable(cls, status) -> bool:
        return isinstance(status, str) and bool(status.strip()) and (
            status not in cls.EDIT_LOCKED)

    def send(self, request):
        self.requests.append(request.as_dict())
        row = self.rows.get(request.path.rsplit("/", 1)[1])
        if row is None:
            return {"status": 404, "body": {"error": "Sales order not found"}}
        if request.method == "GET":
            # route.ts@5cb95e1:96-103: the GET also reports the edit lock (no shipments here).
            editable = self.editable(row.get("status"))
            body = {**row, "editable": editable,
                    "editLock": None if editable else {"reason": "STATUS"}}
            if self.ship_after_read:
                self.ship_after_read = False
                row["status"] = "PARTIALLY_SHIPPED"
            return {"status": 200, "body": body}
        if not self.editable(row.get("status")):
            return {"status": 409, "body": {"error": self.LOCKED,
                                            "code": "ORDER_NOT_EDITABLE"}}
        row.update(request.body)
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise TimeoutError("answer lost after the Portal saved")
        return {"status": 200, "body": {"id": row["id"], "orderNumber": "SO-3",
                                        "status": row["status"]}}

    def writes(self):
        return [r for r in self.requests if r["method"] != "GET"]


class RealSystemAdapterTests(ActionPathTestCase):
    """EXE-02: a cataloged real capability is dispatched through its adapter only when
    the MC approval was read back at dispatch (decided by someone other than the subject)
    and the allow switch names it; otherwise the refusal names the unmet gates and no
    request reaches the owner (injected transport)."""

    def setUp(self) -> None:
        super().setUp()
        self.portal = FakePortal()
        self.adapter = adapters.PortalAdapter(PORTAL_CAPABILITY, self.portal)
        self.real = ap.DispatcherRegistry()
        self.real.register(PORTAL_CAPABILITY, ap.RealSystemDispatcher(self.adapter))
        self.switch = mock.patch.dict("os.environ", {adapters.ALLOW_ENV: PORTAL_CAPABILITY})
        host_env = mock.patch.dict("os.environ")
        host_env.start()   # every test starts with the switch off, whatever the host has
        self.addCleanup(host_env.stop)
        os.environ.pop(adapters.ALLOW_ENV, None)

    def authorize(self, decided_by=DECIDER, preconditions=None):
        intent = self.path.register(OPS, self.case_id, self.propose(
            PORTAL_CAPABILITY, API_ORDER, PORTAL_EFFECT, preconditions=preconditions),
            session=SESSION)
        iid = intent["intent_id"]
        with self.switch:
            self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=decided_by)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        return iid

    def test_nothing_is_sent_until_every_gate_holds_then_the_exact_request_verifies(self):
        intent = self.path.register(OPS, self.case_id, self.propose(
            PORTAL_CAPABILITY, API_ORDER, PORTAL_EFFECT), session=SESSION)
        # Switch off (the default): no MC gate, no MC read, and the gate is named.
        with self.assertRaises(ap.ProductionTargetRefused) as caught:
            self.path.prepare_gate(OPS, intent["intent_id"], "TASK-9", self.mc.client,
                                   session=SESSION)
        self.assertEqual((caught.exception.code, caught.exception.unmet_gates),
                         ("production_target_refused", ("allow_switch",)))
        self.assertEqual(self.mc.sender.calls, [])
        iid = self.authorize()
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
        for registry, mc, switch_on, unmet in (
                (self.registry, None, True, ["adapter_registered", "mc_approval_read_back"]),
                (self.real, None, True, ["mc_approval_read_back"]),
                (self.real, self.mc.client, False, ["allow_switch"])):
            with self.subTest(unmet=unmet):
                with mock.patch.dict("os.environ", {adapters.ALLOW_ENV: PORTAL_CAPABILITY}
                                     if switch_on else {}):
                    result = self.path.dispatch(OPS, iid, registry, mc=mc, session=SESSION)
                self.assertEqual((result["state"], result["reason"], result["unmet_gates"]),
                                 ("dispatch_refused", "production_target_refused", unmet))
                self.assertEqual(self.portal.requests, [])
        # Every gate holds: one read of the order, then exactly the cataloged PATCH.
        with self.switch:
            done = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client, session=SESSION)
        self.assertEqual(done["state"], "dispatched")
        self.assertEqual(self.portal.requests, [
            {"schema": "plx-owner-request/v1", "system": "portal", "method": "GET",
             "path": "/api/mrp/sales-orders/3", "headers": {"Accept": "application/json"},
             "body": None},
            {"schema": "plx-owner-request/v1", "system": "portal", "method": "PATCH",
             "path": "/api/mrp/sales-orders/3",
             "headers": {"Content-Type": "application/json", "Accept": "application/json"},
             "body": {"poReference": "PO-7788"}}])
        dispatcher = self.real.for_capability(PORTAL_CAPABILITY)
        read = self.path.read_back(OPS, iid, dispatcher.read_back, session=SESSION)
        self.assertEqual((read["state"], read["outcome"]), ("verified", "effect_observed"))
        self.assertEqual(self.portal.requests[-1]["method"], "GET")
        verification = self.cases.get(OPS, self.case_id)["verifications"][-1]
        self.assertEqual(verification["detail"]["evidence"], "injected-transport")
        # Only a reviewed transport and the cataloged capability's own adapter plug in.
        with self.assertRaises(adapters.AdapterError):
            adapters.PortalAdapter(PORTAL_CAPABILITY, object())
        with self.assertRaises(adapters.AdapterError):
            adapters.PortalAdapter("portal.assembly_order.set_header_quantities", self.portal)
        with self.assertRaises(ap.ActionPathError):
            ap.DispatcherRegistry().register(ap.FIXTURE_CAPABILITY, dispatcher)

    def test_a_subject_deciding_the_gate_never_dispatches(self):
        roster = ApproverRoster({VINCE: {"role": "owner", "oid": VINCE_OID,
                                         "action_types": ALL_TYPES},
                                 EMPLOYEE: {"role": "owner", "oid": EMPLOYEE_OID,
                                            "action_types": ALL_TYPES}},
                                source_ref="MC mc_users as listed by a person (test)")
        self.path = self.new_path(approver_roster=roster)
        iid = self.authorize(decided_by=EMPLOYEE)
        view = self.path.get(OPS, iid)
        self.assertNotEqual(view["state"], "gate_authorized")
        self.assertEqual(view["decisions"][-1]["reason_code"], "decider_is_subject")
        with self.switch, self.assertRaises(ap.ActionPathError):
            self.path.dispatch(OPS, iid, self.real, mc=self.mc.client, session=SESSION)
        self.assertEqual(self.portal.requests, [])

    def test_a_retry_never_sends_twice_and_a_changed_owner_record_sends_nothing(self):
        iid = self.authorize()
        self.portal.lose_next_answer = True   # the Portal saves; its answer is lost
        with self.switch:
            lost = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client, session=SESSION)
            retry = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                       session=SESSION)
        self.assertEqual(lost["state"], "dispatched_unknown")
        self.assertEqual((retry["replayed"], retry["reconcile_required"]), (True, True))
        self.assertEqual(len(self.portal.writes()), 1)
        # The adapter itself refuses a second send of the key (defence in depth).
        with self.assertRaises(adapters.NotSent):
            self.adapter.dispatch(ap.ActionIntent(**self.path.get(OPS, iid)["intent"]))
        read = self.path.read_back(OPS, iid, self.adapter.read_back, session=SESSION)
        self.assertEqual((read["state"], read["reconciled_by"]), ("verified", "read_back"))
        self.assertEqual(len(self.portal.writes()), 1)

        # Another order whose PO was set at the Portal after approval: the pre-write
        # read finds the precondition broken, so no PATCH is sent.
        self.setUp()
        iid = self.authorize()
        self.portal.rows["3"]["poReference"] = "PO-SET-BY-STAFF"
        with self.switch:
            result = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                        session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("dispatch_refused", "owner_precondition_failed:po_reference"))
        self.assertEqual(self.portal.writes(), [])

    def test_a_key_made_only_of_dots_is_refused_before_any_request(self):
        # '.'/'..' survive quote() and a server normalises them away: a placeholder
        # before another segment could reach a route the catalog never matched.
        stored = self.path.get(OPS, self.authorize())["intent"]
        self.assertEqual(self.adapter.read_request(ap.ActionIntent(**stored)).path,
                         "/api/mrp/sales-orders/3")
        for value in (".", "..", "...", 0):
            with self.subTest(value=value):
                effect = {**stored["expected_effect"], "key": {"id": value}}
                intent = ap.ActionIntent(**{**stored, "expected_effect": effect})
                if value == 0:   # a normal key still builds its path
                    self.assertEqual(self.adapter.read_request(intent).path,
                                     "/api/mrp/sales-orders/0")
                    continue
                with self.assertRaises(adapters.AdapterError) as caught:
                    self.adapter.read_request(intent)
                self.assertEqual(caught.exception.code, "adapter_key_unmapped:id")
                with self.assertRaises(adapters.NotSent) as caught:
                    self.adapter.dispatch(intent)
                self.assertEqual(caught.exception.code, "adapter_key_unmapped:id")
        self.assertEqual(self.portal.requests, [])

    # Review c6e87271 (material): the proposal's only checkable precondition is on
    # another field, so the type's own no-overwrite guard must hold at the owner.
    ORDER_NUMBER_ONLY = [{"check": "field_equals", "citation": API_ORDER,
                          "field": "order_number", "value": "SO-3"}]

    def test_a_po_set_at_the_owner_after_approval_is_never_overwritten(self):
        for held in ("PO-SET-BY-STAFF", "PO-5501", "Vince Ramos"):
            with self.subTest(held=held):
                self.setUp()
                iid = self.authorize(preconditions=self.ORDER_NUMBER_ONLY)
                checked = self.path.get(OPS, iid)["intent"]["checked_preconditions"]
                self.assertEqual([c["field"] for c in checked], ["order_number"])
                self.portal.rows["3"]["poReference"] = held   # staff, after approval
                with self.switch:
                    result = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                                session=SESSION)
                self.assertEqual((result["state"], result["reason"]), (
                    "dispatch_refused", "owner_precondition_failed:target_has_no_po"))
                self.assertEqual(self.portal.writes(), [])
                self.assertEqual([r["method"] for r in self.portal.requests], ["GET"])
                self.assertEqual(self.portal.rows["3"]["poReference"], held)

    def test_an_empty_po_at_the_owner_is_written_once(self):
        iid = self.authorize(preconditions=self.ORDER_NUMBER_ONLY)
        self.portal.rows["3"]["poReference"] = "  "   # read and blank: holds nothing
        with self.switch:
            done = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client, session=SESSION)
            again = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                       session=SESSION)
        self.assertEqual((done["state"], again["replayed"]), ("dispatched", True))
        self.assertEqual([w["body"] for w in self.portal.writes()],
                         [{"poReference": "PO-7788"}])
        self.assertEqual(self.portal.rows["3"]["poReference"], "PO-7788")

    def test_an_owner_record_that_does_not_show_the_po_field_sends_nothing(self):
        iid = self.authorize(preconditions=self.ORDER_NUMBER_ONLY)
        del self.portal.rows["3"]["poReference"]   # unreadable is never empty
        with self.switch:
            result = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                        session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("dispatch_refused", "owner_precondition_failed:target_has_no_po"))
        self.assertEqual(self.portal.writes(), [])

    def test_an_order_is_written_until_its_first_shipment_and_the_owners_refusal_is_kept(self):
        # Every status of the Portal's vocabulary (types.ts SALES_ORDER_STATUSES@eb964b99):
        # the adapter's pre-write GET lets the PATCH go exactly when the owner takes it.
        for status in ("DRAFT", "CONFIRMED", "IN_PRODUCTION", "READY", "READY_TO_SHIP",
                       "Open", "PARTIALLY_SHIPPED", "SHIPPED", "COMPLETE", "CANCELLED"):
            with self.subTest(status=status):
                self.setUp()
                iid = self.authorize()
                self.portal.rows["3"]["status"] = status
                with self.switch:
                    result = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                                session=SESSION)
                if FakePortal.editable(status):
                    self.assertEqual(result["state"], "dispatched")
                    self.assertEqual([w["body"] for w in self.portal.writes()],
                                     [{"poReference": "PO-7788"}])
                else:
                    self.assertEqual((result["state"], result["reason"]), (
                        "dispatch_refused", f"owner_state_refused:status={status}"))
                    self.assertEqual(self.portal.writes(), [])
        # Outside that vocabulary the adapter applies the Portal's own predicate
        # (canEditSalesOrderUntilShipped@31a2623: not blank, not in EDIT_LOCKED, compared
        # exactly): FM status text is written, a blank or unreadable status sends nothing.
        for status, shown in (("OPEN", None), ("Open 2", None), ("shipped", None),
                              (None, "absent"), ("", "blank"), ("  ", "blank"),
                              (7, "unreadable")):
            with self.subTest(status=status):
                self.setUp()
                iid = self.authorize()
                if status is None:
                    del self.portal.rows["3"]["status"]
                else:
                    self.portal.rows["3"]["status"] = status
                with self.switch:
                    result = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                                session=SESSION)
                self.assertEqual(FakePortal.editable(status), shown is None)
                if shown is None:
                    self.assertEqual(result["state"], "dispatched")
                    self.assertEqual([w["body"] for w in self.portal.writes()],
                                     [{"poReference": "PO-7788"}])
                    continue
                self.assertEqual((result["state"], result["reason"]), (
                    "dispatch_refused", f"owner_state_refused:status={shown}"))
                self.assertEqual(self.portal.writes(), [])
        # Shipped between the pre-write read and the PATCH: the Portal refuses (409
        # ORDER_NOT_EDITABLE), and its code, never its message, is the rejection.
        self.setUp()
        iid = self.authorize()
        self.portal.ship_after_read = True
        with self.switch:
            result = self.path.dispatch(OPS, iid, self.real, mc=self.mc.client,
                                        session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("failed", "http_409:ORDER_NOT_EDITABLE"))
        self.assertEqual(len(self.portal.writes()), 1)
        self.assertIsNone(self.portal.rows["3"]["poReference"])


class McGroundingTests(ActionPathTestCase):
    """Slice: every gate request is grounded in a fresh MC read (pack 04 section 8 R3 step 1)."""

    def test_prepare_gate_refuses_unless_mc_is_live_and_the_task_open(self):
        cases = [({"health": {"dataSource": "seed"}}, "mc_data_not_live"),
                 ({"health": {"boringGateMet": False}}, "mc_sync_gate_not_met"),
                 ({"health": {"ok": False}}, "mc_self_check_failed"),
                 ({"task": {"stage": "merged"}}, "mc_task_closed"),
                 ({"task": {"stage": "verified"}}, "mc_task_closed"),
                 ({"task": {"stage": "cancelled"}}, "mc_task_closed"),  # TASK-2529
                 ({"task": {"stage": None}}, "mc_task_stage_unreadable"),
                 # a stage MC adds later is not in MC_STAGES: never read as open
                 ({"task": {"stage": "on_hold"}}, "mc_task_stage_unknown"),
                 ({"missing": True}, "mc_task_not_found")]
        for change, code in cases:
            with self.subTest(code=code, change=change):
                self.setUp()
                self.mc.health.update(change.get("health", {}))
                self.mc.tasks["TASK-9"].update(change.get("task", {}))
                if change.get("missing"):
                    del self.mc.tasks["TASK-9"]
                intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.prepare_gate(OPS, intent["intent_id"], "TASK-9", self.mc.client,
                                           session=SESSION)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.kinds(intent["intent_id"]), ["intent.registered"])
                self.assertNotIn("mc_request_approval", self.mc.tools_called())

    def test_prepare_gate_reads_mc_only_through_the_operator_client_and_never_for_production(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        with self.assertRaises(TypeError):
            self.path.prepare_gate(OPS, intent["intent_id"], "TASK-9", object(), session=SESSION)
        effect = {"relation": "public.app_sales_order", "key": {"id": 3},
                  "fields": ["po_reference"], "equals": "PO-7788"}
        real = self.path.register(OPS, self.case_id, self.propose(
            "portal.sales_order.set_customer_po", API_ORDER, effect), session=SESSION)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.prepare_gate(OPS, real["intent_id"], "TASK-9", self.mc.client,
                                   session=SESSION)
        self.assertEqual(caught.exception.code, "production_target_refused")
        self.assertEqual(self.mc.sender.calls, [])  # refused before any MC read

    def test_the_request_record_shows_the_human_the_mc_task_as_read(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        prepared = self.path.prepare_gate(OPS, intent["intent_id"], "TASK-9",
                                          self.mc.client, session=SESSION)["request"]
        task = prepared["mc_task"]
        self.assertEqual((task["schema"], task["task_id"], task["stage"], task["title"],
                          task["accountable_owner"], task["pending_gates"],
                          task["data_source"], task["boring_gate_met"], task["evidence"]),
                         ("plx-mc-task-grounding/v1", "TASK-9", "progress",
                          "Customer PO on SO 3", VINCE, 0, "live", True, "mc-read"))
        self.assertIsNone(prepared["supersedes_attempt"])
        self.assertTrue(task["read_at"].startswith("2026-10-01T12:00"))


class GateDuplicateTests(ActionPathTestCase):
    """Review of ef97a62: no path creates a duplicate gate silently."""

    def started(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        self.approve(intent["intent_id"], self.call_record(intent))
        return intent, intent["intent_id"]

    def server_outlives_client(self):
        """The client times out while MC's function is still running; ``finish``
        later lets the held call write its gate, as MC would."""
        real, held = self.mc.sender.fn, []

        def slow(method, url, headers, body):
            if json.loads(body)["params"]["name"] == "mc_request_approval" and not held:
                held.append((method, url, headers, body))
                raise TimeoutError("client timeout; MC still running")
            return real(method, url, headers, body)

        self.mc.sender.fn = slow
        return lambda: real(*held[0])

    def test_timeout_then_immediate_reconcile_waits_and_adopts_the_late_gate(self):
        intent, iid = self.started()
        finish = self.server_outlives_client()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_request_unknown", "send_in_flight"))
        with self.assertRaises(ap.ActionPathError):  # no new request while it may land
            self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        finish()  # the old call writes its gate
        adopted = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((adopted["state"], adopted["gate_id"]), ("gate_requested", "apg_1"))
        self.assertEqual(len(self.mc.tasks["TASK-9"]["approvalGates"]), 1)
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), 1)

    def test_a_gate_landing_after_the_window_is_recorded_as_a_duplicate(self):
        intent, iid = self.started()
        finish = self.server_outlives_client()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
        self.assertEqual(self.path.reconcile_gate(OPS, iid, self.mc.client,
                                                  session=SESSION)["state"],
                         "gate_request_lost")
        self.approve(iid, self.call_record(intent, evidence_ref="second approval (test)"))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["gate"]["gate_id"], "apg_1")
        finish()  # far beyond any bound, the old call still lands: apg_2
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)  # the poll notices it
        dups = [r for r in self.path.get(OPS, iid)["recovery"]
                if r["trigger"] == "gate_duplicate"]
        self.assertEqual([(d["gate_id"], d["resend_allowed"]) for d in dups],
                         [("apg_2", False)])
        self.assertIn("rejects it in Mission Control", dups[0]["next_step"])
        result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["reason"], result["untracked_gates"]),
                         ("gate_already_known", ["apg_2"]))
        self.assertEqual(len([r for r in self.path.get(OPS, iid)["recovery"]
                              if r["trigger"] == "gate_duplicate"]), 1)  # once per gate
        self.mc.decide("apg_2", status="rejected")
        self.assertEqual(self.path.reconcile_gate(OPS, iid, self.mc.client,
                                                  session=SESSION)["untracked_gates"],
                         [])
        # Only the tracked gate authorizes the dispatch.
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["decisions"][-1]["gate_id"], "apg_1")
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")

    def test_ambiguous_gates_resolve_once_the_human_rejects_all_but_one(self):
        intent, iid = self.started()
        self.mc.lose_next_answer = True
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        gates = self.mc.tasks["TASK-9"]["approvalGates"]
        gates.append({**gates[0], "id": "apg_dup"})
        result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_request_unknown", "ambiguous_gates"))
        [ambiguous] = [r for r in self.path.get(OPS, iid)["recovery"]
                       if r["trigger"] == "gate_ambiguous"]
        self.assertEqual(ambiguous["gate_ids"], ["apg_1", "apg_dup"])
        self.assertIn("reconcile_gate adopts the one that stands", ambiguous["next_step"])
        self.mc.decide("apg_dup", status="rejected")  # the human acts in MC
        adopted = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((adopted["state"], adopted["gate_id"]), ("gate_requested", "apg_1"))
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
        self.assertEqual(len(self.mc.requests), 1)

    def test_only_rejected_gates_adopt_the_newest_and_record_the_rejection(self):
        intent, iid = self.started()
        self.mc.lose_next_answer = True
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide("apg_1", status="rejected")
        adopted = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((adopted["state"], adopted["gate_id"]), ("gate_requested", "apg_1"))
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_rejected")
        self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client,
                               session=SESSION)  # a new round may start

    def test_an_older_rejected_gate_is_not_adopted_while_this_send_may_land(self):
        """Review 62d6eab: only rejected gates, one older than this send, send in flight."""
        intent, iid = self.started()
        self.mc.tasks["TASK-9"]["approvalGates"].append({
            "id": "apg_old", "status": "rejected", "requestedBy": OPERATOR,
            "reason": intent["gate_reason"], "requestedAt": "2026-10-01T09:00:00Z",
            "decidedBy": VINCE, "decidedAt": "2026-10-01T09:10:00Z"})
        finish = self.server_outlives_client()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        result = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_request_unknown", "send_in_flight"))
        self.assertIsNone(self.path.get(OPS, iid)["gate"]["gate_id"])
        finish()  # this attempt's own gate lands and is the one adopted
        adopted = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((adopted["state"], adopted["gate_id"]), ("gate_requested", "apg_1"))
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), 1)

    def test_a_rejected_gate_of_this_send_is_adopted_and_an_old_one_once_settled(self):
        intent, iid = self.started()
        self.mc.tasks["TASK-9"]["approvalGates"].append({
            "id": "apg_old", "status": "rejected", "requestedBy": OPERATOR,
            "reason": intent["gate_reason"], "requestedAt": "2026-10-01T09:00:00Z"})
        self.server_outlives_client()  # this send never lands
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
        # Settled: nothing of this attempt can still land, so the rejection is recorded.
        adopted = self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((adopted["state"], adopted["gate_id"]), ("gate_requested", "apg_old"))
        self.mc.decide("apg_old", status="rejected")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_rejected")

    def test_a_rejected_stray_gate_does_not_block_prepare_gate(self):
        intent, iid = self.started()
        finish = self.server_outlives_client()
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
        self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
        finish()  # the stray lands while the intent is lost
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.assertEqual(caught.exception.code, "mc_untracked_gate_for_this_action")
        self.mc.decide("apg_1", status="rejected")  # as the human precondition asks
        prepared = self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client,
                                          session=SESSION)["request"]
        self.assertEqual(prepared["mc_task"]["untracked_gates_with_this_reason"], [])

    def test_malformed_mc_answers_refuse_as_mc_unreadable(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        client = self.mc.client
        cases = [("self_check", [1, 2]),
                 ("get_task", {"data": {}}),
                 ("get_task", {"data": {"task": "x"}}),
                 ("get_task", {"data": {"task": {"stage": "progress",
                                                 "approvalGates": {"a": 1}}}})]
        for method, answer in cases:
            with self.subTest(method=method, answer=answer), \
                    mock.patch.object(client, method, return_value=answer):
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.prepare_gate(OPS, iid, "TASK-9", client, session=SESSION)
                self.assertEqual(caught.exception.code, "mc_unreadable")
        self.assertEqual(self.kinds(iid), ["intent.registered"])

    def test_a_late_definite_answer_after_re_prepare_is_recorded_and_blocks_a_send(self):
        intent, iid = self.started()
        real = self.mc.sender.fn

        def hang_then_answer(method, url, headers, body):
            if json.loads(body)["params"]["name"] == "mc_request_approval" \
                    and not self.mc.requests:
                # While the call hangs: the window passes, the intent is judged lost,
                # and a human prepares and approves a new round.
                self.mc.sender.fn = real
                self.clock.now += ap.DEFAULT_GATE_SEND_SETTLE
                self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
                self.approve(iid, self.call_record(intent, evidence_ref="second (test)"))
            return real(method, url, headers, body)  # MC writes apg_1 and answers

        self.mc.sender.fn = hang_then_answer
        late = self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((late["state"], late["reason"], late["gate_id"]),
                         ("call_approved", "late_answer_recorded", "apg_1"))
        [record] = [r for r in self.path.get(OPS, iid)["recovery"]
                    if r["trigger"] == "late_gate_answer"]
        self.assertEqual((record["gate_id"], record["attempt_id"]), ("apg_1", f"{iid}-g1"))
        # The new round must not add a second gate for the same action.
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        view = self.path.get(OPS, iid)
        self.assertEqual(view["state"], "gate_request_refused")
        self.assertEqual(len(self.mc.requests), 1)
        self.assertEqual(self.kinds(iid)[-1], "gate.request_refused")
        self.assertTrue(view["chain_ok"])


class CaseListingTests(ActionPathTestCase):
    """``list_for_case`` / ``view``: permission-aware reads for the MCP tools and console."""

    BOTH = Actor("plx", frozenset({"ops", "viewer"}))
    VIEWER = Actor("plx", frozenset({"viewer"}))   # sees the case, cannot resolve its rows

    def shared_case(self):
        case_id = self.cases.open(self.BOTH, "Shared: set the PO on SO 3 to PO-7788?",
                                  objective_author_kind="human")
        pid = self.cases.propose(
            self.BOTH, case_id, ap.FIXTURE_CAPABILITY, {"field": "po_reference",
                                                        "value": "PO-7788"},
            [{"check": "field_empty", "citation": self.handle, "field": "po_reference"},
             "a person confirms"], citations=[self.handle],
            expected_effect=EFFECT)["proposal_id"]
        return case_id, pid

    def test_lists_this_cases_intents_in_order_with_full_detail_and_next_step(self):
        first = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)["intent_id"]
        second = self.authorized()
        other_case = self.cases.open(OPS, "Another case: PO-7788",
                                     objective_author_kind="human")
        self.cases.propose(OPS, other_case, ap.FIXTURE_CAPABILITY,
                           {"field": "po_reference", "value": "PO-7788"},
                           [{"check": "field_empty", "citation": self.handle,
                             "field": "po_reference"}], citations=[self.handle],
                           expected_effect=EFFECT)
        self.path.register(OPS, other_case, "P1", session=SESSION)
        sent, dispatched = len(self.mc.sender.calls), self.fx.calls

        listed = self.path.list_for_case(OPS, self.case_id)
        self.assertEqual([item["intent_id"] for item in listed["intents"]], [first, second])
        self.assertEqual((listed["total"], listed["truncated"], listed["evidence_label"]),
                         (2, False, "isolated-fixture"))
        proposed, authorized = listed["intents"]
        self.assertEqual((proposed["state"], proposed["withheld"],
                          proposed["next_safe_step"]["step"],
                          proposed["next_safe_step"]["by"],
                          proposed["next_safe_step"]["agent_tool"]),
                         ("proposed", False, "prepare_gate", "host", None))
        self.assertEqual((authorized["state"], authorized["gate"]["gate_id"],
                          authorized["decisions"][-1]["authorizes"],
                          authorized["decisions"][-1]["decider_role"],
                          authorized["production_guard"], authorized["chain_ok"]),
                         ("gate_authorized", "apg_1", True, "owner", None, True))
        self.assertNotIn("decided_by", authorized["decisions"][-1])  # no decider identity
        self.assertEqual(authorized["checked_preconditions"][0]["check"], "field_empty")
        self.assertEqual(authorized["human_preconditions"],
                         ["a person at PLX confirms the PO belongs to this order"])
        self.assertEqual(authorized, self.path.view(OPS, second))
        # Reading sends nothing anywhere.
        self.assertEqual((len(self.mc.sender.calls), self.fx.calls), (sent, dispatched))

    def test_withheld_proposal_shows_state_only(self):
        case_id, pid = self.shared_case()
        iid = self.path.register(self.BOTH, case_id, pid, session=fixture_session(
            "plx", self.BOTH.principals, channel=CHANNEL_MCP))["intent_id"]
        full = self.path.list_for_case(self.BOTH, case_id)["intents"][0]
        self.assertEqual((full["intent_id"], full["withheld"]), (iid, False))
        listed = self.path.list_for_case(self.VIEWER, case_id)
        [entry] = listed["intents"]
        self.assertEqual(entry, {"case_id": case_id, "proposal_id": pid, "state": "proposed",
                                 "withheld": True, "content": ap.WITHHELD})
        text = json.dumps(listed)
        for secret in (iid, ap.FIXTURE_CAPABILITY, "po_reference", "PO-7788",
                       full["idempotency_key"], full["gate_reason"]):
            self.assertNotIn(secret, text)
        self.assertEqual(self.path.view(self.VIEWER, iid), entry)
        # The viewer cannot register it either (the proposal is withheld from them).
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(self.VIEWER, case_id, pid, session=fixture_session(
                "plx", self.VIEWER.principals, channel=CHANNEL_MCP))
        self.assertEqual(caught.exception.code, "proposal_withheld")

    def test_outsider_reads_nothing(self):
        iid = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)["intent_id"]
        with self.assertRaises(ap.CaseNotFound):
            self.path.list_for_case(OUTSIDER, self.case_id)
        with self.assertRaises(ap.IntentNotFound):
            self.path.view(OUTSIDER, iid)
        with self.assertRaises(ap.IntentNotFound):
            self.path.view(OPS, "act_" + "0" * 24)
        self.assertEqual(self.path.list_for_case(OPS, self.cases.open(OPS, "empty"))["intents"],
                         [])

    def test_recovery_records_lead_the_next_step_and_a_broken_chain_shows_nothing(self):
        iid = self.authorized()
        self.fx.lose_next_response = True
        self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        view = self.path.view(OPS, iid)
        self.assertEqual(view["state"], "dispatched_unknown")
        [recovery] = view["recovery"]
        self.assertEqual((recovery["trigger"], recovery["resend_allowed"]),
                         ("dispatch_outcome_unknown", False))
        self.assertNotIn("binding_sha256", recovery)
        self.assertEqual((view["next_safe_step"]["step"], view["next_safe_step"]["detail"],
                          view["next_safe_step"]["resend_allowed"]),
                         ("read_back", recovery["next_step"], False))
        self.assertEqual(view["dispatches"][-1]["outcome"], "dispatched_unknown")
        # A forged row breaks the chain: listed with no detail at all.
        db = sqlite3.connect(self.dir / "actions.db")
        self.addCleanup(db.close)
        last = db.execute("SELECT MAX(intent_seq) FROM action_event WHERE intent_id=?",
                          (iid,)).fetchone()[0]
        db.execute("INSERT INTO action_event(intent_id,intent_seq,kind,actor,at,payload_json,"
                   "prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                   (iid, last + 1, "readback.recorded", "ops", "2026-10-01T12:00:00+00:00",
                    '{"outcome":"effect_observed"}', "x", "forged"))
        db.commit()
        [entry] = self.path.list_for_case(OPS, self.case_id)["intents"]
        self.assertEqual(entry, {"case_id": self.case_id, "state": None,
                                 "integrity": "action_event_chain_broken"})
        with self.assertRaises(ap.ActionPathError):
            self.path.view(OPS, iid)

    def test_every_state_has_a_safe_next_step_and_none_is_an_agent_tool(self):
        self.assertEqual(set(ap._SAFE_STEP), set(ap.STATES))
        for state in ap.STATES:
            step = ap.next_safe_step({"state": state, "recovery": []})
            self.assertIn(step["by"], {"host", "human", "mc_decider", "none"}, state)
            self.assertIsNone(step["agent_tool"], state)
            self.assertFalse(step["resend_allowed"], state)



class CurrencyTests(ActionPathTestCase):
    """Review 1a3beeb: a closed case or changed cited evidence ends the path; the next
    safe step says so instead of naming a step that would only be refused."""

    def change_cited_row(self):
        self.fx.rows[(REL, ap._canon({"id": 3}))]["po_reference"] = "OTHER"

    def test_register_refuses_a_closed_case_with_a_typed_refusal(self):
        pid = self.propose()
        self.cases.close(OPS, self.case_id, "answered")
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(OPS, self.case_id, pid, session=SESSION)
        self.assertEqual(caught.exception.code, "case_closed")
        self.assertEqual(self.path.list_for_case(OPS, self.case_id)["total"], 0)

    def test_a_repeat_registration_on_a_closed_case_is_refused_too(self):
        iid = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)["intent_id"]
        self.cases.close(OPS, self.case_id, "answered")
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.register(OPS, self.case_id, "P1", session=SESSION)
        self.assertEqual(caught.exception.code, "case_closed")
        self.assertEqual(self.kinds(iid), ["intent.registered"])

    def test_changed_evidence_makes_the_next_step_reverify_and_prepare_refuse(self):
        iid = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)["intent_id"]
        fresh = self.path.view(OPS, iid)
        self.assertEqual((fresh["case_status"], fresh["citations_status"],
                          fresh["next_safe_step"]["step"], fresh["next_safe_step"]["blocked_by"]),
                         ("open", "resolved", "prepare_gate", None))
        self.change_cited_row()
        view = self.path.view(OPS, iid)
        step = view["next_safe_step"]
        self.assertEqual((view["state"], view["citations_status"], step["step"], step["by"],
                          step["blocked_by"], step["agent_tool"], step["resend_allowed"]),
                         ("proposed", CITATION_CONTENT_CHANGED, "reverify_and_repropose", "human",
                          "precondition_citation_changed", None, False))
        self.assertIn("never proceeds", step["detail"])
        self.assertEqual(self.path.list_for_case(OPS, self.case_id)["intents"][0], view)
        sent = len(self.mc.sender.calls)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.assertEqual(caught.exception.code, "precondition_citation_changed")
        self.assertEqual(len(self.mc.sender.calls), sent)  # not even an MC read
        self.assertEqual(self.kinds(iid), ["intent.registered"])

    def test_changed_evidence_after_approval_refuses_the_send_with_nothing_sent(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.change_cited_row()
        result = self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(result["kind"], "gate.request_refused")
        state = self.path.get(OPS, iid)
        self.assertEqual(state["state"], "gate_request_refused")
        self.assertEqual(self.mc.requests, [])
        self.assertEqual(self.path.view(OPS, iid)["next_safe_step"]["step"],
                         "reverify_and_repropose")

    def test_changed_evidence_on_a_pending_or_authorized_gate(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.change_cited_row()
        pending = self.path.view(OPS, iid)["next_safe_step"]
        self.assertEqual((pending["step"], pending["by"], pending["blocked_by"]),
                         ("reject_gate_in_mc", "mc_decider", "precondition_citation_changed"))
        self.mc.decide("apg_1")
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        authorized = self.path.view(OPS, iid)
        self.assertEqual((authorized["state"], authorized["next_safe_step"]["step"]),
                         ("gate_authorized", "reverify_and_repropose"))
        # The step it no longer names is indeed refused, and nothing is written.
        writes = self.fx.calls
        refused = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((refused["state"], refused["reason"]),
                         ("dispatch_refused", "precondition_citation_changed"))
        self.assertEqual(self.fx.calls, writes)
        after = self.path.view(OPS, iid)["next_safe_step"]
        self.assertEqual((after["step"], after["resend_allowed"]),
                         ("reverify_and_repropose", False))

    def test_its_own_dispatch_does_not_make_an_intent_stale(self):
        iid = self.authorized()
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        view = self.path.view(OPS, iid)  # the write changed the cited row, as a real one would
        self.assertEqual((view["citations_status"], view["next_safe_step"]["step"],
                          view["next_safe_step"]["blocked_by"]),
                         (CITATION_CONTENT_CHANGED, "read_back", None))

    def test_a_closed_case_has_no_actionable_next_step(self):
        iid = self.authorized()
        self.cases.close(OPS, self.case_id, "answered elsewhere")
        view = self.path.view(OPS, iid)
        step = view["next_safe_step"]
        self.assertEqual((view["state"], view["case_status"], step["step"], step["by"],
                          step["blocked_by"]),
                         ("gate_authorized", "closed", "none", "none", "case_closed"))
        refused = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(refused["reason"], "case_closed")
        self.assertEqual(self.path.view(OPS, iid)["next_safe_step"]["step"], "none")

    def test_a_closed_case_with_a_pending_gate_asks_mc_to_reject_it(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.cases.close(OPS, self.case_id, "answered")
        step = self.path.view(OPS, iid)["next_safe_step"]
        self.assertEqual((step["step"], step["by"], step["blocked_by"]),
                         ("reject_gate_in_mc", "mc_decider", "case_closed"))
        self.assertIn("freezes the task's stage", step["detail"])

    def test_a_closed_case_still_reconciles_an_unknown_request_first(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.mc.lose_next_answer = True
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.cases.close(OPS, self.case_id, "answered")
        step = self.path.view(OPS, iid)["next_safe_step"]
        self.assertEqual((step["step"], step["by"], step["blocked_by"]),
                         ("reconcile_gate", "host", "case_closed"))
        self.assertIn("never re-send", step["detail"])
        self.assertIn("rejects it", step["detail"])

    def test_a_closed_case_after_dispatch_hands_the_check_to_a_person(self):
        iid = self.authorized()
        self.fx.lose_next_response = True
        self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.cases.close(OPS, self.case_id, "answered")
        step = self.path.view(OPS, iid)["next_safe_step"]
        self.assertEqual((step["step"], step["by"], step["blocked_by"]),
                         ("reconcile_in_owning_system", "human", "case_closed"))
        # Indeed no read-back can be recorded on the closed case.
        with self.assertRaises(CaseClosed):
            self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.path.escalate(OPS, iid, "case closed; a person checks SO 3 in the fixture owner",
                           session=SESSION)
        self.assertEqual(self.path.view(OPS, iid)["next_safe_step"]["step"],
                         "reconcile_in_owning_system")

    def test_every_blocked_step_stays_within_the_allowed_takers(self):
        for blocker in ap.BLOCKERS:
            for state in ap.STATES:
                step = ap.next_safe_step({"state": state, "recovery": []}, blocker)
                self.assertIn(step["by"], {"host", "human", "mc_decider", "none"}, state)
                self.assertIsNone(step["agent_tool"], state)
                self.assertFalse(step["resend_allowed"], state)
                after = state in {"dispatched", "dispatched_unknown", "verified", "escalated"}
                self.assertEqual(step["blocked_by"],
                                 None if after and blocker != "case_closed" else blocker)
                self.assertNotIn(step["step"], {"prepare_gate", "approve_call", "request_gate",
                                                "record_decision", "dispatch"}, (blocker, state))
        with self.assertRaises(ValueError):
            ap.next_safe_step({"state": "proposed", "recovery": []}, "anything")

class SubjectTestCase(ActionPathTestCase):
    """A second MC owner (``DECIDER``) on the roster, and helpers that drive one intent."""

    def setUp(self) -> None:
        super().setUp()
        # A second MC owner decides here, so the decider can be the subject while Vince
        # approves the MC calls.
        self.roster = ApproverRoster(
            {VINCE: {"role": "owner", "action_types": ALL_TYPES, "oid": VINCE_OID},
             DECIDER: {"role": "owner", "action_types": ALL_TYPES, "oid": DECIDER_OID},
             STEPHEN: {"role": "member", "action_types": (), "oid": None}},
            source_ref="MC mc_users as listed by a person (test)")
        self.path = self.new_path(approver_roster=self.roster)

    def fresh(self, session, preconditions=None, co_sessions=()):
        """Register one distinct proposal with ``session``, then again with each of
        ``co_sessions`` (co-requests); returns the intent."""
        self.drives = getattr(self, "drives", 0) + 1
        proposal = self.propose(preconditions=[
            {"check": "field_empty", "citation": self.handle, "field": "po_reference"},
            f"a person at PLX confirms the PO belongs to this order ({self.drives})",
            *(preconditions or ())])
        intent = self.path.register(OPS, self.case_id, proposal, session=session)
        for co_session in co_sessions:
            self.path.register(OPS, self.case_id, proposal, session=co_session)
        return intent

    def drive(self, session, decided_by=DECIDER, preconditions=None, co_sessions=()):
        """Register (``fresh``), raise the gate, have ``decided_by`` approve it in MC and
        record the decision; returns the intent id."""
        intent = self.fresh(session, preconditions, co_sessions)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=decided_by)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        return iid

    def assert_parked(self, iid, code):
        state = self.path.get(OPS, iid)
        self.assertEqual((state["state"], state["decisions"][-1]["reason_code"],
                          state["decisions"][-1]["authorizes"]),
                         ("gate_not_authorizing", code, False))
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(caught.exception.code, "gate_not_authorized")
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_not_authorizing")
        self.assertEqual(self.fx.calls, 0)
        return self.path.view(OPS, iid)


class DeciderSubjectTests(SubjectTestCase):
    """Decision g2-1: the MC decider is never the employee the action is for (``sub``),
    whose identity comes only from the registering session; an unknown subject never
    authorizes. Both park the intent (``gate_not_authorizing``), never dispatchable."""

    def test_a_decision_by_the_subject_employee_never_authorizes_and_stays_parked(self):
        # The decider is an MC owner on the roster: as anyone else's decider they authorize.
        control = self.drive(SESSION)
        self.assertEqual(self.path.get(OPS, control)["state"], "gate_authorized")
        for subject in (DECIDER, "Mc.Owner+x@PetraSoap.com"):
            with self.subTest(subject=subject):
                decider = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                        subject_email=subject)
                view = self.assert_parked(self.drive(decider), "decider_is_subject")
                self.assertEqual((view["subject_known"], view["next_safe_step"]["step"]),
                                 (True, "prepare_gate"))
                self.assertIn("different Mission Control admin/owner",
                              view["next_safe_step"]["detail"])
                self.assertNotIn(DECIDER, json.dumps(view))   # no identity projected

    def test_the_proposals_author_and_the_cases_opener_never_approve_it(self):
        # Another employee registers what the MC owner (DECIDER) wrote or asked, and the
        # owner decides the gate. Before: the author was no subject, so their own approval
        # authorized, dispatched and verified.
        owner = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                subject_email=DECIDER, subject_oid=DECIDER_OID)
        effect = {"field": "po_reference", "value": "PO-7788"}
        checks = [{"check": "field_empty", "citation": self.handle, "field": "po_reference"}]
        opened = self.path.open_case(OPS, "Set the customer PO on SO 3 to PO-7788?",
                                     objective_author_kind="human", session=owner)
        for name, case_id, author in (("author", self.case_id, owner),
                                      ("opener", opened, SESSION)):
            with self.subTest(recorded=name):
                pid = self.path.record_proposal(
                    OPS, case_id, ap.FIXTURE_CAPABILITY, effect, [*checks, f"{name} case"],
                    citations=[self.handle], expected_effect=EFFECT,
                    session=author)["proposal_id"]
                intent = self.path.register(OPS, case_id, pid, session=SESSION)
                iid = intent["intent_id"]
                self.approve(iid, self.call_record(intent))
                self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
                self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
                self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
                self.assert_parked(iid, "decider_is_subject")
        # Recorded past the action path (no author on record): never an MC gate at all.
        pid = self.cases.propose(OPS, self.case_id, ap.FIXTURE_CAPABILITY, effect,
                                 [*checks, "unrecorded author"], citations=[self.handle],
                                 expected_effect=EFFECT)["proposal_id"]
        iid = self.path.register(OPS, self.case_id, pid, session=SESSION)["intent_id"]
        sent = len(self.mc.tools_called())
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.assertEqual(caught.exception.code, "subject_unknown")
        self.assertEqual(len(self.mc.tools_called()), sent)
        self.assertEqual(self.path.view(OPS, iid)["next_safe_step"]["step"],
                         "needs_session_subject")
        self.assertEqual(self.fx.calls, 0)

    def test_a_call_approver_who_is_a_subject_under_another_address_is_refused(self):
        # The configured call approver (VINCE, roster oid VINCE_OID) is also a subject
        # under another address with the same Entra oid: as registrant, as author, or
        # co-requesting after the approval. Before: matched by email only, the call was
        # approved and sent (and the intent authorized and dispatched).
        alias = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                subject_email="vince@petralabx.com", subject_oid=VINCE_OID)
        authored = self.path.record_proposal(
            OPS, self.case_id, ap.FIXTURE_CAPABILITY, {"field": "po_reference",
                                                       "value": "PO-7788"},
            [{"check": "field_empty", "citation": self.handle, "field": "po_reference"},
             "authored under another address"], citations=[self.handle],
            expected_effect=EFFECT, session=alias)["proposal_id"]
        for name, intent in (("registrant", self.fresh(alias)),
                             ("author", self.path.register(OPS, self.case_id, authored,
                                                           session=SESSION))):
            with self.subTest(subject=name), self.assertRaises(ap.ActionPathError) as caught:
                self.approve(intent["intent_id"], self.call_record(intent))
            self.assertEqual(caught.exception.code, "call_approver_is_subject")
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.register(OPS, self.case_id, intent["proposal_id"], session=alias)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        with self.path._connect() as db:
            refused = json.loads(db.execute(
                "SELECT payload_json FROM action_event WHERE intent_id=? ORDER BY intent_seq "
                "DESC LIMIT 1", (iid,)).fetchone()[0])
        self.assertEqual((self.path.get(OPS, iid)["state"], refused["reason"], self.mc.requests),
                         ("gate_request_refused", "call_approver_is_subject", []))
        # A configured call approver the roster names no oid for: refused (fail closed).
        self.path = self.new_path(approver_roster=ApproverRoster(
            {VINCE: {"role": "owner", "action_types": ALL_TYPES},
             DECIDER: {"role": "owner", "oid": DECIDER_OID, "action_types": ALL_TYPES}},
            source_ref="MC mc_users as listed by a person (test)"))
        intent = self.fresh(SESSION)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.approve(intent["intent_id"], self.call_record(intent))
        self.assertEqual(caught.exception.code, "call_approver_oid_unknown")

    def test_past_the_gate_request_an_unknown_subject_needs_re_registration(self):
        known = {"identities": [EMPLOYEE]}
        recorded = {"subjects": [{"sub": known}],
                    "authors": [{"kind": kind, "original": True, "sub": known}
                                for kind in ("case.opened", "proposal.recorded")]}
        retry = [{"trigger": "dispatch_failed", "resend_allowed": True, "next_step": "retry"}]
        for state, step, by in (("gate_requested", "reject_gate_in_mc", "mc_decider"),
                                ("gate_authorized", "reregister", "human"),
                                ("dispatch_refused", "reregister", "human")):
            with self.subTest(state=state):
                unknown = ap.next_safe_step({"state": state, "recovery": retry,
                                             "subjects": [{"sub": known}], "authors": []})
                self.assertEqual((unknown["step"], unknown["by"], unknown["resend_allowed"]),
                                 (step, by, False))
                self.assertIn("needs re-registration", unknown["detail"])
                control = ap.next_safe_step({"state": state, "recovery": retry, **recorded})
                self.assertEqual(control["step"], ap._SAFE_STEP[state][0])
                self.assertNotIn("re-registration", control["detail"])

    def test_an_unknown_subject_never_gets_an_mc_gate_and_is_parked(self):
        anonymous = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP)
        console = fixture_session("plx", OPS.principals, channel=CHANNEL_CONSOLE)
        for session in (console, anonymous):
            with self.subTest(session=session):
                iid = self.fresh(session)["intent_id"]
                self.assertIsNone(self.path.get(OPS, iid)["intent"]["sub"])
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
                self.assertEqual(caught.exception.code, "subject_unknown")
                view = self.path.view(OPS, iid)
                self.assertEqual((view["state"], view["subject_known"],
                                  view["next_safe_step"]["step"]),
                                 ("proposed", False, "needs_session_subject"))
                self.assertIn("needs a session subject", view["next_safe_step"]["detail"])
        self.assertEqual(self.path.get(OPS, iid)["intent"]["act"],
                         [{"kind": "agent", "channel": "mcp", "client": "plx-workbench-mcp",
                           "session": anonymous.session_ref}])
        self.assertEqual(self.mc.tools_called(), [])   # no MC read or write at all
        # A co-request with no session subject, after the gate was requested, makes the
        # subject unknown: the decision never authorizes and the intent stays parked.
        iid = self.drive(SESSION, co_sessions=())
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
        tainted = self.fresh(SESSION)
        tid = tainted["intent_id"]
        self.approve(tid, self.call_record(tainted))
        self.path.request_gate(OPS, tid, self.mc.client, session=SESSION)
        self.path.register(OPS, self.case_id, tainted["proposal_id"],
                           session=ANONYMOUS)   # no session subject
        self.mc.decide(self.path.get(OPS, tid)["gate"]["gate_id"], decided_by=DECIDER)
        self.path.record_decision(OPS, tid, self.mc.client, session=SESSION)
        view = self.assert_parked(tid, "subject_unknown")
        self.assertEqual((view["subject_known"], view["next_safe_step"]["step"]),
                         (False, "needs_session_subject"))

    def test_every_co_requesting_subject_is_a_subject_the_decider_may_not_be(self):
        decider = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP, subject_email=DECIDER)
        iid = self.drive(SESSION, co_sessions=(SESSION, decider, decider))
        subjects = self.path.get(OPS, iid)["subjects"]
        self.assertEqual([item["sub"]["identities"] for item in subjects],
                         [EMPLOYEE_IDS, [DECIDER]])   # repeats of a known subject add nothing
        self.assertEqual(self.path.get(OPS, iid)["intent"]["sub"]["identities"], EMPLOYEE_IDS)
        self.assert_parked(iid, "decider_is_subject")

    def test_a_co_request_during_the_mc_read_is_checked_before_the_decision_commits(self):
        # The co-request lands while record_decision reads MC's decision: the subject
        # checks run on the state committed with the decision, never the stale snapshot.
        decider = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                  subject_email=DECIDER)
        # The decider again, under a Portal email MC never shows: matched by Entra oid.
        alias = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                subject_email="d.owner@petralabx.com", subject_oid=DECIDER_OID)
        read = MissionControlMcpClient.approval_decision
        for late, code in ((decider, "decider_is_subject"), (alias, "decider_is_subject"),
                           (ANONYMOUS, "subject_unknown")):
            with self.subTest(code=code):
                intent = self.fresh(SESSION)
                iid = intent["intent_id"]
                self.approve(iid, self.call_record(intent))
                self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
                self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)

                def racing(client, *args, _late=late, _proposal=intent["proposal_id"], **kw):
                    answer = read(client, *args, **kw)
                    self.path.register(OPS, self.case_id, _proposal, session=_late)
                    return answer

                with mock.patch.object(MissionControlMcpClient, "approval_decision", racing):
                    self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
                self.assertEqual(len(self.path.get(OPS, iid)["subjects"]), 2)
                self.assert_parked(iid, code)
                self.assertFalse(any(d["authorizes"]
                                     for d in self.path.get(OPS, iid)["decisions"]))
                self.assertEqual(self.fx.calls, 0)

    def test_a_co_request_after_authorization_stops_dispatch(self):
        # The decider (an owner on the roster) was checked against the subjects MC saw.
        # A new subject co-requesting the authorized intent was never checked against
        # the decider: the decision is withdrawn and nothing is dispatched.
        stephen = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                  subject_email=STEPHEN)
        for late in (stephen, ANONYMOUS):
            with self.subTest(late=late is stephen):
                iid = self.drive(SESSION)
                self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
                again = self.path.register(OPS, self.case_id,
                                           self.path.get(OPS, iid)["intent"]["proposal_id"],
                                           session=late)
                self.assertTrue(again["duplicate"])
                view = self.assert_parked(iid, ap.CO_REQUESTED_AFTER_DECISION)
                if late is ANONYMOUS:   # an unknown co-requester makes the subject unknown
                    self.assertEqual(view["next_safe_step"]["step"], "needs_session_subject")
                else:
                    self.assertEqual(view["next_safe_step"]["step"], "prepare_gate")
                    self.assertIn("co-requested this action after the decision",
                                  view["next_safe_step"]["detail"])
                state = self.path.get(OPS, iid)
                self.assertEqual(state["recovery"][-1]["trigger"], "approval_withdrawn")
                self.assertTrue(state["chain_ok"])
        iid = self.drive(SESSION)
        # A repeat by a subject already recorded changes nothing: it still dispatches.
        self.path.register(OPS, self.case_id, self.path.get(OPS, iid)["intent"]["proposal_id"],
                           session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")

    def test_the_per_call_approver_is_never_a_subject(self):
        vince = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP, subject_email=VINCE)
        intent = self.fresh(vince)
        iid = intent["intent_id"]
        self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.approve_call(OPS, iid, "TASK-9", self.call_record(intent), session=SESSION)
        self.assertEqual(caught.exception.code, "call_approver_is_subject")
        # Approved by Vince for Ricardo's action, then Vince co-requests it before the
        # send: the request is refused for certain and nothing reaches MC.
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.register(OPS, self.case_id, intent["proposal_id"], session=vince)
        sent = len(self.mc.tools_called())
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        state = self.path.get(OPS, iid)
        self.assertEqual(state["state"], "gate_request_refused")
        with sqlite3.connect(self.dir / "actions.db") as db:
            [(payload,)] = db.execute("SELECT payload_json FROM action_event WHERE intent_id=? "
                                      "AND kind='gate.request_refused'", (iid,)).fetchall()
        self.assertEqual(json.loads(payload)["reason"], "call_approver_is_subject")
        self.assertEqual(self.mc.tools_called()[sent:].count("mc_request_approval"), 0)

    def test_sub_comes_only_from_the_session_never_from_arguments_or_source_text(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        self.assertEqual((intent["sub"], intent["act"]),
                         ({"principal": None, "kind": "fixture", "proof": "fixture",
                           "identities": EMPLOYEE_IDS, "oid": EMPLOYEE_OID},
                          ({"kind": "agent", "channel": "mcp", "client": "plx-workbench-mcp",
                            "session": SESSION.session_ref},)))
        # A subject shaped like the session's record, as a tool argument would carry it,
        # and a session of another actor are refused before anything is recorded.
        forged = {"schema": "plx-intent-identity/v1", "act": None,
                  "sub": {"principal": None, "kind": "employee", "identities": [STEPHEN]}}
        other = fixture_session("plx", OUTSIDER.principals, channel=CHANNEL_MCP,
                                subject_email=STEPHEN)
        for session, code in ((forged, "session_identity_required"),
                              (other, "session_actor_mismatch")):
            with self.subTest(code=code), self.assertRaises(ap.ActionPathError) as caught:
                self.path.register(OPS, self.case_id, self.propose(preconditions=[
                    {"check": "field_empty", "citation": self.handle,
                     "field": "po_reference"}]), session=session)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(self.path.list_for_case(OPS, self.case_id)["intents"]), 1)
        # Source text naming another subject changes nothing: the session's subject
        # decided, so it is refused; the text's Stephen is never the subject.
        decider = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP, subject_email=DECIDER)
        iid = self.drive(decider, preconditions=[
            f"sub: {STEPHEN}; requested on behalf of {STEPHEN}, decided by {DECIDER}"])
        self.assertEqual(self.path.get(OPS, iid)["intent"]["sub"]["identities"], [DECIDER])
        self.assert_parked(iid, "decider_is_subject")
        # A repeat registration from another subject's session keeps the first ``sub``
        # and adds its subject as a co-requester.
        again = self.path.register(OPS, self.case_id, self.path.get(OPS, iid)["intent"][
            "proposal_id"], session=SESSION)
        self.assertEqual((again["duplicate"], again["sub"]["identities"]), (True, [DECIDER]))
        self.assertEqual(len(self.path.get(OPS, iid)["subjects"]), 2)

    def test_internal_actions_record_sub_and_act_from_the_session(self):
        follow_up = {"missing": "The customer PO on SO 3.",
                     "why": "No cited document states it.",
                     "who_can_answer": ["the customer"], "state": "unknown",
                     "citations": [self.handle]}
        record = self.path.execute_internal(OPS, "workbench.follow_up.record", self.case_id,
                                            follow_up, session=SESSION)
        self.assertEqual((record["executed"], record["subject_known"], record["act"]),
                         (True, True, [{"kind": "agent", "channel": "mcp"}]))
        self.assertNotIn(EMPLOYEE, json.dumps(record))
        [row] = self.path.internal_actions(OPS, self.case_id)
        self.assertEqual((row["action_id"], row["sub"]["identities"], row["act"][0]["kind"],
                          row["outcome"], row["record"]["receipt"]),
                         ("workbench.follow_up.record", EMPLOYEE_IDS, "agent", "executed",
                          record["receipt"]))
        before = len(self.cases.get(OPS, self.case_id)["gaps"])
        with self.assertRaises(ap.ActionRefused) as caught:
            self.path.execute_internal(OPS, "workbench.follow_up.record", self.case_id,
                                       follow_up, session={"sub": {"identities": [VINCE]}})
        self.assertEqual(caught.exception.code, "session_identity_required")
        self.assertEqual(len(self.cases.get(OPS, self.case_id)["gaps"]), before)
        self.assertEqual(len(self.path.internal_actions(OPS, self.case_id)), 1)
        with self.assertRaises(CaseNotFound):
            self.path.internal_actions(OUTSIDER, self.case_id)

    def test_only_a_person_corrects_a_finding_another_author_wrote(self):
        person = fixture_session("plx", OPS.principals, channel=CHANNEL_CONSOLE,
                                 subject_email=EMPLOYEE)

        def draft(session, text="SO 3 may lack its customer PO."):
            return self.path.execute_internal(
                OPS, "workbench.draft.record", self.case_id,
                {"statement": text, "citations": [self.handle]},
                session=session)["receipt"]["finding_id"]

        def correct(session, finding_id):
            return self.path.execute_internal(
                OPS, "workbench.finding.correct", self.case_id,
                {"finding_id": finding_id, "reason": "checked again"}, session=session)

        # The person's text equals the agent's (it stays a separate human finding); a second
        # identical agent draft would merge into the first, so the third differs.
        written, own, agents = draft(person), draft(SESSION), draft(
            SESSION, "SO 3 may lack its customer PO (second reading).")
        # Same employee, same principals: an agent still never supersedes what a person
        # wrote (refused before anything is written).
        with self.assertRaises(ap.ActionRefused) as caught:
            correct(SESSION, written)
        self.assertEqual(caught.exception.code, "store_refused:supersede_needs_human_author")
        self.assertEqual(self.cases.get(OPS, self.case_id)["findings"]["superseded"], [])
        # An agent corrects its own finding; a person corrects an agent's.
        self.assertEqual(correct(SESSION, own)["read_back"]["outcome"], "verified")
        self.assertEqual(correct(person, agents)["read_back"]["outcome"], "verified")
        self.assertEqual(sorted(item["id"] for item in self.cases.get(
            OPS, self.case_id)["findings"]["superseded"]), sorted([own, agents]))

    def test_the_internal_action_audit_row_is_committed_before_the_case_write(self):
        follow_up = {"missing": "The customer PO on SO 3.", "why": "Not cited anywhere.",
                     "who_can_answer": ["the customer"], "state": "unknown"}
        seen = []

        def dies_mid_write(*args, **kwargs):
            # Inside the case write, from another connection: the row is already durable.
            seen.extend(self.new_path().internal_actions(OPS, self.case_id))
            raise SystemExit("process killed during the case write")

        with mock.patch.object(self.path.catalog, "execute", side_effect=dies_mid_write), \
                self.assertRaises(SystemExit):
            self.path.execute_internal(OPS, "workbench.follow_up.record", self.case_id,
                                       follow_up, session=SESSION)
        self.assertEqual([(row["outcome"], row["sub"]["identities"]) for row in seen],
                         [("unknown", EMPLOYEE_IDS)])
        [row] = self.path.internal_actions(OPS, self.case_id)
        self.assertEqual((row["outcome"], row["sub"]["identities"], row["record"]),
                         ("unknown", EMPLOYEE_IDS, None))
        refused = dict(follow_up, state="not_a_state")
        with self.assertRaises(ap.ActionRefused):
            self.path.execute_internal(OPS, "workbench.follow_up.record", self.case_id,
                                       refused, session=SESSION)
        self.assertEqual([row["outcome"] for row in self.path.internal_actions(
            OPS, self.case_id)], ["unknown", "refused"])



class IdentityModelTests(SubjectTestCase):
    """Decision goal2-identity (devloop/DECISIONS/2026-10-04-goal2-identity.md): ``act``
    on every step from the host's session, one separation-of-duties check on each
    committing write, matched by Entra oid as well as email, and ``act`` never a
    principal."""

    def forge_decision(self, iid, decided_by, decider_oid=None, provenance=None):
        """An authorizing decision recorded straight on the event log (as a defect,
        an older build or a hand-edited store would), past ``record_decision``."""
        record = ap.ApprovalRecord("TASK-9", "apg_forged", "approved", decided_by,
                                   self.clock().isoformat(), "0" * 64, True, "authorized",
                                   "owner", ROSTER.sha256, None, decider_oid, None,
                                   provenance)
        self.legacy_event(iid, "gate.decision", asdict(record))
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")

    def legacy_event(self, iid, kind, payload, path=None):
        """One event as a build before this decision wrote it: ``act: null`` in the
        hashed body, hash-chained and bound like any other (no current step can)."""
        path = path or self.path
        with path._connect(write=True) as db:
            first, last = (db.execute(
                f"SELECT * FROM action_event WHERE intent_id=? ORDER BY intent_seq {order} "
                "LIMIT 1", (iid,)).fetchone() for order in ("ASC", "DESC"))
            payload = {**payload, "binding_sha256": ap._binding(json.loads(
                first["payload_json"]))}
            body, at = ap._canon({**payload, "act": None}), path._now().isoformat()
            seq, label = last["intent_seq"] + 1, ap.actor_label(OPS)
            event_hash = ap._sha([iid, seq, kind, label, at, body, last["event_hash"]])
            db.execute("INSERT INTO action_event(intent_id,intent_seq,kind,actor,at,"
                       "payload_json,prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                       (iid, seq, kind, label, at, body, last["event_hash"], event_hash))

    def test_a_step_without_a_session_is_refused_before_anything_is_read_or_written(self):
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        steps = {
            "register": lambda **kw: self.path.register(OPS, self.case_id, self.propose(),
                                                        **kw),
            "prepare_gate": lambda **kw: self.path.prepare_gate(OPS, iid, "TASK-9",
                                                                self.mc.client, **kw),
            "approve_call": lambda **kw: self.path.approve_call(
                OPS, iid, "TASK-9", self.call_record(intent), **kw),
            "request_gate": lambda **kw: self.path.request_gate(OPS, iid, self.mc.client,
                                                                **kw),
            "reconcile_gate": lambda **kw: self.path.reconcile_gate(OPS, iid, self.mc.client,
                                                                    **kw),
            "record_decision": lambda **kw: self.path.record_decision(OPS, iid,
                                                                      self.mc.client, **kw),
            "dispatch": lambda **kw: self.path.dispatch(OPS, iid, self.registry,
                                                        self.mc.client, **kw),
            "read_back": lambda **kw: self.path.read_back(OPS, iid, self.fx.read_back, **kw),
            "escalate": lambda **kw: self.path.escalate(OPS, iid, "check it", **kw)}

        def written():
            with sqlite3.connect(self.dir / "actions.db") as db:
                return (db.execute("SELECT count(*) FROM action_event").fetchone()[0],
                        db.execute("SELECT count(*) FROM internal_action").fetchone()[0],
                        len(self.mc.tools_called()), self.fx.calls)

        order = ("register", "prepare_gate", "approve_call", "request_gate",
                 "reconcile_gate", "record_decision", "dispatch", "read_back", "escalate")
        for name in order:
            if name == "record_decision":
                self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
            for session in ((), (None,)):   # omitted, and passed as None
                with self.subTest(step=name, session=session):
                    before = written()
                    with self.assertRaises(ap.ActionPathError) as caught:
                        steps[name](**dict(zip(("session",), session)))
                    self.assertEqual(caught.exception.code, "session_required")
                    self.assertEqual(written(), before)
            if name not in ("register", "reconcile_gate", "escalate"):
                steps[name](session=SESSION)   # the same step with the host's session
        before = written()
        with self.assertRaises(ap.ActionRefused) as caught:
            self.path.execute_internal(OPS, "workbench.follow_up.record", self.case_id, {})
        self.assertEqual((caught.exception.code, written()), ("session_required", before))
        state = self.path.get(OPS, iid)
        self.assertEqual((state["state"], state["chain_ok"], self.fx.calls),
                         ("verified", True, 1))
        # Nothing a step wrote reads as an unknown actor.
        with sqlite3.connect(self.dir / "actions.db") as db:
            acts = [json.loads(body)["act"] for (body,) in
                    db.execute("SELECT payload_json FROM action_event")]
        self.assertTrue(acts and all(acts))

    def test_the_call_approver_is_matched_to_the_subject_by_oid(self):
        record = self.call_record
        # Vince approves MC calls under another email than the subject's Portal email,
        # but the configured (or the roster's) oid is the subject's: one person, refused.
        for options in ({"call_approvers": {VINCE: EMPLOYEE_OID}},
                        {"approver_roster": ApproverRoster(
                            {VINCE: {"role": "owner", "oid": EMPLOYEE_OID,
                                     "action_types": ALL_TYPES}},
                            source_ref="MC mc_users as listed by a person (test)")}):
            with self.subTest(options=sorted(options)):
                self.path = self.new_path(**options)
                intent = self.fresh(SESSION)
                with self.assertRaises(ap.ActionPathError) as caught:
                    self.approve(intent["intent_id"], record(intent))
                self.assertEqual(caught.exception.code, "call_approver_is_subject")
        # An approver whose oid nobody configured cannot be matched to an alias: refused.
        self.path = self.new_path(approver_roster=ApproverRoster())
        intent = self.fresh(SESSION)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.approve(intent["intent_id"], record(intent))
        self.assertEqual(caught.exception.code, "call_approver_oid_unknown")
        # Approved, then a co-request by the approver under another email (same oid):
        # the send is refused for certain and nothing reaches MC.
        self.path = self.new_path(approver_roster=self.roster)
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.approve(iid, record(intent))
        alias = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                subject_email="v.alton@petralabx.com", subject_oid=VINCE_OID)
        self.path.register(OPS, self.case_id, intent["proposal_id"], session=alias)
        sent = len(self.mc.tools_called())
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_request_refused")
        self.assertEqual(self.mc.tools_called()[sent:].count("mc_request_approval"), 0)
        # The control: the approver's own oid, not a subject's, sends the request.
        intent = self.fresh(SESSION)
        self.approve(intent["intent_id"], record(intent))
        self.path.request_gate(OPS, intent["intent_id"], self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, intent["intent_id"])["state"], "gate_requested")

    def test_a_call_approval_recorded_without_an_oid_never_sends(self):
        # Upgrade window: a call approval written before oids were required names none,
        # and none is configured now: the request is refused for certain, nothing sent.
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        expected = {"taskId": "TASK-9", "reason": intent["gate_reason"]}
        approval = {key: value for key, value in asdict(ap.CallApproval(
            "mc_request_approval", expected, ap._sha(expected), VINCE, "2026-10-01T11:00:00Z",
            "2026-10-02T11:00:00Z", "advisory record (legacy)", "0" * 64)).items()
            if key != "approver_oid"}
        self.legacy_event(iid, "call.approved", approval)
        self.path = self.new_path(approver_roster=ApproverRoster())
        sent = len(self.mc.tools_called())
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        state = self.path.get(OPS, iid)
        self.assertEqual((state["state"], state["chain_ok"]), ("gate_request_refused", True))
        with sqlite3.connect(self.dir / "actions.db") as db:
            [(payload,)] = db.execute("SELECT payload_json FROM action_event WHERE intent_id=? "
                                      "AND kind='gate.request_refused'", (iid,)).fetchall()
        self.assertEqual(json.loads(payload)["reason"], "call_approver_oid_unknown")
        self.assertEqual(self.mc.tools_called()[sent:].count("mc_request_approval"), 0)

    def test_dispatch_withdraws_a_standing_pre_v2_decision_whose_decider_has_no_oid(self):
        row_before = dict(self.fx.rows[(REL, '{"id":3}')])
        # Recorded by a build before oids (act null, no decider oid) and still standing;
        # the roster names no oid for the decider either: never dispatched.
        self.path = self.new_path(approver_roster=ApproverRoster(
            {DECIDER: {"role": "owner", "action_types": ALL_TYPES}},
            source_ref="MC mc_users as listed by a person (test)"))
        iid = self.fresh(SESSION)["intent_id"]
        self.forge_decision(iid, DECIDER)
        self.assertTrue(self.path.get(OPS, iid)["chain_ok"])   # the legacy event verifies
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"], result["business_system_writes"]),
                         ("gate_not_authorizing", "approval_withdrawn:decider_oid_unknown", 0))
        self.assert_parked(iid, "approval_withdrawn:decider_oid_unknown")
        self.assertEqual((self.fx.calls, self.fx.rows[(REL, '{"id":3}')]), (0, row_before))
        # The control: once the roster names the decider's own oid, the same standing
        # decision is checked against the subjects and dispatches.
        self.path = self.new_path(approver_roster=self.roster)
        iid = self.fresh(SESSION, preconditions=["control"])["intent_id"]
        self.forge_decision(iid, DECIDER)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["state"],
                         "dispatched")

    def test_every_step_records_the_sessions_act_and_another_actors_session_is_refused(self):
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        other = fixture_session("plx", OUTSIDER.principals, channel=CHANNEL_MCP,
                                subject_email=STEPHEN)
        forged = {"sub": {"identities": [STEPHEN]},
                  "act": [{"kind": "human", "client": "x", "session": "0" * 64}]}
        steps = (lambda s: self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=s),
                 lambda s: self.path.approve_call(OPS, iid, "TASK-9",
                                                  self.call_record(intent), session=s),
                 lambda s: self.path.request_gate(OPS, iid, self.mc.client, session=s),
                 lambda s: self.path.record_decision(OPS, iid, self.mc.client, session=s),
                 lambda s: self.path.dispatch(OPS, iid, self.registry, session=s),
                 lambda s: self.path.read_back(OPS, iid, self.fx.read_back, session=s))
        for index, step in enumerate(steps):
            if index == 3:
                self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
            for session, code in ((other, "session_actor_mismatch"),
                                  (forged, "session_identity_required")):
                with self.subTest(step=index, code=code):
                    events, mc_calls = (len(self.path.get(OPS, iid)["history"]),
                                        len(self.mc.tools_called()))
                    with self.assertRaises(ap.ActionPathError) as caught:
                        step(session)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual((len(self.path.get(OPS, iid)["history"]),
                                      len(self.mc.tools_called())), (events, mc_calls))
            step(SESSION)
        state = self.path.get(OPS, iid)
        self.assertEqual((state["state"], state["chain_ok"], self.fx.calls),
                         ("verified", True, 1))
        act = [{"kind": "agent", "channel": "mcp", "client": "plx-workbench-mcp",
                "session": SESSION.session_ref}]
        self.assertGreater(len(state["history"]), len(steps))
        self.assertEqual([item["act"] for item in state["history"]],
                         [act] * len(state["history"]))
        # In the hashed body of every stored event, so the chain check covers it.
        with sqlite3.connect(self.dir / "actions.db") as db:
            stored = [json.loads(body)["act"] for (body,) in db.execute(
                "SELECT payload_json FROM action_event WHERE intent_id=?", (iid,))]
        self.assertEqual(stored, [act] * len(state["history"]))
        self.assertTrue(self.path.view(OPS, iid)["act_known"])

    def test_a_decider_whose_roster_oid_is_the_subjects_is_the_subject(self):
        # The decider's MC label is another email than the subject's Portal email, but the
        # roster names the same Entra object id: one person, refused.
        alias = ApproverRoster({VINCE: {"role": "owner", "oid": VINCE_OID,
                                        "action_types": ALL_TYPES},
                                DECIDER: {"role": "owner", "oid": EMPLOYEE_OID,
                                          "action_types": ALL_TYPES}},
                               source_ref="MC mc_users as listed by a person (test)")
        self.path = self.new_path(approver_roster=alias)
        iid = self.drive(SESSION)
        self.assert_parked(iid, "decider_is_subject")
        decision = self.path.get(OPS, iid)["decisions"][-1]
        self.assertEqual((decision["decider_oid"], decision["subjects_sha256"]),
                         (EMPLOYEE_OID, hashlib.sha256(json.dumps(
                             EMPLOYEE_IDS, separators=(",", ":")).encode()).hexdigest()))
        # A roster member with no oid cannot be matched to an alias: never authorizes.
        self.path = self.new_path(approver_roster=ApproverRoster(
            {VINCE: {"role": "owner", "oid": VINCE_OID, "action_types": ALL_TYPES},
             DECIDER: {"role": "owner", "action_types": ALL_TYPES}},
            source_ref="MC mc_users as listed by a person (test)"))
        self.assert_parked(self.drive(SESSION), "decider_oid_unknown")
        # The control: the same decider with their own oid authorizes.
        self.path = self.new_path(approver_roster=self.roster)
        iid = self.drive(SESSION)
        self.assertEqual(self.path.get(OPS, iid)["decisions"][-1]["decider_oid"], DECIDER_OID)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")

    def test_dispatch_refuses_a_forged_authorizing_decision_by_a_subject(self):
        row_before = dict(self.fx.rows[(REL, '{"id":3}')])
        for decided_by, oid in ((EMPLOYEE, None), ("Requesting.Employee+x@petrasoap.com", None),
                                (DECIDER, EMPLOYEE_OID)):
            with self.subTest(decided_by=decided_by, oid=oid):
                iid = self.fresh(SESSION)["intent_id"]
                self.forge_decision(iid, decided_by, oid)
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((result["state"], result["reason"],
                                  result["business_system_writes"]),
                                 ("gate_not_authorizing", "approval_withdrawn:decider_is_subject",
                                  0))
                view = self.assert_parked(iid, "approval_withdrawn:decider_is_subject")
                self.assertIn("different Mission Control admin/owner",
                              view["next_safe_step"]["detail"])
        # The roster's current oid for a decider counts too, when the record has none.
        self.path = self.new_path(approver_roster=ApproverRoster(
            {VINCE: {"role": "owner", "action_types": ALL_TYPES, "oid": EMPLOYEE_OID}},
            source_ref="MC mc_users as listed by a person (test)"))
        iid = self.fresh(SESSION)["intent_id"]
        self.forge_decision(iid, VINCE)
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry, session=SESSION)["reason"],
                         "approval_withdrawn:decider_is_subject")
        self.assertEqual((self.fx.calls, self.fx.rows[(REL, '{"id":3}')]), (0, row_before))

    def test_a_restart_between_co_request_and_decision_still_refuses(self):
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
        decider = fixture_session("plx", OPS.principals, channel=CHANNEL_MCP,
                                  subject_email="d.owner@petralabx.com", subject_oid=DECIDER_OID)
        self.path.register(OPS, self.case_id, intent["proposal_id"], session=decider)
        # A new process: new case store and action path over the same files.
        self.cases = CaseStore(self.dir / "cases.db", citation_resolver=self.resolve)
        self.path = self.new_path(approver_roster=self.roster)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assert_parked(iid, "decider_is_subject")

    def test_act_is_never_a_principal_nor_read_by_any_acl_check(self):
        readers = []
        get = CaseStore.get

        def recording(store, actor, case_id):
            readers.append(actor)
            return get(store, actor, case_id)

        with mock.patch.object(CaseStore, "get", recording):
            iid = self.drive(SESSION)
            self.path.dispatch(OPS, iid, self.registry, session=SESSION)
            self.path.view(OPS, iid)
        act = self.path.get(OPS, iid)["intent"]["act"][0]
        self.assertEqual({actor for actor in readers}, {OPS})
        named = {act["kind"], act["client"], act["session"], act["channel"]}
        self.assertFalse(named & OPS.principals)
        self.assertEqual(SESSION.actor, OPS)
        # An actor made of the act's names sees nothing: the act is not a principal.
        for principals in ({act["client"]}, {act["kind"]}, {act["session"]}):
            with self.subTest(principals=principals), self.assertRaises(ap.IntentNotFound):
                self.path.view(Actor("plx", frozenset(principals)), iid)
        # plx.act refuses ``sub``/``act``/``session`` arguments outright
        # (test_action_tools ``unknown_arguments:<name>``); here a mapping in their
        # place is refused on a step before anything is read.
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.escalate(OPS, iid, "check it", session={"act": [act]})
        self.assertEqual(caught.exception.code, "session_identity_required")

    def test_a_pre_v2_intent_reads_act_unknown_and_dispatches_only_with_a_known_subject(self):
        v1 = {"schema": "plx-intent-identity/v1",
              "sub": {"principal": None, "kind": "fixture", "proof": "fixture",
                      "identities": [EMPLOYEE]},
              "act": [{"kind": "agent", "channel": "mcp", "proof": "fixture"}]}
        with mock.patch.object(ap, "intent_identity", return_value=v1):
            intent = self.fresh(SESSION)
        with mock.patch.object(ap, "intent_identity", return_value={**v1, "sub": None}):
            unknown = self.fresh(SESSION)["intent_id"]
        iid = intent["intent_id"]
        view = self.path.view(OPS, iid)
        self.assertEqual((view["act_known"], view["subject_known"]), (False, True))
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        # Recorded before subjects existed and authorized then: no subject, no dispatch.
        self.forge_decision(unknown, DECIDER, DECIDER_OID)
        result = self.path.dispatch(OPS, unknown, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"], self.fx.calls),
                         ("gate_not_authorizing", "approval_withdrawn:subject_unknown", 0))
        view = self.path.view(OPS, unknown)
        self.assertEqual((view["act_known"], view["subject_known"],
                          view["next_safe_step"]["step"]),
                         (False, False, "needs_session_subject"))
        # The known subject is checked as any other: it dispatches, its act still unknown.
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        self.assertFalse(self.path.view(OPS, iid)["act_known"])
        self.assertEqual(self.fx.calls, 1)   # only the known subject's dispatch


    def host_start(self, age, oids=None):
        """The ``ActionPath`` options of a host that resolved the roster's null oids from
        Graph /users by UPN ``age`` before now (decided item 1 of directive 46bd612e8000;
        injected transport, never the network). ``oids``: what Graph answers per UPN. A
        later call is a later host start: it rewrites the one host state every host reads."""
        from integrations.graph_directory import GraphDirectoryReader, resolve_roster
        from integrations.graph_read import build_graph
        from integrations.tests.test_graph_directory import ENV, RecordedUsers, user
        roster_file, host_state = self.dir / "approver_roster.json", self.dir / "host-state"
        entry = {"name": "Approver (test)", "mc_role": "owner", "mc_role_source": "test",
                 "oid": None, "oid_source": None}
        roster_file.write_text(json.dumps({
            "schema": APPROVER_ROSTER_SCHEMA, "note": "test", "source_ref": "test roster",
            "approvers": {VINCE: {**entry, "action_types": "all"},
                          DECIDER: {**entry, "action_types": "all"}}}), "utf-8")
        graph = RecordedUsers({upn: user(upn, oid) for upn, oid in
                               (oids or {VINCE: VINCE_OID, DECIDER: DECIDER_OID}).items()})
        read_at = self.clock.now - age
        resolve_roster(GraphDirectoryReader(build_graph(ENV, sender=graph)[0]), host_state,
                       roster=roster_file, clock=lambda: read_at)
        return load_approver_roster(roster_file, state_dir=host_state).path_options()

    def request_refusal(self, iid):
        with self.path._connect() as db:
            rows = db.execute("SELECT payload_json FROM action_event WHERE intent_id=? AND "
                              "kind='gate.request_refused'", (iid,)).fetchall()
        return json.loads(rows[-1][0])["reason"]

    def test_a_graph_resolved_oid_records_its_provenance_and_expires_at_each_check(self):
        def recorded(iid):
            state = self.path.get(OPS, iid)
            with self.path._connect() as db:
                approval = json.loads(db.execute(
                    "SELECT payload_json FROM action_event WHERE intent_id=? AND "
                    "kind='call.approved'", (iid,)).fetchone()[0])
            decision = state["decisions"][-1]
            return (state["state"], approval["approver_oid"], approval["approver_oid_provenance"],
                    decision["decider_oid"], decision["decider_oid_provenance"])

        self.path = self.new_path(**self.host_start(timedelta(minutes=30)))
        graph_read = {"source": "graph_users", "read_at": "2026-10-01T11:30:00Z"}
        self.assertEqual(recorded(self.drive(SESSION)), ("gate_authorized", VINCE_OID, graph_read,
                                                         DECIDER_OID, graph_read))
        self.path = self.new_path(approver_roster=self.roster)   # the committed control
        committed = {"source": "committed"}
        self.assertEqual(recorded(self.drive(SESSION)), ("gate_authorized", VINCE_OID, committed,
                                                         DECIDER_OID, committed))
        # Fresh when the host loaded it (23.5 h), stale an hour later at the approval: a
        # long-running host refuses it as unverified, the call approver and the decider.
        self.path = self.new_path(**self.host_start(timedelta(hours=23, minutes=30)))
        self.clock.now += timedelta(hours=1)
        intent = self.fresh(SESSION)
        with self.assertRaises(ap.ActionPathError) as caught:
            self.approve(intent["intent_id"], self.call_record(intent))
        self.assertEqual(caught.exception.code, "call_approver_oid_unverified")
        options = self.host_start(timedelta(hours=23, minutes=30))
        self.path = self.new_path(**{**options, "call_approvers": {VINCE: VINCE_OID}})
        self.clock.now += timedelta(hours=1)
        self.assert_parked(self.drive(SESSION), "decider_oid_unverified")

    def test_a_recorded_graph_oid_stands_at_request_and_dispatch_only_while_fresh(self):
        # One age rule (architect-20261011T0821Z): the Graph oid an approval or decision
        # recorded at age 23 h is re-checked when acted on at 25 h, through the roster's
        # current copy (the long-running host re-reads it: still the same stale read).
        self.path = self.new_path(**self.host_start(timedelta(hours=23)))
        intent = self.fresh(SESSION)
        self.approve(intent["intent_id"], self.call_record(intent))
        self.clock.now += timedelta(hours=2)
        sent = len(self.mc.tools_called())
        self.path.request_gate(OPS, intent["intent_id"], self.mc.client, session=SESSION)
        self.assertEqual(self.request_refusal(intent["intent_id"]),
                         "call_approver_oid_unverified")
        self.assertEqual(self.mc.tools_called()[sent:].count("mc_request_approval"), 0)
        committed_approver = {"call_approvers": {VINCE: VINCE_OID}}
        self.path = self.new_path(**{**self.host_start(timedelta(hours=23)),
                                     **committed_approver})
        iid = self.drive(SESSION)
        self.clock.now += timedelta(hours=2)
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"], self.fx.calls),
                         ("gate_not_authorizing", "approval_withdrawn:decider_oid_unverified", 0))
        # A host start since then read Graph again: the stale copy is re-read at the check.
        # The same oid stands and dispatches as a fresh one always did; another oid (the
        # account was replaced under the same UPN) refuses, for the approver and the decider.
        # An oid provenance this build does not know is held to the same rule: no oid
        # usable now, none counts.
        self.path = self.new_path(approver_roster=ApproverRoster(
            {DECIDER: {"role": "owner", "action_types": ALL_TYPES}},
            source_ref="MC mc_users as listed by a person (test)"))
        iid = self.fresh(SESSION)["intent_id"]
        self.forge_decision(iid, DECIDER, DECIDER_OID, {"source": "portal"})
        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_not_authorizing", "approval_withdrawn:decider_oid_unknown"))
        recreated = "0000b000-0000-4000-8000-0000000000f0"

        def decide(iid):
            self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
            self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
            self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)

        for oids, refusal in (({VINCE: recreated}, "call_approver_oid_changed"),
                              ({DECIDER: recreated}, "approval_withdrawn:decider_oid_changed"),
                              ({}, None)):
            with self.subTest(oids=oids):
                self.path = self.new_path(**self.host_start(timedelta(hours=23)))
                intent = self.fresh(SESSION)
                iid = intent["intent_id"]
                self.approve(iid, self.call_record(intent))
                if DECIDER in oids:   # decided while the host's copy was fresh
                    decide(iid)
                self.host_start(timedelta(0), {VINCE: VINCE_OID, DECIDER: DECIDER_OID, **oids})
                self.clock.now += timedelta(hours=2)
                if VINCE in oids:
                    self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
                    self.assertEqual(self.request_refusal(iid), refusal)
                    continue
                if not oids:          # requested and decided on the re-read copy
                    decide(iid)
                    self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((result["state"], result["reason"]),
                                 ("dispatched", None) if refusal is None
                                 else ("gate_not_authorizing", refusal))
        self.assertEqual(self.fx.calls, 1)   # only the same-oid re-read dispatched

    def test_a_provenance_less_oid_not_the_committed_one_stands_24_h_from_its_decision(self):
        # architect-20261011T1621Z: a decision recorded with an oid but no provenance (a
        # build between the Graph resolution and its provenance) whose oid is not the
        # roster's committed one was read from Graph: 24 h from its decided_at, like any
        # Graph oid. The decision itself may stand longer (72 h here), so only the oid's
        # age refuses. The committed oid is the control: today's rule, it dispatches.
        unresolved = ApproverRoster({DECIDER: {"role": "owner", "action_types": ALL_TYPES}},
                                    source_ref="MC mc_users as listed by a person (test)")
        graph_read = "0000b000-0000-4000-8000-0000000000f1"
        stale = ("gate_not_authorizing", "approval_withdrawn:decider_oid_unverified:older_than_24h")
        for roster, oid, age, expected in (
                (self.roster, graph_read, timedelta(hours=25), stale),
                (unresolved, DECIDER_OID, timedelta(hours=25), stale),
                (self.roster, graph_read, timedelta(hours=23), ("dispatched", None)),
                (self.roster, DECIDER_OID, timedelta(hours=25), ("dispatched", None))):
            with self.subTest(oid=oid, age=age, committed=roster is self.roster):
                self.path = self.new_path(approver_roster=roster,
                                          decision_max_age=timedelta(hours=72))
                self.fx.rows[(REL, '{"id":3}')]["po_reference"] = None   # undispatched
                self.case_id = self.path.open_case(
                    OPS, f"Set the customer PO on SO 3 to PO-7788 or PO-9999? ({age})", roots=["SO-3"],
                    objective_author_kind="human", session=SESSION)
                iid = self.fresh(SESSION)["intent_id"]
                self.forge_decision(iid, DECIDER, oid)
                self.clock.now += age
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((result["state"], result["reason"]), expected)
                if expected is stale:
                    view = self.assert_parked(iid, expected[1])
                    self.assertIn("new human-approved request",
                                  view["next_safe_step"]["detail"])
        self.assertEqual(self.fx.calls, 2)
    def forge_call_approval(self, iid, tool, arguments, approver, oid, approved_at,
                            path=None):
        """A current call approval naming ``oid`` with no provenance (a build between the
        Graph resolution and its provenance), recorded straight on the event log."""
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        approval = asdict(ap.CallApproval(
            tool, arguments, ap._sha(arguments), approver, approved_at.strftime(stamp),
            (approved_at + timedelta(hours=24)).strftime(stamp), "advisory record (legacy)",
            ap._sha([approver, approved_at.isoformat()]), oid))
        del approval["approver_oid_provenance"]
        self.legacy_event(iid, "call.approved", approval, path)

    def test_a_provenance_less_call_approver_oid_stands_24_h_from_its_approval(self):
        # Overseer ad235a569e34 item 2: the age rule of a decider's provenance-less oid
        # (above) holds for the call approver's where a call is sent, request_gate and
        # send_call: an oid that is not the committed one stands 24 h from approved_at.
        graph_read = "0000b000-0000-4000-8000-0000000000f1"   # not the committed VINCE_OID
        stale = "call_approver_oid_unverified:older_than_24h"
        ages = ((timedelta(hours=25), stale), (timedelta(hours=23), None))
        for age, refusal in ages:
            with self.subTest(step="request_gate", age=age):
                intent = self.fresh(SESSION)
                iid = intent["intent_id"]
                self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
                self.forge_call_approval(iid, "mc_request_approval",
                                         {"taskId": "TASK-9", "reason": intent["gate_reason"]},
                                         VINCE, graph_read, self.clock.now - age)
                sent = len(self.mc.tools_called())
                self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
                state = self.path.get(OPS, iid)["state"]
                self.assertEqual((state, self.mc.tools_called()[sent:].count(
                    "mc_request_approval")), ("gate_request_refused", 0) if refusal
                    else ("gate_requested", 1))
                if refusal:
                    self.assertEqual(self.request_refusal(iid), refusal)
        world = gx.Lifecycle(self.dir / "world")
        world.mc.buckets.add(gx.FOLLOW_UP_BUCKET)
        writer = world.mc.client(self.dir / "mc-create-ledger.jsonl",
                                 write_tools=frozenset({"mc_create_task"}))
        for number, (age, refusal) in enumerate(ages, 1):
            with self.subTest(step="send_call", age=age):
                _case_id, iid = world.follow_up_intent(number)
                prepared = world.path.get(gx.ACTOR, iid)["prepared"]
                self.forge_call_approval(iid, prepared["tool"], prepared["arguments"],
                                         gx.CALL_APPROVER, graph_read, world.clock.now - age,
                                         world.path)
                result = world.path.send_call(gx.ACTOR, iid, writer, session=gx.SESSION)
                self.assertEqual((result["state"], result.get("reason")),
                                 ("dispatch_refused", refusal) if refusal
                                 else ("dispatched", None))
        self.assertEqual(len(world.mc.tasks), 1)   # only the fresh approval's call created

    def test_a_graph_read_roster_oid_is_never_the_committed_one(self):
        # A decision recorded with no oid provenance whose oid is the one the roster now
        # holds from Graph (graph_users, fresh) is still a Graph oid: it stands 24 h from
        # decided_at. The clock ends at the wall clock, so the Graph read is fresh by the
        # store's own time too.
        wall = datetime.now(timezone.utc)
        stale = ("gate_not_authorizing", "approval_withdrawn:decider_oid_unverified:older_than_24h")
        for age, expected in ((timedelta(hours=25), stale),
                              (timedelta(hours=23), ("dispatched", None))):
            with self.subTest(age=age):
                self.fx.rows[(REL, '{"id":3}')]["po_reference"] = None   # undispatched
                self.clock.now = wall - age
                iid = self.fresh(SESSION)["intent_id"]
                self.forge_decision(iid, DECIDER, DECIDER_OID)
                self.clock.now = wall
                self.path = self.new_path(**self.host_start(timedelta(minutes=30)),
                                          decision_max_age=timedelta(hours=72))
                self.assertEqual(
                    (self.path.approver_roster.oid_provenance(DECIDER)["source"],
                     self.path.approver_roster.oid_of(DECIDER, wall)),
                    ("graph_users", DECIDER_OID))
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((result["state"], result["reason"]), expected)
        self.assertEqual(self.fx.calls, 1)



class FourEyesTests(SubjectTestCase):
    """Four-eyes (GOAL2-ACTIONS.md section 2d): whoever approved a call for an intent
    never decides its MC gate, by email or by Entra oid. The refusal records nothing:
    the intent stays in ``gate_requested`` and nothing is dispatched."""

    forge_decision = IdentityModelTests.forge_decision
    legacy_event = IdentityModelTests.legacy_event

    def requested(self):
        """One intent whose gate Vince's call approval requested; returns its id."""
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        self.assertEqual(self.path.get(OPS, iid)["call_approvers"][-1]["approved_by"], VINCE)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        return iid

    def assert_refused_in_place(self, iid, decided_by):
        """``decided_by`` decides in MC: recorded as non-authorizing, parked, nothing
        dispatched; the next step is a new gate."""
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=decided_by)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        view = self.assert_parked(iid, "decider_is_call_approver")
        self.assertEqual(view["next_safe_step"]["step"], "prepare_gate")
        self.assertIn("four-eyes", view["next_safe_step"]["detail"])
        self.assertNotIn(VINCE, json.dumps(view))   # no identity projected
        return view

    def test_the_intent_recovers_by_a_new_gate_decided_by_someone_else(self):
        # MC decisions are final: the refused gate is never re-decided. A new request
        # (prepared, approved, sent) decided by a different owner dispatches once.
        self.mc.final_decisions = True
        iid = self.requested()
        self.assert_refused_in_place(iid, VINCE)
        old_gate = self.path.get(OPS, iid)["gate"]["gate_id"]
        with self.assertRaises(ValueError):   # as real MC: gate_already_decided
            self.mc.decide(old_gate, decided_by=DECIDER)
        intent = self.path.get(OPS, iid)["intent"]
        self.approve(iid, self.call_record(intent))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        new_gate = self.path.get(OPS, iid)["gate"]["gate_id"]
        self.assertNotEqual(new_gate, old_gate)
        self.mc.decide(new_gate, decided_by=DECIDER)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_authorized")
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        self.assertEqual(self.fx.calls, 1)

    def test_the_call_approver_never_decides_the_gate_by_email(self):
        for decided_by in (VINCE, "Vince+mc@PetraSoap.com"):
            with self.subTest(decided_by=decided_by):
                self.assert_refused_in_place(self.requested(), decided_by)

    def test_the_call_approver_never_decides_the_gate_by_oid(self):
        # The MC label is another mailbox, but the roster names Vince's Entra oid.
        self.path = self.new_path(approver_roster=ApproverRoster(
            {VINCE: {"role": "owner", "action_types": ALL_TYPES, "oid": VINCE_OID},
             DECIDER: {"role": "owner", "action_types": ALL_TYPES, "oid": VINCE_OID}},
            source_ref="MC mc_users as listed by a person (test)"))
        self.assert_refused_in_place(self.requested(), DECIDER)

    def test_any_earlier_rounds_approver_is_refused_too(self):
        # Vince approved round 1 (rejected in MC); Rita approves round 2, yet Vince
        # approved a call for this intent: his decision on round 2's gate is refused.
        self.path = self.new_path(approver_roster=self.roster,
                                  call_approvers={VINCE: VINCE_OID, RITA: RITA_OID})
        iid = self.requested()
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], status="rejected",
                       decided_by=DECIDER)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_rejected")
        intent = self.path.get(OPS, iid)["intent"]
        self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client, session=SESSION)
        self.path.approve_call(OPS, iid, "TASK-9", self.call_record(intent, approved_by=RITA),
                               session=SESSION)
        transport, _ = hub(self.mc.sender, clock=self.clock)
        both = MissionControlMcpClient(mc_config(), hub=transport,
                                       write_tools={"mc_request_approval"},
                                       write_approvers={VINCE, RITA}, write_ledger=self.mc.ledger)
        self.path.request_gate(OPS, iid, both, session=SESSION)
        self.assertEqual([a["approved_by"] for a in self.path.get(OPS, iid)["call_approvers"]],
                         [VINCE, RITA])
        self.assert_refused_in_place(iid, VINCE)

    def test_a_send_by_an_approver_the_mc_client_does_not_accept_says_so(self):
        # Rita may approve calls here, but the MC client's write door names only Vince:
        # refused before anything is sent, saying the approval is used up.
        self.path = self.new_path(approver_roster=self.roster,
                                  call_approvers={VINCE: VINCE_OID, RITA: RITA_OID})
        intent = self.fresh(SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent, approved_by=RITA))
        sent = len(self.mc.requests)
        result = self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_request_refused", ap.NOT_MC_WRITE_APPROVER))
        view = self.path.view(OPS, iid)
        self.assertEqual(view["state"], "gate_request_refused")
        self.assertEqual(self.path.get(OPS, iid)["history"][-1]["kind"], "gate.request_refused")
        self.assertNotIn("gate.request_started",
                         [h["kind"] for h in self.path.get(OPS, iid)["history"]])
        self.assertEqual(len(self.mc.requests), sent)
        self.assertEqual(ap.NOT_MC_WRITE_APPROVER.split(":")[0],
                         "call_approver_not_an_mc_write_approver")
        self.assertIn("used up", ap.NOT_MC_WRITE_APPROVER)

    def test_a_rejection_by_the_approver_is_recorded_and_a_different_decider_proceeds(self):
        iid = self.requested()
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], status="rejected",
                       decided_by=VINCE)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_rejected")   # never authorizes
        iid = self.requested()
        self.mc.decide(self.path.get(OPS, iid)["gate"]["gate_id"], decided_by=DECIDER)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["decisions"][-1]["reason_code"], "authorized")
        self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                            session=SESSION)["state"], "dispatched")
        self.assertNotIn(VINCE, json.dumps(self.path.view(OPS, iid)))  # never projected

    def test_dispatch_withdraws_a_recorded_decision_by_the_call_approver(self):
        # As an older build or a hand-edited store would have recorded it.
        for oid in (None, VINCE_OID):
            with self.subTest(oid=oid):
                intent = self.fresh(SESSION)
                iid = intent["intent_id"]
                self.approve(iid, self.call_record(intent))
                self.forge_decision(iid, DECIDER if oid else VINCE, oid)
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((result["state"], result["reason"]),
                                 ("gate_not_authorizing",
                                  "approval_withdrawn:decider_is_call_approver"))
                self.assertIn("four-eyes",
                              self.path.view(OPS, iid)["next_safe_step"]["detail"])
        self.assertEqual(self.fx.calls, 0)


if __name__ == "__main__":
    unittest.main()
