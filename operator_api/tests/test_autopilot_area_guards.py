"""Guards on the autopilot area's write routes (``operator_api.autopilot_area``): the owner's
view-as is read only on every write route, a view-as or read-ahead session switches nobody's
autopilot even when handed to the backend directly, a person's opt-in is theirs alone at the
API too, malformed bodies are refused with no row written, a failed store write is an error
(never a quiet success), and a priced order stays prepare-only through the area's accept.

SYNTHETIC queue, gate files and people; fake transports only, no network. Evidence label:
injected-transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import autopilot as ap
from operating import autopilot_write as aw
from operating import card_actions
from operating import employee_permissions as ep
from operating import session_identity as si
from operator_api import models as m
from operator_api import view_as as va
from operator_api.app import create_app
from operator_api.auth import DevAuth, Reader
from operator_api.errors import UserFacing
from operator_api.tests import test_autopilot_area as base
from operating.tests.test_autopilot_write import po_item
from operator_api.tests.test_autopilot_area import COLLEAGUE, ENV, FLOW, OWNER, ROLE, queued

TID = "11111111-2222-4333-8444-555555555555"
SAM_ROLE = f"person:{COLLEAGUE[0]}"
SAM_ONLY = "supplier_eta_receiving"


def sam_session(proof: str, exp: int | None = None) -> si.SessionIdentity:
    """Sam resolved again in-process: as the owner's view-as, or as the app's read-ahead."""
    principal = ep.EmployeePrincipal(TID, COLLEAGUE[1])
    exp = exp if exp is not None else int(datetime.now(timezone.utc).timestamp()) + 600
    return si.SessionIdentity(tenant="plx", channel=si.CHANNEL_CONSOLE,
                              author_kind=si.AUTHOR_HUMAN, proof=proof, employee=principal,
                              principals=frozenset({principal.principal}), expires_at=exp,
                              identities=frozenset({COLLEAGUE[1], COLLEAGUE[0]}))


class Guards(unittest.TestCase):
    # the existing area fixture, borrowed (not inherited, so its tests do not run twice)
    setUp = base.AutopilotArea.setUp
    client = base.AutopilotArea.client
    score_fresh = base.AutopilotArea.score_fresh

    def both_pass(self) -> None:
        self.score_fresh({ROLE: ([OWNER[1]], {FLOW: (16, 15)}),
                          SAM_ROLE: ([COLLEAGUE[1]], {FLOW: (16, 15)})})

    def journal(self) -> aw.Journal:
        return aw.Journal(self.state / aw.AUDIT_FILE)

    def opt_in_rows(self) -> list[dict]:
        return [r for r in self.journal().rows() if r.get("event") in aw.OPT_IN_EVENTS]

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.state.parent)): p.read_bytes()
                for p in sorted(self.state.parent.rglob("*")) if p.is_file()}


class ViewAsIsReadOnly(Guards):
    def setUp(self) -> None:
        super().setUp()
        audit = tempfile.TemporaryDirectory()
        self.addCleanup(audit.cleanup)
        self.backend._is_owner = lambda r: r.session.subject_oid == OWNER[1]
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(COLLEAGUE[1], COLLEAGUE[0])],
            excluded=lambda: (lambda oid: False),
            identity=lambda oid, exp: sam_session(si.PROOF_VIEW_AS, exp),
            audit=va.Audit(Path(audit.name)))

    def test_every_write_route_refuses_the_owner_viewing_as_someone_and_changes_nothing(self):
        self.both_pass()
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        owner = self.client(OWNER)
        as_sam = {va.HEADER: va.key_of(COLLEAGUE[1])}
        # control: the header is a good one, a read as Sam is served as Sam
        seen = owner.get("/api/autopilot", headers=as_sam)
        self.assertEqual(seen.status_code, 200, seen.text)
        self.assertEqual([i["item_id"] for i in seen.json()["items"]], ["ap_sam"])
        before = self.snapshot()
        writes = (
            ("post", "/api/autopilot/opt-in", {"workflow": FLOW, "on": True}),
            ("post", "/api/autopilot/pause", {"paused": True}),
            ("post", "/api/autopilot/switches", {"global_off": True}),
            ("put", f"/api/autopilot/workflows/{FLOW}/mode", {"mode": "off"}),
            ("post", "/api/autopilot/workflows", {}),
            ("post", f"/api/autopilot/workflows/{FLOW}/replay", {"days": 7}),
            ("post", "/api/autopilot/audit/k/undo", None),
            ("post", "/api/autopilot/items/ap_sam/decision", {"decision": "accept"}),
            ("post", "/api/autopilot/items/ap_own/decision", {"decision": "accept"}),
        )
        for method, path, body in writes:
            with self.subTest(route=f"{method.upper()} {path}"):
                got = getattr(owner, method)(path, json=body, headers=as_sam)
                self.assertEqual(got.status_code, 403, got.text)
                self.assertEqual(got.json()["error"]["code"], "view_as_refused")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.opt_in_rows(), [])
        self.assertFalse((self.state / aw.KILL_FILE).exists())
        self.assertFalse((self.state / aw.WORKFLOW_KILL_FILE).exists())
        self.assertEqual(self.bc.calls, [])


class SessionKinds(Guards):
    def test_a_view_as_or_read_ahead_session_switches_nothing_at_the_backend(self):
        self.both_pass()
        for proof in (si.PROOF_VIEW_AS, si.PROOF_WARM):
            reader = Reader(sam_session(proof), "mock", "Sam", COLLEAGUE[0])
            with self.subTest(proof=proof):
                with self.assertRaises(UserFacing) as on:
                    self.backend.autopilot_opt_in(
                        reader, m.AutopilotOptInRequest(workflow=FLOW, on=True))
                self.assertEqual(on.exception.status, 403)
                with self.assertRaises(UserFacing) as paused:
                    self.backend.autopilot_pause(reader, m.AutopilotPauseRequest(paused=True))
                self.assertEqual(paused.exception.status, 403)
        self.assertEqual(self.opt_in_rows(), [])
        # control: Sam signed in as themselves switches their own on
        got = self.client(COLLEAGUE).post("/api/autopilot/opt-in",
                                          json={"workflow": FLOW, "on": True})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual([(r["person"], r["on"]) for r in self.opt_in_rows()],
                         [(COLLEAGUE[1], True)])

    def test_a_session_naming_nobody_pauses_nobody(self):
        self.both_pass()
        nobody = self.client(("nobody@plx.example", None))
        self.assertEqual(nobody.post("/api/autopilot/pause",
                                     json={"paused": True}).status_code, 403)
        self.assertEqual(self.opt_in_rows(), [])


class PerPerson(Guards):
    def test_anothers_opt_in_neither_shows_as_mine_nor_makes_my_accept_live(self):
        # Sam also passed a workflow the owner never did
        self.score_fresh({ROLE: ([OWNER[1]], {FLOW: (16, 15)}),
                          SAM_ROLE: ([COLLEAGUE[1]], {FLOW: (16, 15), SAM_ONLY: (40, 38)})})
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        base.AutopilotArea.go_live_file(self, "bc")  # owner go-live file: only the person switch left
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        for flow in (FLOW, SAM_ONLY):
            on = sam.post("/api/autopilot/opt-in", json={"workflow": flow, "on": True})
            self.assertEqual(on.status_code, 200, on.text)
        self.assertEqual([(o["workflow"], o["on"]) for o in on.json()["my_opt_ins"]],
                         [(FLOW, True), (SAM_ONLY, True)])
        mine = owner.get("/api/autopilot").json()["my_opt_ins"]
        self.assertEqual([(o["workflow"], o["eligible"], o["on"]) for o in mine],
                         [(FLOW, True, False)])
        got = owner.post("/api/autopilot/items/ap_own/decision", json={"decision": "accept"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertFalse(got.json().get("confirm_token"))      # never offered a live write
        self.assertNotEqual((got.json()["decision"] or {}).get("outcome"), "done")
        self.assertEqual(self.bc.calls, [])
        # Sam pausing pauses Sam only: the owner's own switch stays as the owner left it
        self.assertEqual(owner.post("/api/autopilot/opt-in", json={
            "workflow": FLOW, "on": True}).status_code, 200)
        self.assertEqual(sam.post("/api/autopilot/pause", json={"paused": True}).status_code,
                         200)
        self.assertEqual(aw.paused(self.journal().rows()).keys(), {COLLEAGUE[1]})
        got = owner.post("/api/autopilot/items/ap_own/decision", json={"decision": "accept"})
        self.assertTrue(got.json().get("confirm_token"), got.json())


class MalformedBodies(Guards):
    def test_each_malformed_body_is_refused_with_its_code_and_writes_nothing(self):
        self.both_pass()
        sam = self.client(COLLEAGUE)
        cases = (
            ("/api/autopilot/pause", {}, 422),
            ("/api/autopilot/pause", {"paused": True, "person": OWNER[1]}, 422),
            ("/api/autopilot/pause", {"paused": None}, 422),
            ("/api/autopilot/pause", {"paused": "true"}, 422),
            ("/api/autopilot/pause", [True], 422),
            ("/api/autopilot/opt-in", {"workflow": None, "on": True}, 422),
            ("/api/autopilot/opt-in", {"workflow": FLOW, "on": None}, 422),
            ("/api/autopilot/opt-in", {"workflow": FLOW, "on": 1}, 422),
            ("/api/autopilot/opt-in", {"workflow": "x" * 121, "on": False}, 422),
            ("/api/autopilot/opt-in", {"workflow": FLOW, "on": True, "by": OWNER[1]}, 422),
            ("/api/autopilot/opt-in", {"workflow": "", "on": False}, 404),
            ("/api/autopilot/opt-in", {"workflow": " ", "on": False}, 404),
        )
        for path, body, code in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(sam.post(path, json=body).status_code, code)
        for path in ("/api/autopilot/pause", "/api/autopilot/opt-in"):
            with self.subTest(path=path, body="not json"):
                got = sam.post(path, content=b"{paused: yes",
                               headers={"Content-Type": "application/json"})
                self.assertEqual(got.status_code, 422)
        self.assertEqual(self.opt_in_rows(), [])


class StoreFailure(Guards):
    def test_a_failed_store_write_is_an_error_and_records_nothing(self):
        self.both_pass()
        session = si.fixture_session("plx", ["staff:demo"], channel=si.CHANNEL_CONSOLE,
                                     subject_email=COLLEAGUE[0], subject_oid=COLLEAGUE[1])
        sam = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                    secure_cookie=False), base_url="http://testserver",
                         raise_server_exceptions=False)
        self.assertEqual(sam.post("/api/auth/dev-login", json={}).status_code, 200)
        with mock.patch.object(card_actions.Journal, "_append",
                               side_effect=OSError("disk full")):
            for path, body in (("/api/autopilot/opt-in", {"workflow": FLOW, "on": True}),
                               ("/api/autopilot/pause", {"paused": True})):
                with self.subTest(path=path), \
                        self.assertLogs("operator_api", "ERROR") as logged:
                    got = sam.post(path, json=body)
                    self.assertEqual(got.status_code, 500, got.text)
                    self.assertIn("OSError", logged.output[0])
        self.assertEqual(self.opt_in_rows(), [])
        mine = sam.get("/api/autopilot").json()["my_opt_ins"]
        self.assertEqual([(o["workflow"], o["on"]) for o in mine], [(FLOW, False)])


class Money(Guards):
    def test_a_priced_order_stays_prepare_only_through_a_live_accept(self):
        self.score_fresh({ROLE: ([OWNER[1]], {FLOW: (16, 15)})})
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        # the queue's own order, with a price on its line
        priced = {**queued("ap_priced", OWNER[1]), "body": po_item(price=True)["body"]}
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(priced)
        q.close()
        owner = self.client(OWNER)
        self.assertEqual(owner.post("/api/autopilot/opt-in", json={
            "workflow": FLOW, "on": True}).status_code, 200)
        got = owner.post("/api/autopilot/items/ap_priced/decision", json={"decision": "accept"})
        self.assertEqual(got.status_code, 200, got.text)
        token = got.json().get("confirm_token")
        if token:
            got = owner.post("/api/autopilot/items/ap_priced/decision",
                             json={"decision": "accept", "confirm_token": token})
        self.assertNotEqual((got.json()["decision"] or {}).get("outcome"), "done", got.json())
        self.assertEqual(self.bc.calls, [])


if __name__ == "__main__":
    unittest.main()
