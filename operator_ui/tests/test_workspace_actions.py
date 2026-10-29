"""The workspace's act control over HTTP: server routes -> ``ObjectWorkspace.act`` ->
``WorkspaceActions`` -> the existing ``ActionPath`` (WS-11).

Two isolated worlds: the action path's own test world (an owner-system intent on the
in-memory ``FixtureSystem`` and a Portal intent, the real ``MissionControlMcpClient`` over
an injected fake MC) and ``operating.goal2_exec.Lifecycle`` (the MC follow-up task through
the client's write door into ``IsolatedMissionControl``). Ontology tools are stubs: only
the object card's citation matters here. Evidence: isolated-fixture / injected-transport;
no network beyond 127.0.0.1.
"""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import quote

from ontology.core import Actor
from operating import action_path as ap
from operating import goal2_exec as gx
from operating.cases import CitationError
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests import test_action_path as tap
from operator_ui import object_workspace as objects
from operator_ui.server import make_server

OBJECT = 'SalesOrder:portal.app_sales_order:{"id":3}'
# Sees the case, but not the Portal row its proposals cite.
LIMITED = Actor("plx", frozenset({"ops", "limited"}))


def stub_tools(card_handle):
    """The four tools an object workspace needs; the card cites ``card_handle``."""
    card = {"object": {"object_id": OBJECT, "object_type": "SalesOrder"},
            "values": [{"property": "order_number", "value": "SO-3",
                        "citation": {"l1_handle": card_handle, "digest": "a" * 64}}]}
    answers = {"plx.object": card}
    return {name: {"handler": (lambda name: lambda arguments, actor: answers.get(name, {}))(name)}
            for name in objects.TOOLS[:4]}


def serve(test, session, actions, *, cases=None, path=None, card_handle=tap.API_ORDER):
    """A fixture console for ``session`` whose object workspace has ``actions``."""
    workspace = objects.ObjectWorkspace(
        stub_tools(card_handle), session.actor, actions=actions,
        intents=objects.CaseIntents(cases, path) if cases is not None else None)
    server = make_server(case_workspace=SimpleNamespace(actor=session.actor), session=session,
                         object_workspace=workspace)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    test.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10), server.server_close()))
    port, token = server.server_address[1], server.operator_access_token

    def call(method, route, body=None):
        conn = HTTPConnection("127.0.0.1", port, timeout=60)
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "X-Operator-Token": call.csrf})
        conn.request(method, route, body=data, headers=headers)
        response = conn.getresponse()
        value = response.status, json.loads(response.read())
        conn.close()
        return value
    call.csrf = None
    call.csrf = call("GET", "/api/session")[1]["csrf_token"]
    return call


