"""Goal 2 authorized actions, end to end (EXE-04/EXE-10/EXE-11).

One test per outcome (``OutcomeTests``): approve -> dispatch -> verified, agents are
never principals, changed evidence is refused, an approval is single use, a process
death reads unknown and is reconciled by read-back, a production target is refused,
a forged decision is refused, a partial write is never resent. The other classes keep
every further failure scenario (uncertain outcomes, contradicted read-backs, owner
refusals, withdrawn approvals, registration guards, the MC write door, the follow-up
action type, the durable trail), table-driven where the scenarios share a shape.

Two worlds share one ``setUp``:

- the MC-approval path (``ActionPathTestCase``): propose -> preconditions on cited
  evidence -> MC approval request -> human per-call approval -> ``mc_request_approval``
  through the real, opted-in ``MissionControlMcpClient`` over a fake MC sender ->
  decision -> ``ap.FixtureSystem`` dispatch -> read-back;
- the isolated lifecycle (``gx.Lifecycle``, ``self.iso``): ``goal2_exec_owner``'s
  ``IsolatedOwnerSystem`` (plain and typed relations: typed fields, version etag,
  faults) and the in-process ``goal2_exec.IsolatedMissionControl`` mirroring PLX_MC
  ``approvals.ts`` / ``approvals/decide/route.ts`` / ``grants.ts`` / ``policy.ts`` /
  ``read-actions.ts`` @ 1fba083.

Evidence label: ``injected-transport`` for MC on the approval path, ``isolated-fixture``
for everything else. ``urlopen`` is patched to fail in every test: no network, no
credential, no Petra system.
"""

from __future__ import annotations

import ast
import contextlib
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from integrations.live_source_guard import Secret
from integrations.mc import MissionControlMcpClient, WriteAuthorizationLedger
from integrations.plx_hub_guard import McConfig
from integrations.tests.test_mc_brain_guard import (MC, MC_KEY, OPERATOR, FakeSender, hub,
                                                     ok_json, tool_result)
from ontology.core import Actor, ValidationError
from operating import action_adapters
from operating import action_path as ap
from operating import case_workspace_live
from operating import goal2_exec as gx
from operating import goal2_exec_owner as owner_mod
from operating import goal2_mc_approval as g2
from operating import goal2_preconditions as pre
from operating import mcp_server
from operating.action_catalog import ActionCatalog
from operating.cases import CaseNotFound
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests import test_employee_permissions as permissions_fixture
from operating.tests.test_action_path import (ALL_TYPES, API_ORDER, DECIDER_OID, EFFECT, OPS,
                                              OUTSIDER, REL, ROSTER, SESSION, STEPHEN, VINCE, VINCE_OID,
                                              ActionPathTestCase)
from operating.tests.test_mcp_action_tools import ActionToolsTestCase, fixture_host_session
from operating.tests.test_mcp_case_tools import FINANCE, OPS_FINANCE
from operator_ui import case_console
from operator_ui import object_workspace as objects
from operator_ui.tests import case_fixtures
from operator_ui.tests.test_workspace_actions import OBJECT as WS_OBJECT, stub_tools
from twin import mode as twin_mode
from twin.portal import PortalTwin
from twin.world import ORDER_RELATION, TwinWorld

ROOT = Path(__file__).resolve().parents[2]
ACTOR = gx.ACTOR
PRODUCTION_EFFECT = {"relation": "public.app_sales_order", "key": {"id": 3},
                     "fields": ["po_reference"], "equals": "PO-7788"}


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if not any(mark in k for mark in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
            and not k.startswith(("AWS_", "CLOUDSDK_"))}


class Goal2World(ActionPathTestCase):
    def setUp(self) -> None:
        super().setUp()
        network = mock.patch("urllib.request.urlopen",
                             side_effect=AssertionError("no network in the goal-2 e2e"))
        network.start()
        self.addCleanup(network.stop)
        self.world = gx.Lifecycle(self.dir / "world")
        self.world.define_typed_relations()
        self.iso, self.owner = self.world.path, self.world.owner
        self.number = 0

    # ---- the MC-approval path --------------------------------------------
    def request(self, iid, evidence_ref):
        intent = self.path.get(OPS, iid)["intent"]
        self.approve(iid, self.call_record(intent, evidence_ref=evidence_ref))
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        return self.path.get(OPS, iid)["gate"]["gate_id"]

    def ready(self):
        intent = self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
        iid = intent["intent_id"]
        self.approve(iid, self.call_record(intent))
        return iid

    def write_client(self, sender, *, approvers=(VINCE,), operator=OPERATOR, ledger=None,
                     write_tools=("mc_request_approval",)):
        transport = hub(sender, clock=self.clock)[0]
        return MissionControlMcpClient(
            McConfig(MC, Secret(MC_KEY), operator), hub=transport,
            write_tools=set(write_tools), write_approvers=set(approvers) if write_tools else (),
            write_ledger=ledger or WriteAuthorizationLedger(self.dir / "ledger.jsonl"))

    # ---- the isolated lifecycle --------------------------------------------
    def iso_authorized(self) -> tuple[str, str, int]:
        """A plain fixture-owner intent whose value comes from the human-authored question."""
        self.number += 1
        case_id, iid = self.world.open_case_and_register(self.number)
        self.assertEqual(self.iso.get(ACTOR, iid)["state"], "proposed")
        self.world.authorize(iid, f"TASK-ISO-{self.number}")
        self.assertEqual(self.iso.get(ACTOR, iid)["state"], "gate_authorized")
        return case_id, iid, self.number

    def typed_authorized(self, fields=("po_reference",), evidence_field="customer_po",
                         prior=None):
        """A typed-owner intent from a case finding, every parameter traced to evidence."""
        self.number += 1
        case_id, iid, target = self.world.open_case_from_finding(
            self.number, fields, evidence_field, prior)
        task_id = f"TASK-ISO-OWN-{self.number}"
        self.world.authorize(iid, task_id)
        self.assertEqual(self.iso.get(ACTOR, iid)["state"], "gate_authorized")
        return case_id, iid, target, task_id

    def iso_dispatch(self, iid):
        return self.iso.dispatch(ACTOR, iid, self.world.registry, self.world.mc_client,
                                 session=gx.SESSION)

    def iso_read_back(self, iid, read=None):
        return self.iso.read_back(ACTOR, iid, read or self.world.read_back, session=gx.SESSION)

    def iso_view(self, iid):
        return self.world.new_path().view(ACTOR, iid)   # read from disk, new instance

    def outcomes(self, iid):
        return [item["outcome"] for item in self.iso_view(iid)["outcome_trail"]]

    def key(self, iid):
        return self.iso.get(ACTOR, iid)["intent"]["idempotency_key"]

    def record(self, relation=gx.TYPED_RELATION):
        return self.owner.record(relation, {"id": self.number})


