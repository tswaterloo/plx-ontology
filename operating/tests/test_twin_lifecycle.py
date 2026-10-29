"""The write twin in test mode (owner directive 746d96497341; EXE-02/03/04/05/09/10/11, G-01).

Every action type of the catalog runs its lifecycle on the twin (``twin.world``): the
Portal type through the reviewed Portal adapter over ``TwinTransport`` to the Portal twin,
MC through the real MC client to the MC twin, the isolated-owner types through
``IsolatedOwnerSystem``, the Workbench-internal types on the case store. A socket guard
around the run counts every connection attempt, to a production host or any other.
Evidence label: ``twin-transport`` (Portal), ``isolated-fixture`` (MC, isolated owner).
"""

from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology.core import Actor
from operating import action_adapters as adapters
from operating import action_path as ap
from operating import goal2_exec as g2
from operating.action_catalog import ActionCatalog
from twin import mode
from twin.portal import PortalTwin, TwinError
from twin.transport import TwinTransport
from twin.world import (ACTOR, ORDER_RELATION, PORTAL_CAPABILITY, SESSION, TwinWorld,
                        run_lifecycle)

RUNNABLE_END = {"verified", "executed"}
REFUSED = {"test.record.set_field": ("prepare_gate", "production_target_refused"),
           "portal.assembly_order.set_header_quantities": (
               "register", "action_type_registration_refused:"
                           "owner_write_surface_does_not_set_these_fields")}