class OwnerActionTest(tap.ActionPathTestCase):
    """An owner-system intent: request approval on an MC task, a person approves that exact
    request, the MC decision, dispatch once, read back; the Portal intent stays refused."""

    def setUp(self):
        super().setUp()
        # The employee the actions are for, at the console (a person's session).
        self.console = fixture_session("plx", tap.OPS.principals, channel=CHANNEL_CONSOLE,
                                       subject_email=tap.EMPLOYEE,
                                       subject_oid=tap.EMPLOYEE_OID)
        proposal = self.path.record_proposal(
            tap.OPS, self.case_id, ap.FIXTURE_CAPABILITY,
            {"field": "po_reference", "value": "PO-7788"},
            [{"check": "field_empty", "citation": self.handle, "field": "po_reference"},
             "a person at PLX confirms the PO belongs to this order"],
            citations=[self.handle, tap.API_ORDER], expected_effect=tap.EFFECT,
            session=tap.SESSION)["proposal_id"]
        self.owned = self.path.register(tap.OPS, self.case_id, proposal, session=tap.SESSION)
        self.portal = self.path.register(tap.OPS, self.case_id, self.propose(
            "portal.sales_order.set_customer_po", tap.API_ORDER,
            {"relation": "public.app_sales_order", "key": {"id": 3},
             "fields": ["po_reference"], "equals": "PO-7788"}), session=tap.SESSION)

    def resolve(self, handle, actor):
        if "limited" in actor.principals and handle.startswith("api://"):
            raise CitationError("not visible")
        return super().resolve(handle, actor)

    def actions(self, session, actors=tap.OPS.principals):
        return objects.WorkspaceActions(self.path, session, actors=actors, mc=self.mc.client,
                                        registry=self.registry, read_back=self.fx.read_back)

    def approver(self, email=tap.VINCE, oid=tap.VINCE_OID):
        """A person's own console session (a roster approver by default)."""
        return fixture_session("plx", tap.OPS.principals, channel=CHANNEL_CONSOLE,
                               subject_email=email, subject_oid=oid)

    def serve(self, session, actors=tap.OPS.principals):
        return serve(self, session, self.actions(session, actors), cases=self.cases,
                     path=self.path)

    def test_dispatch_only_after_another_persons_approval_then_once_and_read_back(self):
        call = self.serve(self.console)
        owned, portal = self.owned["intent_id"], self.portal["intent_id"]
        step = lambda iid, name, **extra: call("POST", "/api/object-action",  # noqa: E731
                                               {"intent_id": iid, "step": name, **extra})
        status, listed = call("GET", "/api/object-actions?id=" + quote(OBJECT, safe=""))
        self.assertEqual(status, 200, listed)
        controls = {item["intent_id"]: item["control"] for item in listed["intents"]["intents"]}
        self.assertEqual((controls[owned]["step"], controls[owned]["available"],
                          controls[owned]["needs_task_id"]), ("prepare_gate", True, True))
        self.assertEqual((controls[portal]["available"], controls[portal]["why"]),
                         (False, "production_target_refused"))

        # No approval: dispatch is refused by the action path; nothing reaches the owner.
        status, refused = step(owned, "dispatch")
        self.assertEqual((status, refused["error"]), (409, "gate_not_authorized"))
        # An owner-system target is refused before any MC read, whatever is asked.
        status, refused = step(portal, "prepare_gate", task_id="TASK-9")
        self.assertEqual((status, refused["error"]), (409, "production_target_refused"))
        self.assertEqual(self.mc.sender.calls, [])
        # Nothing is prepared yet: there is no call to approve.
        self.assertEqual(step(owned, "approve_call", approves_sha256="0" * 64)[1]["error"],
                         "invalid_state:proposed")

        status, prepared = step(owned, "prepare_gate", task_id="TASK-9")
        self.assertEqual((status, prepared["state"], prepared["control"]["step"]),
                         (200, "gate_prepared", "approve_call"))
        self.assertEqual(step(owned, "dispatch")[1]["error"], "gate_not_authorized")
        # The approval of that exact request is a person's other than the employee it is
        # for; an agent never approves. The employee cannot approve at their own console.
        shown = prepared["control"]["approves_sha256"]
        status, refused = step(owned, "approve_call", approves_sha256=shown)
        self.assertEqual((status, refused["error"]), (409, "call_approver_is_subject"))
        intent = self.path.get(tap.OPS, owned)["intent"]
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.approve_call(tap.OPS, owned, "TASK-9", self.call_record(
                intent, approved_by="agent:planner"), session=tap.SESSION)
        self.assertEqual(caught.exception.code, "call_approver_not_allowed")
        # A roster approver approves it at their own console session.
        before = list(self.mc.sender.calls)
        status, approved = self.serve(self.approver())("POST", "/api/object-action", {
            "intent_id": owned, "step": "approve_call", "approves_sha256": shown})
        self.assertEqual((status, approved["state"], approved["control"]["step"]),
                         (200, "call_approved", "request_gate"))
        self.assertEqual(self.mc.sender.calls, before)   # approving sent nothing, not even a read
        self.assertNotIn("mc_request_approval", self.mc.tools_called())

        status, requested = step(owned, "request_gate")
        self.assertEqual((status, requested["state"]), (200, "gate_requested"))
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), 1)
        self.assertEqual(step(owned, "record_decision")[1]["state"], "gate_requested")  # pending
        self.mc.decide(self.path.get(tap.OPS, owned)["gate"]["gate_id"])
        status, decided = step(owned, "record_decision")
        self.assertEqual((decided["state"], decided["control"]["step"]),
                         ("gate_authorized", "dispatch"))

        status, sent = step(owned, "dispatch")
        self.assertEqual((status, sent["state"], sent["outcome"], sent["done"]),
                         (200, "dispatched", "unknown", False))
        status, again = step(owned, "dispatch")   # a repeat click replays
        self.assertEqual((status, again["result"]["replayed"], self.fx.calls), (200, True, 1))
        status, verified = step(owned, "read_back")
        self.assertEqual((status, verified["state"], verified["outcome"], verified["done"]),
                         (200, "verified", "verified", True))
        self.assertEqual(self.fx.calls, 1)

    def test_only_a_roster_person_who_is_no_subject_approves_the_shown_call_once(self):
        """The console's approve step: a person's own session on the approver roster with
        a known Entra oid approves exactly the call shown (its digest), never an agent, the
        eval principal, a person off the roster, an unknown or mismatched oid, a subject,
        a stale view or a second time; nothing is sent by any of it."""
        from dataclasses import replace

        from operating.employee_permissions import EmployeePrincipal
        from operating.goal2_mc_approval import ApproverRoster
        from operating.session_identity import PROOF_EVAL

        owned = self.owned["intent_id"]
        request = self.serve(self.console)
        self.assertEqual(request("POST", "/api/object-action", {
            "intent_id": owned, "step": "prepare_gate", "task_id": "TASK-9"})[0], 200)
        sent = list(self.mc.sender.calls)
        status, listed = self.serve(self.approver())(
            "GET", "/api/object-actions?id=" + quote(OBJECT, safe=""))
        control = next(item["control"] for item in listed["intents"]["intents"]
                       if item["intent_id"] == owned)
        shown, subject = control["approves_sha256"], control["approves"]
        state = self.path.get(tap.OPS, owned)
        # What the click approves is shown first: type, target, traced parameters, effect,
        # the exact call; bound to this intent and this prepared request.
        self.assertEqual((control["step"], control["available"], subject["action_type"],
                          subject["target"], subject["call"], subject["binding_sha256"],
                          shown),
                         ("approve_call", True, ap.FIXTURE_CAPABILITY,
                          {"relation": tap.REL, "key": {"id": 3}},
                          {"tool": "mc_request_approval", "arguments": {
                              "taskId": "TASK-9", "reason": state["intent"]["gate_reason"]}},
                          ap._binding(state["intent"]), ap.call_approval_sha256(state)))
        self.assertEqual(subject["parameters"], [
            {"name": name, "source": trace} for name, trace in
            sorted(state["intent"]["parameter_trace"]["traces"].items())])
        self.assertTrue(subject["parameters"])

        # Another intent, prepared too: its digest is not this call's.
        other = self.path.register(tap.OPS, self.case_id, self.path.record_proposal(
            tap.OPS, self.case_id, ap.FIXTURE_CAPABILITY,
            {"field": "po_reference", "value": "PO-9999"},
            [{"check": "field_empty", "citation": self.handle, "field": "po_reference"}],
            citations=[self.handle, tap.API_ORDER],
            expected_effect={**tap.EFFECT, "equals": "PO-9999"},
            session=tap.SESSION)["proposal_id"], session=tap.SESSION)["intent_id"]
        self.path.prepare_gate(tap.OPS, other, "TASK-9", self.mc.client, session=tap.SESSION)
        sent = list(self.mc.sender.calls)
        other_shown = ap.call_approval_sha256(self.path.get(tap.OPS, other))
        self.assertNotEqual(other_shown, shown)

        employee = EmployeePrincipal("0000c000-0000-4000-8000-0000000000e1", tap.VINCE_OID)
        evaluator = replace(self.approver(), proof=PROOF_EVAL, employee=employee,
                            principals=frozenset({employee.principal}),
                            expires_at=4102444800)
        nobody = "someone.else@petrasoap.com"
        cases = [
            ("the employee it is for", self.console, shown, (409, "call_approver_is_subject")),
            ("a person off the roster", self.approver(
                nobody, "0000b000-0000-4000-8000-0000000000f1"), shown,
             (409, "call_approver_not_allowed")),
            ("a roster member who approves no type", self.approver(
                tap.STEPHEN, "0000b000-0000-4000-8000-0000000000f2"), shown,
             (409, "call_approver_not_allowed")),
            ("a session without an oid", self.approver(oid=None), shown,
             (409, "call_approver_oid_unknown")),
            ("a session whose oid is not the roster's", self.approver(
                oid="0000b000-0000-4000-8000-0000000000f3"), shown,
             (409, "call_approver_session_oid_mismatch")),
            ("a stale view: another intent's call", self.approver(), other_shown,
             (409, "call_approval_stale")),
            ("no shown call", self.approver(), None, (409, "call_approval_subject_required"))]
        for name, session, digest, expected in cases:
            with self.subTest(name):
                body = {"intent_id": owned, "step": "approve_call"}
                if digest is not None:
                    body["approves_sha256"] = digest
                status, refused = self.serve(session)("POST", "/api/object-action", body)
                self.assertEqual((status, refused.get("detail") or refused.get("error")),
                                 expected, refused)
        with self.subTest("an agent session"):   # no console serves one; the step refuses it
            with self.assertRaises(PermissionError) as caught:
                self.actions(tap.SESSION).take(tap.OPS, {
                    "intent_id": owned, "step": "approve_call", "approves_sha256": shown})
            self.assertEqual(str(caught.exception), "only_a_person_acts")
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.approve_in_session(tap.OPS, owned, session=tap.SESSION,
                                             shown_sha256=shown)
            self.assertEqual(caught.exception.code, "only_a_person_approves")
        with self.subTest("the eval principal"):
            actions = self.actions(evaluator, actors=evaluator.actor.principals)
            self.assertEqual(actions.control(evaluator.actor, {
                "intent_id": owned, "next_safe_step": {"step": "approve_call"}})["why"],
                "eval_principal_never_approves")
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.approve_in_session(evaluator.actor, owned, session=evaluator,
                                             shown_sha256=shown)
            self.assertEqual(caught.exception.code, "eval_principal_never_approves")
        with self.subTest("a roster approver with no known oid"):
            self.path = self.new_path(call_approvers=frozenset(), approver_roster=ApproverRoster(
                {tap.VINCE: {"role": "owner", "action_types": tap.ALL_TYPES, "oid": None}},
                source_ref="MC mc_users as listed by a person (test)"))
            status, refused = self.serve(self.approver())("POST", "/api/object-action", {
                "intent_id": owned, "step": "approve_call", "approves_sha256": shown})
            self.assertEqual((status, refused["error"]), (409, "call_approver_oid_unknown"))
            self.path = self.new_path()
        self.assertEqual(self.path.get(tap.OPS, owned)["state"], "gate_prepared")

        approve = self.serve(self.approver())
        status, approved = approve("POST", "/api/object-action", {
            "intent_id": owned, "step": "approve_call", "approves_sha256": shown})
        self.assertEqual((status, approved["state"]), (200, "call_approved"))
        record = self.path.get(tap.OPS, owned)["call_approval"]
        self.assertEqual((record["approved_by"], record["approver_oid"], record["tool"]),
                         (tap.VINCE, tap.VINCE_OID, "mc_request_approval"))
        self.assertTrue(record["evidence_ref"].endswith(":shown:" + shown))
        # Single use: a second click is refused, the same approval never stands twice.
        status, again = approve("POST", "/api/object-action", {
            "intent_id": owned, "step": "approve_call", "approves_sha256": shown})
        self.assertEqual((status, again["error"]), (409, "invalid_state:call_approved"))
        self.assertEqual(self.mc.sender.calls, sent)   # nothing was sent by any of it
        self.assertEqual(self.path.get(tap.OPS, other)["state"], "gate_prepared")

    def test_the_offered_step_is_the_action_paths_next_safe_step(self):
        """Reproduction (direction review of 5eecea1e): the control follows
        ``next_safe_step`` in every state and override (escalate, reregister, a closed
        case, a pending gate to reject), never a table of its own: a console step is an
        ``ActionPath`` method, any other step is unavailable with the path's detail."""
        actions = self.actions(self.console)
        folded = self.path.get(tap.OPS, self.owned["intent_id"])
        unseen = [{"trigger": "effect_not_observed"}] * ap.READ_BACKS_BEFORE_ESCALATION
        cases = [(state, cap, {}, None) for state in ap.STATES
                 for cap in (ap.FIXTURE_CAPABILITY, objects.FOLLOW_UP_CAPABILITY)]
        cases += [("dispatched", ap.FIXTURE_CAPABILITY, {"recovery": unseen}, None),
                  ("dispatched_unknown", objects.FOLLOW_UP_CAPABILITY,
                   {"recovery": unseen}, None),
                  ("gate_authorized", ap.FIXTURE_CAPABILITY, {"authors": []}, None),
                  ("gate_requested", ap.FIXTURE_CAPABILITY, {"authors": []}, None),
                  ("proposed", ap.FIXTURE_CAPABILITY, {}, "case_closed"),
                  ("dispatched", ap.FIXTURE_CAPABILITY, {}, "case_closed"),
                  ("gate_requested", ap.FIXTURE_CAPABILITY, {}, "precondition_citation_changed")]
        seen = set()
        for state, cap, extra, blocked in cases:
            folded_now = {**folded, "state": state, "decisions": [], "recovery": [],
                          "intent": {**folded["intent"], "capability": cap}, **extra}
            safe = ap.next_safe_step(folded_now, blocked)
            with self.subTest(state=state, cap=cap, step=safe["step"]):
                seen.add(safe["step"])
                control = actions.control(tap.OPS, {
                    "intent_id": self.owned["intent_id"], "state": state, "capability": cap,
                    "next_safe_step": safe, "production_guard": None})
                self.assertEqual(control["step"], safe["step"])
                if safe["step"] in objects.ACTION_STEPS:
                    self.assertTrue(callable(getattr(self.path, safe["step"])))
                    self.assertEqual((control["available"], control["why"]), (True, None))
                else:
                    self.assertEqual((control["available"], control["why"]),
                                     (False, safe["detail"]))
        self.assertTrue({"escalate", "reregister", "reject_gate_in_mc", "none",
                         "reconcile_in_owning_system"} <= seen, seen)
        # A step the path does not offer is not taken from the console.
        status, refused = self.serve(self.console)("POST", "/api/object-action", {
            "intent_id": self.owned["intent_id"], "step": "escalate"})
        self.assertEqual(status, 400, refused)

    def test_the_person_who_requests_an_approval_can_never_approve_it(self):
        """Reproduction (review of fe2e9984): an employee who is not a subject requests the
        approval at the console, then decides that same request in MC as an MC owner; the
        decision never authorizes and nothing is dispatched."""
        from operating.goal2_mc_approval import ApproverRoster

        self.path = self.new_path(approver_roster=ApproverRoster(
            {tap.VINCE: {"role": "owner", "oid": tap.VINCE_OID, "action_types": tap.ALL_TYPES},
             tap.DECIDER: {"role": "owner", "oid": tap.DECIDER_OID,
                           "action_types": tap.ALL_TYPES}},
            source_ref="MC mc_users as listed by a person (test)"))
        requester = fixture_session("plx", tap.OPS.principals, channel=CHANNEL_CONSOLE,
                                    subject_email=tap.DECIDER, subject_oid=tap.DECIDER_OID)
        call = self.serve(requester)
        owned = self.owned["intent_id"]
        step = lambda name, **extra: call("POST", "/api/object-action",  # noqa: E731
                                          {"intent_id": owned, "step": name, **extra})
        self.assertEqual(step("prepare_gate", task_id="TASK-9")[1]["state"], "gate_prepared")
        intent = self.path.get(tap.OPS, owned)["intent"]
        with self.assertRaises(ap.ActionPathError) as caught:   # nor the exact call
            self.path.approve_call(tap.OPS, owned, "TASK-9", self.call_record(
                intent, approved_by=tap.DECIDER), session=tap.SESSION)
        self.assertEqual(caught.exception.code, "call_approver_is_subject")
        self.path.approve_call(tap.OPS, owned, "TASK-9", self.call_record(intent),
                               session=tap.SESSION)
        self.assertEqual(step("request_gate")[1]["state"], "gate_requested")
        self.mc.decide(self.path.get(tap.OPS, owned)["gate"]["gate_id"],
                       decided_by=tap.DECIDER)
        status, decided = step("record_decision")
        self.assertEqual((status, decided["state"]), (200, "gate_not_authorizing"))
        self.assertEqual(self.path.get(tap.OPS, owned)["decisions"][-1]["reason_code"],
                         "decider_is_subject")
        status, refused = step("dispatch")
        self.assertEqual((status, refused["error"], self.fx.calls),
                         (409, "gate_not_authorized", 0))

    def test_a_restricted_reader_gets_no_control_and_no_leak(self):
        owned = self.owned["intent_id"]
        body = {"intent_id": owned, "step": "dispatch"}
        outsider = fixture_session("plx", tap.OUTSIDER.principals, channel=CHANNEL_CONSOLE,
                                   subject_email="someone.else@petrasoap.com")
        limited = fixture_session("plx", LIMITED.principals, channel=CHANNEL_CONSOLE,
                                  subject_email="someone.else@petrasoap.com")
        for name, session, withheld in (("cannot see the case", outsider, 0),
                                        ("cannot see the cited evidence", limited, 2)):
            with self.subTest(name):
                call = self.serve(session, actors=session.actor.principals)
                status, listed = call("GET", "/api/object-actions?id=" + quote(OBJECT, safe=""))
                self.assertEqual((status, listed["intents"]["intents"],
                                  listed["intents"]["unmatchable"]["withheld"]),
                                 (200, [], withheld))
                status, opened = call("GET", "/api/object?id=" + quote(OBJECT, safe=""))
                self.assertEqual((status, opened["intents"]["intents"]), (200, []))
                status, refused = call("POST", "/api/object-action", body)
                self.assertEqual((status, refused), (404, {"error": "not_found"}))
                self.assertNotIn(owned, json.dumps([listed, opened]))
        with self.subTest("no action role"):
            call = self.serve(self.console, actors=frozenset({"staff:approvers"}))
            status, listed = call("GET", "/api/object-actions?id=" + quote(OBJECT, safe=""))
            self.assertEqual([item["control"] for item in listed["intents"]["intents"]],
                             [None, None])
            status, refused = call("POST", "/api/object-action", body)
            self.assertEqual((status, refused["detail"]), (403, "no_action_role"))
        with self.subTest("an agent session never acts"):
            agent = self.actions(tap.SESSION)
            self.assertEqual(agent.refusal(tap.OPS), "only_a_person_acts")
            self.assertIsNone(agent.control(tap.OPS, self.path.view(tap.OPS, owned)))
        self.assertEqual(self.path.get(tap.OPS, owned)["state"], "proposed")
        self.assertEqual((self.mc.sender.calls, self.fx.calls), ([], 0))

    def test_a_console_that_takes_no_steps_answers_unserved_never_an_error(self):
        """The fixture console registers intents but configures no act control: the panel's
        listing answers 200 ``served`` false (the page hides it) and lists nothing, and a
        step is still refused; the object view keeps the intents read-only."""
        call = serve(self, self.console, None, cases=self.cases, path=self.path)
        status, listed = call("GET", "/api/object-actions?id=" + quote(OBJECT, safe=""))
        self.assertEqual((status, listed["served"], listed["read_only"]), (200, False, True))
        self.assertNotIn("intents", listed)
        status, opened = call("GET", "/api/object?id=" + quote(OBJECT, safe=""))
        self.assertEqual((status, len(opened["intents"]["intents"])), (200, 2))
        self.assertTrue(all(item.get("control") is None for item in opened["intents"]["intents"]))
        status, refused = call("POST", "/api/object-action",
                               {"intent_id": self.owned["intent_id"], "step": "dispatch"})
        self.assertEqual((status, refused), (501, {"error": "actions_not_served"}))
        self.assertEqual(self.path.get(tap.OPS, self.owned["intent_id"])["state"], "proposed")