class OutcomeTests(Goal2World):
    def test_approve_dispatch_verified(self):
        with self.subTest("fixture owner: human MC admin, dispatched once, independent read-back"):
            case_id, iid, number = self.iso_authorized()
            state = self.iso.get(ACTOR, iid)
            # MC: exactly one gate request, sent through the guarded write door after the
            # human per-call approval; MC itself refused the agent and the member.
            self.assertEqual(self.world.mc.tool_calls.count("mc_request_approval"), 1)
            self.assertEqual(self.world.mc.refusals, ["context_denied", "forbidden"])
            decision = state["decisions"][-1]
            self.assertEqual((decision["authorizes"], decision["decided_by"],
                              decision["decider_role"], decision["reason_code"]),
                             (True, gx.MC_ADMIN, "admin", "authorized"))
            self.assertEqual(state["decisions"][0]["reason_code"], "not_approved")  # pending

            result = self.iso_dispatch(iid)
            self.assertEqual(result["state"], "dispatched")
            [operation] = self.owner.operations()
            self.assertEqual((operation["idempotency_key"], operation["status"],
                              operation["receipt"]), (self.key(iid), "applied", result["receipt"]))
            self.assertEqual(self.record(REL)["po_reference"], f"PO-ISO-{number}")

            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["outcome"], checked["reconciled_by"],
                              checked["attribution"], checked["operation"]["status"]),
                             ("verified", "effect_observed", None, "idempotency_key", "applied"))
            [verification] = self.world.cases.get(ACTOR, case_id)["verifications"]
            self.assertEqual((verification["outcome"], verification["detail"]["intent_id"],
                              verification["detail"]["evidence"]),
                             ("effect_observed", iid, "isolated-fixture"))

            # Exactly once: a repeated dispatch replays the record and calls nobody.
            replay = self.iso_dispatch(iid)
            self.assertEqual((replay["state"], replay["replayed"]), ("verified", True))
            self.assertEqual((self.owner.dispatch_calls, len(self.owner.operations())), (1, 1))
            view = self.iso_view(iid)
            self.assertEqual((view["outcome"], view["chain_ok"], view["recovery"]),
                             ("verified", True, []))
            self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "verified"])
            self.assertEqual([item["state"] for item in view["state_trail"]][-3:],
                             ["dispatched_unknown", "dispatched", "verified"])

        self.setUp()
        with self.subTest("typed owner: the cited value, the version etag, idempotent duplicates"):
            _case, iid, target, _task = self.typed_authorized()
            intent = self.iso.get(ACTOR, iid)["intent"]
            cited_value = self.owner.resolve(
                next(e["handle"] for e in intent["evidence"] if e["handle"] != target),
                ACTOR)["row"]["customer_po"]
            self.assertEqual(intent["expected_effect"]["values"], {"po_reference": cited_value})
            payload = owner_mod.dispatch_payload(ap.ActionIntent(**intent))
            self.assertIn({"check": "field_equals", "field": gx.VERSION_FIELD, "value": 1},
                          payload["expect"])
            reads_before = self.world.mc.tool_calls.count("mc_get_task")
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")
            # The approval was re-read in MC immediately before the dispatch.
            self.assertGreaterEqual(self.world.mc.tool_calls.count("mc_get_task") - reads_before,
                                    2)
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["attribution"]),
                             ("verified", "idempotency_key"))
            self.assertEqual(self.record()["po_reference"], cited_value)
            self.assertEqual(self.record()[gx.VERSION_FIELD], 2)
            # Duplicate through the path: replayed, nothing sent.
            calls = self.owner.dispatch_calls
            self.assertTrue(self.iso_dispatch(iid)["replayed"])
            self.assertEqual(self.owner.dispatch_calls, calls)
            # Duplicate at the owner itself (same key and payload): same receipt, no write.
            receipt = self.iso.get(ACTOR, iid)["dispatches"][-1]["receipt"]
            self.assertEqual(self.owner.dispatch(payload, intent["idempotency_key"]), receipt)
            self.assertEqual(self.record()[gx.VERSION_FIELD], 2)
            with self.assertRaises(owner_mod.OwnerRejected) as other:
                self.owner.dispatch({**payload, "set": {"po_reference": "PO-OTHER"}},
                                    intent["idempotency_key"])
            self.assertEqual(other.exception.code, "idempotency_key_reused_with_other_payload")
            self.assertEqual([op["status"] for op in self.owner.operations()], ["applied"])
            self.assertEqual(self.iso_view(iid)["outcome"], "verified")

    def test_agents_are_never_principals(self):
        self.number += 1
        _case_id, iid = self.world.open_case_and_register(self.number)
        self.world.mc.add_task("TASK-ISO-1", "Isolated goal-2 lifecycle")
        self.iso.prepare_gate(ACTOR, iid, "TASK-ISO-1", self.world.mc_client, session=gx.SESSION)
        reason = self.iso.get(ACTOR, iid)["intent"]["gate_reason"]
        now = self.world.clock.now
        record = {"schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
                  "arguments": {"taskId": "TASK-ISO-1", "reason": reason},
                  "approved_at": (now - timedelta(minutes=1)).isoformat(),
                  "expires_at": (now + timedelta(hours=1)).isoformat(),
                  "evidence_ref": "isolated test"}
        for agent in (gx.AGENT_SERVICE, "agent:workbench", gx.OPERATOR):
            with self.assertRaises(ap.ActionPathError) as refused:
                self.iso.approve_call(ACTOR, iid, "TASK-ISO-1", {**record, "approved_by": agent},
                                      session=gx.SESSION)
            self.assertEqual(refused.exception.code, "call_approver_not_allowed")
        self.iso.approve_call(ACTOR, iid, "TASK-ISO-1",
                              {**record, "approved_by": gx.CALL_APPROVER}, session=gx.SESSION)
        self.iso.request_gate(ACTOR, iid, self.world.mc_client, session=gx.SESSION)
        gate_id = self.iso.get(ACTOR, iid)["gate"]["gate_id"]
        # In MC: a service session, a member, and the requester itself never decide.
        for label, kind, code in ((gx.AGENT_SERVICE, "service", "context_denied"),
                                  (gx.MC_MEMBER, "human", "forbidden")):
            with self.assertRaises(gx.IsolatedMcRefused) as refused:
                self.world.mc.decide("TASK-ISO-1", gate_id, "approved", session_label=label,
                                     session_kind=kind)
            self.assertEqual(refused.exception.code, code)
        mc = gx.IsolatedMissionControl(self.world.clock, {gx.OPERATOR: "owner"})
        mc.tasks = self.world.mc.tasks
        with self.assertRaises(gx.IsolatedMcRefused) as refused:
            mc.decide("TASK-ISO-1", gate_id, "approved", session_label=gx.OPERATOR,
                      session_kind="human")
        self.assertEqual(refused.exception.code, "separation_of_duties")
        self.iso.record_decision(ACTOR, iid, self.world.mc_client, session=gx.SESSION)
        self.assertEqual(self.iso.get(ACTOR, iid)["state"], "gate_requested")
        with self.assertRaises(ap.ActionPathError):
            self.iso_dispatch(iid)
        self.assertEqual(self.owner.dispatch_calls, 0)

    def test_changed_evidence_refused(self):
        with self.subTest("cited record changed after approval: refused before any owner call"):
            _case_id, iid, number = self.iso_authorized()
            self.owner.update_by_other_writer(REL, {"id": number}, {"ship_note": "changed"})
            result = self.iso_dispatch(iid)
            self.assertEqual((result["state"], result["reason"],
                              result["business_system_writes"]),
                             ("dispatch_refused", "precondition_citation_changed", 0))
            self.assertEqual((self.owner.dispatch_calls, self.owner.operations()), (0, []))
            view = self.iso_view(iid)
            step = view["next_safe_step"]
            self.assertEqual((view["outcome"], step["step"], step["by"], step["blocked_by"],
                              step["resend_allowed"]),
                             ("not_dispatched", "reverify_and_repropose", "human",
                              "precondition_citation_changed", False))
            self.assertEqual(self.outcomes(iid), ["not_dispatched"])
            # Retrying is refused the same way: the approval is stale for good.
            self.assertEqual(self.iso_dispatch(iid)["reason"], "precondition_citation_changed")
            self.assertEqual(self.owner.dispatch_calls, 0)

        with self.subTest("the precondition no longer holds"):
            _case_id, iid, number = self.iso_authorized()
            self.owner.update_by_other_writer(REL, {"id": number}, {"po_reference": "PO-OTHER"})
            self.assertEqual(self.iso_dispatch(iid)["reason"], "precondition_citation_changed")
            self.assertEqual(self.owner.dispatch_calls, 0)
            self.assertEqual(self.record(REL)["po_reference"], "PO-OTHER")

        with self.subTest("proposal evidence gives content only while it is the cited content"):
            pid = self.propose()
            [item] = self.cases.proposal_evidence(OPS, self.case_id, pid)
            self.assertEqual((item["handle"], item["current_digest"], item["content"]["row"]),
                             (self.handle, item["cited_digest"], {"id": 3, "po_reference": None}))
            self.fx.rows[(REL, '{"id":3}')]["po_reference"] = "changed"
            [item] = self.cases.proposal_evidence(OPS, self.case_id, pid)
            self.assertNotEqual(item["current_digest"], item["cited_digest"])
            self.assertIsNone(item["content"])
            with self.assertRaises(CaseNotFound):
                self.cases.proposal_evidence(OUTSIDER, self.case_id, pid)
            with self.assertRaises(ValidationError) as caught:
                self.cases.proposal_evidence(OPS, self.case_id, "P99")
            self.assertEqual(str(caught.exception), "proposal_not_found")

        with self.subTest("precondition evaluation fails closed on changed evidence"):
            check = {"check": "field_empty", "citation": "h", "field": "po_reference"}
            changed = [{"handle": "h", "cited_digest": "a", "current_digest": "b",
                        "content": None}]
            self.assertEqual(pre.evaluate([check], changed)["reason"],
                             "precondition_citation_changed")
            self.assertFalse(pre.evaluate([], [])["holds"])

    def test_single_use(self):
        """One approval spans action databases through the shared MC write ledger."""
        iid = self.ready()
        record = self.path.get(OPS, iid)["call_approval"]
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(len(self.mc.requests), 1)
        # A second action database (say a copy) with the same approval recorded:
        # its own consumed table is empty, but the MC write ledger is shared.
        other = ap.ActionPath(self.dir / "other-actions.db", self.cases,
                              call_approvers=frozenset({VINCE}), mc_operator_email=OPERATOR,
                              approver_roster=ROSTER, clock=self.clock)
        # The copy carries the case's recorded opener and author (``case_author``), so
        # its intent has known subjects, as on the first database.
        db = sqlite3.connect(self.dir / "other-actions.db")
        try:
            db.execute("ATTACH DATABASE ? AS first", (str(self.dir / "actions.db"),))
            db.execute("INSERT INTO case_author SELECT * FROM first.case_author")
            db.commit()
        finally:
            db.close()
        other_iid = other.register(OPS, self.case_id, self.path.get(OPS, iid)["intent"]
                                   ["proposal_id"], session=SESSION)["intent_id"]
        # Its MC read already sees the first database's gate for this exact action.
        with self.assertRaises(ap.ActionPathError) as caught:
            other.prepare_gate(OPS, other_iid, "TASK-9", self.mc.client, session=SESSION)
        self.assertEqual(caught.exception.code, "mc_untracked_gate_for_this_action")
        # Without that read (the gate hidden from it), the shared write ledger still holds.
        gates = self.mc.tasks["TASK-9"]["approvalGates"]
        hidden, gates[:] = list(gates), []
        other.prepare_gate(OPS, other_iid, "TASK-9", self.mc.client, session=SESSION)
        gates[:] = hidden
        other.approve_call(OPS, other_iid, "TASK-9", {
            "schema": ap.CALL_APPROVAL_SCHEMA, **{k: record[k] for k in (
                "tool", "arguments", "approved_by", "approved_at", "expires_at",
                "evidence_ref")}}, session=SESSION)
        other.request_gate(OPS, other_iid, self.mc.client, session=SESSION)
        self.assertEqual(other.get(OPS, other_iid)["state"], "gate_request_refused")
        self.assertEqual(len(self.mc.requests), 1)

    def test_death_unknown_reconciled(self):
        with self.subTest("killed after the owner applied: unknown on restart, read-back verifies"):
            _case_id, iid, number = self.iso_authorized()
            child = textwrap.dedent("""
                import json, os, sys
                from datetime import datetime
                from pathlib import Path
                from operating import goal2_exec as gx
                world = gx.Lifecycle(Path(sys.argv[1]),
                                     gx.Clock(datetime.fromisoformat(sys.argv[2])))
                world.mc.tasks = json.loads(sys.argv[4])          # the same MC state
                world.owner.after_apply = lambda: os._exit(17)   # dies after the owner commit
                world.path.dispatch(gx.ACTOR, sys.argv[3], world.registry, world.mc_client,
                                    session=gx.SESSION)
                os._exit(0)
            """)
            env = {**_child_env(), "PYTHONPATH": str(ROOT)}
            done = subprocess.run([sys.executable, "-c", child, str(self.world.state_dir),
                                   self.world.clock.now.isoformat(), iid,
                                   json.dumps(self.world.mc.tasks)],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 17, done.stderr)
            # A fresh instance (the restarted host) sees the write-ahead dispatch intent.
            restarted = self.world.new_path()
            self.assertEqual(restarted.get(ACTOR, iid)["state"], "dispatched_unknown")
            self.assertEqual([op["status"] for op in self.owner.operations()], ["applied"])
            again = restarted.dispatch(ACTOR, iid, self.world.registry, self.world.mc_client,
                                       session=gx.SESSION)
            self.assertTrue(again["reconcile_required"])
            restarted.dispatch(ACTOR, iid, self.world.registry, self.world.mc_client,
                               session=gx.SESSION)
            [recovery] = restarted.get(ACTOR, iid)["recovery"]   # recorded once
            self.assertEqual((recovery["trigger"], recovery["reason"]),
                             ("dispatch_outcome_unknown", "no_outcome_recorded"))
            self.assertEqual(self.owner.dispatch_calls, 0)        # never re-sent from here
            checked = restarted.read_back(ACTOR, iid, self.world.read_back, session=gx.SESSION)
            self.assertEqual((checked["state"], checked["reconciled_by"]),
                             ("verified", "read_back"))
            self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "verified"])
            self.assertEqual(self.record(REL)["po_reference"], f"PO-ISO-{number}")

        with self.subTest("death before the outcome: recovery is recorded once on restart"):
            iid = self.authorized()

            def die(fixture, *args, **kwargs):
                raise KeyboardInterrupt

            with mock.patch.object(ap.FixtureSystem, "apply", die), \
                    self.assertRaises(KeyboardInterrupt):
                self.path.dispatch(OPS, iid, self.registry, session=SESSION)
            restarted = self.new_path()
            self.assertEqual(restarted.get(OPS, iid)["recovery"], [])
            restarted.dispatch(OPS, iid, self.registry, session=SESSION)
            restarted.dispatch(OPS, iid, self.registry, session=SESSION)
            [recovery] = restarted.get(OPS, iid)["recovery"]
            self.assertEqual((recovery["trigger"], recovery["reason"]),
                             ("dispatch_outcome_unknown", "no_outcome_recorded"))

        self.setUp()
        with self.subTest("MC wrote the gate, the process died before recording"):
            iid = self.ready()
            log = self.dir / "sent.jsonl"
            child = textwrap.dedent(f"""
                import json, os, sys
                from datetime import datetime, timezone
                from pathlib import Path
                from operating import action_path as ap
                from operating.cases import CaseStore
                from operating.tests.test_action_path import (FakeMc, OPS, OPERATOR, REL,
                                                              ROSTER, SESSION, VINCE)
                d = Path({str(self.dir)!r})
                clock = lambda: datetime(2026, 10, 1, 12, 0, 1, tzinfo=timezone.utc)
                fx = ap.FixtureSystem()
                fx.seed(REL, {{"id": 3}}, {{"id": 3, "po_reference": None}})
                cases = CaseStore(d / "cases.db", citation_resolver=fx.resolve)
                path = ap.ActionPath(d / "actions.db", cases,
                                     call_approvers=frozenset({{VINCE}}),
                                     mc_operator_email=OPERATOR, approver_roster=ROSTER,
                                     clock=clock)
                mc = FakeMc(clock, d / "mc-write-ledger.jsonl")
                respond = mc.sender.fn
                def die_after_mc_wrote(method, url, headers, body):
                    answer = respond(method, url, headers, body)
                    with open({str(log)!r}, "a") as out:
                        out.write(json.dumps(json.loads(body)["params"]) + "\\n")
                    os._exit(9)
                mc.sender.fn = die_after_mc_wrote
                path.request_gate(OPS, {iid!r}, mc.client, session=SESSION)
            """)
            done = subprocess.run([sys.executable, "-c", child], cwd=str(ROOT), env=_child_env(),
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 9, done.stderr)
            [sent] = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(sent["name"], "mc_request_approval")
            view = self.path.get(OPS, iid)
            self.assertEqual((view["state"], view["history"][-1]["kind"]),
                             ("gate_request_unknown", "gate.request_started"))
            # Nothing re-sends it: not this process, not a retry of the step.
            with self.assertRaises(ap.ActionPathError):
                self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
            self.assertEqual(self.mc.requests, [])
            # Only a read of the task resolves it (the gate the dead process created).
            self.mc.tasks["TASK-9"]["approvalGates"].append({
                "id": "apg_child", "status": "pending", "requestedBy": OPERATOR,
                "reason": sent["arguments"]["reason"], "requestedAt": "2026-10-01T12:00:01Z"})
            reads = len(self.mc.sender.calls)
            self.path.reconcile_gate(OPS, iid, self.mc.client, session=SESSION)
            self.assertEqual(self.path.get(OPS, iid)["gate"]["gate_id"], "apg_child")
            self.assertEqual(self.mc.tools_called()[reads:], ["mc_get_task"])

    def test_production_target_refused(self):
        with self.subTest("the guard does not depend on the registry"):
            intent = ap.ActionIntent("act_x", "c", "P1", ap.FIXTURE_CAPABILITY, "fixture", "0",
                                     dict(EFFECT), "wb-x", "r")
            self.assertIsNone(ap.production_target_refusal(intent))
            for change in ({"owner_system": "portal"}, {"owner_system": "bc"},
                           {"capability": "portal.sales_order.set_customer_po"},
                           {"capability": "fixture.other"},
                           {"expected_effect": {**EFFECT, "relation": "public.app_sales_order"}}):
                forged = ap.ActionIntent(**{**intent.__dict__, **change})
                self.assertEqual(ap.production_target_refusal(forged),
                                 "production_target_refused", change)

        with self.subTest("the isolated owner never holds or dispatches a production row"):
            ids = [item["id"] for item in owner_mod.REAL_CAPABILITY_REQUIREMENTS]
            self.assertIn("operation_lookup_and_fence", ids)
            self.assertIn("conditional_write", ids)
            with self.assertRaises(ValueError):
                self.owner.seed("public.app_sales_order", {"id": 1}, {"id": 1})
            registry = ap.DispatcherRegistry()
            with self.assertRaises(ap.ActionPathError):
                registry.register("portal.sales_order.set_customer_po",
                                  ap.FixtureDispatcher(self.owner))
            portal = ap.ActionIntent("act_x", "c", "p", "portal.sales_order.set_customer_po",
                                     "portal", "0" * 64,
                                     {"relation": "public.app_sales_order", "key": {"id": 1},
                                      "fields": ["po_reference"], "equals": "X"}, "wb-x", "r")
            self.assertEqual(ap.production_target_refusal(portal), "production_target_refused")
            self.assertIsInstance(registry.for_capability(portal.capability),
                                  ap.RefusingDispatcher)
            # The isolated type aimed at a real relation is refused too.
            aimed = ap.ActionIntent("act_y", "c", "p", gx.CITED_VALUE_CAPABILITY, "fixture",
                                    "0" * 64, {"relation": "public.app_sales_order",
                                               "key": {"id": 1}, "fields": ["po_reference"],
                                               "equals": "X"}, "wb-y", "r")
            self.assertEqual(ap.production_target_refusal(aimed), "production_target_refused")
            self.assertEqual(ap.FixtureDispatcher(self.owner).refusal(aimed),
                             "fixture_dispatcher_refuses_non_fixture_relation")

        with self.subTest("a fixture capability aimed at a production row: refused at register"):
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.register(OPS, self.case_id, self.propose(
                    ap.FIXTURE_CAPABILITY, API_ORDER, PRODUCTION_EFFECT), session=SESSION)
            self.assertIn("effect_relation_not_bound", caught.exception.code)

        with self.subTest("a production target gets no MC gate"):
            intent = self.path.register(OPS, self.case_id, self.propose(
                "portal.sales_order.set_customer_po", API_ORDER, PRODUCTION_EFFECT),
                session=SESSION)
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.prepare_gate(OPS, intent["intent_id"], "TASK-9", self.mc.client,
                                       session=SESSION)
            self.assertEqual(caught.exception.code, "production_target_refused")
            self.assertEqual(self.kinds(intent["intent_id"]), ["intent.registered"])

        for capability in ("portal.sales_order.set_customer_po", ap.FIXTURE_CAPABILITY):
            with self.subTest("refused at dispatch even with a valid owner approval",
                              capability=capability):
                self.setUp()
                iid = self.authorized(capability, API_ORDER, PRODUCTION_EFFECT,
                                      past_prepare_guard=True)
                decision = self.path.get(OPS, iid)["decisions"][-1]
                self.assertEqual((decision["authorizes"], decision["decider_role"]),
                                 (True, "owner"))
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
                self.assertEqual((result["state"], result["reason"],
                                  result["business_system_writes"]),
                                 ("dispatch_refused", "production_target_refused", 0))
                self.assertNotIn("dispatch.started", self.kinds(iid))
                self.assertEqual(self.fx.calls, 0)

    def test_forged_decision_refused(self):
        pid = self.propose()
        intent = self.path.register(OPS, self.case_id, pid, session=SESSION)
        iid = intent["intent_id"]
        proposal = next(p for p in self.cases.get(OPS, self.case_id)["proposals"]
                        if p["id"] == pid)
        self.assertEqual([dict(e) for e in intent["evidence"]],
                         [{"handle": c["handle"], "digest": c["digest"]}
                          for c in proposal["citations"]])
        self.assertEqual([c["check"] for c in intent["checked_preconditions"]], ["field_empty"])
        self.assertEqual(list(intent["human_preconditions"]),
                         ["a person at PLX confirms the PO belongs to this order"])

        # The MC approval request record: exact call, bound to case and evidence.
        prepared = self.path.prepare_gate(OPS, iid, "TASK-9", self.mc.client,
                                          session=SESSION)["request"]
        self.assertEqual((prepared["schema"], prepared["tool"], prepared["case_id"],
                          prepared["sent"]),
                         (g2.REQUEST_SCHEMA, "mc_request_approval", self.case_id, False))
        self.assertEqual(prepared["arguments"], {"taskId": "TASK-9",
                                                 "reason": intent["gate_reason"]})
        self.assertEqual(prepared["evidence"], [dict(e) for e in intent["evidence"]])
        self.assertTrue(all(r["holds"] for r in prepared["checked_preconditions"]))
        # Preparing reads MC and sends nothing.
        self.assertEqual(self.mc.tools_called(), ["mc_self_check", "mc_get_task"])
        self.assertEqual((prepared["mc_task"]["stage"], prepared["mc_task"]["data_source"],
                          prepared["mc_task"]["untracked_gates_with_this_reason"]),
                         ("progress", "live", []))

        self.path.approve_call(OPS, iid, "TASK-9", self.call_record(intent), session=SESSION)
        self.path.request_gate(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.mc.requests, [("TASK-9", intent["gate_reason"])])
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        self.assertEqual(self.path.get(OPS, iid)["state"], "gate_requested")  # pending

        # Forged or insufficient approvals: each one on a fresh gate, none authorizes.
        forged = [(STEPHEN, "decider_role_not_admin_or_owner"),     # MC member
                  (OPERATOR, "self_decided"),                       # the operator email
                  ("agent:cos", "decider_not_human"),               # an agent
                  ("sp_mcp_workbench", "decider_not_human"),        # a service principal
                  ("ricardo@petrasoap.com", "decider_not_in_mc_roster")]
        gate_id = "apg_1"
        for number, (decider, code) in enumerate(forged):
            if number:
                gate_id = self.request(iid, f"source record forged #{number} (test)")
            self.mc.decide(gate_id, decided_by=decider)
            self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
            view = self.path.get(OPS, iid)
            self.assertEqual((view["state"], view["decisions"][-1]["reason_code"]),
                             ("gate_not_authorizing", code), decider)
            with self.assertRaises(ap.ActionPathError):
                self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(self.fx.calls, 0)
        self.assertNotIn("dispatch.started", self.kinds(iid))

        # A valid decision: a human MC owner, not the operator, after the request.
        gate_id = self.request(iid, "source record valid (test)")
        self.mc.decide(gate_id, decided_by=VINCE)
        self.path.record_decision(OPS, iid, self.mc.client, session=SESSION)
        decision = self.path.get(OPS, iid)["decisions"][-1]
        self.assertEqual((decision["authorizes"], decision["decider_role"],
                          decision["roster_sha256"]), (True, "owner", ROSTER.sha256))

        result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual(result["state"], "dispatched")
        self.assertEqual(self.fx.rows[(REL, '{"id":3}')]["po_reference"], "PO-7788")
        checked = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
        self.assertEqual((checked["state"], checked["outcome"]), ("verified", "effect_observed"))
        [verification] = self.cases.get(OPS, self.case_id)["verifications"]
        self.assertEqual(verification["detail"]["intent_id"], iid)

        # Every record is bound to the same case/evidence binding; replay sends nothing.
        view = self.path.get(OPS, iid)
        self.assertTrue(view["chain_ok"])
        self.assertEqual(view["recovery"], [])
        replay = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
        self.assertEqual((replay["state"], replay["replayed"]), ("verified", True))
        self.assertEqual(self.fx.calls, 1)
        self.assertEqual(self.mc.tools_called().count("mc_request_approval"), len(forged) + 1)

        with self.subTest("an empty roster authorizes no one"):
            # The call approver's oid is configured with the call approvers instead.
            self.setUp()
            self.path = self.new_path(approver_roster=None, call_approvers={VINCE: VINCE_OID})
            iid = self.authorized()
            view = self.path.get(OPS, iid)
            self.assertEqual((view["state"], view["decisions"][-1]["reason_code"]),
                             ("gate_not_authorizing", "decider_not_in_mc_roster"))
            with self.assertRaises(ValueError):
                g2.ApproverRoster({VINCE: {"role": "owner", "oid": VINCE_OID,   # needs a source
                                           "action_types": ALL_TYPES}})
            for bad in ({"agent:cos@x.com": {"role": "owner", "action_types": ALL_TYPES}},
                        {"sp_mcp_cursor": {"role": "admin", "action_types": ALL_TYPES}},
                        {VINCE: {"role": "superuser", "action_types": ALL_TYPES}}, {VINCE: "owner"},
                        {VINCE: {"role": "owner", "action_types": ALL_TYPES, "oid": "vince"}},
                        {VINCE: {"role": "owner", "action_types": ALL_TYPES, "oid": VINCE_OID,
                                 "email": VINCE}},
                        {VINCE: {"role": "owner", "oid": VINCE_OID}},   # no action types
                        {VINCE: {"role": "owner", "oid": VINCE_OID, "action_types": "all"}}):
                with self.assertRaises(ValueError):
                    g2.ApproverRoster(bad, source_ref="test")
            with self.assertRaises(TypeError):
                self.new_path(approver_roster={VINCE: {"role": "owner", "oid": VINCE_OID,
                                                       "action_types": ALL_TYPES}})
        with self.subTest("an approver whose MC role no source states decides no gate"):
            self.setUp()
            self.path = self.new_path(approver_roster=g2.ApproverRoster(
                {VINCE: {"role": None, "oid": VINCE_OID, "action_types": ALL_TYPES}},
                source_ref="test"))
            iid = self.authorized()
            view = self.path.get(OPS, iid)
            self.assertEqual((view["state"], view["decisions"][-1]["reason_code"]),
                             ("gate_not_authorizing", "decider_not_in_mc_roster"))
            with self.assertRaises(ap.ActionPathError):
                self.path.dispatch(OPS, iid, self.registry, session=SESSION)

        alias_roster = g2.ApproverRoster({VINCE: {"role": "owner", "oid": VINCE_OID,
                                                  "action_types": ALL_TYPES},
                                          "Cos+WB@petrasoap.com": {"role": "owner",
                                                                   "oid": DECIDER_OID,
                                                                   "action_types": ALL_TYPES}},
                                         source_ref="test")
        for decider, code in (("cos+wb@petrasoap.com", "self_decided"),
                              ("COS+x@PetraSoap.com", "self_decided"),
                              ("vince+mc@petrasoap.com", "authorized")):
            with self.subTest("plus aliases and case are one person", decider=decider):
                self.setUp()
                self.path = self.new_path(approver_roster=alias_roster)
                iid = self.authorized(decided_by=decider)
                self.assertEqual(self.path.get(OPS, iid)["decisions"][-1]["reason_code"], code)
        with self.subTest("the operator's alias never approves its own call"):
            self.assertEqual(g2.canonical_email(" Cos+A@PetraSoap.com "), OPERATOR)
            path = self.new_path(call_approvers=frozenset({VINCE, "cos+x@petrasoap.com"}))
            intent = path.register(OPS, self.case_id, self.propose(), session=SESSION)
            path.prepare_gate(OPS, intent["intent_id"], "TASK-9", self.mc.client,
                              session=SESSION)
            with self.assertRaises(ap.ActionPathError) as caught:
                path.approve_call(OPS, intent["intent_id"], "TASK-9",
                                  self.call_record(intent, approved_by="cos+x@petrasoap.com"),
                                  session=SESSION)
            self.assertEqual(caught.exception.code, "call_approver_not_allowed")

    def test_partial_write_never_resent(self):
        with self.subTest("partial write"):
            case_id, iid, target, _task = self.typed_authorized(
                ("po_reference", "external_document_number"), "customer_po")
            self.owner.faults.append("partial_apply")
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched_unknown")
            self.assertEqual(self.owner.operations()[-1]["status"], "partial")
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["outcome"], checked["reason"]),
                             ("dispatched_unknown", "effect_not_observed", "partial_effect"))
            self.assertEqual(checked["fields"]["external_document_number"]["status"], "empty")
            recovery = self.iso.get(ACTOR, iid)["recovery"][-1]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"]),
                             ("effect_contradicted", False))
            step = self.iso_view(iid)["next_safe_step"]
            self.assertEqual((step["step"], step["by"], step["resend_allowed"]),
                             ("escalate", "human", False))
            self.assertTrue(self.iso_dispatch(iid)["reconcile_required"])
            self.assertEqual(self.owner.dispatch_calls, 1)
            flag = self.world.flag_on_case(case_id, iid, target)
            self.assertEqual((flag["recorded"], flag["read_back"]["outcome"]), (True, "verified"))
            self.iso.escalate(ACTOR, iid, "Partial write on the isolated order: reconcile.",
                              session=gx.SESSION)
            self.assertEqual(self.iso.get(ACTOR, iid)["state"], "escalated")

        self.setUp()
        with self.subTest("review repro 4ed9adc: two fields, prior PO-WRONG, partial_apply"):
            _case, iid, _target, _task = self.typed_authorized(
                ("po_reference", "external_document_number"), "customer_po", prior="PO-WRONG")
            self.owner.faults.append("partial_apply")
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched_unknown")
            record = self.record()
            self.assertEqual((record["po_reference"], record["external_document_number"]),
                             (f"PO-DOC-{self.number}", "PO-WRONG"))
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["outcome"], checked["reason"]),
                             ("effect_not_observed", "partial_effect"))
            recovery = self.iso.get(ACTOR, iid)["recovery"][-1]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"]),
                             ("effect_contradicted", False))
            step = self.iso_view(iid)["next_safe_step"]
            self.assertEqual((step["step"], step["by"], step["resend_allowed"]),
                             ("escalate", "human", False))
            self.assertTrue(self.iso_dispatch(iid)["reconcile_required"])
            self.assertEqual(self.owner.dispatch_calls, 1)