class NetworkGuard:
    """Every connection attempt (any host) is recorded and refused."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def _refuse(self, target):
        self.attempts.append(str(target))
        raise OSError("network refused in the twin test")

    def __enter__(self):
        self._patches = [
            mock.patch.object(socket.socket, "connect",
                              lambda _sock, address: self._refuse(address)),
            mock.patch.object(socket.socket, "connect_ex",
                              lambda _sock, address: self._refuse(address)),
            mock.patch.object(socket, "create_connection",
                              lambda address, *a, **k: self._refuse(address)),
            mock.patch.object(socket, "getaddrinfo",
                              lambda host, *a, **k: self._refuse(host))]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()


class TwinTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in mode.credentialed():
            os.environ.pop(name)
        os.environ.pop(adapters.ALLOW_ENV, None)

    def world(self):
        context = mode.test_mode(allow={PORTAL_CAPABILITY})
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return TwinWorld(self.dir / "world")

    def requested_gate(self, world, number):
        """A registered Portal intent with its MC gate requested (not yet decided)."""
        _case, iid = world.portal_intent(number, f"PO70{number:02d}")
        task = f"TASK-TWIN-{number}"
        world.mc.add_task(task, "Twin gate")
        world.path.prepare_gate(ACTOR, iid, task, world.mc_client, session=SESSION)
        now = world.clock.now
        world.path.approve_call(ACTOR, iid, task, {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
            "arguments": {"taskId": task,
                          "reason": world.path.get(ACTOR, iid)["intent"]["gate_reason"]},
            "approved_by": g2.CALL_APPROVER,
            "approved_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "evidence_ref": f"twin per-call approval {number}"}, session=SESSION)
        world.path.request_gate(ACTOR, iid, world.mc_client, session=SESSION)
        return iid, task, world.path.get(ACTOR, iid)["gate"]["gate_id"]


class LifecycleTests(TwinTestCase):
    def test_every_catalog_type_runs_its_lifecycle_on_the_twin_and_nothing_leaves(self):
        with NetworkGuard() as guard:
            report = run_lifecycle(self.dir / "run")
        self.assertEqual(guard.attempts, [])   # 0 production-host (or any) connections
        rows = {row["action_type"]: row for row in report["types"]}
        self.assertEqual(set(rows), set(ActionCatalog.builtin().ids()))
        for action_type, row in rows.items():
            with self.subTest(action_type=action_type):
                self.assertTrue(row["chain_ok"] and row["case_chain_ok"], row)
                if action_type in REFUSED:
                    self.assertEqual((row["refused_at"], row["refusal"]),
                                     REFUSED[action_type])
                    self.assertEqual(row["outcome"], "not_dispatched")
                else:
                    self.assertIn(row["end_state"], RUNNABLE_END, row)
                    self.assertEqual(row["outcome"], "verified", row)
        self.assertEqual(rows[PORTAL_CAPABILITY]["evidence"], ["twin-transport"])
        # The MC twin itself refused the agent's and the member's decisions.
        self.assertEqual(report["mc_refusals"], {"context_denied": 6, "forbidden": 6})

        scenarios = {item["scenario"]: item for item in report["portal_scenarios"]}
        lost, conflict, unknown = (scenarios[name] for name in
                                   ("lost_answer", "etag_mismatch", "unknown"))
        # Lost answer: written once, never re-sent, reconciled by the GET read-back.
        self.assertEqual((lost["first_dispatch"], lost["end_state"], lost["twin_writes"],
                          lost["read_back"]), ("dispatched_unknown", "verified", 1,
                                               ["effect_observed"]))
        # 412: a definite refusal, nothing written, handed to a person as one MC task.
        self.assertEqual((conflict["first_dispatch"], conflict["first_reason"],
                          conflict["twin_writes"], conflict["escalation_tasks"]),
                         ("failed", "etag_mismatch", 0, 1))
        # Unknown: not observed by read-back, escalated; two escalations make one task.
        self.assertEqual((unknown["end_state"], unknown["outcome"], unknown["twin_writes"],
                          unknown["escalation_tasks"]), ("escalated", "unknown", 0, 1))
        self.assertTrue(all(item["replayed"] for item in scenarios.values()))
        self.assertEqual(report["portal_requests"]["PATCH /api/mrp/sales-orders"], 4)


class TransportGuardTests(TwinTestCase):
    def test_the_twin_transport_is_admitted_only_in_test_mode_and_never_with_credentials(self):
        portal = PortalTwin()
        with self.assertRaises(adapters.AdapterError):   # outside test mode
            adapters.PortalAdapter(PORTAL_CAPABILITY, TwinTransport(portal))
        self.assertFalse(adapters.allow_switch_on(PORTAL_CAPABILITY))
        # NEW_SERVICE_SECRET: no list names it, the gate's keyless pattern does.
        for credential in ("PLX_WORKBENCH_API_KEY", "DATABASE_URL", "PGPASSWORD", "POSTGRES_URL",
                           "POSTGRES_URL_NON_POOLING", "DIRECT_URL", "DB_URL", "PGURL",
                           "PGPASSFILE", "REPORTING_DATABASE_URL", "NEW_SERVICE_SECRET"):
            with self.subTest(credential=credential), \
                    mock.patch.dict(os.environ, {credential: "x"}), \
                    self.assertRaises(mode.TwinModeRefused) as caught, mode.test_mode():
                pass
            self.assertEqual(caught.exception.code, "credentialed_host")
        # What a keyless worker shell keeps (git's config, the GitHub token) is no business
        # credential: test mode still runs there.
        with mock.patch.dict(os.environ, {"GH_TOKEN": "x", "GIT_CONFIG_COUNT": "1",
                                          "GIT_CONFIG_KEY_0": "http.extraheader"}), \
                mode.test_mode():
            pass

        class Claims:   # a transport that only claims the twin's label
            evidence = mode.TWIN_LABEL

            def send(self, request):
                raise AssertionError("never sent")

        with mode.test_mode(allow={PORTAL_CAPABILITY}):
            with self.assertRaises(adapters.AdapterError):
                adapters.PortalAdapter(PORTAL_CAPABILITY, Claims())
            transport = TwinTransport(portal)
            adapter = adapters.PortalAdapter(PORTAL_CAPABILITY, transport)

            class Lookalike(TwinTransport):   # not the exact type
                pass

            # The test-mode allow shows on for the twin's exact transport only: a view or
            # ``status`` names none, a lookalike or label claimant is another transport.
            self.assertTrue(adapters.allow_switch_on(PORTAL_CAPABILITY, transport=transport))
            for other in (None, Claims(), Lookalike(portal)):
                with self.subTest(transport=type(other).__name__):
                    self.assertFalse(adapters.allow_switch_on(PORTAL_CAPABILITY,
                                                              transport=other))
            status = adapters.status()
            self.assertFalse(status["allow_switch_any"])
            self.assertIn(PORTAL_CAPABILITY, [row["capability"] for row in status["capabilities"]])
        # Built in test mode, used after it: nothing is sent, nothing is concluded.
        intent = ap.ActionIntent("act_x", "c", "p", PORTAL_CAPABILITY, "portal", "0" * 64,
                                 {"relation": ORDER_RELATION, "key": {"id": "so-twin-1"},
                                  "fields": ["po_reference"], "values": {"po_reference": "P1"}},
                                 "wb-" + "0" * 40, "reason")
        with self.assertRaises(adapters.NotSent) as sent:
            adapter.dispatch(intent)
        self.assertEqual(sent.exception.code, "transport_not_admitted")
        self.assertEqual(adapter.read_back(intent)["reason"], "transport_not_admitted")
        self.assertEqual((transport.requests, portal.writes), ([], 0))

    def test_test_mode_switches_on_the_twin_transport_only(self):
        """A reviewed non-twin transport registered in the same process (here an injected
        one) is never switched on by test mode: the dispatch gate names its transport."""

        class Injected:
            evidence = adapters.EVIDENCE_LABEL

            def __init__(self):
                self.requests = []

            def send(self, request):
                self.requests.append(request)
                return {"status": 200, "body": {"id": "so-twin-1", "status": "DRAFT",
                                                "poReference": None}}

        world = self.world()
        _case, iid = world.portal_intent(1, "PO7001")
        world.authorize(iid, "TASK-TWIN-1")
        # The MC gate request went through (authorize); the view names no transport.
        self.assertEqual(world.path.view(ACTOR, iid)["unmet_gates"], ["allow_switch"])
        injected = Injected()
        world.registry.register(PORTAL_CAPABILITY, ap.RealSystemDispatcher(
            adapters.PortalAdapter(PORTAL_CAPABILITY, injected)))
        out = world.path.dispatch(ACTOR, iid, world.registry, world.mc_client,
                                  session=SESSION)
        self.assertEqual((out["state"], out["reason"], out["unmet_gates"]),
                         ("dispatch_refused", "production_target_refused", ["allow_switch"]))
        self.assertEqual((injected.requests, world.portal.writes), ([], 0))
        self.assertFalse(adapters.allow_switch_on(PORTAL_CAPABILITY, transport=injected))
        self.assertTrue(adapters.allow_switch_on(PORTAL_CAPABILITY,
                                                 transport=world.transport))

    def test_the_twins_answer_only_what_the_generator_facts_declare(self):
        portal = PortalTwin()
        for row, code in (({"id": "x", "no_such_column": 1}, "column_not_served:no_such_column"),
                          ({"id": "x"}, "required_field_missing:")):
            with self.subTest(code=code), self.assertRaises(TwinError) as caught:
                portal.seed(ORDER_RELATION, row)
            self.assertTrue(caught.exception.code.startswith(code), caught.exception.code)
        world = self.world()
        patch = adapters.OwnerRequest("portal", "PATCH", "/api/mrp/sales-orders/so-twin-1",
                                      body={"notes": "not cataloged"})
        unrouted = adapters.OwnerRequest("portal", "DELETE", "/api/mrp/sales-orders/so-twin-1")
        self.assertEqual([world.transport.send(r)["status"] for r in (patch, unrouted)],
                         [400, 404])
        # After the first shipment, or on a blank status, the Portal locks the edit
        # (canEditSalesOrderUntilShipped, status.ts@31a2623:28-33); any other status, FM
        # status text included, takes it.
        for status in ("PARTIALLY_SHIPPED", "SHIPPED", "", "  ", None):
            with self.subTest(status=status):
                world.portal.records[ORDER_RELATION]["so-twin-1"]["status"] = status
                locked = world.transport.send(adapters.OwnerRequest(
                    "portal", "PATCH", "/api/mrp/sales-orders/so-twin-1",
                    body={"poReference": "P"}))
                self.assertEqual((locked["status"], locked["body"]["code"]),
                                 (409, "ORDER_NOT_EDITABLE"))
        self.assertEqual(world.portal.writes, 0)
        for status in ("OPEN", "Open"):
            with self.subTest(status=status):
                world.portal.records[ORDER_RELATION]["so-twin-1"]["status"] = status
                taken = world.transport.send(adapters.OwnerRequest(
                    "portal", "PATCH", "/api/mrp/sales-orders/so-twin-1",
                    body={"poReference": "P-" + status}))
                self.assertEqual(taken["status"], 200)
        self.assertEqual(world.portal.writes, 2)
        world.portal.records[ORDER_RELATION]["so-twin-1"]["status"] = "DRAFT"
        # The MC twin's own answer, behind the client's guard: unpinned tool, an input the
        # pin does not declare or a missing required one, a pinned tool it does not emulate.
        for name, arguments, code in (("mc_no_such_tool", {}, "tool_not_found"),
                                      ("mc_get_task", {"id": "T", "x": 1}, "invalid_input"),
                                      ("mc_request_approval", {"taskId": "T"}, "invalid_input"),
                                      ("mc_list_buckets", {}, "not_emulated")):
            with self.subTest(code=code, name=name):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}}).encode()
                _status, _headers, raw = world.mc.send(
                    "POST", g2.MC_URL + "/api/cursor/mcp", {}, body, 5, 1 << 20)
                result = json.loads(raw)["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(json.loads(result["content"][0]["text"])["error"]["code"], code)
        self.assertEqual(world.mc.tasks, {})


class ApprovalBindingTests(TwinTestCase):
    """EXE-09: the decider is on the twin's MC role roster and is not the requester or a
    subject; the decision is bound to the payload (gate reason SHA-256, proposal digest)."""

    def test_a_decider_off_the_roster_the_requester_or_a_subject_never_authorizes(self):
        world = self.world()
        late = "late.admin@isolated-fixture.example"
        world.mc.add_user(late, "admin")
        world.mc.add_user(g2.SUBJECT, "admin")
        for number, decider, code in ((1, late, "decider_not_in_mc_roster"),
                                      (2, g2.OPERATOR, "self_decided"),
                                      (3, g2.SUBJECT, "decider_is_subject")):
            with self.subTest(code=code):
                if decider == g2.SUBJECT:   # a person writes the subject onto the roster
                    roster = json.loads(g2.ISOLATED_APPROVER_ROSTER.read_text(encoding="utf-8"))
                    roster["approvers"][g2.SUBJECT] = dict(
                        roster["approvers"][g2.MC_ADMIN],
                        oid="0000a000-0000-4000-8000-00000000ad0a")
                    (self.dir / "roster.json").write_text(json.dumps(roster), encoding="utf-8")
                    world.read_roster(self.dir / "roster.json")
                    world.path = world.new_path()
                iid, task, gate = self.requested_gate(world, number)
                if decider == g2.OPERATOR:   # MC refuses its operator; a forged gate is read
                    with self.assertRaises(g2.IsolatedMcRefused):
                        world.mc.decide(task, gate, "approved", session_label=decider,
                                        session_kind="human")
                    world.mc.tasks[task]["approvalGates"][0].update(
                        status="approved", decidedBy=decider, decidedAt=world.mc._stamp())
                else:
                    world.mc.decide(task, gate, "approved", session_label=decider,
                                    session_kind="human")
                world.path.record_decision(ACTOR, iid, world.mc_client, session=SESSION)
                state = world.path.get(ACTOR, iid)
                self.assertEqual((state["state"], state["decisions"][-1]["reason_code"]),
                                 ("gate_not_authorizing", code))
                with self.assertRaises(ap.ActionPathError):
                    world.path.dispatch(ACTOR, iid, world.registry, world.mc_client,
                                        session=SESSION)
        self.assertEqual((world.portal.writes, world.transport.requests), (0, []))

    def test_a_payload_edited_after_approval_is_refused(self):
        world = self.world()
        # In MC: the approved gate's reason (the payload the admin read) is edited.
        _case, iid = world.portal_intent(1, "PO7001")
        world.authorize(iid, "TASK-TWIN-1")
        gate = world.mc.tasks["TASK-TWIN-1"]["approvalGates"][0]
        gate["reason"] = gate["reason"].replace("PO7001", "PO7999")
        moved = world.path.dispatch(ACTOR, iid, world.registry, world.mc_client,
                                    session=SESSION)
        self.assertEqual((moved["state"], moved["reason"]),
                         ("gate_not_authorizing", "approval_withdrawn:reason_mismatch"))
        # In the Workbench: the stored proposal payload is edited behind the journal.
        case_id, iid = world.portal_intent(2, "PO7002")
        world.authorize(iid, "TASK-TWIN-2")
        db = sqlite3.connect(world.state_dir / "cases.db")
        db.execute("DROP TRIGGER case_event_no_update")
        seq, payload = db.execute("SELECT seq, payload_json FROM case_event WHERE case_id=? "
                                  "AND kind='proposal.recorded'", (case_id,)).fetchone()
        db.execute("UPDATE case_event SET payload_json=? WHERE seq=?",
                   (payload.replace("PO7002", "PO7666"), seq))
        db.commit()
        db.close()
        edited = world.path.dispatch(ACTOR, iid, world.registry, world.mc_client,
                                     session=SESSION)
        self.assertEqual((edited["state"], edited["reason"]),
                         ("dispatch_refused", "proposal_changed_since_approval"))
        self.assertEqual((world.portal.writes, world.transport.requests), (0, []))


class ConsolePathTests(TwinTestCase):
    def test_people_at_the_console_take_the_portal_action_to_read_back_on_the_twin(self):
        """The whole path reached by people at the console (``WorkspaceActions``), test
        mode: the employee requests approval, the roster's call approver approves exactly
        the shown call at their own session (nothing sent), the request goes to the MC
        twin once, its admin decides, the employee dispatches once and verifies."""
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        from operator_ui.object_workspace import WorkspaceActions

        world = self.world()
        _case, iid = world.portal_intent(1, "PO7001")
        world.mc.add_task("TASK-TWIN-CONSOLE", "Twin gate")
        dispatcher = world.registry.for_capability(PORTAL_CAPABILITY)

        def at_console(session):
            return WorkspaceActions(world.path, session, actors=ACTOR.principals,
                                    mc=world.mc_client, registry=world.registry,
                                    read_back=dispatcher.read_back)
        employee = at_console(SESSION)
        approver = at_console(fixture_session("plx", ACTOR.principals, channel=CHANNEL_CONSOLE,
                                              subject_email=g2.CALL_APPROVER,
                                              subject_oid=g2.CALL_APPROVER_OID))
        with NetworkGuard() as guard:
            prepared = employee.take(ACTOR, {"intent_id": iid, "step": "prepare_gate",
                                             "task_id": "TASK-TWIN-CONSOLE"})
            self.assertEqual((prepared["state"], prepared["control"]["step"]),
                             ("gate_prepared", "approve_call"))
            shown = approver.control(ACTOR, world.path.view(ACTOR, iid))
            self.assertEqual((shown["available"], shown["approves"]["action_type"]),
                             (True, PORTAL_CAPABILITY))
            # The employee it is for never approves it: their session names no oid, and
            # with one they are its subject.
            subject = at_console(fixture_session(
                "plx", ACTOR.principals, channel=CHANNEL_CONSOLE, subject_email=g2.SUBJECT,
                subject_oid="0000a000-0000-4000-8000-00000000ee01"))
            for actions, code in ((employee, "call_approver_oid_unknown"),
                                  (subject, "call_approver_is_subject")):
                mine = actions.take(ACTOR, {"intent_id": iid, "step": "approve_call",
                                            "approves_sha256": shown["approves_sha256"]})
                self.assertEqual((mine["refusal"], mine["state"]), (code, "gate_prepared"))
            approved = approver.take(ACTOR, {"intent_id": iid, "step": "approve_call",
                                             "approves_sha256": shown["approves_sha256"]})
            self.assertEqual((approved["refusal"], approved["state"]), (None, "call_approved"))
            self.assertNotIn("mc_request_approval", world.mc.tool_calls)
            requested = employee.take(ACTOR, {"intent_id": iid, "step": "request_gate"})
            self.assertEqual(requested["state"], "gate_requested")
            gate = world.path.get(ACTOR, iid)["gate"]["gate_id"]
            world.mc.decide("TASK-TWIN-CONSOLE", gate, "approved", session_label=g2.MC_ADMIN,
                            session_kind="human")
            steps = [employee.take(ACTOR, {"intent_id": iid, "step": name})
                     for name in ("record_decision", "dispatch", "read_back")]
        self.assertEqual(guard.attempts, [])
        self.assertEqual([(item["refusal"], item["state"]) for item in steps],
                         [(None, "gate_authorized"), (None, "dispatched"),
                          (None, "verified")])
        self.assertEqual((steps[-1]["done"], world.portal.writes,
                          world.mc.tool_calls.count("mc_request_approval")), (True, 1, 1))


class PortalRouteTests(TwinTestCase):
    """The Portal twin answers as the route it mirrors (plx-customer-portal@728a995
    portal/src/app/api/mrp/sales-orders/[id]/route.ts, lib/sales-order/service.ts)."""

    def patch(self, world, order, body):
        return world.transport.send(adapters.OwnerRequest(
            "portal", "PATCH", f"/api/mrp/sales-orders/{order}", body=body))

    def get(self, world, order):
        return world.transport.send(adapters.OwnerRequest(
            "portal", "GET", f"/api/mrp/sales-orders/{order}"))

    def test_a_po_is_stored_and_read_back_as_sent_untrimmed(self):
        world = self.world()
        value = "  PO 7009 "   # service.ts:651-654 stores body.poReference as sent
        self.assertEqual(self.patch(world, "so-twin-9", {"poReference": value})["status"], 200)
        self.assertEqual(self.get(world, "so-twin-9")["body"]["poReference"], value)
        self.assertEqual(world.portal.resolve(PortalTwin.citation(ORDER_RELATION, "so-twin-9"),
                                              ACTOR)["row"]["po_reference"], value)
        # An empty PO is stored as "", never turned into null; a value Prisma's String
        # column refuses is a 500 with nothing applied.
        self.assertEqual(self.patch(world, "so-twin-1", {"poReference": ""})["status"], 200)
        self.assertEqual(self.get(world, "so-twin-1")["body"]["poReference"], "")
        self.assertEqual(self.patch(world, "so-twin-2", {"poReference": 7002})["status"], 500)
        self.assertIsNone(self.get(world, "so-twin-2")["body"]["poReference"])
        self.assertEqual(world.portal.writes, 2)

    def test_a_session_below_the_staff_floor_is_refused_and_nothing_is_written(self):
        world = self.world()
        before = self.get(world, "so-twin-1")["body"]
        for role, status in ((None, 401), ("CUSTOMER", 403), ("SUPPLIER", 403),
                             ("staff", 403)):
            with self.subTest(role=role):
                world.portal.session_role = role
                self.assertEqual([self.patch(world, "so-twin-1", {"poReference": "PO7001"})
                                  ["status"], self.get(world, "so-twin-1")["status"]],
                                 [status, status])
                adapter = adapters.PortalAdapter(PORTAL_CAPABILITY, world.transport)
                intent = ap.ActionIntent(
                    "act_x", "c", "p", PORTAL_CAPABILITY, "portal", "0" * 64,
                    {"relation": ORDER_RELATION, "key": {"id": "so-twin-1"},
                     "fields": ["po_reference"], "values": {"po_reference": "PO7001"}},
                    "wb-" + "0" * 40, "reason")
                with self.assertRaises(adapters.NotSent) as caught:
                    adapter.dispatch(intent)
                self.assertEqual(caught.exception.code, f"pre_write_read_failed:http_{status}")
        world.portal.session_role = "STAFF"
        self.assertEqual((world.portal.writes, self.get(world, "so-twin-1")["body"]),
                         (0, before))


class CustomerPoReuseTests(TwinTestCase):
    """c208bfff0a95: the proposal flags a PO the same customer already used, as the Portal
    warns on reuse (plx-customer-portal@728a995 portal/src/lib/orders/customer-po.ts)."""

    def patch(self, world, order, body):
        return world.transport.send(adapters.OwnerRequest(
            "portal", "PATCH", f"/api/mrp/sales-orders/{order}", body=body))

    def warnings(self, world, number, value):
        _case, iid = world.portal_intent(number, value)
        intent = world.path.get(ACTOR, iid)["intent"]
        return intent["owner_warnings"], intent["gate_reason"], iid

    def test_the_customer_po_proposal_flags_reuse_by_the_same_customer(self):
        world = self.world()
        # so-twin-6 holds " po7006 " for the same customer (counted: trimmed, upper case);
        # so-twin-7 is cancelled and so-twin-8 is another customer's (not counted).
        flagged, reason, iid = self.warnings(world, 5, "PO7006")
        self.assertEqual(flagged, [{"id": "po_reused_by_customer", "status": "flagged",
                                    "count": 1}])
        self.assertIn("WARNING po_reused_by_customer: 1 other record(s)", reason)
        self.assertEqual(world.path.view(ACTOR, iid)["owner_warnings"], flagged)
        with self.assertRaises(ap.IntentNotFound):   # a reader without the case sees none
            world.path.view(Actor("plx", frozenset({"finance"})), iid)
        clear, reason, _ = self.warnings(world, 1, "PO7001")
        self.assertEqual(clear[0]["status"], "clear")
        self.assertNotIn("po_reused_by_customer", reason)
        # A host without the owner's records: the check did not run, and the gate says so.
        world.path = ap.ActionPath(world.state_dir / "actions.db", world.cases,
                                   clock=world.clock)
        missing, reason, _ = self.warnings(world, 2, "PO7002")
        self.assertEqual(missing[0]["status"], "not_checked")
        self.assertIn("po_reused_by_customer not_checked", reason)
        # The Portal twin's own PATCH answer carries the same reuse, shaped as
        # customer-po.ts:47-89 builds it (the PO normalized, the other orders listed).
        answer = self.patch(world, "so-twin-5", {"poReference": " po7006 "})
        self.assertEqual(answer["body"]["warnings"], [{
            "code": "PO_REUSED_BY_CUSTOMER",
            "message": "This customer already used PO PO7006 on SO-T0006. Check that this "
                       "is not a duplicate order.",
            "poReference": "PO7006",
            "orders": [{"salesOrderId": "so-twin-6", "orderNumber": "SO-T0006",
                        "status": "DRAFT"}]}])
        # route.ts:70-81: checked only when the PATCH sets a non-empty PO; a failed check
        # is reported (customer-po.ts:107-114) and the save stands.
        for value, warnings in (("   ", []), ("PO7006", [{
                "code": "PO_REUSE_CHECK_FAILED", "message": "The order saved, but the portal "
                "could not check this customer's PO for reuse."}])):
            with self.subTest(value=value):
                world.portal.faults.append("reuse_check_failed")
                answer = self.patch(world, "so-twin-5", {"poReference": value})
                self.assertEqual((answer["status"], answer["body"]["warnings"]),
                                 (200, warnings))
                self.assertEqual(world.portal.faults, [])
                self.assertEqual(world.portal.records[ORDER_RELATION]["so-twin-5"]
                                 ["poReference"], value)

    def test_every_earlier_order_of_the_customer_is_counted(self):
        world = self.world()
        template = world.portal.resolve(PortalTwin.citation(ORDER_RELATION, "so-twin-1"),
                                        ACTOR)["row"]
        for count in (1, 10, 60):
            with self.subTest(count=count):
                customer = f"cust-twin-n{count}"
                for index in range(count):
                    world.portal.seed(ORDER_RELATION, {
                        **template, "id": f"so-n{count}-{index}", "customer_id": customer,
                        "order_number": f"SO-N{count}-{index}", "po_reference": "PO7300"})
                world.portal.seed(ORDER_RELATION, {**template, "id": f"so-t{count}",
                                                   "customer_id": customer})
                case_id = world.path.open_case(ACTOR, "Set the customer PO?",
                                               objective_author_kind="human", session=SESSION)
                document = world.owner.seed(g2.SOURCE_RELATION, {"id": 500 + count}, {
                    "id": 500 + count, "customer_po": "PO7300", "lines_stated": "1"})
                target = PortalTwin.citation(ORDER_RELATION, f"so-t{count}")
                proposal = world.path.record_proposal(
                    ACTOR, case_id, PORTAL_CAPABILITY,
                    {"proposed_customer_po": "PO7300", "evidence_citation": document,
                     "evidence_field": "customer_po"},
                    [{"check": "field_empty", "citation": target, "field": "po_reference"},
                     "a person at PLX confirms the evidence belongs to this order"],
                    citations=[target, document], session=SESSION,
                    expected_effect={"relation": ORDER_RELATION, "key": {"id": f"so-t{count}"},
                                     "fields": ["po_reference"], "equals": "PO7300"})
                intent = world.path.register(ACTOR, case_id, proposal["proposal_id"],
                                             session=SESSION)
                self.assertEqual(intent["owner_warnings"][0]["count"], count)
                # The Portal lists at most 10 of them, by order number (customer-po.ts:73).
                [warning] = self.patch(world, f"so-t{count}",
                                       {"poReference": "PO7300"})["body"]["warnings"]
                self.assertEqual([item["orderNumber"] for item in warning["orders"]],
                                 sorted(f"SO-N{count}-{index}" for index in
                                        range(count))[:10])


if __name__ == "__main__":
    unittest.main()
