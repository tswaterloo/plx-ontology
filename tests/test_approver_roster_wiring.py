"""One approver rule: the roster's ``action_types`` scope a per-call approver and an MC
gate decider alike.

``operating/approver_roster.json`` names, per person, the catalog action types they
approve. A gate decided by an MC admin/owner whom the roster does not list for the
intent's action type never authorizes, and a decider whose types change before the
dispatch withdraws the approval; an empty list approves nothing, per call or as a
decider; a type-scoped approver approves its types per call through the same
``path_options`` every host takes. An invalid roster stops the read-only live workspace
with a typed error, as it stops the console and the MCP host. Isolated fixture world
(``operating.goal2_exec.Lifecycle``): no network, no credentials.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from operating import action_path as ap
from operating import case_workspace_live
from operating import goal2_exec as gx
from operating import goal2_mc_approval as g2
from operating.action_catalog import ActionCatalog
from operator_ui.tests import case_fixtures
from twin import mode as twin_mode

FIXTURE = ap.FIXTURE_CAPABILITY          # the isolated owner's action type
FOLLOW_UP = gx.FOLLOW_UP_CAPABILITY      # the MC follow-up action type


class RosterWorld(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.world = gx.Lifecycle(self.dir / "world")
        self.tasks = 0

    def roster(self, **types: list[str] | str) -> g2.ActionApprovers:
        """The isolated roster with ``types`` replacing the named approvers' action types
        (``call_approver``, ``mc_admin``), read by the one loader and served to a fresh
        path through ``path_options``, as every host takes it."""
        document = json.loads(gx.ISOLATED_APPROVER_ROSTER.read_text(encoding="utf-8"))
        names = {"call_approver": gx.CALL_APPROVER, "mc_admin": gx.MC_ADMIN}
        for name, value in types.items():
            document["approvers"][names[name]]["action_types"] = value
        path = self.dir / f"roster-{len(list(self.dir.glob('roster-*')))}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.world.read_roster(path)
        self.world.path = self.world.new_path()
        return self.world.approvers

    def decided(self, number: int) -> tuple[str, dict]:
        """An owner intent whose gate the MC admin approved in MC, as recorded."""
        _case_id, iid = self.world.open_case_and_register(number)
        self.tasks += 1
        self.world.authorize(iid, f"TASK-ISO-{self.tasks}")
        return iid, self.world.path.get(gx.ACTOR, iid)

    def call_record(self, iid: str, approver: str, task_id: str | None) -> dict:
        prepared = self.world.path.get(gx.ACTOR, iid)
        arguments = (prepared["prepared"]["arguments"] if task_id is None else
                     {"taskId": task_id, "reason": prepared["intent"]["gate_reason"]})
        now = self.world.clock.now
        return {"schema": ap.CALL_APPROVAL_SCHEMA,
                "tool": prepared["prepared"]["tool"] if task_id is None
                else "mc_request_approval",
                "arguments": arguments, "approved_by": approver,
                "approved_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
                "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
                "evidence_ref": f"test approval by {approver} #{self.tasks}"}

    def refused_call(self, iid: str, approver: str, task_id: str | None) -> str:
        with self.assertRaises(ap.ActionPathError) as caught:
            self.world.path.approve_call(gx.ACTOR, iid, task_id,
                                         self.call_record(iid, approver, task_id),
                                         session=gx.SESSION)
        return caught.exception.code


class DeciderScopeTests(RosterWorld):
    def test_a_decider_counts_only_for_the_action_types_the_roster_lists(self):
        self.roster(mc_admin=[FOLLOW_UP])
        iid, state = self.decided(1)
        self.assertEqual((state["state"], state["decisions"][-1]["reason_code"]),
                         ("gate_not_authorizing", "decider_not_approver_of_action_type"))
        self.assertFalse(state["decisions"][-1]["authorizes"])
        # The control: the same admin listed for the owner's type authorizes.
        self.roster(mc_admin=[FIXTURE])
        iid, state = self.decided(2)
        self.assertEqual((state["state"], state["decisions"][-1]["reason_code"]),
                         ("gate_authorized", "authorized"))
        # A roster that drops the type before the dispatch withdraws the approval: the
        # dispatch re-reads MC and re-checks the decider by the same rule.
        self.roster(mc_admin=[FOLLOW_UP])
        result = self.world.path.dispatch(gx.ACTOR, iid, self.world.registry,
                                          self.world.mc_client, session=gx.SESSION)
        self.assertEqual((result["state"], result["reason"]), (
            "gate_not_authorizing",
            "approval_withdrawn:decider_no_longer_approver_of_action_type"))
        self.assertEqual(self.world.owner.dispatch_calls, 0)

    def test_an_empty_list_approves_nothing_per_call_or_as_a_decider(self):
        approvers = self.roster(mc_admin=[])
        catalog = ActionCatalog.builtin().ids()
        self.assertEqual([t for t in catalog if gx.MC_ADMIN in approvers.for_action(t)], [])
        self.assertEqual([t for t in catalog if approvers.mc_roster.approves(gx.MC_ADMIN, t)
                          or approvers.mc_roster.may_decide(gx.MC_ADMIN, t)], [])
        # Its MC role and oid stay known (MC itself lets an admin decide), yet the
        # decision it makes never authorizes, and it approves no call.
        self.assertEqual((approvers.mc_roster.role_of(gx.MC_ADMIN),
                          approvers.mc_roster.oid_of(gx.MC_ADMIN)), ("admin", gx.MC_ADMIN_OID))
        _iid, state = self.decided(1)
        self.assertEqual(state["decisions"][-1]["reason_code"],
                         "decider_not_approver_of_action_type")
        _case_id, iid = self.world.follow_up_intent(2)
        self.assertEqual(self.refused_call(iid, gx.MC_ADMIN, None), "call_approver_not_allowed")
        self.assertEqual(self.world.path.get(gx.ACTOR, iid)["state"], "call_approval_required")
        self.assertNotIn("mc_create_task", self.world.mc.tool_calls)


class CallApproverScopeTests(RosterWorld):
    def test_a_type_scoped_approver_approves_its_types_and_nothing_else(self):
        approvers = self.roster(call_approver=[FOLLOW_UP])
        # Every host takes the roster whole: no per-host intersection drops the approver.
        self.assertEqual(approvers.path_options()["approver_roster"], approvers.mc_roster)
        self.assertEqual(self.world.path.call_approvers, frozenset())
        _case_id, follow_up = self.world.follow_up_intent(1)
        self.world.approve_follow_up(follow_up)
        self.assertEqual(self.world.path.get(gx.ACTOR, follow_up)["state"], "call_approved")
        # Another action type: refused before anything is sent to MC.
        _case_id, owner_intent = self.world.open_case_and_register(2)
        self.world.mc.add_task("TASK-ISO-9", "Isolated goal-2 lifecycle")
        self.world.path.prepare_gate(gx.ACTOR, owner_intent, "TASK-ISO-9",
                                     self.world.mc_client, session=gx.SESSION)
        self.assertEqual(self.refused_call(owner_intent, gx.CALL_APPROVER, "TASK-ISO-9"),
                         "call_approver_not_allowed")
        self.assertEqual(self.world.path.get(gx.ACTOR, owner_intent)["state"], "gate_prepared")
        self.assertNotIn("mc_request_approval", self.world.mc.tool_calls)


class LiveWorkspaceRosterTest(unittest.TestCase):
    """The read-only live workspace refuses an invalid roster with a typed parser error
    (exit 2) and no traceback, whatever is wrong with it."""

    def test_an_invalid_roster_is_a_typed_error_not_a_traceback(self):
        tmp = Path(tempfile.mkdtemp(prefix="roster-live-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in twin_mode.credentialed():
            os.environ.pop(name)
        state = tmp / "state"
        seeded = case_fixtures.seed(state, "plx")
        self.assertTrue((state / "actions.sqlite").exists())
        document = json.loads(g2.DEFAULT_APPROVER_ROSTER.read_text(encoding="utf-8"))
        vince = next(iter(document["approvers"]))
        for code, broken in (
                ("roster_document_invalid", {"schema": "other/v1"}),
                ("roster_action_type_unknown", {**document, "approvers": {vince: {
                    **document["approvers"][vince], "action_types": [{"type": "all"}]}}}),
                ("roster role must be member, admin or owner", {**document, "approvers": {
                    vince: {**document["approvers"][vince], "mc_role": ["owner"]}}}),
                ("Expecting property name", None)):
            with self.subTest(code):
                path = tmp / "roster.json"
                path.write_text("{" if broken is None else json.dumps(broken), "utf-8")
                errors = io.StringIO()
                with mock.patch.object(g2, "DEFAULT_APPROVER_ROSTER", path), \
                        contextlib.redirect_stderr(errors), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaises(SystemExit) as stopped:
                    case_workspace_live.main([
                        "--state-dir", str(state), "--mirror-audience",
                        str(state / case_fixtures.MIRROR_AUDIENCE_FILE),
                        "--principal", case_fixtures.OPERATIONS, "--case-id", seeded["po"]])
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn(f"approver roster is invalid: {code}", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