class UncertainOutcomeTests(Goal2World):
    def test_an_unknown_dispatch_is_never_resent_and_concludes_only_by_read_back(self):
        with self.subTest("timeout after apply: reconciled to verified"):
            _case_id, iid, number = self.iso_authorized()
            self.owner.faults.append("timeout_after_apply")
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched_unknown")
            self.assertEqual(self.iso_view(iid)["outcome"], "unknown")
            [recovery] = self.iso.get(ACTOR, iid)["recovery"]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"],
                              recovery["idempotency_key"]),
                             ("dispatch_outcome_unknown", False, self.key(iid)))
            for _ in range(2):  # no blind resend, however often dispatch is asked
                self.assertTrue(self.iso_dispatch(iid)["reconcile_required"])
            self.assertEqual(self.owner.dispatch_calls, 1)
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["reconciled_by"],
                              checked["attribution"]),
                             ("verified", "read_back", "idempotency_key"))
            self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "verified"])
            self.assertEqual(self.record(REL)["po_reference"], f"PO-ISO-{number}")

        self.setUp()
        with self.subTest("accepted then dropped: unknown while pending, failed on the fence"):
            _case_id, iid, number = self.iso_authorized()
            self.owner.faults.append("timeout_pending")
            self.iso_dispatch(iid)
            pending = self.iso_read_back(iid)
            self.assertEqual((pending["state"], pending["operation"]["status"],
                              pending["concludes"]), ("dispatched_unknown", "pending", None))
            self.assertEqual(self.iso_view(iid)["outcome"], "unknown")
            self.owner.process_pending(self.key(iid), apply=False)
            failed = self.iso_read_back(iid)
            self.assertEqual((failed["state"], failed["concludes"], failed["reason"],
                              failed["reconciled_by"]),
                             ("failed", "failed", "owner_reports_not_applied", "read_back"))
            view = self.iso_view(iid)
            self.assertEqual(view["recovery"][-1]["trigger"], "owner_reports_not_applied")
            self.assertFalse(view["recovery"][-1]["resend_allowed"])
            self.assertEqual((view["next_safe_step"]["step"],
                              view["next_safe_step"]["resend_allowed"]),
                             ("reverify_and_repropose", False))
            self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "failed"])
            self.assertIsNone(self.record(REL)["po_reference"])
            self.assertEqual(self.owner.dispatch_calls, 1)

        self.setUp()
        with self.subTest("owner unavailable: the read-back fences the key, a late copy is refused"):
            _case_id, iid, number = self.iso_authorized()
            self.owner.faults.append("unavailable")
            self.assertEqual(self.iso_dispatch(iid)["state"],
                             "dispatched_unknown")   # a refused connection is still unknown
            self.assertTrue(self.iso_dispatch(iid)["reconcile_required"])   # never re-sent
            self.assertEqual(self.owner.dispatch_calls, 1)
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["reason"]),
                             ("failed", "owner_reports_not_applied"))
            self.assertEqual(self.iso_view(iid)["outcome_trail"][-1]["outcome"], "failed")
            intent = ap.ActionIntent(**self.iso.get(ACTOR, iid)["intent"])
            with self.assertRaises(owner_mod.OwnerRejected) as late:
                self.owner.dispatch(owner_mod.dispatch_payload(intent), intent.idempotency_key)
            self.assertEqual(late.exception.code, "idempotency_key_fenced")
            self.assertIsNone(self.record(REL)["po_reference"])

        with self.subTest("fixture failure before the send: recovery records, no resend"):
            iid = self.authorized()
            self.fx.fail_next_before_send = True
            self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                                session=SESSION)["state"], "dispatched_unknown")
            [recovery] = self.path.get(OPS, iid)["recovery"]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"],
                              recovery["attempt_id"]),
                             ("dispatch_outcome_unknown", False, f"{iid}-d1"))
            checked = self.path.read_back(OPS, iid, self.fx.read_back, session=SESSION)
            self.assertEqual(checked["outcome"], "effect_not_observed")
            triggers = [r["trigger"] for r in self.path.get(OPS, iid)["recovery"]]
            self.assertEqual(triggers, ["dispatch_outcome_unknown", "effect_not_observed"])
            # A replay does not resend and does not duplicate the recovery record.
            self.assertTrue(self.path.dispatch(OPS, iid, self.registry,
                                               session=SESSION)["reconcile_required"])
            self.assertEqual(len(self.path.get(OPS, iid)["recovery"]), 2)
            self.path.escalate(OPS, iid, "Outcome unknown; check fixture SO 3.", session=SESSION)
            self.assertEqual(self.path.get(OPS, iid)["recovery"][-1]["trigger"],
                             "escalated_to_human")
            self.assertEqual(self.fx.calls, 1)

    def test_a_read_back_that_proves_nothing_concludes_nothing(self):
        def answer(status, fenced):
            return lambda intent: {"outcome": "effect_not_observed", "reason": None,
                                   "fields": {}, "operation": {"status": status,
                                                               "fenced": fenced}}

        # (fault before the dispatch, read-back, state and concludes after it, last recovery):
        # not applied without a fence proves nothing; an owner contradicting an
        # acknowledged dispatch is not concluded failed.
        rows = (("unavailable", answer("not_applied", False), ("dispatched_unknown", None),
                 "effect_not_observed"),
                (None, answer("not_applied", True), ("dispatched", None), None))
        for fault, read, expected, trigger in rows:
            with self.subTest(fault=fault, expected=expected):
                _case_id, iid, _number = self.iso_authorized()
                if fault:
                    self.owner.faults.append(fault)
                self.iso_dispatch(iid)
                result = self.iso_read_back(iid, read)
                self.assertEqual((result["state"], result["concludes"]), expected)
                self.assertEqual(self.iso_view(iid)["outcome"], "unknown")
                if trigger:
                    self.assertEqual(self.iso.get(ACTOR, iid)["recovery"][-1]["trigger"],
                                     trigger)
        with self.subTest("a malformed operation is refused and recorded nowhere"):
            _case_id, iid, _number = self.iso_authorized()
            self.owner.faults.append("unavailable")
            self.iso_dispatch(iid)
            events = len(self.iso.get(ACTOR, iid)["history"])
            with self.assertRaises(ap.ActionPathError) as refused:
                self.iso_read_back(iid, lambda intent: {
                    "outcome": "effect_not_observed", "fields": {},
                    "operation": {"status": "gone", "fenced": True}})
            self.assertEqual(refused.exception.code, "invalid_read_back_operation")
            self.assertEqual(len(self.iso.get(ACTOR, iid)["history"]), events)

    def test_pending_and_escalated_intents_wait_for_a_read_back_never_a_resend(self):
        with self.subTest("review repro d7ae93f: a pending correction reads back later"):
            _case, iid, _target, _task = self.typed_authorized(prior="PO-WRONG")
            self.owner.faults.append("timeout_pending")
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched_unknown")
            self.assertEqual(self.owner.operations()[-1]["status"], "pending")
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["outcome"], checked["reason"],
                              checked["operation"]["status"]),
                             ("effect_not_observed", None, "pending"))
            self.assertEqual(self.iso.get(ACTOR, iid)["recovery"][-1]["trigger"],
                             "effect_not_observed")
            self.assertNotEqual(self.iso_view(iid)["next_safe_step"]["step"], "escalate")
            self.owner.process_pending(self.key(iid), apply=True)
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["outcome"]),
                             ("verified", "effect_observed"))
            self.assertEqual(self.record()["po_reference"], f"PO-DOC-{self.number}")

        self.setUp()
        with self.subTest("three unobserved read-backs escalate without a resend"):
            _case, iid, _target, _task = self.typed_authorized()
            self.owner.faults.append("timeout_pending")
            self.iso_dispatch(iid)
            steps = []
            for _ in range(ap.READ_BACKS_BEFORE_ESCALATION):
                checked = self.iso_read_back(iid)
                self.assertEqual((checked["outcome"], checked["reason"]),
                                 ("effect_not_observed", None))
                steps.append(self.iso_view(iid)["next_safe_step"])
            self.assertEqual([item["step"] for item in steps],
                             ["read_back"] * (ap.READ_BACKS_BEFORE_ESCALATION - 1)
                             + ["escalate"])
            self.assertEqual((steps[-1]["by"], steps[-1]["resend_allowed"]), ("human", False))
            self.assertTrue(self.iso_dispatch(iid)["reconcile_required"])
            self.assertEqual(self.owner.dispatch_calls, 1)

        self.setUp()
        with self.subTest("an escalated intent is verified only by a fresh read-back"):
            _case, iid, _target, _task = self.typed_authorized()
            self.owner.faults.append("overwritten_after_apply")
            self.iso_dispatch(iid)
            self.iso.escalate(ACTOR, iid, "A different value is on the order: reconcile.",
                              session=gx.SESSION)
            self.assertEqual(self.iso.get(ACTOR, iid)["state"], "escalated")
            # Nothing but a read-back moves it: not-observed leaves it escalated.
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["outcome"]),
                             ("escalated", "effect_not_observed"))
            self.assertTrue(self.iso_dispatch(iid)["reconcile_required"])
            self.assertEqual(self.iso.get(ACTOR, iid)["state"], "escalated")
            # A person fixes the owning system; the next read-back observes the effect.
            self.owner.update_by_other_writer(gx.TYPED_RELATION, {"id": self.number},
                                              {"po_reference": f"PO-DOC-{self.number}"})
            verified = self.iso_read_back(iid)
            self.assertEqual((verified["state"], verified["outcome"]),
                             ("verified", "effect_observed"))
            self.assertEqual(self.owner.dispatch_calls, 1)

    def test_late_dispatch_answer_never_regresses_a_concurrent_verified_read_back(self):
        # G2-TRAIL-002: another session reads back between the owner's commit and the
        # dispatch answer; the late dispatch.finished must not undo "verified".
        case_id, iid, number = self.iso_authorized()
        seen = {}

        def concurrent_read_back() -> None:
            self.owner.after_apply = None
            seen["read_back"] = self.iso_read_back(iid)

        self.owner.after_apply = concurrent_read_back
        result = self.iso_dispatch(iid)
        self.assertEqual((seen["read_back"]["state"], seen["read_back"]["outcome"]),
                         ("verified", "effect_observed"))
        self.assertEqual((result["state"], result["replayed"]), ("verified", False))
        self.assertTrue(result["receipt"].startswith("owner-receipt:"))
        state = self.iso.get(ACTOR, iid)
        self.assertEqual(state["state"], "verified")
        # The late answer still lands on the attempt: receipt and outcome recorded.
        self.assertEqual((state["dispatches"][-1]["outcome"], state["dispatches"][-1]["receipt"]),
                         ("dispatched", result["receipt"]))
        self.assertEqual(self.iso_view(iid)["outcome"], "verified")
        self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "verified"])
        self.assertEqual([v["outcome"] for v in
                          self.world.cases.get(ACTOR, case_id)["verifications"]],
                         ["effect_observed"])
        replay = self.iso_dispatch(iid)
        self.assertEqual((replay["state"], replay["replayed"]), ("verified", True))
        self.assertEqual(self.owner.dispatch_calls, 1)
        self.assertEqual(self.record(REL)["po_reference"], f"PO-ISO-{number}")