class FollowUpActionTest(unittest.TestCase):
    """The MC follow-up task (Workbench-owned, executed through MC's write door): sent only
    after another person approved the exact call, shown uncertain when MC's answer is lost,
    never sent twice, verified by reading the task back."""

    def test_an_uncertain_send_is_shown_uncertain_and_verified_by_read_back(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        world = gx.Lifecycle(Path(tmp.name))
        world.mc.buckets.add(gx.FOLLOW_UP_BUCKET)
        client = world.mc.client(Path(tmp.name) / "create-ledger.jsonl",
                                 write_tools=frozenset({"mc_create_task"}))
        _case, iid = world.follow_up_intent(1)
        call = serve(self, gx.SESSION, objects.WorkspaceActions(
            world.path, gx.SESSION, actors=gx.ACTOR.principals, mc=client))
        step = lambda name: call("POST", "/api/object-action",  # noqa: E731
                                 {"intent_id": iid, "step": name})
        status, refused = step("send_call")
        self.assertEqual((status, refused["error"]), (409, "invalid_state:call_approval_required"))
        world.approve_follow_up(iid)
        world.mc.lose_next_answer = True
        status, sent = step("send_call")
        self.assertEqual((status, sent["state"], sent["outcome"], sent["done"],
                          sent["approval"]["stage"]),
                         (200, "dispatched_unknown", "unknown", False, "approved"))
        self.assertEqual(sent["control"]["step"], "read_back_call")
        status, again = step("send_call")
        self.assertEqual((status, again["result"]["replayed"]), (200, True))
        status, verified = step("read_back_call")
        self.assertEqual((status, verified["state"], verified["done"]), (200, "verified", True))
        self.assertEqual((world.mc.tool_calls.count("mc_create_task"),
                          len(world.mc.create_claims)), (1, 1))


if __name__ == "__main__":
    unittest.main()