class ContradictedReadBackTests(Goal2World):
    def test_a_contradicting_read_back_is_flagged_and_never_resent(self):
        with self.subTest("a different value on read-back"):
            _case, iid, _target, _task = self.typed_authorized()
            self.owner.faults.append("overwritten_after_apply")
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")
            checked = self.iso_read_back(iid)
            self.assertEqual((checked["state"], checked["reason"]),
                             ("dispatched", "value_differs"))
            self.assertEqual(checked["operation"]["status"], "applied")
            self.assertEqual(self.iso.get(ACTOR, iid)["recovery"][-1]["trigger"],
                             "effect_contradicted")
            self.assertEqual(self.iso_view(iid)["next_safe_step"]["step"], "escalate")
            self.assertTrue(self.iso_dispatch(iid)["replayed"])
            self.assertEqual(self.owner.dispatch_calls, 1)

        # The isolated owner reads synchronously: a revert to the cited prior, or an
        # emptied field, is value_differs (no cited-prior exemption) ...
        for prior, after in (("PO-WRONG", "PO-WRONG"), (None, "")):
            with self.subTest("an applied write later reads differently", prior=prior):
                _case, iid, _target, _task = self.typed_authorized(prior=prior)
                self.iso_dispatch(iid)
                intent = ap.ActionIntent(**self.iso.get(ACTOR, iid)["intent"])
                self.owner.update_by_other_writer(gx.TYPED_RELATION, {"id": self.number},
                                                  {"po_reference": after})
                observed = owner_mod.read_back_for(self.owner)(intent)
                self.assertEqual((observed["fields"]["po_reference"]["status"] == "empty",
                                  observed["outcome"], observed["reason"],
                                  observed["operation"]["status"]),
                                 (after == "", "effect_not_observed", "value_differs",
                                  "applied"))
                checked = self.iso_read_back(iid)
                self.assertEqual(self.iso.get(ACTOR, iid)["recovery"][-1]["trigger"],
                                 "effect_contradicted")
                self.assertEqual(checked["reason"], "value_differs")
                # ... unless the owner declares replica lag: it may just not show it yet.
                self.owner.declares_replica_lag = True
                observed = owner_mod.read_back_for(self.owner)(intent)
                self.assertEqual((observed["outcome"], observed["reason"]),
                                 ("effect_not_observed", None))
                self.owner.declares_replica_lag = False

    def test_no_contradiction_while_the_owner_may_still_apply(self):
        # An unseen key (not applied, fenced by this read) is never a contradiction.
        read = owner_mod.read_back_for(self.owner)
        self.number += 1
        key = {"id": self.number}
        self.owner.seed(gx.TYPED_RELATION, key, {"id": self.number, "po_reference": "A",
                                                 "external_document_number": "B"})
        intent = mock.Mock(idempotency_key="wb-" + "0" * 40, checked_preconditions=(),
                           expected_effect={"relation": gx.TYPED_RELATION, "key": key,
                                            "fields": ["po_reference",
                                                       "external_document_number"],
                                            "equals": "A"})
        self.assertEqual(read(intent)["reason"], None)
        intent.expected_effect = {**intent.expected_effect, "equals": "C"}
        self.assertEqual((read(intent)["outcome"], read(intent)["reason"]),
                         ("effect_not_observed", None))


class OwnerRefusalTests(Goal2World):
    def test_a_certain_owner_refusal_fails_with_nothing_written(self):
        with self.subTest("conditional write conflict"):
            _case_id, iid, _number = self.iso_authorized()
            self.owner.faults.append("conflict")
            result = self.iso_dispatch(iid)
            self.assertEqual((result["state"], result["reason"]),
                             ("failed", "conditional_write_conflict"))
            [recovery] = self.iso.get(ACTOR, iid)["recovery"]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"],
                              recovery["dispatch_outcome"], recovery["idempotency_key"]),
                             ("dispatch_rejected_by_owner", False, "rejected", self.key(iid)))
            self.assertTrue(recovery["next_step"].startswith("certain_nothing_written"))
            # The other writer's value stands: the conditional write never overwrote it.
            self.assertEqual(self.record(REL)["po_reference"], "changed-by-another-writer")
            self.assertEqual([op["status"] for op in self.owner.operations()], ["rejected"])
            replay = self.iso_dispatch(iid)
            self.assertEqual((replay["state"], replay["replayed"]), ("failed", True))
            self.assertEqual(self.owner.dispatch_calls, 1)
            with self.assertRaises(ap.ActionPathError):
                self.iso_read_back(iid)
            with self.assertRaises(ap.ActionPathError):
                self.iso.escalate(ACTOR, iid, "nothing to escalate", session=gx.SESSION)
            view = self.iso_view(iid)
            self.assertEqual((view["outcome"], view["next_safe_step"]["step"]),
                             ("failed", "reverify_and_repropose"))
            self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "failed"])

        with self.subTest("etag mismatch from a concurrent edit"):
            _case, iid, _target, _task = self.typed_authorized()
            self.owner.faults.append("concurrent_write")
            result = self.iso_dispatch(iid)
            self.assertEqual((result["state"], result["reason"]), ("failed", "etag_mismatch"))
            self.assertIsNone(self.record()["po_reference"])           # nothing we set landed
            self.assertEqual(self.record()[gx.VERSION_FIELD], 2)        # the other writer's save
            recovery = self.iso.get(ACTOR, iid)["recovery"][-1]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"]),
                             ("dispatch_rejected_by_owner", False))
            self.assertEqual(self.iso_view(iid)["next_safe_step"]["step"],
                             "reverify_and_repropose")

    def test_conflict_holds_for_a_target_handle_spelled_with_json_spacing(self):
        # G2-CAS-001: the case store accepts the target by relation and canonical key,
        # so the owner-side compare-and-set must too, however the handle is spelled.
        key = {"id": 77}
        self.owner.seed(REL, key, {"id": 77, "po_reference": None})
        spaced = f"fixture://{REL}/" + json.dumps(key, separators=(", ", " : "))
        self.assertNotEqual(spaced, owner_mod.handle(REL, key))
        case_id = self.iso.open_case(ACTOR, "Set the PO on an order cited with spacing to "
                                            "PO-ISO-77?",
                                     roots=["isolated-order-77"], objective_author_kind="human",
                                     session=gx.SESSION)
        proposal = self.iso.record_proposal(
            ACTOR, case_id, ap.FIXTURE_CAPABILITY,
            {"field": "po_reference", "value": "PO-ISO-77"},
            [{"check": "field_empty", "citation": spaced, "field": "po_reference"},
             "a person confirms the PO belongs to this order"],
            citations=[spaced], session=gx.SESSION,
            expected_effect={"relation": REL, "key": key, "fields": ["po_reference"],
                             "equals": "PO-ISO-77"})
        iid = self.iso.register(ACTOR, case_id, proposal["proposal_id"],
                                session=gx.SESSION)["intent_id"]
        self.world.authorize(iid, "TASK-ISO-77")
        stored = self.iso.get(ACTOR, iid)["intent"]
        intent = ap.ActionIntent(**{**stored,
                                    "checked_preconditions": tuple(stored["checked_preconditions"]),
                                    "human_preconditions": (), "evidence": ()})
        self.assertEqual(owner_mod.dispatch_payload(intent)["expect"],
                         [{"check": "field_empty", "field": "po_reference"}])
        self.owner.faults.append("conflict")
        result = self.iso_dispatch(iid)
        self.assertEqual((result["state"], result["reason"]),
                         ("failed", "conditional_write_conflict"))
        self.assertEqual(self.owner.record(REL, key)["po_reference"], "changed-by-another-writer")
        self.assertEqual([op["status"] for op in self.owner.operations()], ["rejected"])

    def test_a_known_transient_refusal_may_retry_on_the_same_key(self):
        with self.subTest("isolated owner"):
            _case_id, iid, number = self.iso_authorized()
            with mock.patch.object(owner_mod.IsolatedOwnerSystem, "dispatch",
                                   side_effect=ap.DispatchRefused("owner_locked")):
                refused = self.iso_dispatch(iid)
            self.assertEqual((refused["state"], self.iso_view(iid)["outcome"]),
                             ("dispatch_refused", "not_dispatched"))
            self.assertTrue(self.iso.get(ACTOR, iid)["recovery"][-1]["resend_allowed"])
            self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")
            self.assertEqual(self.iso_read_back(iid)["state"], "verified")
            self.assertEqual([op["idempotency_key"] for op in self.owner.operations()],
                             [self.key(iid)])
            self.assertEqual(self.record(REL)["po_reference"], f"PO-ISO-{number}")

        with self.subTest("fixture system, retried on a current decision"):
            iid = self.authorized()
            original = ap.FixtureSystem.apply

            def refuse_once(fixture, *args, **kwargs):
                fixture.calls += 1
                raise ap.DispatchRefused("fixture_target_locked")

            with mock.patch.object(ap.FixtureSystem, "apply", refuse_once):
                result = self.path.dispatch(OPS, iid, self.registry, session=SESSION)
            self.assertEqual((result["state"], result["reason"]),
                             ("dispatch_refused", "fixture_target_locked"))
            [recovery] = self.path.get(OPS, iid)["recovery"]
            self.assertEqual((recovery["trigger"], recovery["resend_allowed"]),
                             ("dispatch_failed", True))
            self.assertIs(ap.FixtureSystem.apply, original)
            self.assertEqual(self.path.dispatch(OPS, iid, self.registry,
                                                session=SESSION)["state"], "dispatched")
            self.assertEqual(self.path.read_back(OPS, iid, self.fx.read_back,
                                                 session=SESSION)["state"], "verified")


class ApprovalWithdrawnTests(Goal2World):
    def demoted_path(self):
        demoted = g2.ApproverRoster({gx.MC_ADMIN: {"role": "member", "oid": gx.MC_ADMIN_OID,
                                                   "action_types": ALL_TYPES}},
                                    source_ref="isolated roster v2")
        return ap.ActionPath(self.world.state_dir / "actions.db", self.world.cases,
                             call_approvers=frozenset({gx.CALL_APPROVER}),
                             mc_operator_email=gx.OPERATOR, approver_roster=demoted,
                             clock=self.world.clock)

    def long_lived_path(self):
        """The same path, with a Workbench decision age (100h) longer than MC's approval
        expiry (72h for an unscoped gate), so MC's own expiry is what is checked."""
        return ap.ActionPath(self.world.state_dir / "actions.db", self.world.cases,
                             **self.world.approvers.path_options(),
                             mc_operator_email=gx.OPERATOR, clock=self.world.clock,
                             decision_max_age=timedelta(hours=100))

    def gate_of(self, task):
        [gate] = self.world.mc.tasks[task]["approvalGates"]
        return gate

    def test_a_withdrawn_approval_dispatches_nothing(self):
        # (withdraw the approval in MC, or dispatch through a path whose roster demoted the
        # decider; the reason the dispatch is refused with)
        rows = (("task closed", lambda task: self.world.mc.set_stage(
                    task, "verified", session_label=gx.MC_ADMIN), None,
                 "approval_withdrawn:mc_task_closed"),
                # PLX_MC @77a5af6 (TASK-2529/2598): an admin cancels the task; the gate
                # stays approved, and cancelled is closed like merged/verified.
                ("task cancelled", lambda task: self.world.mc.cancel(
                    task, "obsolete", session_label=gx.MC_ADMIN, session_kind="human"), None,
                 "approval_withdrawn:mc_task_closed"),
                ("task no longer visible", lambda task: self.world.mc.restrict(task), None,
                 "approval_withdrawn:mc_task_not_found"),
                # A cancel then a reopen after the
                # approval changes what it was granted on; the reopened task needs a new one.
                ("task cancelled and reopened", self.cancel_and_reopen, None,
                 "approval_withdrawn:task_reopened_after_cancel"),
                # A stage MC adds later (not in MC_STAGES) is never read as open.
                ("unknown MC stage", lambda task: self.world.mc.tasks[task].update(
                    stage="on_hold"), None, "approval_withdrawn:mc_task_stage_unknown"),
                # 25 events since the decision: MC's window no longer shows whether a
                # cancel came between; fail closed.
                ("history past MC's event window", lambda task: [self.world.mc._event(
                    "task.progress", gx.MC_ADMIN, task, {}) for _ in range(25)], None,
                 "approval_withdrawn:task_history_unreadable"),
                ("decider no longer admin or owner", None, self.demoted_path,
                 "approval_withdrawn:decider_no_longer_admin_or_owner"),
                # PLX_MC @2d2cac7 (TASK-3408): an approved gate carries ``expiresAt``
                # (unscoped: decidedAt + 72h) and stops covering at that time, even
                # inside the Workbench's own decision age.
                ("MC approval expired", lambda task: self.world.clock.advance(
                    timedelta(hours=72, minutes=1)), self.long_lived_path,
                 "approval_withdrawn:mc_approval_expired"),
                ("MC expiry unreadable", lambda task: self.gate_of(task).update(
                    expiresAt="next tuesday"), None,
                 "approval_withdrawn:mc_approval_expiry_unreadable"),
                ("MC expiry without an offset", lambda task: self.gate_of(task).update(
                    expiresAt="2099-01-01T00:00:00"), None,
                 "approval_withdrawn:mc_approval_expiry_unreadable"),
                ("MC expiry not after the decision", lambda task: self.gate_of(task).update(
                    expiresAt=self.gate_of(task)["decidedAt"]), None,
                 "approval_withdrawn:mc_approval_expiry_unreadable"),
                ("MC expiry of the wrong type", lambda task: self.gate_of(task).update(
                    expiresAt=1793000000), None,
                 "approval_withdrawn:mc_approval_expiry_unreadable"))
        for name, withdraw, path, reason in rows:
            with self.subTest(name):
                self.setUp()
                _case, iid, _target, task_id = self.typed_authorized()
                if withdraw:
                    withdraw(task_id)
                path = path() if path else self.iso
                result = path.dispatch(ACTOR, iid, self.world.registry, self.world.mc_client,
                                       session=gx.SESSION)
                self.assertEqual((result["state"], result["reason"]),
                                 ("gate_not_authorizing", reason))
                # The withdrawn approval can never be used again; a recovery record says so.
                state = self.iso.get(ACTOR, iid)
                self.assertEqual(state["dispatches"], [])
                self.assertEqual(state["recovery"][-1]["trigger"], "approval_withdrawn")
                self.assertFalse(state["recovery"][-1]["resend_allowed"])
                with self.assertRaises(ap.ActionPathError):
                    self.iso_dispatch(iid)
                self.assertEqual(self.owner.dispatch_calls, 0)
                self.assertEqual(self.iso_view(iid)["outcome"], "not_dispatched")

    def test_an_unexpired_or_legacy_mc_approval_still_dispatches(self):
        # Inside MC's 72h window the approval stands; a gate MC stamped no expiry on (a
        # legacy gate, or an MC not yet at TASK-3408) is bounded by the Workbench's own
        # decision age alone, never read as expired.
        _case, iid, _target, task_id = self.typed_authorized()
        gate = self.gate_of(task_id)
        instant = lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))  # noqa: E731
        self.assertEqual(instant(gate["expiresAt"]) - instant(gate["decidedAt"]),
                         timedelta(hours=72))
        self.assertIsNone(gate["signature"])     # no signing key in the isolated MC
        self.world.clock.advance(timedelta(hours=71, minutes=59))
        self.assertEqual(self.long_lived_path().dispatch(
            ACTOR, iid, self.world.registry, self.world.mc_client,
            session=gx.SESSION)["state"], "dispatched")
        self.setUp()
        _case, iid, _target, task_id = self.typed_authorized()
        gate = self.gate_of(task_id)
        del gate["expiresAt"], gate["signature"]
        self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")
        self.setUp()      # null is MC's own "not set" (ApprovalGateState.expiresAt)
        _case, iid, _target, task_id = self.typed_authorized()
        self.gate_of(task_id)["expiresAt"] = None
        self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")
        self.setUp()
        _case, iid, _target, task_id = self.typed_authorized()
        del self.gate_of(task_id)["expiresAt"]
        self.world.clock.advance(timedelta(hours=25))
        result = self.iso_dispatch(iid)
        self.assertEqual(result["reason"], "decision_too_old")
        self.assertEqual(self.owner.dispatch_calls, 0)

    def cancel_and_reopen(self, task):
        self.world.clock.advance(timedelta(minutes=1))
        self.world.mc.cancel(task, "obsolete", session_label=gx.MC_ADMIN, session_kind="human")
        self.world.clock.advance(timedelta(minutes=1))
        self.world.mc.reopen(task, session_label=gx.MC_ADMIN, session_kind="human")
        self.world.clock.advance(timedelta(minutes=1))

    def test_an_approval_recorded_while_the_task_was_cancelled_does_not_stand_once_reopened(self):
        # MC still decides a gate on a cancelled task (policy.ts stageAdvanceViolation lets the
        # cancel through the pending gate). Cancelled: closed. Reopened: withdrawn, never
        # standing again; a task cancelled and reopened before its gate was requested is not.
        def authorized_while_cancelled():
            decide = self.world.mc.decide

            def cancelled_first(task_id, gate_id, decision, *, session_label, session_kind):
                if session_label == gx.MC_ADMIN:
                    self.world.clock.advance(timedelta(minutes=1))
                    self.world.mc.cancel(task_id, "obsolete", session_label=gx.MC_ADMIN,
                                         session_kind="human")
                    self.world.clock.advance(timedelta(minutes=1))
                return decide(task_id, gate_id, decision, session_label=session_label,
                              session_kind=session_kind)

            with mock.patch.object(self.world.mc, "decide", cancelled_first):
                return self.typed_authorized()

        _case, iid, _target, task_id = authorized_while_cancelled()
        self.assertEqual(self.world.mc.tasks[task_id]["stage"], "cancelled")
        result = self.iso_dispatch(iid)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_not_authorizing", "approval_withdrawn:mc_task_closed"))
        self.setUp()
        _case, iid, _target, task_id = authorized_while_cancelled()
        self.world.clock.advance(timedelta(minutes=1))
        self.world.mc.reopen(task_id, session_label=gx.MC_ADMIN, session_kind="human")
        self.assertEqual(self.world.mc.tasks[task_id]["stage"], "progress")
        result = self.iso_dispatch(iid)
        self.assertEqual((result["state"], result["reason"]),
                         ("gate_not_authorizing", "approval_withdrawn:task_reopened_after_cancel"))
        self.assertEqual(self.owner.dispatch_calls, 0)
        self.assertFalse(self.iso.get(ACTOR, iid)["recovery"][-1]["resend_allowed"])
        # Cancelled and reopened before the gate was requested: the approval stands.
        self.setUp()
        add_task = self.world.mc.add_task

        def reopened_first(task_id, title):
            add_task(task_id, title)
            self.cancel_and_reopen(task_id)

        with mock.patch.object(self.world.mc, "add_task", reopened_first):
            _case, iid, _target, task_id = self.typed_authorized()
        self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")

    def test_dispatch_needs_a_fresh_readable_mc_approval(self):
        _case, iid, _target, _task = self.typed_authorized()
        with self.assertRaises(ap.ActionPathError) as refused:   # no MC client: no re-read
            self.iso.dispatch(ACTOR, iid, self.world.registry, session=gx.SESSION)
        self.assertEqual(str(refused.exception), "mc_recheck_required")
        self.assertEqual(self.iso.get(ACTOR, iid)["state"], "gate_authorized")
        self.world.mc.down = True
        refused = self.iso_dispatch(iid)
        self.world.mc.down = False
        self.assertEqual(refused["state"], "dispatch_refused")
        self.assertTrue(refused["reason"].startswith("mc_unreadable"))
        self.assertEqual(self.owner.dispatch_calls, 0)
        self.assertEqual(self.iso_dispatch(iid)["state"], "dispatched")   # a later read
        self.assertEqual(self.owner.dispatch_calls, 1)
        # MC itself freezes a task's stage while a gate is pending.
        self.world.mc.add_task("TASK-ISO-PENDING", "pending gate")
        self.world.mc._request_approval({"taskId": "TASK-ISO-PENDING", "reason": "r"},
                                        gx.OPERATOR)
        with self.assertRaises(gx.IsolatedMcRefused) as frozen:
            self.world.mc.set_stage("TASK-ISO-PENDING", "verified", session_label=gx.MC_ADMIN)
        self.assertEqual(frozen.exception.code, "input_required")


class RegistrationTests(Goal2World):
    def test_register_checks_preconditions_and_traces_every_parameter(self):
        with self.subTest("structured preconditions on the cited evidence"):
            self.fx.rows[(REL, '{"id":3}')]["po_reference"] = "already set"
            with self.assertRaises(ap.ActionPathError) as caught:
                self.path.register(OPS, self.case_id, self.propose(), session=SESSION)
            self.assertEqual(caught.exception.code, "precondition_not_met")
            self.fx.rows[(REL, '{"id":3}')]["po_reference"] = None
            cases = [([], "proposal_has_no_checkable_precondition"),
                     (["prose only"], "proposal_has_no_checkable_precondition"),
                     ([{"check": "field_empty", "citation": self.handle}],
                      "precondition_unrecognized:0"),
                     ([{"check": "run_sql", "citation": self.handle, "field": "x"}],
                      "precondition_unrecognized:0"),
                     ([{"check": "field_empty", "citation": API_ORDER,
                        "field": "po_reference"}], "precondition_not_cited"),
                     ([{"check": "field_equals", "citation": self.handle, "field": "id",
                        "value": 4}], "precondition_not_met")]
            for preconditions, code in cases:
                with self.subTest(code=code), self.assertRaises(ap.ActionPathError) as caught:
                    self.path.register(OPS, self.case_id,
                                       self.propose(preconditions=preconditions),
                                       session=SESSION)
                self.assertEqual(caught.exception.code, code)
            ok = self.path.register(OPS, self.case_id, self.propose(preconditions=[
                {"check": "field_equals", "citation": self.handle, "field": "id", "value": 3},
                {"check": "field_empty", "citation": self.handle, "field": "po_reference"}]),
                session=SESSION)
            self.assertEqual(len(ok["checked_preconditions"]), 2)

        with self.subTest("every parameter of the isolated type traces to cited evidence"):
            entry = ActionCatalog.builtin().get(gx.CITED_VALUE_CAPABILITY)
            traces = {name: spec["trace"]["kind"] for name, spec in entry["parameters"].items()}
            self.assertEqual(traces, {"field": "effect", "value": "cited_value",
                                      "evidence_citation": "citation",
                                      "evidence_field": "cited_field"})
            self.assertEqual((entry["owner_system"], entry["executor"]["dispatch"]),
                             ("fixture", "isolated_owner_only"))
            self.assertIn(gx.CITED_VALUE_CAPABILITY, ap.FIXTURE_CAPABILITIES)

        with self.subTest("a value not in the cited field never becomes an intent"):
            self.number += 1
            key = {"id": self.number}
            target = self.owner.seed(gx.TYPED_RELATION, key, {"id": self.number,
                                                              "po_reference": None})
            source = self.owner.seed(gx.SOURCE_RELATION, key, {"id": self.number,
                                                               "customer_po": "PO-DOC-1"})
            case_id = self.world.cases.open(ACTOR, "Which PO does isolated order 1 carry?")
            proposal = self.world.cases.propose(
                ACTOR, case_id, gx.CITED_VALUE_CAPABILITY,
                {"field": "po_reference", "value": "PO-NOT-CITED", "evidence_citation": source,
                 "evidence_field": "customer_po"},
                [{"check": "field_empty", "citation": target, "field": "po_reference"}],
                citations=[source, target],
                expected_effect={"relation": gx.TYPED_RELATION, "key": key,
                                 "fields": ["po_reference"], "equals": "PO-NOT-CITED"})
            with self.assertRaises(ap.ActionPathError) as raised:
                self.iso.register(ACTOR, case_id, proposal["proposal_id"], session=gx.SESSION)
            self.assertEqual(str(raised.exception),
                             "parameter_untraced:value:value_not_in_cited_field")

        with self.subTest("a value of the wrong type is refused at register before any gate"):
            self.number += 1
            with self.assertRaises(ap.ActionPathError) as refused:
                self.world.open_case_from_finding(self.number, ("line_count",), "lines_stated")
            self.assertEqual(str(refused.exception), "effect_value_type_mismatch:line_count")
            self.assertNotIn("mc_request_approval", self.world.mc.tool_calls)
            self.assertEqual(self.owner.dispatch_calls, 0)
            # The owner checks again at apply (a write that bypassed the catalog).
            base = {"schema": owner_mod.DISPATCH_SCHEMA,
                    "capability": gx.CITED_VALUE_CAPABILITY, "relation": gx.TYPED_RELATION,
                    "key": {"id": self.number}, "expect": []}
            with self.assertRaises(owner_mod.OwnerRejected) as owner_refused:
                self.owner.dispatch({**base, "set": {"line_count": "1"}}, "wb-" + "1" * 40)
            self.assertEqual(owner_refused.exception.code, "field_type_mismatch:line_count")

    def test_the_owner_applies_only_typed_fields_under_conditions_it_can_check(self):
        with self.subTest("typed relations refuse bad rows and manage the version"):
            for define in ((gx.TYPED_RELATION, {"id": "money"}),
                           (gx.TYPED_RELATION, {"version": "integer"}),
                           ("public.app_sales_order", {"id": "integer"})):
                with self.assertRaises(ValueError):
                    self.owner.define(*define)
            for row in ({"id": "nine"}, {"id": 9, "colour": "red"}):
                with self.assertRaises(ValueError):
                    self.owner.seed(gx.TYPED_RELATION, {"id": 9}, row)
            self.owner.seed(gx.TYPED_RELATION, {"id": 9}, {"id": 9, gx.VERSION_FIELD: 41})
            self.assertEqual(self.owner.record(gx.TYPED_RELATION, {"id": 9})[gx.VERSION_FIELD], 1)
            self.owner.update_by_other_writer(gx.TYPED_RELATION, {"id": 9}, {"line_count": 3})
            self.assertEqual(self.owner.record(gx.TYPED_RELATION, {"id": 9})[gx.VERSION_FIELD], 2)

        with self.subTest("the version field is read-only and unknown fields are refused"):
            self.owner.seed(gx.TYPED_RELATION, {"id": 8}, {"id": 8})
            base = {"schema": owner_mod.DISPATCH_SCHEMA,
                    "capability": gx.CITED_VALUE_CAPABILITY, "relation": gx.TYPED_RELATION,
                    "key": {"id": 8}, "expect": []}
            for index, (fields, code) in enumerate((({gx.VERSION_FIELD: 7},
                                                     "field_read_only:version"),
                                                    ({"colour": "red"},
                                                     "field_unknown:colour"))):
                with self.assertRaises(owner_mod.OwnerRejected) as refused:
                    self.owner.dispatch({**base, "set": fields}, f"wb-{index:040d}")
                self.assertEqual(refused.exception.code, code)
            self.assertEqual(self.owner.record(gx.TYPED_RELATION, {"id": 8})[gx.VERSION_FIELD], 1)

        self.setUp()
        with self.subTest("a precondition that cannot become a condition on the target"):
            _case_id, iid, number = self.iso_authorized()
            stored = self.iso.get(ACTOR, iid)["intent"]
            base = {**stored, "human_preconditions": (), "evidence": ()}
            cases = {
                "precondition_citation_not_mappable":
                    {"check": "field_empty", "citation": f"fixture://{REL}/{{not json",
                     "field": "po_reference"},
                "precondition_not_compare_and_set":
                    {"check": "field_matches",
                     "citation": f"fixture://{REL}/" + json.dumps({"id": number},
                                                                  separators=(", ", " : ")),
                     "field": "po_reference"},
            }
            for code, check in cases.items():
                with self.subTest(code=code):
                    intent = ap.ActionIntent(**{**base, "checked_preconditions": (check,)})
                    with self.assertRaises(owner_mod.PayloadUnmappable) as raised:
                        owner_mod.dispatch_payload(intent)
                    self.assertEqual(raised.exception.code, code)
                    dispatcher = self.world.registry.for_capability(ap.FIXTURE_CAPABILITY)
                    self.assertEqual(ap.FixtureDispatcher.refusal(dispatcher, intent), code)
                    with self.assertRaises(ap.DispatchRefused):
                        ap.FixtureDispatcher.dispatch(dispatcher, intent)
            # A precondition citing another record is not a condition on the target.
            other = {"check": "field_empty", "citation": owner_mod.handle(REL, {"id": -1}),
                     "field": "po_reference"}
            intent = ap.ActionIntent(**{**base, "checked_preconditions": (other,)})
            self.assertEqual(owner_mod.dispatch_payload(intent)["expect"], [])
            self.assertEqual(self.owner.dispatch_calls, 0)
            self.assertEqual(self.owner.operations(), [])


class McWriteDoorTests(Goal2World):
    def test_the_write_needs_the_opted_in_client_and_its_approver(self):
        sender = FakeSender(fn=lambda *a: self.fail("no I/O for a refused write"))
        for client in (self.write_client(sender, write_tools=()),          # default: no writes
                       self.write_client(sender, approvers=(STEPHEN,))):   # not this approver
            with self.subTest(client=client):
                self.setUp()
                iid = self.ready()
                self.path.request_gate(OPS, iid, client, session=SESSION)
                view = self.path.get(OPS, iid)
                self.assertEqual(view["state"], "gate_request_refused")
                self.assertEqual(view["history"][-2]["kind"], "gate.request_started")
        self.assertEqual(sender.calls, [])
        # Another operator's client is refused before anything is recorded or sent.
        iid = self.ready()
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.request_gate(OPS, iid, self.write_client(
                sender, operator="ops@petrasoap.com"), session=SESSION)
        self.assertEqual(caught.exception.code, "mc_operator_mismatch")
        self.assertEqual(self.path.get(OPS, iid)["state"], "call_approved")
        self.assertEqual(sender.calls, [])

    def test_mc_answers_map_to_refused_or_unknown_conservatively(self):
        def answer_with(response):
            def fn(method, url, headers, body):
                return response(json.loads(body)["id"])
            return fn

        cases = [
            (lambda i: (403, {}, b'{"error":{"code":"forbidden"}}'), "gate_request_refused"),
            (lambda i: ok_json(tool_result({"error": {"code": "not_found"}}, request_id=i,
                                           is_error=True)), "gate_request_refused"),
            (lambda i: ok_json(tool_result({"error": {"code": "internal"}}, request_id=i,
                                           is_error=True)), "gate_request_unknown"),
            (lambda i: (500, {}, b"{}"), "gate_request_unknown"),
            (lambda i: ok_json(tool_result({"data": {"taskId": "TASK-9", "gateId": "x",
                                                     "status": "pending"}}, request_id=i)),
             "gate_request_unknown"),
        ]
        for response, state in cases:
            with self.subTest(state=state):
                self.setUp()
                iid = self.ready()
                sender = FakeSender(fn=answer_with(response))
                self.path.request_gate(OPS, iid, self.write_client(sender), session=SESSION)
                self.assertEqual(self.path.get(OPS, iid)["state"], state)
                self.assertEqual(len(sender.calls), 1)  # sent once, never retried
                method, url, headers, body = sender.calls[0]
                self.assertEqual((method, url), ("POST", MC + "/api/cursor/mcp"))
                self.assertEqual(json.loads(body)["params"]["name"], "mc_request_approval")

    def test_a_record_bound_to_another_case_breaks_the_chain(self):
        iid = self.authorized()
        db = sqlite3.connect(self.dir / "actions.db")
        self.addCleanup(db.close)
        last = db.execute("SELECT intent_seq, event_hash FROM action_event WHERE intent_id=? "
                          "ORDER BY intent_seq DESC LIMIT 1", (iid,)).fetchone()
        seq, at = last[0] + 1, "2026-10-01T12:00:00.000000+00:00"
        body = ap._canon({"trigger": "x", "binding_sha256": "0" * 64})
        event_hash = ap._sha([iid, seq, "recovery.recorded", "ops", at, body, last[1]])
        db.execute("INSERT INTO action_event(intent_id,intent_seq,kind,actor,at,payload_json,"
                   "prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                   (iid, seq, "recovery.recorded", "ops", at, body, last[1], event_hash))
        db.commit()
        with self.assertRaises(ap.ActionPathError) as caught:
            self.path.get(OPS, iid)
        self.assertEqual(caught.exception.code, "action_event_chain_broken")

    def test_the_only_mc_write_door_is_the_mc_client(self):
        source = Path(g2.__file__).read_text()
        for marker in ("urllib", "HubTransport", ".send(", "check_mc_request"):
            self.assertNotIn(marker, source)
        self.assertIn("call_write_tool", source)


class McFollowUpTests(Goal2World):
    """The first real action type, ``mc.task.create_follow_up``, against the isolated MC:
    the exact ``mc_create_task`` call waits for a person's per-call approval, is sent
    once through the real client's write door and is verified only by an ``mc_get_task``
    read-back. No MC write happens without an approval record, and the intent key never
    creates a second task."""

    def setUp(self) -> None:
        super().setUp()
        self.world.mc.buckets.add(gx.FOLLOW_UP_BUCKET)
        self.writer = self.world.mc.client(self.dir / "mc-create-ledger.jsonl",
                                           write_tools=frozenset({"mc_create_task"}))

    def test_an_approved_follow_up_is_created_once_and_verified_by_read_back(self):
        case_id, iid = self.world.follow_up_intent(1)
        view = self.iso_view(iid)
        key = view["idempotency_key"]
        self.assertEqual((view["state"], view["next_safe_step"]["step"],
                          view["next_safe_step"]["by"]),
                         ("call_approval_required", "approve_call", "human"))
        sent = view["prepared_request"]["arguments"]
        self.assertEqual((view["prepared_request"]["tool"], sent["idempotencyKey"],
                          sent["bucket"], sent["reporter"]),
                         ("mc_create_task", key, gx.FOLLOW_UP_BUCKET, gx.OPERATOR))
        self.assertEqual(self.world.mc.tool_calls, [])   # nothing read or sent so far

        self.world.approve_follow_up(iid)
        first = self.iso.send_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertEqual(first["state"], "dispatched")
        task = self.world.mc.tasks[first["task_id"]]
        self.assertEqual({name: task[name] for name in ("title", "description", "bucket",
                                                        "priority", "reporter")},
                         {name: sent[name] for name in ("title", "description", "bucket",
                                                        "priority", "reporter")})
        checked = self.iso.read_back_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertEqual((checked["state"], checked["outcome"], checked["attribution"],
                          checked["task_id"]),
                         ("verified", "effect_observed", "idempotency_key", first["task_id"]))
        # The same intent key never creates a second task: the path replays without a
        # call, a re-registration is the same intent, and MC's own replay of the key
        # returns the original task.
        replay = self.iso.send_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertEqual((replay["state"], replay["replayed"]), ("verified", True))
        proposal_id = self.iso.get(ACTOR, iid)["intent"]["proposal_id"]
        again = self.iso.register(ACTOR, case_id, proposal_id, session=gx.SESSION)
        self.assertEqual((again["intent_id"], again["idempotency_key"], again["duplicate"]),
                         (iid, key, True))
        echo = self.world.mc._create_task(sent, gx.OPERATOR)
        self.assertEqual((echo["taskId"], echo.get("replayed")), (first["task_id"], True))
        self.assertEqual((self.world.mc.tool_calls.count("mc_create_task"),
                          len(self.world.mc.create_claims)), (1, 1))
        self.assertEqual(self.outcomes(iid), ["not_dispatched", "unknown", "verified"])

    def test_without_an_approval_record_no_mc_write_happens(self):
        """Live configuration: an operator and a bucket, no call approver and an MC client
        that opted in to no write. The intent waits with its exact call shown; nothing a
        caller does sends it."""
        live = ap.ActionPath(self.dir / "live-actions.db", self.world.cases,
                             mc_operator_email=gx.OPERATOR, clock=self.world.clock,
                             mc_follow_up_bucket=gx.FOLLOW_UP_BUCKET)
        self.world.path = live
        _case_id, iid = self.world.follow_up_intent(2)
        view = live.view(ACTOR, iid)
        self.assertEqual(view["state"], "call_approval_required")
        self.assertEqual(view["prepared_request"]["tool"], "mc_create_task")
        reader = self.world.mc.client(self.dir / "live-ledger.jsonl", write_tools=frozenset())
        with self.assertRaisesRegex(ap.ActionPathError, "invalid_state:call_approval_required"):
            live.send_call(ACTOR, iid, reader, session=gx.SESSION)
        with self.assertRaisesRegex(ap.ActionPathError, "call_approvers_not_configured"):
            self.world.approve_follow_up(iid)
        # Approved on a host with approvers, the call still goes nowhere through a client
        # that has not opted in to the write: a certain refusal before any I/O.
        self.world.path = self.iso
        _case_id, other = self.world.follow_up_intent(3)
        self.world.approve_follow_up(other)
        refused = self.iso.send_call(ACTOR, other, reader, session=gx.SESSION)
        self.assertEqual((refused["state"], self.iso_view(other)["next_safe_step"]["step"]),
                         ("dispatch_refused", "prepare_call"))
        self.assertNotIn("mc_create_task", self.world.mc.tool_calls)
        self.assertEqual(self.world.mc.create_claims, {})

    def test_a_lost_answer_is_reconciled_by_reading_never_by_a_second_send(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(gx.main(["follow-up", "--state-dir", str(self.dir / "cli")]), 0)
        report = json.loads(out.getvalue())
        lost = next(item for item in report["scenarios"] if item["scenario"] == "lost_answer")
        self.assertEqual((lost["first_send_state"], lost["state"], lost["mc_create_calls"],
                          lost["recovery_triggers"]),
                         ("dispatched_unknown", "verified", 1, ["dispatch_outcome_unknown"]))
        self.assertEqual((report["outcomes"], report["mc_tool_calls"]["mc_create_task"]),
                         ({"verified": 2}, 2))

    def cited_follow_up(self, number, actor, session, *, proposal_cites=True, notes=None):
        """The console's shape: the gap and its proposal cite owner records (one, or one per
        note of ``notes``)."""
        notes = notes or [f"Supplier wrote: ship date to follow (order {number})."]
        handles = [self.world.owner.seed(gx.RELATION, {"id": number * 100 + index}, {
            "id": number * 100 + index, "note": note}) for index, note in enumerate(notes)]
        path, missing = self.world.path, f"The confirmed ship date of isolated order {number}."
        case_id = path.open_case(actor, missing, objective_author_kind="human", session=session)
        gap = path.execute_internal(actor, "workbench.follow_up.record", case_id, {
            "missing": missing, "why": "Production planning waits on the receipt date.",
            "who_can_answer": ["purchasing"], "state": "unknown", "citations": handles},
            session=session)
        proposal = path.record_proposal(actor, case_id, gx.FOLLOW_UP_CAPABILITY, {
            "gap_id": gap["receipt"]["gap_id"], "title": missing,
            "description": "Production planning waits on the receipt date.",
            "priority": "medium"}, citations=handles if proposal_cites else [],
            session=session)
        return case_id, handles[0] if len(handles) == 1 else handles, proposal["proposal_id"]

    def audience_path(self):
        """The world's action path with an MC audience for its bucket: ``self.audience``
        (bucket -> ``McBucketAudience`` or None), read each time the path asks."""
        self.audience = lambda bucket: None
        path = ap.ActionPath(self.dir / "audience-actions.db", self.world.cases,
                             call_approvers={gx.CALL_APPROVER: gx.CALL_APPROVER_OID},
                             mc_operator_email=gx.OPERATOR, approver_roster=self.world.roster,
                             clock=self.world.clock, mc_follow_up_bucket=gx.FOLLOW_UP_BUCKET,
                             mc_follow_up_audience=lambda bucket: self.audience(bucket))
        self.world.path = self.iso = path
        return path

    def members(self, *actors, bucket=gx.FOLLOW_UP_BUCKET):
        self.audience = lambda _bucket: g2.McBucketAudience(
            bucket, actors, "isolated fixture MC project members")

    def prepared_description(self, number, actor=ACTOR, session=gx.SESSION, **kwargs):
        case_id, handle, proposal_id = self.cited_follow_up(number, actor, session, **kwargs)
        iid = self.iso.register(actor, case_id, proposal_id, session=session)["intent_id"]
        self.iso.prepare_call(actor, iid, session=session)
        return (iid, handle,
                self.iso.view(actor, iid)["prepared_request"]["arguments"]["description"])

    def test_the_follow_up_task_carries_its_cited_source_text_and_citation(self):
        """Approvers see the sources in the MC task when
        every member of the bucket's audience reads them; a case reader who cannot resolve
        the cited record sees none of it."""
        opener = Actor("plx", frozenset({"ops", "finance"}))
        session = fixture_session("plx", opener.principals, channel=CHANNEL_CONSOLE,
                                  subject_email=gx.SUBJECT)
        self.audience_path()
        self.members(ACTOR, opener)
        case_id, handle, proposal_id = self.cited_follow_up(7, opener, session)
        iid = self.iso.register(opener, case_id, proposal_id, session=session)["intent_id"]
        self.iso.prepare_call(opener, iid, session=session)
        quoted = f"[1] {handle}\nid: 700\nnote: Supplier wrote: ship date to follow (order 7)."
        sent = self.iso.view(opener, iid)["prepared_request"]["arguments"]
        self.assertIn(quoted, sent["description"])
        self.assertTrue(sent["description"].startswith(
            "Production planning waits on the receipt date.\n\n" + g2.CITED_SOURCES_HEADING))
        finance = Actor("plx", frozenset({"finance"}))   # reads the case, not the record
        self.assertEqual(self.iso.view(finance, iid),
                         {"case_id": case_id, "proposal_id": proposal_id,
                          "state": "call_approval_required", "withheld": True,
                          "content": ap.WITHHELD})
        self.assertNotIn("Supplier wrote", json.dumps(self.iso.list_for_case(finance, case_id)))

        self.world.approve_follow_up(iid)
        first = self.iso.send_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertIn(quoted, self.world.mc.tasks[first["task_id"]]["description"])
        checked = self.iso.read_back_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertEqual((checked["state"], checked["outcome"]), ("verified", "effect_observed"))

        # The quoted sources are the gap's: a proposal that leaves one out never registers.
        other, _handle, uncited = self.cited_follow_up(8, ACTOR, gx.SESSION,
                                                       proposal_cites=False)
        with self.assertRaisesRegex(ap.ActionPathError,
                                    "record_citations_not_on_proposal:gap_id"):
            self.iso.register(ACTOR, other, uncited, session=gx.SESSION)

    def test_source_text_is_withheld_unless_every_audience_member_reads_it(self):
        """Item 1 authorizes no disclosure beyond read rights.
        One member the citation read refuses, or an audience (or a member's rights) the
        host cannot resolve, sends the citation alone with the reason; fail closed."""
        finance = Actor("plx", frozenset({"finance"}))   # reads the case, not the record
        stranger = Actor("other-tenant", frozenset({"ops"}))   # the case read refuses
        source = "Supplier wrote"
        path = self.audience_path()
        for number, configure, reason in (
                (21, lambda: self.members(ACTOR, finance), g2.WITHHELD_NOT_READABLE),
                (22, lambda: self.members(ACTOR, stranger), g2.WITHHELD_NOT_READABLE),
                (23, lambda: None, g2.WITHHELD_UNRESOLVED),   # no audience known
                (24, lambda: self.members(ACTOR, None), g2.WITHHELD_UNRESOLVED),
                (25, lambda: self.members(ACTOR, bucket="BKT-OTHER"), g2.WITHHELD_UNRESOLVED),
                (26, lambda: setattr(self, "audience", lambda bucket: 1 / 0),
                 g2.WITHHELD_UNRESOLVED)):
            with self.subTest(number=number):
                self.audience = lambda bucket: None
                configure()
                with contextlib.ExitStack() as stack:
                    if number == 26:   # an audience read that fails is logged, then withheld
                        stack.enter_context(self.assertLogs("operating.action_path", "WARNING"))
                    _iid, handle, description = self.prepared_description(number)
                self.assertIn(f"\n\n[1] {handle}\n{reason}\n\n", description)
                self.assertNotIn(source, description)
        # The default host (no audience: the console today) quotes nothing.
        self.world.path = self.iso = self.world.new_path()
        _iid, handle, description = self.prepared_description(27)
        self.assertIn(f"[1] {handle}\n{g2.WITHHELD_UNRESOLVED}", description)
        self.assertNotIn(source, description)

        # A member whose read fails is never skipped: an unexpected error, or an employee
        # gate whose permission provider is down (each outage: Portal provider or snapshot,
        # Entra directory or group membership, a 5xx), leaves the audience unresolved; the
        # gate refusing the member (the same reason with no outage included) is a refusal.
        self.world.path = self.iso = path
        broken = Actor("plx", frozenset({"ops", "broken"}))   # reads the record when asked
        self.members(ACTOR, broken)
        read = self.world.cases.proposal_evidence

        def employee_gate(portal_read):
            return permissions_fixture.resolver(permissions_fixture.Calls(portal_read)).resolve(
                permissions_fixture.EMPLOYEE).require_actor

        def store_down():
            raise RuntimeError("store down")

        outages = [(number, res.resolve(permissions_fixture.EMPLOYEE).require_actor,
                    g2.WITHHELD_UNRESOLVED)
                   for number, res in enumerate(permissions_fixture.outage_resolvers(
                       self.dir).values(), 40)]
        for number, fail, reason in (
                (31, store_down, g2.WITHHELD_UNRESOLVED), *outages,
                (33, employee_gate(None), g2.WITHHELD_NOT_READABLE),   # no Portal row
                (34, employee_gate(permissions_fixture.portal_user(is_active=False)),
                 g2.WITHHELD_NOT_READABLE)):
            def proposal_evidence(actor, *args, fail=fail, **kwargs):
                if actor == broken:
                    fail()
                return read(actor, *args, **kwargs)

            with self.subTest(number=number), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.world.cases, "proposal_evidence",
                                                      proposal_evidence))
                if reason == g2.WITHHELD_UNRESOLVED:
                    stack.enter_context(self.assertLogs("operating.action_path", "WARNING"))
                _iid, handle, description = self.prepared_description(number)
                self.assertIn(f"\n\n[1] {handle}\n{reason}\n\n", description)
                self.assertNotIn(source, description)

        # Rights are checked again at the send: an audience that no longer reads a quoted
        # source sends nothing; a new call quotes it no more.
        self.world.path = self.iso = path
        self.members(ACTOR)
        iid, handle, description = self.prepared_description(28)
        self.assertIn(source, description)
        self.world.approve_follow_up(iid)
        self.members(ACTOR, finance)
        refused = path.send_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertEqual((refused["state"], refused["reason"], refused["mc_writes"]),
                         ("dispatch_refused", "cited_source_audience_changed", 0))
        self.assertEqual(self.world.mc.tasks, {})
        path.prepare_call(ACTOR, iid, session=gx.SESSION)
        again = path.view(ACTOR, iid)["prepared_request"]["arguments"]["description"]
        self.assertIn(f"[1] {handle}\n{g2.WITHHELD_NOT_READABLE}", again)
        self.assertNotIn(source, again)

    def test_twenty_quoted_citations_stay_within_mcs_description_limit(self):
        """20 citations (a proposal's most), each over the per-source cut and with non-BMP
        text: the description stays within MC's 32,000 UTF-16 units, every citation is
        listed once and the cut is stated."""
        self.audience_path()
        self.members(ACTOR)
        notes = [f"{index} " + "\U0001F4E6 box " * 1000 for index in range(20)]
        _iid, handles, description = self.prepared_description(30, notes=notes)
        self.assertLessEqual(g2.mc_length(description), g2.MC_TASK_DESCRIPTION_MAX)
        self.assertGreater(g2.mc_length(description), 30_000)
        self.assertEqual([description.count(f"[{index}] {handle}\n")
                          for index, handle in enumerate(handles, 1)], [1] * 20)
        omitted = description.count("\n[text omitted: description limit]")
        self.assertGreater(omitted, 0)
        # Each cut keeps at most 2,000 UTF-16 units and states the characters it shows.
        shown = re.findall(r"\n\[\d+\] \S+\n(id: \d+\nnote: [^[]*)\n\[cut: (\d+) of (\d+) "
                           r"characters shown;", description)
        self.assertEqual(len(shown), 20 - omitted)
        for head, count, total in shown:
            self.assertEqual(int(count), len(head))
            self.assertIn(g2.mc_length(head), (1999, 2000))   # 1999: a pair kept whole
            self.assertGreater(int(total), int(count))
        self.assertIn(f"the text of {omitted} of 20 sources omitted, 0 of 20 sources not "
                      "listed", description)
        self.assertTrue(description.endswith(
            f"; the case cites every one]\n\nPLX Workbench case "
            f"{self.iso.view(ACTOR, _iid)['case_id']}, action {_iid}, key "
            f"{self.iso.view(ACTOR, _iid)['idempotency_key']}."))

    def test_a_cited_row_that_is_not_unicode_is_withheld_alone(self):
        """A cited row read with a lone surrogate (JSON accepts "\\ud800"; the fixture
        store cannot hold one, so the read returns it) has no UTF-16 length MC could check:
        that one source is withheld with its reason, never a codec error, and the call
        prepares and sends with the other sources quoted."""
        self.audience_path()
        self.members(ACTOR)
        notes = ["Supplier wrote: ship date to follow.", "replaced", "Freight booked."]
        read = self.world.cases.proposal_evidence

        def proposal_evidence(*args, **kwargs):
            return [{**item, "content": {"row": {"id": 3501, "note": "a\ud800b"}}}
                    if item["content"]["row"]["note"] == "replaced" else item
                    for item in read(*args, **kwargs)]

        with mock.patch.object(self.world.cases, "proposal_evidence", proposal_evidence):
            iid, handles, description = self.prepared_description(35, notes=notes)
            self.assertIn(f"[2] {handles[1]}\n{g2.WITHHELD_NOT_TEXT}", description)
            for index in (0, 2):
                self.assertIn(f"[{index + 1}] {handles[index]}\nid: {3500 + index}\n"
                              f"note: {notes[index]}", description)
            self.assertNotIn("\ud800", description)
            prepared = self.iso.get(ACTOR, iid)["prepared"]
            self.assertEqual(prepared["quoted_sources"], [handles[0], handles[2]])
            self.world.approve_follow_up(iid)
            sent = self.iso.send_call(ACTOR, iid, self.writer, session=gx.SESSION)
        self.assertEqual(self.world.mc.tasks[sent["task_id"]]["description"],
                         prepared["arguments"]["description"])

    def test_cited_sources_keep_every_citation_once_bounded_and_fail_closed(self):
        for count in (1, 10, 60):   # cardinality: no citation lost or duplicated
            evidence = [{"handle": f"fixture://fixture.note/{index}", "cited_digest": "d",
                         "current_digest": "d", "content": {"row": {"text": "x" * 5000}}}
                        for index in range(count)]
            withheld = [None if index % 3 else g2.WITHHELD_NOT_READABLE
                        for index in range(count)]
            text = g2.cited_sources(evidence, withheld)
            self.assertEqual([text.count(f"[{index + 1}] {item['handle']}\n")
                              for index, item in enumerate(evidence)], [1] * count)
            self.assertEqual(text.count(g2.WITHHELD_NOT_READABLE), withheld.count(
                g2.WITHHELD_NOT_READABLE))
            self.assertLessEqual(g2.mc_length(text), g2.MC_TASK_DESCRIPTION_MAX)
            self.assertEqual("[cut to stay within" in text, count == 60)
            # A source that is not Unicode text is withheld alone, every other one kept.
            mixed = [{**item, "content": {"row": {"text": "a\udc00"}}} if index % 4 == 0
                     else item for index, item in enumerate(evidence)]
            reasons = g2.audience_withholding(
                mixed, [[{"handle": item["handle"], "current_digest": "d"} for item in mixed]])
            self.assertEqual(reasons, [None if index % 4 else g2.WITHHELD_NOT_TEXT
                                       for index in range(count)])
            mixed_text = g2.cited_sources(mixed, reasons)
            self.assertEqual([mixed_text.count(f"[{index + 1}] {item['handle']}\n")
                              for index, item in enumerate(mixed)], [1] * count)
            self.assertEqual(mixed_text.count(g2.WITHHELD_NOT_TEXT), reasons.count(
                g2.WITHHELD_NOT_TEXT))
            small = g2.cited_sources(evidence, withheld, limit=600)
            self.assertLessEqual(g2.mc_length(small), 600)
            self.assertEqual(f"of {count} sources not listed" in small, count > 1)
        with self.assertRaisesRegex(ValueError, "mc_description_too_long"):
            g2.cited_sources(evidence, withheld, limit=100)
        for changed in ({"content": None}, {"current_digest": "e"}, {"current_digest": None}):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    ValueError, "cited_source_unavailable"):
                g2.cited_sources([{**evidence[0], **changed}], [None])
        # The per-source cut is in UTF-16 units (an astral character is 2) and never splits
        # a surrogate pair; its note counts characters.
        box = "\U0001F4E6"
        for text, head, note in ((box * 1500, box * 1000, "1000 of 1500"),
                                 ("a" + box * 1500, "a" + box * 999, "1000 of 1501"),
                                 ("a" * 2500, "a" * 2000, "2000 of 2500")):
            with self.subTest(text=text[:2]):
                quoted = g2.cited_sources([{"handle": "h", "cited_digest": "d",
                                            "current_digest": "d", "content": {"row": text}}],
                                          [None])
                self.assertIn(f"\n\n[1] h\n{head}\n[cut: {note} characters shown;", quoted)
        # Audience reads: every member must resolve the cited digest.
        item = {"handle": "h", "cited_digest": "d"}
        read = [{"handle": "h", "current_digest": "d"}]
        self.assertEqual(g2.audience_withholding([item], [read, read]), [None])
        for members, reason in (([read, None], g2.WITHHELD_NOT_READABLE),
                                ([read, [{"handle": "h", "current_digest": "e"}]],
                                 g2.WITHHELD_NOT_READABLE),
                                ([read, []], g2.WITHHELD_NOT_READABLE),
                                ([], g2.WITHHELD_UNRESOLVED), (None, g2.WITHHELD_UNRESOLVED)):
            self.assertEqual(g2.audience_withholding([item], members), [reason])


class DefaultApproverRosterTests(Goal2World):
    """The committed default roster names Vince Alton and Tanush Shah as approvers of
    every catalog action type; their Entra oids stay null until read, Tanush's MC role stays null while
    no source states it (he approves per call and decides no gate), and an approver without
    an oid is refused."""

    def roster_copy(self, mutate):
        document = json.loads(g2.DEFAULT_APPROVER_ROSTER.read_text(encoding="utf-8"))
        mutate(document)
        path = self.dir / "roster.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return g2.load_approver_roster(path)

    def test_the_committed_roster_covers_every_catalog_action_type_and_fails_closed(self):
        catalog = ActionCatalog.builtin()
        roster = g2.load_approver_roster()
        self.assertEqual(sorted(roster.by_action), sorted(catalog.ids()))
        tanush = "tanush@petrasoap.com"
        for action_type in catalog.ids():
            self.assertIn("vince@petrasoap.com", roster.for_action(action_type))
            self.assertTrue(roster.mc_roster.may_decide("vince@petrasoap.com", action_type))
            self.assertIn(tanush, roster.for_action(action_type))
            self.assertTrue(roster.mc_roster.approves(tanush, action_type))
            self.assertFalse(roster.mc_roster.may_decide(tanush, action_type))
        document = json.loads(g2.DEFAULT_APPROVER_ROSTER.read_text(encoding="utf-8"))
        self.assertEqual(sorted(document["approvers"]), [tanush, "vince@petrasoap.com"])
        for entry in document["approvers"].values():   # an oid only with where it was read
            self.assertEqual(entry["oid"] is None, entry["oid_source"] is None)
        # Nothing invented for Tanush: no oid, no MC role and no source for either.
        self.assertEqual({k: v for k, v in document["approvers"][tanush].items()
                          if k != "name"},
                         {"mc_role": None, "mc_role_source": None, "oid": None,
                          "oid_source": None, "action_types": g2.ALL_ACTION_TYPES})
        self.assertIsNone(roster.mc_roster.oid_of(tanush))
        self.assertIsNone(roster.mc_roster.role_of(tanush))
        vince = "vince@petrasoap.com"
        for code, mutate in (
                ("roster_approver_source", lambda d: d["approvers"][tanush].update(
                    mc_role="owner")),
                ("roster_approver_source", lambda d: d["approvers"][tanush].update(
                    mc_role_source="a guess")),
                ("roster_approver_source", lambda d: d["approvers"][vince].update(
                    mc_role_source=None)),
                ("roster_oid_without_source", lambda d: d["approvers"][vince].update(
                    oid="0000a000-0000-4000-8000-0000000000aa", oid_source=None)),
                ("roster_action_type_unknown", lambda d: d["approvers"][vince].update(
                    action_types=["mc.task.unknown"])),
                ("roster_approver_keys", lambda d: d["approvers"][vince].update(extra=1)),
                ("roster_duplicate_mailbox", lambda d: d["approvers"].update(
                    {"vince+alias@petrasoap.com": d["approvers"][vince]})),
                ("roster_document_invalid", lambda d: d.update(schema="other/v1"))):
            with self.subTest(code), self.assertRaisesRegex(ValueError, code):
                self.roster_copy(mutate)
        with self.assertRaisesRegex(ValueError, "roster_action_type_unknown"):
            roster.for_action("mc.task.unknown")

    def test_an_approver_without_an_oid_is_refused(self):
        def no_oids(document):
            for entry in document["approvers"].values():
                entry.update(oid=None, oid_source=None)

        roster = self.roster_copy(no_oids)
        self.world.mc.buckets.add(gx.FOLLOW_UP_BUCKET)
        path = ap.ActionPath(self.dir / "roster-actions.db", self.world.cases,
                             mc_operator_email=gx.OPERATOR, approver_roster=roster.mc_roster,
                             clock=self.world.clock, mc_follow_up_bucket=gx.FOLLOW_UP_BUCKET)
        self.world.path = path
        _case_id, iid = self.world.follow_up_intent(9)
        prepared, now = path.get(ACTOR, iid)["prepared"], self.world.clock.now
        for approver in roster.for_action(gx.FOLLOW_UP_CAPABILITY):
            with self.subTest(approver), self.assertRaisesRegex(
                    ap.ActionPathError, "call_approver_oid_unknown"):
                path.approve_call(ACTOR, iid, None, {
                    "schema": ap.CALL_APPROVAL_SCHEMA, "tool": prepared["tool"],
                    "arguments": prepared["arguments"], "approved_by": approver,
                    "approved_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
                    "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
                    "evidence_ref": "isolated fixture per-call approval"}, session=gx.SESSION)
        self.assertEqual(path.view(ACTOR, iid)["state"], "call_approval_required")
        self.assertEqual(self.world.mc.tool_calls, [])


class TrailTests(Goal2World):
    LEAKS = (r"act_[0-9a-f]{24}", r"apg_[0-9a-f]{16}", r"wb-[0-9a-f]{40}", "TASK-", "@",
             "fixture://")

    def cli(self, command, state_dir):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(gx.main([command, "--state-dir", str(state_dir)]), 0)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            gx.main([command, "--state-dir", str(state_dir)])   # a used state dir is refused
        return json.loads(out.getvalue())

    def test_trail_has_all_four_outcomes_and_survives_a_new_instance(self):
        report = self.cli("demo", self.dir / "demo")
        self.assertEqual(report["schema"], gx.TRAIL_SCHEMA)
        self.assertEqual(report["outcomes"], {"failed": 2, "not_dispatched": 1, "verified": 2})
        by_name = {item["scenario"]: item for item in report["scenarios"]}
        self.assertEqual(set(by_name), set(gx.SCENARIOS))
        self.assertEqual({name: item["outcome"] for name, item in by_name.items()},
                         {"happy": "verified", "stale_approval": "not_dispatched",
                          "uncertain_then_verified": "verified",
                          "uncertain_then_failed": "failed", "owner_rejects": "failed"})
        self.assertEqual(by_name["uncertain_then_failed"]["outcome_trail"],
                         ["not_dispatched", "unknown", "failed"])
        self.assertEqual({item["outcome"] for item in report["scenarios"]}
                         | {o for item in report["scenarios"] for o in item["outcome_trail"]},
                         set(ap.OUTCOMES))
        self.assertTrue(all(item["chain_ok"] for item in report["scenarios"]))
        self.assertEqual(report["mc_tool_calls"]["mc_request_approval"], len(gx.SCENARIOS))
        self.assertEqual(report["intents_listed"], len(gx.SCENARIOS))
        self.assertEqual(report["owner_operations"], {"applied": 2, "rejected": 2})
        self.assertEqual((report["business_system_writes"], report["evidence_label"]),
                         (0, "isolated-fixture"))
        # Counts and states only: no case, intent, gate or key ids and no values.
        text = json.dumps(report)
        for leaked in self.LEAKS + ("PO-ISO",):
            self.assertNotRegex(text, leaked)
        for item in report["scenarios"]:
            self.assertNotIn("case_id", item)

    def test_owner_lifecycle_cli_runs_every_scenario_and_prints_counts_only(self):
        report = self.cli("owner-lifecycle", self.dir / "cli")
        by_name = {item["scenario"]: item for item in report["scenarios"]}
        self.assertEqual(set(by_name), set(gx.OWNER_SCENARIOS))
        self.assertEqual({name: (item["state"], item["outcome"])
                          for name, item in by_name.items()}, {
            "verified": ("verified", "verified"),
            "approval_withdrawn": ("gate_not_authorizing", "not_dispatched"),
            "approval_task_restricted": ("gate_not_authorizing", "not_dispatched"),
            "etag_conflict": ("failed", "failed"),
            "type_refused_at_register": ("not_registered", "not_dispatched"),
            "owner_timeout": ("failed", "failed"),
            "correction_pending_then_applied": ("verified", "verified"),
            "partial_failure": ("dispatched_unknown", "unknown"),
            "value_differs": ("dispatched", "unknown")})
        self.assertTrue(by_name["verified"]["owner_replay_same_receipt"])
        self.assertTrue(all(item["replay_owner_calls"] == 0 for item in report["scenarios"]))
        self.assertTrue(all(item["chain_ok"] for item in report["scenarios"]))
        for name in ("approval_withdrawn", "approval_task_restricted",
                     "type_refused_at_register"):
            self.assertEqual(by_name[name]["owner_dispatch_calls"], 0)
        self.assertEqual(by_name["type_refused_at_register"]["mc_requests"], 0)
        self.assertEqual(by_name["type_refused_at_register"]["register_refusal"],
                         "effect_value_type_mismatch:line_count")
        self.assertEqual(by_name["correction_pending_then_applied"]["read_back_reasons"],
                         [None, None])
        self.assertNotIn("effect_contradicted",
                         by_name["correction_pending_then_applied"]["recovery_triggers"])
        self.assertEqual(by_name["approval_task_restricted"]["mc_events"],
                         "mc_task_not_found")
        for name, item in by_name.items():
            self.assertEqual(item["flagged_on_case"], name not in (
                "verified", "correction_pending_then_applied", "type_refused_at_register"),
                name)
        self.assertEqual((report["business_system_writes"], report["network_calls"],
                          report["evidence_label"]), (0, 0, "isolated-fixture"))
        text = json.dumps(report)
        for leaked in self.LEAKS + ("PO-DOC", "case-"):
            self.assertNotRegex(text, leaked)

    def test_the_case_carries_a_visible_audit_trail(self):
        case_id, iid, target, task_id = self.typed_authorized()
        self.owner.faults.append("overwritten_after_apply")
        self.iso_dispatch(iid)
        self.iso_read_back(iid)
        self.world.flag_on_case(case_id, iid, target)
        audit = gx.case_audit_trail(self.world, case_id, {iid: task_id})
        self.assertEqual(audit["timeline"], ["case.opened", "finding.recorded",
                                             "proposal.recorded", "verification.recorded",
                                             "gap.recorded"])
        self.assertEqual(audit["follow_ups"], 1)
        [entry] = audit["intents"]
        self.assertTrue(entry["chain_ok"])
        for kind in ("intent.registered", "gate.prepared", "call.approved",
                     "gate.request_started", "gate.requested", "gate.decision",
                     "dispatch.started", "dispatch.finished", "readback.recorded",
                     "recovery.recorded"):
            self.assertIn(kind, entry["history"])
        self.assertEqual(entry["read_backs"], [{"outcome": "effect_not_observed",
                                                "reason": "value_differs"}])
        self.assertEqual(entry["mc_events"], ["approval.requested", "approval.decided"])
        self.assertEqual(entry["next_safe_step"], "escalate")


class ActionToolVisibilityTests(ActionToolsTestCase):
    """The EXE-04 trail through plx.act mode status (listing and one intent)."""

    def setUp(self) -> None:
        super().setUp()
        self.fx = owner_mod.IsolatedOwnerSystem(self.root / "owner.db")
        self.fixture_handle = self.fx.seed(REL, {"id": 3}, {"id": 3, "po_reference": None})
        self.registry = ap.DispatcherRegistry()
        self.registry.register(ap.FIXTURE_CAPABILITY, ap.FixtureDispatcher(self.fx))

    def test_list_and_get_show_outcome_and_trail_after_an_unknown_reconciled(self):
        ops = self.action_server(OPS_FINANCE)
        actor = Actor("plx", OPS_FINANCE)
        session = fixture_host_session(OPS_FINANCE)   # the host's, as every step needs
        case_id = self.employee_case("Set the PO on SO 3 to PO-7788?")
        iid = self.ok(ops, "plx.act", {
            "mode": "propose", "case_id": case_id,
            "proposal_id": self.fixture_proposal(case_id)})["intent"]["intent_id"]
        intent = self.path.get(actor, iid)["intent"]
        self.path.prepare_gate(actor, iid, "TASK-9", self.mc.client, session=session)
        self.path.approve_call(actor, iid, "TASK-9", {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
            "arguments": {"taskId": "TASK-9", "reason": intent["gate_reason"]},
            "approved_by": VINCE, "approved_at": "2026-10-01T11:00:00Z",
            "expires_at": "2026-10-02T11:00:00Z", "evidence_ref": "source record (test)"},
            session=session)
        self.path.request_gate(actor, iid, self.mc.client, session=session)
        self.mc.decide("apg_1", decided_by=VINCE)
        self.path.record_decision(actor, iid, self.mc.client, session=session)
        self.fx.faults.append("timeout_after_apply")
        self.path.dispatch(actor, iid, self.registry, self.mc.client, session=session)

        unknown = self.ok(ops, "plx.act", {"mode": "status", "intent_id": iid})["intent"]
        self.assertEqual((unknown["outcome"], [i["outcome"] for i in unknown["outcome_trail"]]),
                         ("unknown", ["not_dispatched", "unknown"]))
        self.path.read_back(actor, iid, owner_mod.read_back_for(self.fx), session=session)
        listed = self.ok(ops, "plx.act", {"mode": "status", "case_id": case_id})
        [item] = listed["intents"]
        self.assertEqual((item["state"], item["outcome"], listed["business_system_writes"]),
                         ("verified", "verified", 0))
        self.assertEqual([i["outcome"] for i in item["outcome_trail"]],
                         ["not_dispatched", "unknown", "verified"])
        self.assertEqual(item["read_backs"][-1]["reconciled_by"], "read_back")
        self.assertEqual(item["state_trail"][-2:], [
            {"state": "dispatched_unknown", "seq": item["state_trail"][-2]["seq"],
             "kind": "dispatch.started"},
            {"state": "verified", "seq": item["state_trail"][-1]["seq"],
             "kind": "readback.recorded"}])
        self.assertEqual(self.fx.dispatch_calls, 1)
        # A reader who cannot see the cited record sees the state only, no trail.
        withheld = self.ok(self.action_server(FINANCE), "plx.act",
                           {"mode": "status", "case_id": case_id})
        self.assertNotIn("outcome_trail", withheld["intents"][0])
        self.assertEqual(EFFECT["relation"], REL)


# ---- the approver roster is every dispatching host's one source (WS-11):
# each host builds its ActionPath from load_approver_roster, a null oid stays refused
# at the host, no product module builds approvers by hand, and WS-11 runs in test
# mode (workspace follow-up -> another roster approver -> MC twin -> read-back verified).

DEFAULT_APPROVER = "vince@petrasoap.com"
# A second approver with a read oid, only in this test's copy of the roster.
SECOND_APPROVER = "second.approver@isolated-fixture.example"
SECOND_APPROVER_OID = "0000a000-0000-4000-8000-0000000000b2"
ROSTER_EMPLOYEE = "requesting.employee@isolated-fixture.example"
ROSTER_OPERATOR = "workbench.operator@isolated-fixture.example"
ROSTER_BUCKET = "BKT-ROSTER-WIRING"
ROSTER_OPS = Actor("plx", frozenset({case_fixtures.OPERATIONS}))
ROSTER_SESSION = fixture_session("plx", ROSTER_OPS.principals, channel=CHANNEL_CONSOLE,
                                 subject_email=ROSTER_EMPLOYEE)


def roster_call_record(prepared, approver, clock_now, evidence):
    return {"schema": ap.CALL_APPROVAL_SCHEMA, "tool": prepared["tool"],
            "arguments": prepared["arguments"], "approved_by": approver,
            "approved_at": (clock_now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "expires_at": (clock_now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "evidence_ref": evidence}


class HostRosterTest(unittest.TestCase):
    """Every host serves the roster ``load_approver_roster`` reads, nothing else."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="roster-wiring-"))
        cls.state = cls.tmp / "state"
        cls.seeded = case_fixtures.seed(cls.state, "plx")
        roster = json.loads(g2.DEFAULT_APPROVER_ROSTER.read_text(encoding="utf-8"))
        roster["approvers"][SECOND_APPROVER] = {
            "name": "Second approver (test)", "mc_role": "admin",
            "mc_role_source": "this test", "oid": SECOND_APPROVER_OID, "oid_source": "this test",
            "action_types": "all"}
        cls.roster_file = cls.tmp / "approver_roster.json"
        cls.roster_file.write_text(json.dumps(roster), encoding="utf-8")
        cls.expected = g2.load_approver_roster(cls.roster_file)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in twin_mode.credentialed():
            os.environ.pop(name)
        # The hosts read the default roster path at build time: this test's copy.
        swap = mock.patch.object(g2, "DEFAULT_APPROVER_ROSTER", self.roster_file)
        swap.start()
        self.addCleanup(swap.stop)
        self.built: list[ap.ActionPath] = []
        init = ap.ActionPath.__init__

        def spy(path, *args, **kwargs):
            init(path, *args, **kwargs)
            self.built.append(path)
        patch = mock.patch.object(ap.ActionPath, "__init__", spy)
        patch.start()
        self.addCleanup(patch.stop)

    def assert_served_roster(self, path: ap.ActionPath) -> None:
        # No approver outside the roster: each one counts for the types it lists.
        self.assertEqual(path.call_approvers, frozenset())
        self.assertEqual(path.approver_roster, self.expected.mc_roster)
        self.assertTrue(all(path.approver_roster.approves(email, gx.FOLLOW_UP_CAPABILITY)
                            for email in (DEFAULT_APPROVER, SECOND_APPROVER)))
        self.assertIsNone(path.approver_roster.oid_of(DEFAULT_APPROVER))

    def test_the_console_serves_the_roster_and_refuses_a_null_oid_approver(self):
        checked = {}

        def fake_serve(**served):
            path = served["case_workspace"].action_path
            self.assert_served_roster(path)
            missing = "The supplier's confirmed ship date (roster wiring test)."
            case_id = path.open_case(ROSTER_OPS, missing, objective_author_kind="human",
                                     session=ROSTER_SESSION)
            gap = path.execute_internal(ROSTER_OPS, "workbench.follow_up.record", case_id, {
                "missing": missing, "why": "Planning waits on it.",
                "who_can_answer": ["purchasing"], "state": "unknown"}, session=ROSTER_SESSION)
            proposal = path.record_proposal(ROSTER_OPS, case_id, gx.FOLLOW_UP_CAPABILITY, {
                "gap_id": gap["receipt"]["gap_id"], "title": missing,
                "description": "Planning waits on it.", "priority": "medium"},
                session=ROSTER_SESSION)
            iid = path.register(ROSTER_OPS, case_id, proposal["proposal_id"],
                                session=ROSTER_SESSION)["intent_id"]
            path.prepare_call(ROSTER_OPS, iid, session=ROSTER_SESSION)
            prepared = path.get(ROSTER_OPS, iid)["prepared"]
            now = datetime.now(timezone.utc)
            for approver, code in ((DEFAULT_APPROVER, "call_approver_oid_unknown"),
                                   (ROSTER_EMPLOYEE, "call_approver_is_subject")):
                with self.subTest(approver=approver), \
                        self.assertRaises(ap.ActionPathError) as caught:
                    path.approve_call(ROSTER_OPS, iid, None, roster_call_record(
                        prepared, approver, now, f"test: {approver}"), session=ROSTER_SESSION)
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(path.get(ROSTER_OPS, iid)["state"], "call_approval_required")
            # The roster's approver with a read oid is the one who can approve.
            path.approve_call(ROSTER_OPS, iid, None,
                              roster_call_record(prepared, SECOND_APPROVER, now, "test"),
                              session=ROSTER_SESSION)
            checked["state"] = path.get(ROSTER_OPS, iid)["state"]

        with mock.patch.object(case_console, "serve", fake_serve):
            self.assertEqual(case_console.main([
                "--state-dir", str(self.state), "--fixture",
                "--principal", case_fixtures.OPERATIONS,
                "--action-store", str(self.state / case_fixtures.ACTIONS_FILE),
                "--mc-operator-email", ROSTER_OPERATOR, "--mc-follow-up-bucket", ROSTER_BUCKET],
                fixtures=case_fixtures), 0)
        self.assertEqual(checked, {"state": "call_approved"})
        self.assertEqual(len(self.built), 1)

    def test_the_mcp_host_and_the_live_workspace_serve_the_roster(self):
        host, _server = mcp_server.session_server([
            "--state-dir", str(self.state), "--tenant", "plx", "--fixture",
            "--principal", case_fixtures.OPERATIONS, "--source-mirror-audience",
            str(self.state / case_fixtures.MIRROR_AUDIENCE_FILE), "--enable-action-path"])
        self.addCleanup(host.case_runtime.close)
        self.assertEqual(self.built, [host.action_host])
        with contextlib.redirect_stdout(io.StringIO()):
            case_workspace_live.main([
                "--state-dir", str(self.state), "--mirror-audience",
                str(self.state / case_fixtures.MIRROR_AUDIENCE_FILE),
                "--principal", case_fixtures.OPERATIONS, "--case-id", self.seeded["po"]])
        self.assertEqual(len(self.built), 2)
        for path in self.built:
            self.assert_served_roster(path)

    def test_every_host_reads_the_oid_resolved_at_host_start_through_one_reader(self):
        # The console, the MCP host and the live workspace
        # all take a host-resolved oid from the state directory, with its provenance.
        from integrations.graph_directory import (APPROVER_OIDS_DIR, GraphDirectoryReader,
                                                  resolve_roster)
        from integrations.graph_read import build_graph
        from integrations.tests.test_graph_directory import ENV, RecordedUsers, user
        resolved_oid = "0000a000-0000-4000-8000-0000000000b9"
        graph = RecordedUsers({DEFAULT_APPROVER: user(DEFAULT_APPROVER, resolved_oid)})
        self.addCleanup(shutil.rmtree, self.state / APPROVER_OIDS_DIR, ignore_errors=True)
        summary = resolve_roster(GraphDirectoryReader(build_graph(ENV, sender=graph)[0]),
                                 self.state)
        self.assertEqual((summary["resolved"], summary["state_file"]), (1, "written"))
        with mock.patch.object(case_console, "serve", lambda **served: None):
            self.assertEqual(case_console.main([
                "--state-dir", str(self.state), "--fixture",
                "--principal", case_fixtures.OPERATIONS,
                "--action-store", str(self.state / case_fixtures.ACTIONS_FILE),
                "--mc-operator-email", ROSTER_OPERATOR],
                fixtures=case_fixtures), 0)
        host, _server = mcp_server.session_server([
            "--state-dir", str(self.state), "--tenant", "plx", "--fixture",
            "--principal", case_fixtures.OPERATIONS, "--source-mirror-audience",
            str(self.state / case_fixtures.MIRROR_AUDIENCE_FILE), "--enable-action-path"])
        self.addCleanup(host.case_runtime.close)
        with contextlib.redirect_stdout(io.StringIO()):
            case_workspace_live.main([
                "--state-dir", str(self.state), "--mirror-audience",
                str(self.state / case_fixtures.MIRROR_AUDIENCE_FILE),
                "--principal", case_fixtures.OPERATIONS, "--case-id", self.seeded["po"]])
        self.assertEqual(len(self.built), 3)
        for path in self.built:
            roster = path.approver_roster
            self.assertEqual((roster.oid_of(DEFAULT_APPROVER),
                              roster.oid_provenance(DEFAULT_APPROVER)["source"],
                              roster.oid_of(SECOND_APPROVER)),
                             (resolved_oid, "graph_users", SECOND_APPROVER_OID))

    def test_a_host_refuses_to_start_on_an_invalid_roster(self):
        broken = self.tmp / "broken.json"
        broken.write_text(json.dumps({"schema": "other/v1"}), encoding="utf-8")
        with mock.patch.object(g2, "DEFAULT_APPROVER_ROSTER", broken), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mcp_server.session_server([
                "--state-dir", str(self.state), "--tenant", "plx", "--fixture",
                "--principal", case_fixtures.OPERATIONS, "--source-mirror-audience",
                str(self.state / case_fixtures.MIRROR_AUDIENCE_FILE), "--enable-action-path"])
        self.assertEqual(self.built, [])


class NoHandBuiltRosterTest(unittest.TestCase):
    """Outside tests, an ``ApproverRoster`` with members is built only by
    ``load_approver_roster``, and no call passes ``call_approvers`` / ``approver_roster``
    by keyword (hosts take ``path_options()`` of the loaded roster)."""

    def test_no_product_module_builds_approvers_by_hand(self):
        files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.split()
        self.assertGreater(len(files), 100)
        found = []
        for name in files:
            parts = Path(name).parts
            if "tests" in parts or parts[-1].startswith("test_"):
                continue
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            loaders = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                       and node.name == "load_approver_roster"
                       and name == "operating/goal2_mc_approval.py"]
            allowed = {id(inner) for loader in loaders for inner in ast.walk(loader)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or id(node) in allowed:
                    continue
                func = node.func
                callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if callee == "ApproverRoster" and (node.args or node.keywords):
                    found.append(f"{name}:{node.lineno} ApproverRoster(...)")
                found += [f"{name}:{node.lineno} {kw.arg}=" for kw in node.keywords
                          if kw.arg in ("call_approvers", "approver_roster")]
        self.assertEqual(found, [])


class FollowUpOnTheTwinTest(unittest.TestCase):
    """WS-11 in test mode: workspace follow-up -> another roster approver -> MC twin ->
    read-back verified."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in twin_mode.credentialed():
            os.environ.pop(name)
        os.environ.pop(action_adapters.ALLOW_ENV, None)
        context = twin_mode.test_mode()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def test_an_employee_prepares_another_roster_approver_approves_the_twin_verifies(self):
        world = TwinWorld(self.dir / "world")
        order = PortalTwin.citation(ORDER_RELATION, "so-twin-1")
        workspace = objects.ObjectWorkspace(
            stub_tools(order), gx.ACTOR,
            follow_ups=objects.FollowUps(world.path, gx.SESSION),
            actions=objects.WorkspaceActions(world.path, gx.SESSION,
                                             actors=gx.ACTOR.principals, mc=world.mc_create))
        missing = "The supplier's confirmed ship date for order SO-T0001."
        proposed = workspace.follow_up({"object_id": WS_OBJECT, "missing": missing,
                                        "why": "Planning waits on it.",
                                        "who_can_answer": ["purchasing"]})["proposal"]
        iid = proposed["intent_id"]
        self.assertEqual((proposed["state"], proposed["cited"], proposed["executed"]),
                         ("call_approval_required", [order], False))
        step = lambda name: workspace.act({"intent_id": iid, "step": name})  # noqa: E731
        self.assertEqual(step("send_call")["refusal"], "invalid_state:call_approval_required")
        # The approvers are the roster's; the employee who prepared it is never one.
        approvers = world.approvers.for_action(gx.FOLLOW_UP_CAPABILITY)
        self.assertEqual(approvers, g2.load_approver_roster(
            gx.ISOLATED_APPROVER_ROSTER).for_action(gx.FOLLOW_UP_CAPABILITY))
        approver = sorted(approvers)[0]
        self.assertNotEqual(approver, gx.SUBJECT)
        prepared = world.path.get(gx.ACTOR, iid)["prepared"]
        with self.assertRaises(ap.ActionPathError) as caught:
            world.path.approve_call(gx.ACTOR, iid, None, roster_call_record(
                prepared, gx.SUBJECT, world.clock.now, "test: self-approval"),
                session=gx.SESSION)
        self.assertEqual(caught.exception.code, "call_approver_is_subject")
        world.path.approve_call(gx.ACTOR, iid, None, roster_call_record(
            prepared, approver, world.clock.now, "test: the roster approver"),
            session=gx.SESSION)
        sent = step("send_call")
        self.assertEqual((sent["state"], sent["control"]["step"]),
                         ("dispatched", "read_back_call"))
        self.assertTrue(step("send_call")["result"]["replayed"])
        verified = step("read_back_call")
        self.assertEqual((verified["state"], verified["outcome"], verified["done"]),
                         ("verified", "verified", True))
        created = world.mc.created_tasks(missing)
        self.assertEqual(len(created), 1)
        self.assertEqual((world.mc.tasks[created[0]]["bucket"],
                          world.mc.tool_calls.count("mc_create_task")),
                         (gx.FOLLOW_UP_BUCKET, 1))


class AnswererTests(Goal2World):
    """gap-follow-ups-one-path (LOG-07, EXE-07): every who_can_answer goal 2 and the action
    path write is a Portal Department code (seed-rules.ts@48d76e835:19-38 ROUTING_DEPARTMENTS,
    read here through employee_permissions' own copy) or a typed ``undeclared:`` label, from
    the one resolver (operating/answerers.py)."""

    def routed(self, who):
        from operating.employee_permissions import PORTAL_DEPARTMENT_CODES
        for label in who:
            self.assertTrue(label in set(PORTAL_DEPARTMENT_CODES.values())
                            or (label.startswith("undeclared:") and len(label) > 11), label)

    def test_follow_ups_and_the_mc_escalation_name_a_department_or_an_undeclared_owner(self):
        from twin.world import ACTOR as TWIN_ACTOR, ESCALATION_PREFIX, PORTAL_CAPABILITY
        from twin.world import run_portal_scenario
        # A supplier confirmation is the evidence of a Procurement stage.
        case_id, _iid = self.world.follow_up_intent(1)
        [gap] = self.world.cases.get(ACTOR, case_id)["gaps"]
        self.assertEqual(gap["who_can_answer"], ["PROC"])
        # An isolated owner's relation no object type binds: typed undeclared, naming it.
        case_id, iid, target, _task = self.typed_authorized()
        self.world.flag_on_case(case_id, iid, target)
        [gap] = self.world.cases.get(ACTOR, case_id)["gaps"]
        self.assertEqual(gap["who_can_answer"],
                         [f"undeclared:fixture owner of {gx.TYPED_RELATION}"])
        # The MC escalation of a Portal sales-order outcome the owner refused (412) routes to
        # the department of the order's stage, Sales.
        with mock.patch.dict(os.environ):
            for name in twin_mode.credentialed():
                os.environ.pop(name, None)
            os.environ.pop(action_adapters.ALLOW_ENV, None)
            with twin_mode.test_mode(allow={PORTAL_CAPABILITY}):
                twin = TwinWorld(self.dir / "twin")
                self.assertEqual(run_portal_scenario(twin, "etag_mismatch")["escalation_tasks"], 1)
                [escalation] = [g for g in twin.cases.get(TWIN_ACTOR, twin.case_ids[-1])["gaps"]
                                if g["missing"].startswith(ESCALATION_PREFIX)]
        self.assertEqual(escalation["who_can_answer"], ["SALES"])
        for who in (gap["who_can_answer"], escalation["who_can_answer"]):
            self.routed(who)
        # Process graphs that do not load declare no department: undeclared, never a guess.
        from operating import ontology_tools_process as process
        broken = self.dir / "graphs"
        broken.mkdir()
        shutil.copy(process.FORMAT_PATH, broken / "_process.schema.json")
        (broken / "purchase_to_invoice.json").write_text("{", encoding="utf-8")
        with mock.patch.object(process, "PROCESS_DIR", broken):
            case_id, _iid = self.world.follow_up_intent(2)
        [gap] = self.world.cases.get(ACTOR, case_id)["gaps"]
        self.assertEqual(gap["who_can_answer"], ["undeclared:supplier"])


if __name__ == "__main__":
    unittest.main()
