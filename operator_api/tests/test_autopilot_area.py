"""The autopilot area (``/api/autopilot*``) through the app's routes: a reader sees only
the items they may see, only the owner changes switches or reads the audit log, an accept
is a recorded dry run until the workflow passed its fresh gate and the owner switched it
live, a live accept goes through the one gated write path (a fake BC transport), and undo
performs the recorded inverse once.

SYNTHETIC queue, gate files and people; fake transports only. Evidence label:
``injected-transport``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from urllib.parse import quote, unquote

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_queue
from operating import autopilot_engine
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.autopilot_records import object_href
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_autopilot_write import (
    BC_COMPANY, NEW_ID, Transport, admit_judged, po_item)
from operator_api import autopilot_area as ws
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
COLLEAGUE = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")
ROLE = f"person:{OWNER[0]}"
FLOW = "customer_order_intake"
ENV = {"PLX_AUTOPILOT_BC_COMPANY_ID": BC_COMPANY}


def queued(item_id: str, owner: str, *, markings=(), created: str | None = None) -> dict:
    found = po_item()
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {**found, "item_id": item_id, "event_id": "e-" + item_id, "owner": owner,
            "audiences": ["mail:" + owner], "markings": list(markings),
            "due_at": created, "created_at": created}


def switched(rows) -> dict:
    """person -> the workflows prepared for them by their own switch alone (no Draft)."""
    return {p: frozenset(f) for p, f in aw.prepared_for(rows, {}).items()}


def steps(decision: dict) -> list[tuple[str, str, str]]:
    return [(s["kind"], s["system"], s["status"]) for s in decision["steps"]]


class AutopilotArea(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state = root / "autopilot-write"
        self.state.mkdir()
        path = root / "queue" / ap.QUEUE_FILE
        q = ap.Queue(path)
        with q.conn:
            q.add_item(queued("ap_own", OWNER[1]))
            q.add_item(queued("ap_fin", OWNER[1], markings=["finance"]))
            q.add_item(queued("ap_sam", COLLEAGUE[1]))
        q.close()
        # dev scores beside the queue; the colleague's role is not one of the owner's
        (path.parent / autopilot_engine.GATE_FILE).write_text(json.dumps({"schema": autopilot_engine.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER[1]], "workflows": {FLOW: {"judged": 40, "matched": 37}}},
            "receivables": {"members": [COLLEAGUE[1]],
                            "workflows": {"ar_collections": {"judged": 20, "matched": 15}}}}}))
        self.bc = Transport()
        guard = PeopleExclusion(["99999999-9999-4999-8999-999999999999"])
        guard.attach_directory([(OWNER[1], OWNER[0], []), (COLLEAGUE[1], COLLEAGUE[0], [])])
        b = self.backend = MockBackend()
        b.writes_environ = dict(ENV)
        b.autopilot_queue = path
        b.autopilot_write_state = self.state
        b.autopilot_grant = (lambda aud: True, frozenset(), lambda hs: {h: None for h in hs})
        b.autopilot_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        b.autopilot_guard = guard
        b.autopilot_adapters = {"bc_write": wa.BCAdapter(self.bc, environ=ENV)}

    def client(self, who) -> TestClient:
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def pass_fresh_gate(self) -> None:
        run = self.state.parent / "fresh-run"
        (run / "slug").mkdir(parents=True)
        (run / autopilot_engine.GATE_FILE).write_text(json.dumps({"schema": autopilot_engine.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER[1]], "workflows": {FLOW: {"judged": 16, "matched": 15}}}}}))
        (run / "slug" / "result.json").write_text(json.dumps(
            {"role": ROLE, "status": "complete",
             "window": ["2026-11-06T07:00:00Z", "2026-11-10T07:00:00Z"]}))
        rules = aw.load_rules()
        self.assertIn(ROLE, aw.record_scores(run, self.state / aw.WRITE_GATE_FILE,
                                             rules)["admitted"])
        admit_judged(self.state, self.state.parent, 16, 15, person=OWNER[0])
        aw.sync_gate(aw.Journal(self.state / aw.AUDIT_FILE),
                     aw.WriteGate(self.state / aw.WRITE_GATE_FILE, rules),
                     datetime.now(timezone.utc))

    def go_live_file(self, system: str) -> None:
        folder = aw.control_dir(self.state, {})
        folder.mkdir(parents=True, exist_ok=True)
        (folder / aw.LIVE_FLAG.format(system=system)).write_text("")

    def accept_live(self, client: TestClient, item_id: str, **extra) -> dict:
        """A live accept: the first answers a confirm token and performs nothing; the
        same accept with the token performs it."""
        calls = len(self.bc.calls)
        first = client.post(f"/api/autopilot/items/{item_id}/decision",
                            json={"decision": "accept", **extra})
        self.assertEqual(first.status_code, 200, first.text)
        token = first.json()["confirm_token"]
        self.assertTrue(token, first.json())
        self.assertNotEqual((first.json()["decision"] or {}).get("outcome"), "done")
        self.assertEqual(len(self.bc.calls), calls)            # nothing written
        got = client.post(f"/api/autopilot/items/{item_id}/decision",
                          json={"decision": "accept", "confirm_token": token, **extra})
        self.assertEqual(got.status_code, 200, got.text)
        return got.json()

    def test_outcomes_count_only_what_the_reader_may_see_settled_items_too(self) -> None:
        """Row 22 measure: accepted / edited / rejected / ignored / waiting per prepared
        item, open and settled, under the list's own checks (the finance-marked item is
        counted for nobody; the colleague never counts the owner's)."""
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.settle_item("ap_sam", "done", "owner_replied", "2026-11-15T12:00:00Z")
        q.close()
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        got = owner.post("/api/autopilot/items/ap_own/decision",
                         json={"decision": "edit", "text": "Changed text."})
        self.assertEqual(got.status_code, 200, got.text)
        mine = sam.get("/api/autopilot").json()["outcomes"]
        self.assertEqual((mine["items"], mine["ignored"], mine["edited"]), (1, 1, 0))
        self.assertEqual(mine["ignored_reasons"], {"acted_without_it:owner_replied": 1})
        whole = owner.get("/api/autopilot").json()["outcomes"]
        self.assertEqual((whole["items"], whole["edited"], whole["ignored"]), (2, 1, 1))
        self.assertEqual(sum(whole[k] for k in
                             ("accepted", "edited", "rejected", "ignored", "waiting")), 2)
        self.assertEqual(whole["shares"], {"accepted": "0.0", "edited": "50.0",
                                           "rejected": "0.0", "ignored": "50.0"})

    def test_reader_sees_only_theirs_and_owner_only_controls(self) -> None:
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        area = owner.get("/api/autopilot").json()
        self.assertTrue(area["owner"])
        # the owner sees every person's items; the finance-marked one stays hidden
        self.assertEqual(sorted(i["item_id"] for i in area["items"]), ["ap_own", "ap_sam"])
        own = next(i for i in area["items"] if i["item_id"] == "ap_own")
        self.assertEqual((own["workflow"], own["system"], own["state"], own["today"]),
                         (FLOW, "bc", "waiting", True))
        self.assertEqual(own["citations"], ["graph-mail://x/m1@1"])
        self.assertEqual({p["name"] for p in area["people"]}, {"Ana", ""})
        row = next(w for w in area["workflows"] if w["role"] == ROLE)
        self.assertEqual((row["dev"]["percent"], row["fresh"], row["n"], row["state"]),
                         (92.5, None, 40, "dry_run"))
        self.assertEqual(len(area["workflows"]), 2)
        # the colleague: their own item, their own role's row, no controls
        theirs = sam.get("/api/autopilot").json()
        self.assertFalse(theirs["owner"])
        self.assertEqual([i["item_id"] for i in theirs["items"]], ["ap_sam"])
        self.assertEqual([w["role"] for w in theirs["workflows"]], ["receivables"])
        self.assertNotIn(OWNER[1], [p["id"] for p in theirs["people"]])
        # the filter catalogue is the reader's too: the workflows their page shows, not the
        # owner's whole catalogue (SP-05)
        from operating import workflows as wf
        shown = {w["workflow"] for w in theirs["workflows"]} | {i["workflow"] for i in theirs["items"]}
        self.assertEqual(theirs["known_workflows"], sorted(shown))
        self.assertLess(len(theirs["known_workflows"]), len(wf.names()))
        self.assertEqual(area["known_workflows"], sorted(wf.names()))
        for path, body in (("/api/autopilot/switches", {"global_off": True}),
                           ("/api/autopilot/audit/k/undo", None)):
            self.assertEqual(sam.post(path, json=body).status_code, 403, path)
        # the per-workflow stop is the owner's Mode control only
        self.assertEqual(sam.put(f"/api/autopilot/workflows/{FLOW}/mode",
                                 json={"mode": "off"}).status_code, 403)
        self.assertEqual(sam.get("/api/autopilot/audit").status_code, 403)
        self.assertFalse((self.state / aw.KILL_FILE).exists())
        self.assertFalse((self.state / aw.WORKFLOW_KILL_FILE).exists())
        # nobody decides another person's item, the owner included
        self.assertEqual(sam.post("/api/autopilot/items/ap_own/decision",
                                  json={"decision": "accept"}).status_code, 404)
        self.assertEqual(owner.post("/api/autopilot/items/ap_sam/decision",
                                    json={"decision": "reject"}).status_code, 404)
        self.assertEqual(owner.post("/api/autopilot/items/ap_fin/decision",
                                    json={"decision": "reject"}).status_code, 404)
        self.assertEqual(self.bc.calls, [])

    def score_fresh(self, roles: dict, judged=(15, 15)) -> None:
        """A fresh scorer run of ``roles`` (role -> (members, {workflow: (judged, matched)}))
        admitted to the write gate, every category's judged score ``judged`` (judged,
        matched; None: none on record), and the gate synced."""
        run = self.state.parent / f"fresh-{len(list(self.state.parent.glob('fresh-*')))}"
        run.mkdir(parents=True)
        (run / autopilot_engine.GATE_FILE).write_text(json.dumps({"schema": autopilot_engine.GATE_SCHEMA, "roles": {
            r: {"members": list(mem), "workflows": {f: {"judged": j, "matched": k}
                                                     for f, (j, k) in flows.items()}}
            for r, (mem, flows) in roles.items()}}))
        for n, r in enumerate(roles):
            (run / f"s{n}").mkdir()
            (run / f"s{n}" / "result.json").write_text(json.dumps(
                {"role": r, "status": "complete",
                 "window": ["2026-11-06T07:00:00Z", "2026-11-10T07:00:00Z"]}))
        rules = aw.load_rules()
        aw.record_scores(run, self.state / aw.WRITE_GATE_FILE, rules)
        if judged:
            admit_judged(self.state, self.state.parent, *judged,
                         categories=tuple(c for c, _l, _f in aw.DRAFT_CHOICES))
        aw.sync_gate(aw.Journal(self.state / aw.AUDIT_FILE),
                     aw.WriteGate(self.state / aw.WRITE_GATE_FILE, rules),
                     datetime.now(timezone.utc))

    def test_each_person_switches_only_their_own_and_the_owner_sees_who(self) -> None:
        """A person switches their own passed workflows on (off by
        default), can pause everything, and the owner sees who is on but cannot switch it
        on for them; every switch is on the audit log; an opt-in whose gate drops is
        suspended with why."""
        lee = ("lee@plx.example", "66666666-6666-4666-8666-666666666666")
        lee_role, lee_flow = f"person:{lee[0]}", "supplier_eta_receiving"
        self.score_fresh({ROLE: ([OWNER[1]], {FLOW: (16, 15)}),
                          lee_role: ([lee[1]], {lee_flow: (40, 38)})}, judged=(16, 15))
        owner, them = self.client(OWNER), self.client(lee)
        mine = owner.get("/api/autopilot").json()
        self.assertEqual([(o["workflow"], o["eligible"], o["on"]) for o in mine["my_opt_ins"]],
                         [(FLOW, True, False)])                     # off by default
        self.assertEqual(mine["my_opt_ins"][0]["score"], {      # the judged score, sourced
            "judged": 16, "matched": 15, "percent": 93.8, "need_judged": 15,
            "need_percent": 90.0, "source": "fixture set", "measured_on": "2026-11-12",
            "window": ["2026-11-06T15:00:00Z", "2026-11-06T15:15:01Z"], "scope": "team"})
        self.assertEqual(mine["switched_on"], [])
        theirs = them.get("/api/autopilot").json()
        self.assertEqual([(o["workflow"], o["on"]) for o in theirs["my_opt_ins"]],
                         [(lee_flow, False)])
        self.assertIsNone(theirs.get("switched_on"))             # only the owner and admins
        # the owner cannot switch it on for them: the request names no person, and their
        # own workflow has not passed for the owner
        self.assertEqual(owner.post("/api/autopilot/opt-in", json={
            "workflow": lee_flow, "on": True, "person": lee[1]}).status_code, 422)
        self.assertEqual(owner.post("/api/autopilot/opt-in", json={
            "workflow": lee_flow, "on": True}).status_code, 409)
        self.assertEqual(owner.put(f"/api/autopilot/workflows/{lee_flow}/mode",
                                   json={"mode": "auto", "role": lee_role}).status_code, 409)
        # anything unexpected fails closed: no row written
        for body, code in (({"workflow": "a:b", "on": True}, 404),
                           ({"workflow": "../x", "on": True}, 404),
                           ({"workflow": "no_such_flow", "on": True}, 409),
                           ({"workflow": FLOW, "on": "yes"}, 422),
                           ({"workflow": FLOW}, 422)):
            self.assertEqual(them.post("/api/autopilot/opt-in", json=body).status_code, code,
                             body)
        self.assertEqual(them.post("/api/autopilot/pause", json={"paused": 1}).status_code, 422)
        nobody = self.client(("nobody@plx.example", None))
        self.assertEqual(nobody.post("/api/autopilot/opt-in", json={
            "workflow": FLOW, "on": True}).status_code, 403)
        self.assertFalse(any(r.get("event") in aw.OPT_IN_EVENTS
                             for r in aw.Journal(self.state / aw.AUDIT_FILE).rows()))
        # they switch it on themselves, then pause everything
        on = them.post("/api/autopilot/opt-in", json={"workflow": lee_flow, "on": True})
        self.assertEqual(on.status_code, 200, on.text)
        self.assertEqual([(o["workflow"], o["on"]) for o in on.json()["my_opt_ins"]],
                         [(lee_flow, True)])
        self.assertTrue(on.json()["my_opt_ins"][0]["since"])
        self.assertEqual(them.post("/api/autopilot/pause", json={"paused": True}).status_code,
                         200)
        seen = owner.get("/api/autopilot").json()
        self.assertEqual([(w["person"], w["workflow"], w["paused"]) for w in seen["switched_on"]],
                         [(lee[1], lee_flow, True)])
        self.assertEqual(seen["my_opt_ins"][0]["on"], False)       # the owner's own: still off
        audit = owner.get("/api/autopilot/audit").json()["rows"]
        self.assertEqual([(r["event"], r["status"], r["by"], r["workflow"]) for r in audit
                          if r["event"] in aw.OPT_IN_EVENTS],
                         [("opt_in_paused", "paused", lee[1], None),
                          ("opt_in", "on", lee[1], lee_flow)])
        self.assertTrue(all(r["at"] for r in audit if r["event"] in aw.OPT_IN_EVENTS))
        # their score drops under the bar: suspended at the next read, saying why
        self.score_fresh({lee_role: ([lee[1]], {lee_flow: (40, 30)})})
        after = them.get("/api/autopilot").json()["my_opt_ins"]
        self.assertEqual([(o["workflow"], o["eligible"], o["on"]) for o in after],
                         [(lee_flow, False, False)])
        self.assertEqual((after[0]["suspended"]["reason"], after[0]["suspended"]["score"]
                          ["percent"]), ("below_bar", 75.0))
        self.assertEqual(owner.get("/api/autopilot").json()["switched_on"], [])
        held = next(r for r in owner.get("/api/autopilot/audit").json()["rows"]
                    if r["event"] == "opt_in_suspended")
        self.assertEqual((held["status"], held["by"], held["reasons"]),
                         ("suspended", "autopilot", ["below_bar"]))
        self.assertEqual(them.post("/api/autopilot/opt-in", json={
            "workflow": lee_flow, "on": True}).status_code, 409)
        self.assertEqual(self.bc.calls, [])

    def test_a_dry_run_of_auto_shows_only_for_an_earned_workflow_and_does_nothing(self) -> None:
        from operating import workflows as wf
        from operator_api import autopilot_area as aa
        mixin = next(v for v in vars(aa).values()
                     if isinstance(v, type) and "_apa_auto_would" in vars(v))
        item = {"item_id": "ap_x", "autopilot": "customer_request", "title": "Re: COA",
                "body": {"happened": {"sender": "kim@customer.example", "subject": "COA please"},
                         "prepared": {"draft": {"to": ["kim@customer.example"],
                                                "subject": "Re: COA please", "body": "Attached."}}}}
        flow = wf.of_item("customer_request", item["body"]["happened"])
        self.assertEqual(mixin._apa_auto_would(self.state, {OWNER[1]})(item), "")  # not earned
        self.score_fresh({ROLE: ([OWNER[1]], {flow: (16, 15)})})
        before = aw.Journal(self.state / aw.AUDIT_FILE).rows()
        got = mixin._apa_auto_would(self.state, {OWNER[1], OWNER[0]})(item)
        self.assertTrue(got == "send" or got.startswith("keep: "), got)
        self.assertEqual(mixin._apa_auto_would(self.state, {COLLEAGUE[1]})(item), "")
        self.assertEqual(mixin._apa_auto_would(None, {OWNER[1]})(item), "")
        self.assertEqual(aw.Journal(self.state / aw.AUDIT_FILE).rows(), before)
        self.assertEqual(self.bc.calls, [])

    def test_the_owner_is_told_once_of_a_first_gate_pass_and_nobody_else_is(self) -> None:
        """AP-22: a workflow's first fresh-gate pass reaches the owner's page as a notice
        until they acknowledge it; it turns nothing on, nobody else sees or acks it, and a
        later scoring that still passes never tells them again."""
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        self.assertEqual(owner.get("/api/autopilot").json()["gate_notices"], [])
        self.pass_fresh_gate()
        area = owner.get("/api/autopilot").json()
        self.assertEqual([(n["role"], n["workflow"]) for n in area["gate_notices"]],
                         [(ROLE, FLOW)])
        self.assertTrue(area["gate_notices"][0]["passed_at"])
        row = next(w for w in area["workflows"] if w["role"] == ROLE)
        self.assertEqual((row["state"], row["switched_on"]), ("passed", 0))  # nothing on
        self.assertIsNone(sam.get("/api/autopilot").json().get("gate_notices"))
        ack = {"role": ROLE, "workflow": FLOW}
        self.assertEqual(sam.post("/api/autopilot/notices/ack", json=ack).status_code, 403)
        self.assertEqual(owner.post("/api/autopilot/notices/ack",
                                    json={"role": ROLE, "workflow": "ar_collections"}
                                    ).status_code, 404)
        self.assertEqual(owner.post("/api/autopilot/notices/ack", json={"role": ROLE}
                                    ).status_code, 422)
        got = owner.post("/api/autopilot/notices/ack", json=ack)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["gate_notices"], [])
        row = next(w for w in got.json()["workflows"] if w["role"] == ROLE)
        self.assertEqual(row["switched_on"], 0)
        self.assertEqual(owner.post("/api/autopilot/notices/ack", json=ack).status_code, 404)
        # scored again, still passing: told once only
        self.score_fresh({ROLE: ([OWNER[1]], {FLOW: (16, 15)})})
        self.assertEqual(owner.get("/api/autopilot").json()["gate_notices"], [])
        events = [r["event"] for r in aw.Journal(self.state / aw.AUDIT_FILE).rows()
                  if r.get("workflow") == FLOW and r.get("role") == ROLE]
        self.assertEqual(events, ["gate_passed", "owner_notified"])
        self.assertEqual(self.bc.calls, [])

    def test_an_empty_page_says_no_workflow_passed_and_the_filter_offers_every_workflow(
            self) -> None:
        """Owner 2026-11-11: an empty Autopilot page did not say why; 2026-11-17: items
        are prepared only for what a person switched on themselves, so an empty page says
        that first (a gate pass no longer prepares anything); the filter still offers the
        catalogue. Once the reader switched something on, the note goes."""
        from operating import workflows as wf
        from operator_api.autopilot_area import NO_DRAFTS_ON
        path = self.state.parent / "empty" / ap.QUEUE_FILE
        ap.Queue(path).close()
        self.backend.autopilot_queue = path
        gate = path.parent / autopilot_engine.GATE_FILE
        gate.write_text(json.dumps({"schema": autopilot_engine.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER[1]], "workflows": {FLOW: {"judged": 40, "matched": 37}}}}}))
        area = self.client(OWNER).get("/api/autopilot").json()
        self.assertEqual(area["items"], [])
        self.assertEqual(area["notes"][0], NO_DRAFTS_ON)        # a passing gate changes nothing
        self.assertEqual(area["known_workflows"], sorted(wf.names()))
        self.assertIn(FLOW, area["known_workflows"])
        on = self.client(OWNER).post("/api/autopilot/drafts",
                                     json={"choice": "customer_replies", "on": True})
        self.assertEqual(on.status_code, 200, on.text)
        self.assertNotIn(NO_DRAFTS_ON, on.json()["notes"])

    def test_prepared_work_is_switched_on_by_the_person_alone_from_a_short_list(self) -> None:
        """Owner walk 2026-11-17: 509 items prepared by role for 16 people, none of whom
        asked; 205 role x workflow rows. Now each reader gets a few plain choices, all off,
        and only they switch theirs (no person in the request; the owner cannot; a view-as
        or nameless session cannot); an unknown or malformed choice writes nothing; a
        pause stops everything at once."""
        owner, them = self.client(OWNER), self.client(COLLEAGUE)
        mine = owner.get("/api/autopilot").json()["my_drafts"]
        self.assertEqual([c["choice"] for c in mine], [c[0] for c in aw.DRAFT_CHOICES])
        self.assertTrue(all(c["label"] and c["on"] is False for c in mine))
        for body, code in (({"choice": "../x", "on": True}, 404),
                           ({"choice": "teams_chat", "on": True}, 404),   # a workflow, not a choice
                           ({"choice": "teams_replies", "on": "yes"}, 422),
                           ({"choice": "teams_replies"}, 422),
                           ({"choice": "teams_replies", "on": True, "person": OWNER[1]}, 422)):
            self.assertEqual(them.post("/api/autopilot/drafts", json=body).status_code, code, body)
        nobody = self.client(("nobody@plx.example", None))
        self.assertEqual(nobody.post("/api/autopilot/drafts", json={
            "choice": "teams_replies", "on": True}).status_code, 403)
        self.assertEqual(switched(aw.Journal(self.state / aw.AUDIT_FILE).rows()), {})
        got = them.post("/api/autopilot/drafts", json={"choice": "teams_replies", "on": True})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual([c["choice"] for c in got.json()["my_drafts"] if c["on"]],
                         ["teams_replies"])
        seen = owner.get("/api/autopilot").json()
        self.assertFalse(any(c["on"] for c in seen["my_drafts"]))
        # the owner sees who switched what on (read only); the colleague sees no one's list
        self.assertEqual(seen["drafts_switched_on"], [
            {"person": COLLEAGUE[1], "choice": "teams_replies", "label": "Replies in Teams",
             "by_owner": False, "partly": False}])
        self.assertIsNone(got.json().get("drafts_switched_on"))
        rows = aw.Journal(self.state / aw.AUDIT_FILE).rows()
        self.assertEqual(switched(rows), {COLLEAGUE[1]: frozenset({"teams_chat", "teams_group"})})
        self.assertTrue(all(r["by"] == r["person"] == COLLEAGUE[1] for r in rows
                            if r.get("event") == aw.DRAFTS_EVENT))
        # a pause stops everything they switched on; off switches the choice off
        self.assertEqual(them.post("/api/autopilot/pause", json={"paused": True}).status_code, 200)
        self.assertEqual(switched(aw.Journal(self.state / aw.AUDIT_FILE).rows()), {})
        self.assertEqual(them.post("/api/autopilot/pause", json={"paused": False}).status_code, 200)
        off = them.post("/api/autopilot/drafts", json={"choice": "teams_replies", "on": False})
        self.assertFalse(any(c["on"] for c in off.json()["my_drafts"]))
        self.assertEqual(switched(aw.Journal(self.state / aw.AUDIT_FILE).rows()), {})

    def test_auto_rests_on_the_judged_score_never_the_replay_count(self) -> None:
        """A category earns Auto only on its judged precision (items
        Auto would have performed, graded right by the judge, >= 90% on >= 15, held-out
        days). A replay count that passes (the person acted on the thread) earns nothing
        without it, a judged score under the bar keeps Auto shut, and the switch shows the
        judged number with its source and date."""
        lee = ("lee@plx.example", "66666666-6666-4666-8666-666666666666")
        lee_role, lee_flow = f"person:{lee[0]}", "supplier_eta_receiving"
        them = self.client(lee)

        def row():
            return next(c for c in them.get("/api/autopilot").json()["my_drafts"]
                        if c["choice"] == "supplier_followups")
        self.score_fresh({lee_role: ([lee[1]], {lee_flow: (40, 40)})}, judged=None)
        self.assertEqual((row()["auto_ok"], row().get("score")), (False, None))
        self.assertEqual(them.post("/api/autopilot/mode", json={
            "choice": "supplier_followups", "mode": "auto"}).status_code, 409)
        admit_judged(self.state, self.state.parent, 30, 26,
                     categories=("supplier_followups",))          # 86.7%: under the bar
        got = row()
        self.assertFalse(got["auto_ok"])
        self.assertEqual((got["score"]["judged"], got["score"]["matched"],
                          got["score"]["percent"], got["score"]["source"]),
                         (30, 26, 86.7, "fixture set"))
        self.assertEqual(them.post("/api/autopilot/mode", json={
            "choice": "supplier_followups", "mode": "auto"}).status_code, 409)
        admit_judged(self.state, self.state.parent, 30, 27,
                     categories=("supplier_followups",))          # 90.0%: earned
        self.assertTrue(row()["auto_ok"])
        self.assertEqual(them.post("/api/autopilot/mode", json={
            "choice": "supplier_followups", "mode": "auto"}).status_code, 200)
        # a later judged run under the bar stops it at once: the opt-in is suspended
        admit_judged(self.state, self.state.parent, 30, 20,
                     categories=("supplier_followups",))
        gate = aw.WriteGate(self.state / aw.WRITE_GATE_FILE, aw.load_rules())
        self.assertEqual(aw.eligible(gate, lee[1], lee_flow), [])
        self.assertEqual(aw.shortfall(gate, lee[1], lee_flow)["reason"], "below_bar")
        self.assertFalse(row()["auto_ok"])
        self.assertEqual(self.bc.calls, [])

    def test_one_switch_per_category_off_confirm_auto_and_auto_only_where_earned(self) -> None:
        """Each category is off, confirm
        (prepared in full; nothing is performed without the person's click) or auto (only
        for the workflows the person's replay score earned). Only their own; an unearned
        auto, an unknown mode or choice, a nameless session write nothing."""
        lee = ("lee@plx.example", "66666666-6666-4666-8666-666666666666")
        lee_role, lee_flow = f"person:{lee[0]}", "supplier_eta_receiving"
        self.score_fresh({lee_role: ([lee[1]], {lee_flow: (40, 38)})}, judged=(20, 19))
        them = self.client(lee)
        journal = aw.Journal(self.state / aw.AUDIT_FILE)

        def row(area, choice="supplier_followups"):
            return next(c for c in area["my_drafts"] if c["choice"] == choice)
        start = row(them.get("/api/autopilot").json())
        self.assertEqual((start["mode"], start["auto_ok"], start["score"]["judged"],
                          start["score"]["matched"]), ("off", True, 20, 19))
        self.assertEqual(row(them.get("/api/autopilot").json(), "teams_replies")["auto_ok"],
                         False)
        for body, code in (({"choice": "supplier_followups", "mode": "live"}, 422),
                           ({"choice": "supplier_followups"}, 422),
                           ({"choice": "no_such", "mode": "confirm"}, 404),
                           ({"choice": "supplier_followups", "mode": "auto",
                             "person": OWNER[1]}, 422),
                           ({"choice": "teams_replies", "mode": "auto"}, 409)):
            self.assertEqual(them.post("/api/autopilot/mode", json=body).status_code, code, body)
        nobody = self.client(("nobody@plx.example", None))
        self.assertEqual(nobody.post("/api/autopilot/mode", json={
            "choice": "supplier_followups", "mode": "confirm"}).status_code, 403)
        self.assertFalse([r for r in journal.rows() if r.get("person")])
        # confirm: prepared, never performed by itself (the write path stays a dry run
        # until the person's own click approves the exact item)
        got = them.post("/api/autopilot/mode", json={"choice": "supplier_followups",
                                                     "mode": "confirm"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(row(got.json())["mode"], "confirm")
        rows = journal.rows()
        self.assertEqual(switched(rows)[lee[1]], frozenset(
            aw.draft_choice("supplier_followups")))
        self.assertFalse(any(o["on"] for o in aw.opt_ins(rows).values()))
        gate = aw.WriteGate(self.state / aw.WRITE_GATE_FILE, aw.load_rules())
        env = {aw.MASTER_ENV: "1"}
        self.assertEqual(aw.write_state(gate, rows, env, lee[1], lee_flow, ()),
                         ("dry_run", ["not_switched_on_by_person"]))
        # auto: only the earned workflow performs by itself, the rest of it still waits
        got = them.post("/api/autopilot/mode", json={"choice": "supplier_followups",
                                                     "mode": "auto"})
        self.assertEqual(row(got.json())["mode"], "auto")
        rows = journal.rows()
        self.assertEqual({w for (p, w), o in aw.opt_ins(rows).items() if o["on"]}, {lee_flow})
        self.assertEqual(aw.write_state(gate, rows, env, lee[1], lee_flow, ())[0], "live")
        self.assertEqual(aw.write_state(gate, rows, {}, lee[1], lee_flow, ())[1],
                         [f"master_switch_off:{aw.MASTER_ENV}"])
        # back to confirm, then off: performing stops, then preparing stops
        back = them.post("/api/autopilot/mode", json={"choice": "supplier_followups",
                                                      "mode": "confirm"})
        self.assertEqual(row(back.json())["mode"], "confirm")
        self.assertFalse(any(o["on"] for o in aw.opt_ins(journal.rows()).values()))
        off = them.post("/api/autopilot/mode", json={"choice": "supplier_followups",
                                                     "mode": "off"})
        self.assertEqual(row(off.json())["mode"], "off")
        self.assertEqual(switched(journal.rows()), {})
        self.assertTrue(all(r["by"] == r["person"] == lee[1] for r in journal.rows()
                            if r.get("person")))
        self.assertEqual(self.bc.calls, [])

    def test_the_owners_draft_file_shows_on_and_the_persons_own_off_wins_on_page_and_feed(
            self) -> None:
        """The owner was in the Draft file for every workflow,
        the live feed prepared for him, yet the page showed every choice off ("Turn on").
        The page and the live feed now read one rule (``aw.prepared_for``): Draft-on
        shows on, set by the owner; the person's own off (or pause) wins over the Draft
        file in both; their own on again is theirs. Every Draft workflow sits in a
        choice, so each can be switched off. An excluded person, an unavailable people
        check, or an address the session does not hold shows nothing on."""
        from operating import workflows as wf
        draft = self.state.parent / "draft.json"
        every = sorted({f for _c, _l, flows in aw.DRAFT_CHOICES for f in flows})
        draft.write_text(json.dumps({"schema": autopilot_engine.DRAFT_SCHEMA,
                                     autopilot_engine.DRAFT_NEVER_FOR: [OWNER[0]],
                                     "people": {OWNER[0]: every,
                                                COLLEAGUE[0]: ["teams_chat", "teams_group",
                                                               "quote_pricing"]}}))
        self.backend.writes_environ = {**ENV, autopilot_engine.DRAFT_ENV: str(draft)}
        # every Draft workflow the real file names is in one of the page's choices
        real = autopilot_engine.load_draft({})
        self.assertTrue(real)
        self.assertLessEqual(set().union(*map(set, real.values())), set(every))
        self.assertEqual(set(every), set(wf.names()))      # every workflow has a switch
        journal = self.state / aw.AUDIT_FILE
        feed = autopilot_engine.SwitchGate(journal, {OWNER[1]: frozenset(every)})
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)

        def choices(area: dict) -> dict:
            return {c["choice"]: (c["on"], c.get("by_owner"), bool(c.get("since")))
                    for c in area["my_drafts"]}
        area = owner.get("/api/autopilot").json()
        self.assertEqual(choices(area), {c[0]: (True, True, False) for c in aw.DRAFT_CHOICES})
        self.assertNotIn(ws.NO_DRAFTS_ON, area["notes"])
        self.assertTrue(all(feed(OWNER[1], f) for f in every))
        # the owner sees the colleague on through the Draft file too, set by the owner;
        # the colleague sees only their own, never the owner's or the who-list
        self.assertIn({"person": COLLEAGUE[1], "choice": "teams_replies",
                       "label": "Replies in Teams", "by_owner": True, "partly": False},
                      area["drafts_switched_on"])
        theirs = sam.get("/api/autopilot").json()
        self.assertEqual({k for k, v in choices(theirs).items() if v[0]},
                         {"teams_replies", "customer_replies"})
        # a choice the Draft file covers in part shows partly on, and its off stops it
        part = {c["choice"]: c for c in theirs["my_drafts"]}["customer_replies"]
        self.assertEqual((part["on"], part["partly"], part["by_owner"]), (True, True, True))
        self.assertFalse({c["choice"]: c for c in theirs["my_drafts"]}["teams_replies"]["partly"])
        self.assertIsNone(theirs.get("drafts_switched_on"))
        feed_sam = autopilot_engine.SwitchGate(journal, {COLLEAGUE[1]: frozenset(
            {"teams_chat", "teams_group", "quote_pricing"})})
        self.assertTrue(feed_sam(COLLEAGUE[1], "quote_pricing"))
        sam.post("/api/autopilot/drafts", json={"choice": "customer_replies", "on": False})
        self.assertFalse(feed_sam(COLLEAGUE[1], "quote_pricing"))
        self.assertFalse(choices(sam.get("/api/autopilot").json())["customer_replies"][0])
        # the owner's own off wins over the Draft file: on the page and in the feed
        off = owner.post("/api/autopilot/drafts", json={"choice": "customer_replies", "on": False})
        self.assertEqual(off.status_code, 200, off.text)
        self.assertEqual(choices(off.json())["customer_replies"], (False, False, False))
        self.assertTrue(choices(off.json())["teams_replies"][0])
        self.assertFalse(any(feed(OWNER[1], f) for f in aw.draft_choice("customer_replies")))
        self.assertTrue(feed(OWNER[1], "teams_chat"))
        # the last choice holds the never-list workflow: its off reaches it, its on is
        # only the Draft file's (a switch row never turns it on for anyone)
        owner.post("/api/autopilot/drafts", json={"choice": "other_work", "on": False})
        self.assertFalse(feed(OWNER[1], "other_external") or feed(OWNER[1], "reporting"))
        sam.post("/api/autopilot/drafts", json={"choice": "other_work", "on": True})
        self.assertFalse(feed(COLLEAGUE[1], "other_external"))
        self.assertTrue(feed(COLLEAGUE[1], "reporting"))
        self.assertTrue(choices(sam.get("/api/autopilot").json())["other_work"][0])
        on = owner.post("/api/autopilot/drafts", json={"choice": "other_work", "on": True})
        self.assertEqual(choices(on.json())["other_work"], (True, False, True))
        self.assertTrue(feed(OWNER[1], "other_external"))
        # their own on again is theirs (since when), not the owner's
        on = owner.post("/api/autopilot/drafts", json={"choice": "customer_replies", "on": True})
        self.assertEqual(choices(on.json())["customer_replies"], (True, False, True))
        self.assertTrue(all(feed(OWNER[1], f) for f in aw.draft_choice("customer_replies")))
        # a pause takes out everything, the Draft file's too; resume brings it back
        self.assertEqual(owner.post("/api/autopilot/pause", json={"paused": True}).status_code, 200)
        self.assertFalse(any(feed(OWNER[1], f) for f in every))
        self.assertFalse(any(c["on"] for c in owner.get("/api/autopilot").json()["my_drafts"]))
        owner.post("/api/autopilot/pause", json={"paused": False})
        self.assertTrue(feed(OWNER[1], "teams_chat"))
        # an excluded person or an unavailable people check: nothing on through the
        # Draft file (fail closed)
        barred = PeopleExclusion([OWNER[1]])
        barred.attach_directory([(OWNER[1], OWNER[0], []), (COLLEAGUE[1], COLLEAGUE[0], [])])
        for guard, colleague in ((barred, True),
                                 (PeopleExclusion(unavailable="excluded_people_oids_absent"),
                                  False)):
            self.backend.autopilot_guard = guard
            got = owner.get("/api/autopilot").json()
            self.assertFalse(choices(got)["approvals"][0], guard)
            self.assertTrue(choices(got)["customer_replies"][0])     # their own switch
            self.assertEqual(any(d["person"] == COLLEAGUE[1] and d["by_owner"]
                                 for d in got["drafts_switched_on"]), colleague, guard)
        # a broken journal: the person's own off cannot be read, so the feed prepares
        # nothing at all (not the Draft file alone)
        journal.write_text(journal.read_text() + "{not json\n")
        self.assertEqual(feed.passed, {})

    def test_an_unnamed_teams_sender_never_shows_as_an_id_and_a_named_one_is_unchanged(
            self) -> None:
        """The queue row line and "What happened" read ``happened.sender``; the title and
        summary of an item prepared before the rule carry the id too. The API shows
        "Someone" in all of them; the stored sender (the exclusion's input) is kept."""
        raw = "e1dd02bd-0017-4203-95ef-585f1c801321"
        old = f"{raw} is waiting on a reply: (Teams message)"
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            for item_id, sender, summary in (
                    ("ap_teams", raw, old),
                    ("ap_mail", "vince@example.com",
                     "vince@example.com is waiting on a reply: PO 90412 date")):
                item = queued(item_id, OWNER[1])
                item["title"] = "Reply prep: " + summary
                item["body"] = {**item["body"], "happened": {
                    "channel": "teams" if item_id == "ap_teams" else "mail",
                    "object_id": "x:" + item_id, "sender": sender},
                    "prepared": {**item["body"]["prepared"], "summary": summary}}
                q.add_item(item)
        q.close()
        owner = self.client(OWNER)
        for path in ("/api/autopilot", "/api/my-work"):
            got = owner.get(path)
            self.assertEqual(got.status_code, 200, got.text)
            self.assertNotIn(raw[:8], got.text.lower(), path)
            items = {i["item_id"]: i for i in got.json()["items"]}
            teams, mail = items["ap_teams"], items["ap_mail"]
            self.assertEqual(teams["happened"]["sender"], "Someone", path)
            self.assertEqual(teams["prepared"]["summary"],
                             "Someone is waiting on a reply: (Teams message)", path)
            self.assertEqual(teams["title"],
                             "Reply prep: Someone is waiting on a reply: (Teams message)")
            self.assertEqual(mail["happened"]["sender"], "vince@example.com", path)
            self.assertEqual(mail["prepared"]["summary"],
                             "vince@example.com is waiting on a reply: PO 90412 date")
            self.assertEqual(mail["title"],
                             "Reply prep: vince@example.com is waiting on a reply: PO 90412 date")
        # the stored item keeps the raw sender: every action is still cleared against them
        q = ap.Queue(self.backend.autopilot_queue)
        try:
            stored = next(i for i in q.items() if i["item_id"] == "ap_teams")
        finally:
            q.close()
        self.assertEqual(stored["body"]["happened"]["sender"], raw)
        self.assertTrue(aw.actions_of(stored))
        for action in aw.actions_of(stored):
            self.assertIn(raw, action["people"])

    def test_a_teams_item_shows_the_stated_name_and_message_only_to_who_may_see_it(
            self) -> None:
        """The live defect (11-16): Teams items read "Someone" / "(Teams message)" and the
        owners as "<role> colleague N". The item shows the sender's stated name and the
        message's snippet; an owner the roster names only by id takes the name a message
        of theirs the reader may see states; a reader the item is not visible to sees
        neither the snippet nor the name."""
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            item = queued("ap_chat", OWNER[1])
            item["title"] = "Teams reply: Sam Lee is waiting on a reply: \u201cIs PO 7 in?\u201d"
            item["body"] = {**item["body"], "happened": {
                "channel": "teams", "event_type": "message.received",
                "object_id": "TeamsChatMessage:x", "sender": COLLEAGUE[1],
                "sender_name": "Sam Lee", "snippet": "Is PO 7 in? Need it for the run"}}
            q.add_item(item)
            bare = queued("ap_bare", OWNER[1])
            bare["body"] = {**bare["body"], "happened": {
                "channel": "teams", "event_type": "message.received",
                "object_id": "TeamsChatMessage:y", "sender": COLLEAGUE[1]}}
            q.add_item(bare)
        q.close()
        area = self.client(OWNER).get("/api/autopilot")
        self.assertEqual(area.status_code, 200, area.text)
        items = {i["item_id"]: i for i in area.json()["items"]}
        chat = items["ap_chat"]["happened"]
        self.assertEqual(chat["sender"], "Sam Lee")
        self.assertEqual(chat["message"], "Is PO 7 in? Need it for the run")
        self.assertNotIn("sender_name", chat)
        # an item whose text is not read yet says so (``autopilot.name_teams_items``)
        self.assertEqual(items["ap_bare"]["happened"]["message"],
                         "(message text not read yet)")
        people = {p["id"]: p for p in area.json()["people"]}
        self.assertEqual(people[COLLEAGUE[1]]["name"], "Sam Lee")
        mine = self.client(OWNER).get("/api/my-work").json()
        self.assertEqual({i["item_id"]: i for i in mine["items"]}["ap_chat"]["happened"][
            "message"], "Is PO 7 in? Need it for the run")
        # Nothing of the message reaches them
        for path in ("/api/autopilot", "/api/my-work"):
            got = self.client(COLLEAGUE).get(path)
            self.assertEqual(got.status_code, 200, path)
            self.assertNotIn("Is PO 7 in", got.text, path)
            self.assertNotIn("ap_chat", got.text, path)

    def test_decisions_dry_run_records_only(self) -> None:
        owner = self.client(OWNER)
        later = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bad = owner.post("/api/autopilot/items/ap_own/decision",
                         json={"decision": "snooze", "until": "2020-01-01T00:00:00Z"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(owner.post("/api/autopilot/items/ap_own/decision",
                                    json={"decision": "unknown"}).status_code, 422)
        snoozed = owner.post("/api/autopilot/items/ap_own/decision",
                             json={"decision": "snooze", "until": later}).json()
        self.assertEqual((snoozed["state"], snoozed["decision"]["until"]), ("snoozed", later))
        edited = owner.post("/api/autopilot/items/ap_own/decision",
                            json={"decision": "edit", "text": "Thanks, PO P-1 is in."}).json()
        self.assertEqual(edited["state"], "edited")
        done = owner.post("/api/autopilot/items/ap_own/decision", json={"decision": "accept"})
        self.assertEqual(done.status_code, 200, done.text)
        item = done.json()
        self.assertEqual((item["state"], item["decision"]["outcome"], item["decision"]["text"]),
                         ("accepted", "dry_run", "Thanks, PO P-1 is in."))
        self.assertIn("gate_not_passed", item["decision"]["reasons"])
        self.assertEqual(self.bc.calls, [])            # nothing written
        audit = owner.get("/api/autopilot/audit").json()
        self.assertEqual(audit["live"], 0)
        self.assertEqual([(r["kind"], r["status"], r["mode"]) for r in audit["rows"]],
                         [("send_mail", "unavailable", None), ("bc_write", "dry_run", "dry_run")])
        # one write path: the accept ran through the prepared-writes desk Ask uses (its
        # journal holds the item prepared by this autopilot, confirmed by its owner, and
        # the executor's outcome), keyed by the autopilot item's own id
        desk = [json.loads(line) for line in (
            self.state / "prepared-writes" / "journal.jsonl").read_text().splitlines()]
        events = [(r.get("event"), r.get("origin")) for r in desk]
        self.assertEqual([e for e, _o in events], ["prepared", "confirmed", "outcome"], events)
        self.assertEqual(events[0][1], "customer_po")
        self.assertEqual([(o["kind"], o["status"]) for o in desk[-1]["results"]],
                         [("bc_write", "dry_run"), ("send_mail", "unavailable")])
        # the global kill switch refuses an accept and is on the audit log
        self.assertFalse(owner.post("/api/autopilot/switches",
                                    json={"global_off": False}).json()["global_off"])
        self.assertTrue(owner.post("/api/autopilot/switches",
                                   json={"global_off": True}).json()["global_off"])
        self.assertEqual(owner.post("/api/autopilot/items/ap_own/decision",
                                    json={"decision": "accept"}).status_code, 409)
        rows = owner.get("/api/autopilot/audit").json()["rows"]
        self.assertEqual((rows[0]["event"], rows[0]["by"]), ("switch", OWNER[1]))
        area = owner.get("/api/autopilot").json()
        self.assertTrue(all(w["state"] == "off" for w in area["workflows"]))

    def test_live_only_after_gate_and_the_persons_own_switch_then_undo_once(self) -> None:
        owner = self.client(OWNER)
        live = {"workflow": FLOW, "on": True}
        refused = owner.post("/api/autopilot/opt-in", json=live)
        self.assertEqual(refused.status_code, 409)      # not passed: stays a dry run
        self.pass_fresh_gate()
        row = next(w for w in owner.get("/api/autopilot").json()["workflows"]
                   if w["role"] == ROLE)
        self.assertEqual((row["state"], row["fresh"]["percent"], row["n"]),
                         ("passed", 93.8, 16))
        row = next(w for w in owner.post("/api/autopilot/opt-in", json=live)
                   .json()["workflows"] if w["role"] == ROLE)
        self.assertEqual(row["state"], "live_held")    # the host's master switch is off
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        self.go_live_file("bc")             # the owner's go-live files (BC, mail)
        self.go_live_file("mail")
        # switched off: the accept is recorded killed, nothing written
        owner.put(f"/api/autopilot/workflows/{FLOW}/mode", json={"mode": "off"})
        self.assertEqual(owner.post("/api/autopilot/items/ap_own/decision",
                                    json={"decision": "accept"}).json()["decision"]["outcome"],
                         "killed")
        self.assertEqual(self.bc.calls, [])
        owner.put(f"/api/autopilot/workflows/{FLOW}/mode", json={"mode": "draft"})
        self.bc.answers = [(201, {"id": NEW_ID, "@odata.etag": 'W/"1"'}), (204, {})]
        item = self.accept_live(owner, "ap_own")
        # the BC entry went through; the confirmation email has no sender wired here, so
        # it never ran: partly done, each step named (WEB-036)
        self.assertEqual((item["decision"]["outcome"], steps(item["decision"])),
                         ("partly_done", [("bc_write", "bc", "done"),
                                          ("send_mail", "mail", "unavailable")]))
        self.assertEqual([c[0]["method"] for c in self.bc.calls], ["POST"])
        audit = owner.get("/api/autopilot/audit").json()
        done = next(r for r in audit["rows"] if r["status"] == "done")
        self.assertEqual((audit["live"], done["undoable"], done["system"]), (1, True, "bc"))
        undone = owner.post(f"/api/autopilot/audit/{done['key']}/undo")
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(self.bc.calls[-1][0]["method"], "DELETE")
        self.assertTrue(next(r for r in undone.json()["rows"]
                             if r["key"] == done["key"] and r["event"] == "outcome")["undone"])
        self.assertEqual(owner.post(f"/api/autopilot/audit/{done['key']}/undo").status_code, 409)
        self.assertEqual(len(self.bc.calls), 2)
        # back to dry run: a second accept is never written again
        owner.post("/api/autopilot/opt-in", json={"workflow": FLOW, "on": False})
        owner.post("/api/autopilot/items/ap_own/decision", json={"decision": "accept"})
        self.assertEqual(len(self.bc.calls), 2)

    def test_an_undo_not_done_is_never_shown_as_undone(self) -> None:
        """API-019: an undo pending, failed or of unknown outcome says so; only a done (or
        reconciled done) undo marks the action undone; any attempt blocks a second."""
        owner = self.client(OWNER)
        journal = aw.Journal(self.state / aw.AUDIT_FILE)
        cases = {"pending": ["pending"], "failed": ["pending", "failed"],
                 "unknown": ["pending", "unknown"], "done": ["pending", "done"],
                 "reconciled": ["pending", "unknown", "reconciled:done"]}
        with journal.locked() as held:
            for n, (name, states) in enumerate(cases.items()):
                held.append({"event": "outcome", "at": "2026-11-01T00:00:00Z",
                             "key": f"k{n}", "kind": "bc_write", "mode": "live",
                             "status": "done", "inverse": {"method": "DELETE"}})
                for state in states:
                    event, _, status = state.rpartition(":")
                    held.append({"event": event or ("attempt" if status == "pending"
                                                     else "outcome"),
                                 "at": "2026-11-01T00:01:00Z", "key": f"u{n}",
                                 **({} if event else {"of": f"k{n}"}),
                                 "mode": "live", "status": status})
        rows = {r["key"]: r for r in owner.get("/api/autopilot/audit").json()["rows"]
                if r["event"] == "outcome" and r["key"].startswith("k")}
        shown = {name: (rows[f"k{n}"]["undone"], rows[f"k{n}"]["undo_status"],
                        rows[f"k{n}"]["undoable"]) for n, name in enumerate(cases)}
        self.assertEqual(shown, {"pending": (False, "pending", False),
                                 "failed": (False, "failed", False),
                                 "unknown": (False, "unknown", False),
                                 "done": (True, None, False),
                                 "reconciled": (True, None, False)})

    def test_owner_accept_is_an_approval_written_once_the_kind_is_opened(self) -> None:
        """The owner's accept of their own item is a person's approval: with the owner's
        per-kind switch open it is written without a passed replay gate; never twice."""
        owner = self.client(OWNER)
        self.backend.writes_environ = {**ENV, aw.APPROVED_ENV: "bc_write"}
        self.bc.answers = [(201, {"id": NEW_ID, "@odata.etag": 'W/"1"'})]
        decision = self.accept_live(owner, "ap_own")["decision"]
        self.assertEqual((decision["outcome"], steps(decision)),
                         ("partly_done", [("bc_write", "bc", "done"),
                                          ("send_mail", "mail", "unavailable")]))
        self.assertEqual([c[0]["method"] for c in self.bc.calls], ["POST"])
        rows = [r for r in owner.get("/api/autopilot/audit").json()["rows"]
                if r.get("kind") == "bc_write" and r.get("status") == "done"]
        self.assertEqual(len(rows), 1)
        owner.post("/api/autopilot/items/ap_own/decision", json={"decision": "accept"})
        self.assertEqual(len(self.bc.calls), 1)
        self.assertEqual(self.client(COLLEAGUE).post(
            "/api/autopilot/items/ap_own/decision", json={"decision": "accept"}).status_code, 404)

    def test_a_live_accept_needs_its_confirm_and_a_bulk_accept_refuses_it(self) -> None:
        """Review 8dd669edac: an item an accept would perform live (its kind opened to
        approved writes) is marked live by the server; a single accept without the
        confirm writes nothing, a bulk accept is refused, the confirm writes it once; a
        token is bound to its item, person and text, and spent once."""
        owner = self.client(OWNER)
        listed = {i["item_id"]: i for i in owner.get("/api/autopilot").json()["items"]}
        self.assertFalse(listed["ap_own"]["live"])               # nothing opened: dry run
        self.backend.writes_environ = {**ENV, aw.APPROVED_ENV: "bc_write"}
        listed = {i["item_id"]: i for i in owner.get("/api/autopilot").json()["items"]}
        self.assertEqual((listed["ap_own"]["live"], listed["ap_own"]["live_kinds"]),
                         (True, ["bc_write"]))
        bulk = owner.post("/api/autopilot/items/ap_own/decision",
                          json={"decision": "accept", "bulk": True})
        self.assertEqual(bulk.status_code, 409, bulk.text)
        first = owner.post("/api/autopilot/items/ap_own/decision", json={"decision": "accept"})
        token = first.json()["confirm_token"]
        self.assertEqual((first.json()["state"], self.bc.calls), ("waiting", []))
        # a made-up token, the token with a bulk flag, or with other text: nothing written
        for extra in ({"confirm_token": "x" * 32}, {"confirm_token": token, "bulk": True},
                      {"confirm_token": token, "text": "other words"}):
            got = owner.post("/api/autopilot/items/ap_own/decision",
                             json={"decision": "accept", **extra})
            self.assertIn(got.status_code, (200, 409, 422), got.text)
            self.assertEqual(self.bc.calls, [], extra)
        self.bc.answers = [(201, {"id": NEW_ID, "@odata.etag": 'W/"1"'})]
        done = owner.post("/api/autopilot/items/ap_own/decision",
                          json={"decision": "accept", "confirm_token": token}).json()
        self.assertEqual((done["decision"]["outcome"], steps(done["decision"])[0],
                          len(self.bc.calls)), ("partly_done", ("bc_write", "bc", "done"), 1))
        self.assertFalse(done["live"])                           # decided: nothing to confirm
        # the token is spent: sent again it is no confirm (and the key is used anyway)
        again = owner.post("/api/autopilot/items/ap_own/decision",
                           json={"decision": "accept", "confirm_token": token}).json()
        self.assertEqual((again.get("confirm_token") is not None, len(self.bc.calls)),
                         (True, 1))
        # another item of the same owner is offered a token of its own: this item's
        # token does not confirm it
        q = ap.Queue(self.state.parent / "queue" / ap.QUEUE_FILE)
        with q.conn:
            q.add_item(queued("ap_two", OWNER[1]))
        q.close()
        other = owner.post("/api/autopilot/items/ap_two/decision",
                           json={"decision": "accept", "confirm_token": token}).json()
        self.assertTrue(other["confirm_token"])
        self.assertEqual(len(self.bc.calls), 1)

    def test_an_items_outcome_never_reads_an_unnamed_status_as_done_or_not_run(self) -> None:
        """WEB-036: done only when every step was; a step the write path does not name
        (or one still pending) makes the item unknown (check first), never partly done or
        nothing sent."""
        self.assertIsNone(ws.item_outcome([]))
        self.assertEqual(ws.item_outcome(["done", "done"]), "done")
        self.assertEqual(ws.item_outcome(["done", "unavailable"]), "partly_done")
        self.assertEqual(ws.item_outcome(["done", "failed"]), "partly_done")
        self.assertEqual(ws.item_outcome(["failed", "unavailable"]), "failed")
        self.assertEqual(ws.item_outcome(["refused", "unavailable"]), "refused")
        for odd in ("pending", "unknown", "sent", "", "DONE"):
            self.assertEqual(ws.item_outcome(["done", odd]), "unknown", odd)
            self.assertEqual(ws.item_outcome([odd]), "unknown", odd)

    def test_a_send_is_logged_only_when_the_email_itself_went_out(self) -> None:
        """A customer PO: the BC entry is done but the confirmation email is not. The
        item's outcome says partly done (never done, never "nothing was sent"); an email
        that cannot be confirmed says unknown; the work log records no send and opens no
        loop."""
        from operator_api import work_log as wl
        from operator_api.tests.test_work_log import Guard

        class Mail:
            def __init__(self, status):
                self.status, self.calls = status, 0

            def dispatch(self, kind, sender, payload):
                self.calls += 1
                return {"status": self.status, "http_status": 500 if self.status == "failed"
                        else 202, "missing": []}

        self.backend.work_log = wl.WorkLog(None, guard=lambda: Guard())
        # four live accepts of two actions each: this workflow's own hourly cap is raised
        # in a copy of the shipped rules (the shipped limits are unchanged and enforced)
        rules = json.loads(aw.RULES_FILE.read_text("utf-8"))
        rules["rate_limits"]["workflows"][FLOW] = {"per_hour": 20, "per_day": 40}
        rules_file = self.state.parent / "rules.json"
        rules_file.write_text(json.dumps(rules), "utf-8")
        patcher = patch.object(aw, "RULES_FILE", rules_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        owner = self.client(OWNER)
        self.pass_fresh_gate()
        owner.post("/api/autopilot/opt-in", json={"workflow": FLOW, "on": True})
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        self.go_live_file("bc")             # the owner's go-live files (BC, mail)
        self.go_live_file("mail")
        owner.put(f"/api/autopilot/workflows/{FLOW}/mode", json={"mode": "draft"})

        class FromMailbox(wa.MailAdapter):
            """The owner's mailbox as the sender (the fixture's owner is an object id)."""
            box: str | None = None

            def request(self, action):
                if self.box:
                    action = {**action, "target": {"sender": self.box}}
                return super().request(action)

        def accept(item_id, mail_status, mailbox=OWNER[0]):
            mail = Mail(mail_status)
            adapter = FromMailbox(mail, environ=self.backend.writes_environ)
            adapter.box = mailbox
            self.backend.autopilot_adapters = {
                "bc_write": wa.BCAdapter(self.bc, environ=ENV), "send_mail": adapter}
            self.bc.answers = [(201, {"id": NEW_ID, "@odata.etag": 'W/"1"'})]
            return self.accept_live(owner, item_id)["decision"], mail.calls

        q = ap.Queue(self.state.parent / "queue" / ap.QUEUE_FILE)
        with q.conn:
            q.add_item(queued("ap_two", OWNER[1]))
            q.add_item(queued("ap_three", OWNER[1]))
            q.add_item(queued("ap_four", OWNER[1]))
        q.close()
        # an email refused before sending (the item names no sender mailbox) is no send
        decision, calls = accept("ap_own", "sent", mailbox=None)
        self.assertEqual((decision["outcome"], decision["sent"], calls),
                         ("partly_done", False, 0))
        self.assertEqual(steps(decision)[0], ("bc_write", "bc", "done"))
        self.assertIn(steps(decision)[1][2], ws.NOT_RUN)
        decision, calls = accept("ap_two", "failed")
        self.assertEqual((decision["outcome"], decision["sent"], calls, steps(decision)),
                         ("partly_done", False, 1, [("bc_write", "bc", "done"),
                                                    ("send_mail", "mail", "failed")]),
                         decision)
        # an email whose send cannot be confirmed: check before retrying
        decision, calls = accept("ap_four", "unknown")
        self.assertEqual((decision["outcome"], decision["sent"], calls, steps(decision)),
                         ("unknown", False, 1, [("bc_write", "bc", "done"),
                                                ("send_mail", "mail", "unknown")]), decision)
        events = owner.get("/api/work-log").json()["events"]
        self.assertEqual([e["verb"] for e in events], ["accepted", "accepted", "accepted"])
        self.assertTrue(all(e.get("loop") is None for e in events))
        # control: the same entry with the email delivered is a send that waits for a reply
        decision, calls = accept("ap_three", "sent")
        self.assertEqual((decision["outcome"], decision["sent"], calls), ("done", True, 1))
        events = owner.get("/api/work-log").json()["events"]
        self.assertEqual(sorted(e["verb"] for e in events),
                         ["accepted", "accepted", "accepted", "accepted", "sent"])
        self.assertEqual(next(e for e in events if e["verb"] == "sent")["loop"]["state"],
                         "waiting")
        # AP-11: the same decisions as recorded before ``sent`` and ``steps`` were kept
        # (outcomes only) read by the outcome of the item's own email step: only the
        # delivered one is a send; the BC entry done first never counts as one
        path = self.backend._apa_state() / ws.DECISIONS_FILE
        rows = ws.Journal(path).rows()
        path.unlink()
        legacy = ws.Journal(path)
        with legacy.locked() as held:
            for row in rows:
                held.append({k: v for k, v in row.items()
                             if k not in ("sent", "steps", "seq", "prev", "hash")})
        sent = {i["item_id"]: (i["decision"] or {}).get("sent")
                for i in owner.get("/api/autopilot").json()["items"]
                if i["item_id"] in ("ap_own", "ap_two", "ap_four", "ap_three")}
        self.assertEqual(sent, {"ap_own": False, "ap_two": False, "ap_four": False,
                                "ap_three": True})
        self.assertFalse(ws.decision_sent({"decision": "accept"}, {}))
        self.assertFalse(ws.decision_sent({"decision": "accept", "outcomes": "done"}, {}))


class SupersedeDraftless(unittest.TestCase):
    def test_draftless_items_are_set_aside_and_come_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            q = ap.Queue(Path(tmp) / ap.QUEUE_FILE)
            with q.conn:
                empty = queued("ap_empty", OWNER[1])
                empty["body"]["prepared"] = {"summary": "Nothing to act on."}
                q.add_item(empty)
                q.add_item(ReplyFromLine.card("ap_card", OWNER[1]))
            self.assertEqual(q.supersede_draftless("2026-11-18T00:00:00Z")["items"], 1)
            self.assertEqual(len(q.items()), 2)                 # a dry run changes nothing
            self.assertEqual(q.supersede_draftless("2026-11-18T00:00:00Z", True)["items"], 1)
            self.assertEqual([i["item_id"] for i in q.items()], ["ap_card"])
            self.assertEqual([i["item_id"] for i in q.items(states=("superseded",))],
                             ["ap_empty"])
            self.assertEqual(q.unsupersede(True), 1)
            self.assertEqual(len(q.items()), 2)
            q.close()

    def test_an_item_that_informs_or_is_no_reply_is_never_set_aside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            q = ap.Queue(Path(tmp) / ap.QUEUE_FILE)
            with q.conn:
                empty = queued("ap_empty", OWNER[1])
                empty["body"]["prepared"] = {"summary": "X is waiting on a reply."}
                q.add_item(empty)
                lines = queued("ap_lines", OWNER[1])
                lines["body"]["prepared"] = {"summary": "Order status",
                                             "records": ["SalesOrder:1"]}
                q.add_item(lines)
                hold = queued("ap_hold", OWNER[1])
                hold["autopilot"] = "qc_hold"
                hold["body"]["prepared"] = {"summary": "Lot 7 is on hold"}
                q.add_item(hold)
            replies = [x["id"] for x in ap.load_registry(None)["autopilots"]
                       if x.get("done_when") == "owner_replied"]
            got = q.supersede_draftless("2026-11-18T00:00:00Z", True, replies)
            self.assertEqual(got["items"], 1)
            self.assertEqual([i["item_id"] for i in q.items(states=("superseded",))],
                             ["ap_empty"])
            q.close()


if __name__ == "__main__":
    unittest.main()



class TodoItems(unittest.TestCase):
    """Things to do by hand (the assistant's ``todos``: a system with no write path, a
    look's question): an item holding only those is a ``todo`` card for its person alone;
    "done" records their word and runs nothing through the write path; a reply card carries
    its to-dos too; a to-do with no text is none (no empty card)."""

    client = AutopilotArea.client

    def setUp(self) -> None:
        AutopilotArea.setUp(self)
        todo = {"kind": "job", "jobs": [], "summary": "PO 5416 is 6 days late",
                "todos": [{"system": "sage", "record": "PO 5416", "what": "Move the date"},
                          {"system": "", "record": "", "what": "Ask whether it ships split"}]}
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            for item_id, owner, prepared in (
                    ("ap_todo", OWNER[1], todo), ("ap_todo2", OWNER[1], todo),
                    ("ap_todo_sam", COLLEAGUE[1], todo),
                    ("ap_blank", OWNER[1], {**todo, "todos": [{"system": "bc", "what": " "}]})):
                item = queued(item_id, OWNER[1] if owner == OWNER[1] else owner)
                item["body"]["prepared"] = prepared
                q.add_item(item)
            card = ReplyFromLine.card("ap_card_todo", OWNER[1])
            card["body"]["prepared"]["todos"] = [{"system": "filemaker", "record": "AO-1",
                                                  "what": "Note the new date"}]
            q.add_item(card)
        q.close()

    def listed(self, client) -> dict:
        got = client.get("/api/autopilot/drafts")
        self.assertEqual(got.status_code, 200, got.text)
        return {i["item_id"]: i for i in got.json()["items"]}

    def test_a_todo_card_is_its_persons_and_done_writes_nothing(self) -> None:
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        items = self.listed(owner)
        self.assertEqual(items["ap_todo"]["kind"], "todo")
        self.assertEqual(items["ap_todo"]["summary"], "PO 5416 is 6 days late")
        self.assertEqual([t["what"] for t in items["ap_todo"]["todos"]],
                         ["Move the date", "Ask whether it ships split"])
        self.assertEqual(items["ap_todo"]["todos"][0]["record"], "PO 5416")
        self.assertNotIn("ap_todo_sam", items)                   # another person's
        self.assertNotIn("ap_blank", items)                      # no text: no to-do
        self.assertEqual(items["ap_card_todo"]["kind"], "your_call")
        self.assertEqual(items["ap_card_todo"]["todos"][0]["what"], "Note the new date")
        self.assertNotIn("ap_todo", self.listed(sam))
        self.assertEqual(sam.post("/api/autopilot/items/ap_todo/decision",
                                  json={"decision": "accept"}).status_code, 404)
        self.assertEqual(owner.post("/api/autopilot/items/ap_todo/decision",
                                    json={"decision": "accept", "text": "x"}).status_code, 422)
        calls = len(self.bc.calls)
        done = owner.post("/api/autopilot/items/ap_todo/decision", json={"decision": "accept"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["decision"]["outcome"], "done")
        self.assertIsNone(done.json().get("confirm_token"))
        self.assertEqual(len(self.bc.calls), calls)                 # nothing written
        rows = [json.loads(x) for x in (self.state / ws.DECISIONS_FILE).read_text().splitlines()]
        row = next(r for r in rows if r["item_id"] == "ap_todo")
        self.assertEqual((row["decision"], row["closed"], row["steps"]),
                         ("accept", "person_done", []))
        self.assertEqual(owner.post("/api/autopilot/items/ap_todo2/decision",
                                    json={"decision": "reject"}).status_code, 200)
        items = self.listed(owner)
        self.assertNotIn("ap_todo", items)
        self.assertNotIn("ap_todo2", items)


class NeverSender(unittest.TestCase):
    """``POST /api/autopilot/never``: a person says never (or again) to prepare anything for
    them from one sender; only their own list changes, and nothing is decided or written."""

    client = AutopilotArea.client

    def setUp(self) -> None:
        AutopilotArea.setUp(self)
        q = ap.Queue(self.backend.autopilot_queue)
        mine = ReplyFromLine.card("ap_a", OWNER[1])
        mine["body"]["prepared"].pop("your_call")
        mine["body"]["prepared"]["draft"]["body"] = "Thanks, noted."
        other = json.loads(json.dumps(mine))
        other.update(item_id="ap_b", event_id="e-ap_b")
        other["body"]["happened"]["sender"] = "other@supplier.example"
        theirs = json.loads(json.dumps(mine))
        theirs.update(item_id="ap_c", event_id="e-ap_c", owner=COLLEAGUE[1],
                      audiences=["mail:" + COLLEAGUE[1]])
        with q.conn:
            for item in (mine, other, theirs):
                q.add_item(item)
        q.close()

    def listed(self, client) -> tuple[set, list]:
        got = client.get("/api/autopilot/drafts").json()
        return {i["item_id"] for i in got["items"]} & {"ap_a", "ap_b", "ap_c"}, \
            got["never_senders"]

    def test_never_hides_that_senders_items_for_that_person_only_and_is_undone(self) -> None:
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        self.assertEqual(self.listed(owner), ({"ap_a", "ap_b"}, []))
        got = owner.post("/api/autopilot/never",
                         json={"sender": " Buyer@Customer.example ", "never": True})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertIn("ap_b", [i["item_id"] for i in got.json()["items"]])
        self.assertNotIn("ap_a", [i["item_id"] for i in got.json()["items"]])
        self.assertEqual(self.listed(owner), ({"ap_b"}, ["buyer@customer.example"]))
        self.assertEqual(self.listed(sam), ({"ap_c"}, []))
        # never needs a sender on the person's own list
        self.assertEqual(sam.post("/api/autopilot/never", json={
            "sender": "other@supplier.example", "never": True}).status_code, 404)
        self.assertEqual(owner.post("/api/autopilot/never", json={
            "sender": "buyer@customer.example", "never": "yes"}).status_code, 422)
        back = owner.post("/api/autopilot/never",
                          json={"sender": "buyer@customer.example", "never": False})
        self.assertEqual(back.status_code, 200, back.text)
        self.assertEqual(self.listed(owner), ({"ap_a", "ap_b"}, []))
        self.assertFalse((self.state / ws.DECISIONS_FILE).exists())
        self.assertEqual(self.bc.calls, [])


class AlertCards(unittest.TestCase):
    """An approval asked of the person, a QC hold or a Mission Control question sits in
    their Assist list as an alert: what happened, why, where it is decided and its records;
    only Later and Not needed act on it here, and nothing is written."""

    client = AutopilotArea.client

    def setUp(self) -> None:
        AutopilotArea.setUp(self)
        q = ap.Queue(self.backend.autopilot_queue)
        task = 'MCTask:mission_control.tasks:{"id":"TASK-1","scope":"live"}'
        approval = ReplyFromLine.card("ap_appr", OWNER[1])
        approval.update(autopilot="approval_request", title="Approval requested: TASK-1")
        approval["body"]["prepared"] = {
            "summary": "Approval requested for TASK-1: merge gate.",
            "reasons": ["no earlier approval of this task"], "evidence": [{"object_id": task}],
            "recommendation": "review",
            "mc": {"link": "https://mc.plxcustomer.io/tasks/TASK-1"},
            "action": {"kind": "mc_approval", "gate": "apg_secret1",
                       "note": "Decide in Mission Control."}}
        hold = ReplyFromLine.card("ap_hold", OWNER[1])
        hold.update(autopilot="qc_hold", title="QC hold: Lot L-1")
        hold["body"]["prepared"] = {
            "summary": "Lot L-1 is on-hold; it holds up 1 open order.", "lot": "L-1",
            # a link not on MC's own task pages is never shown
            "mc": {"link": "https://elsewhere.example/tasks/TASK-1"},
            "affected": [{"customer": "Three Ships", "order_number": "OR-9",
                          "object_id": 'SalesOrder:portal.app_sales_order:{"id":"s1"}'}]}
        theirs = json.loads(json.dumps(approval))
        theirs.update(item_id="ap_appr2", event_id="e-ap_appr2", owner=COLLEAGUE[1],
                      audiences=["mail:" + COLLEAGUE[1]])
        with q.conn:
            for item in (approval, hold, theirs):
                q.add_item(item)
        q.close()

    def alerts(self, client) -> dict:
        got = client.get("/api/autopilot/drafts")
        self.assertEqual(got.status_code, 200, got.text)
        return {i["item_id"]: i for i in got.json()["items"] if i["kind"] == "alert"}

    def test_alerts_are_the_persons_own_with_records_and_no_secret_and_set_aside_only(self):
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        mine = self.alerts(owner)
        self.assertEqual(set(mine), {"ap_appr", "ap_hold"})
        appr, hold = mine["ap_appr"], mine["ap_hold"]
        self.assertEqual((appr["channel"], appr["note"], appr["facts"]),
                         ("system", "Decide in Mission Control.",
                          ["Recommended: review", "no earlier approval of this task"]))
        self.assertEqual([(lk["label"], lk["href"]) for lk in appr["links"]][0],
                         ("Decide in Mission Control", "https://mc.plxcustomer.io/tasks/TASK-1"))
        self.assertEqual([lk["label"] for lk in appr["links"]],
                         ["Decide in Mission Control", "Open the task"])
        self.assertEqual(appr["links"][1]["href"], object_href(
            'MCTask:mission_control.tasks:{"id":"TASK-1","scope":"live"}'))
        self.assertEqual([lk["label"] for lk in hold["links"]], ["Open the order"])
        self.assertEqual(hold["links"][0]["href"],
                         "/object/SalesOrder/" + quote('SalesOrder:portal.app_sales_order:{"id":"s1"}', safe=""))
        self.assertEqual(hold["facts"], ["OR-9, Three Ships"])
        self.assertEqual(hold["links"][0]["label"], "Open the order")
        self.assertNotIn("apg_secret1", json.dumps(mine))
        self.assertEqual(set(self.alerts(sam)), {"ap_appr2"})
        got = owner.post("/api/autopilot/items/ap_hold/decision", json={"decision": "reject"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(set(self.alerts(owner)), {"ap_appr"})
        self.assertEqual(self.bc.calls, [])

    def test_every_record_link_on_a_card_opens_a_record_page(self):
        """The app opens a record at /object/<type>/<id> only; a link a handler stored
        before as /object/<id> is rebuilt from the id's own prefix, and no writer builds the
        one-part form again."""
        q = ap.Queue(self.backend.autopilot_queue)
        old = ReplyFromLine.card("ap_old", OWNER[1])
        old.update(autopilot="order_entry", title="Order entered")
        order = 'SalesOrder:portal.app_sales_order:{"id":"s2"}'
        old["body"]["prepared"] = {"summary": "PO 7 is entered as order OR-7.",
                                   "links": [{"label": "Open the order", "href": "/object/" + order},
                                             {"label": "Mission Control", "href": "https://mc.plxcustomer.io/x"}]}
        with q.conn:
            q.add_item(old)
        q.close()
        record = re.compile(r"^/object/([A-Za-z][A-Za-z0-9]*)/([^/?#]+)$")
        items = self.client(OWNER).get("/api/autopilot/drafts").json()["items"]
        seen = 0
        for item in items:
            for link in item["links"]:
                if not link["href"].startswith("/object/"):
                    continue
                got = record.match(link["href"])
                self.assertIsNotNone(got, link["href"])
                self.assertTrue(unquote(got.group(2)).startswith(got.group(1) + ":"), link["href"])
                seen += 1
        self.assertGreaterEqual(seen, 3)
        mine = {i["item_id"]: i for i in items}
        self.assertEqual(mine["ap_old"]["links"][0]["href"], object_href(order))
        root = Path(__file__).resolve().parents[2]
        for path in [*(root / "operating").glob("*.py"), *(root / "operator_api").glob("*.py")]:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(re.search(r'f"/object/\{(?!quote\()[^}]+\}"|"/object/" \+ (?!ref\b)', line),
                                  f"{path.name}:{n} builds a one-part record link: {line.strip()}")

    def test_one_card_per_lot_one_line_per_order_and_no_title_saying_the_summary_twice(self):
        """Items stored before the handler kept one card per lot: the newest item about a lot
        stands for it (an older one is not listed, decided or not); one line and one link per
        order however many systems hold a copy; the heading is the workflow, not the summary
        again."""
        order = 'SalesOrder:portal.app_sales_order:{"id":"s%s"}'
        summary = "Lot L-1 (NBY-005) is on-hold; it holds up 3 open orders."
        newer = ReplyFromLine.card("ap_hold2", OWNER[1])
        newer.update(autopilot="qc_hold", title="QC hold or fail: " + summary,
                     created_at="2099-01-01T00:00:00Z")
        newer["body"]["label"] = "QC hold or fail"
        newer["body"]["prepared"] = {
            "summary": summary, "lot": "l-1",
            "affected": [{"customer": "Three Ships", "order_number": "OR-9",
                          "object_id": order % 1},
                         {"customer": "THREE SHIPS INC.", "order_number": "or 9",
                          "object_id": order % 2},
                         {"customer": "Soapbox", "order_number": "OR-10",
                          "object_id": order % 3}]}
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(newer)
        q.close()
        owner = self.client(OWNER)
        mine = self.alerts(owner)
        self.assertEqual(set(mine), {"ap_appr", "ap_hold2"})
        hold = mine["ap_hold2"]
        # the stored summary counted each copy; the card counts the orders it lists
        self.assertEqual((hold["subject"], hold["summary"]), (
            "QC hold or fail", summary.replace("3 open orders", "2 open orders")))
        self.assertEqual(hold["facts"], ["OR-9, Three Ships", "OR-10, Soapbox"])
        self.assertEqual([lk["label"] for lk in hold["links"]],
                         ["Open order OR-9", "Open order OR-10"])
        # set aside, the lot's card is gone: the older item never comes back in its place
        got = owner.post("/api/autopilot/items/ap_hold2/decision", json={"decision": "reject"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(set(self.alerts(owner)), {"ap_appr"})

    def newer_hold(self) -> None:
        """A newer item about lot L-1 (another batch record), with the engine's settle note
        that it replaces ``ap_hold``."""
        newer = json.loads(json.dumps(ReplyFromLine.card("ap_hold3", OWNER[1])))
        newer.update(autopilot="qc_hold", title="QC hold: Lot L-1",
                     created_at="2099-01-01T00:00:00Z")
        newer["body"]["prepared"] = {
            "summary": "Lot L-1 is on-hold; it holds up 1 open order.", "lot": "L-1",
            "affected": [{"customer": "Three Ships", "order_number": "OR-9",
                          "object_id": 'SalesOrder:portal.app_sales_order:{"id":"s1"}'}]}
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(newer)
            q.settle_item("ap_hold", "superseded", "same_subject", "2099-01-01T00:00:01Z")
            q.set_meta(autopilot_queue.SUPERSEDED_BY, {"ap_hold": "ap_hold3"})
        q.close()

    def test_a_snooze_carries_to_the_newer_item_about_the_same_lot(self):
        owner = self.client(OWNER)
        later = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        got = owner.post("/api/autopilot/items/ap_hold/decision",
                         json={"decision": "snooze", "until": later})
        self.assertEqual(got.status_code, 200, got.text)
        self.newer_hold()
        self.assertEqual(set(self.alerts(owner)), {"ap_appr"})

    def test_a_pass_to_a_colleague_carries_to_the_newer_item_about_the_same_lot(self):
        from operator_api import view_as as va
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(OWNER[1], OWNER[0]), (COLLEAGUE[1], COLLEAGUE[0])],
            identity=lambda oid, seconds: None, excluded=lambda: (lambda oid: False))
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        key = owner.get("/api/autopilot/colleagues").json()["people"][0]["key"]
        got = owner.post("/api/autopilot/items/ap_hold/hand", json={"to": key})
        self.assertEqual(got.status_code, 200, got.text)
        self.newer_hold()
        self.assertEqual(set(self.alerts(sam)), {"ap_appr2", "ap_hold3"})
        self.assertEqual(self.alerts(sam)["ap_hold3"]["handed_from"], "Ana")
        self.assertEqual(self.alerts(owner)["ap_hold3"]["handed_to"], "Sam")

    def test_a_person_the_roster_names_only_by_id_is_named_from_the_staff_list(self):
        """Named from the staff list only while the people exclusion clears them; nobody is
        added to the list by it."""
        from operator_api import view_as as va
        gone = {"sam": False}
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(OWNER[1], OWNER[0]), (COLLEAGUE[1], COLLEAGUE[0]),
                                ("66666666-6666-4666-8666-666666666666", "zed@plx.example")],
            identity=lambda oid, seconds: None,
            excluded=lambda: (lambda oid: gone["sam"] and oid == COLLEAGUE[1]))
        owner = self.client(OWNER)
        people = {p["id"]: p["name"] for p in owner.get("/api/autopilot").json()["people"]}
        self.assertEqual(people.get(COLLEAGUE[1]), "Sam")
        self.assertNotIn("66666666-6666-4666-8666-666666666666", people)
        # excluded after the staff cache is built (no cache clear): the read-time check names nobody
        gone["sam"] = True
        people = {p["id"]: p["name"] for p in owner.get("/api/autopilot").json()["people"]}
        self.assertEqual(people.get(COLLEAGUE[1], ""), "")
        self.assertEqual(self.backend.view_as.names_of([COLLEAGUE[1]]), {})

    def test_names_of_names_nobody_when_the_exclusion_check_raises(self):
        from operator_api import view_as as va

        def boom(oid):
            raise RuntimeError("exclusion unavailable")
        v = va.ViewAs(candidates=lambda: [(COLLEAGUE[1], COLLEAGUE[0])],
                      identity=lambda oid, seconds: None, excluded=lambda: boom)
        self.assertEqual(v.names_of([COLLEAGUE[1]]), {})
        v2 = va.ViewAs(candidates=lambda: [(COLLEAGUE[1], COLLEAGUE[0])],
                       identity=lambda oid, seconds: None, excluded=lambda: (_ for _ in ()).throw(OSError("x")))
        self.assertEqual(v2.names_of([COLLEAGUE[1]]), {})

    def test_an_alert_passed_to_a_colleague_is_on_their_list_to_set_aside_only(self):
        from operator_api import view_as as va
        gone = {"sam": False}
        self.backend.view_as = va.ViewAs(
            candidates=lambda: [(OWNER[1], OWNER[0]), (COLLEAGUE[1], COLLEAGUE[0])],
            identity=lambda oid, seconds: None,
            excluded=lambda: (lambda oid: gone["sam"] and oid == COLLEAGUE[1]))
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        people = owner.get("/api/autopilot/colleagues").json()["people"]
        self.assertEqual([p["name"] for p in people], ["Sam"])
        self.assertNotIn(COLLEAGUE[1], json.dumps(people))
        key = people[0]["key"]
        hand = lambda who, item: who.post(  # noqa: E731
            f"/api/autopilot/items/{item}/hand", json={"to": key})
        self.assertEqual(hand(sam, "ap_hold").status_code, 404)        # not on Sam's list
        self.assertEqual(hand(owner, "ap_hold").status_code, 200)
        self.assertEqual(self.alerts(owner)["ap_hold"]["handed_to"], "Sam")
        theirs = self.alerts(sam)
        self.assertEqual(set(theirs), {"ap_appr2", "ap_hold"})
        self.assertEqual(theirs["ap_hold"]["handed_from"], "Ana")
        for decision in ("accept", "edit"):
            got = sam.post("/api/autopilot/items/ap_hold/decision",
                           json={"decision": decision, "text": "ok"})
            self.assertIn(got.status_code, (409, 422), got.text)
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(ReplyFromLine.card("ap_call", OWNER[1]))
        q.close()
        self.assertEqual(hand(owner, "ap_call").status_code, 422)     # only an alert
        gone["sam"] = True                                              # excluded since
        self.assertEqual(hand(owner, "ap_appr").status_code, 404)
        self.assertEqual(owner.get("/api/autopilot/colleagues").json()["people"], [])
        gone["sam"] = False
        got = sam.post("/api/autopilot/items/ap_hold/decision", json={"decision": "reject"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertNotIn("ap_hold", self.alerts(owner))
        self.assertNotIn("ap_hold", self.alerts(sam))
        self.assertEqual(self.bc.calls, [])


class PooledItems(unittest.TestCase):
    """An item no record names a person for (a late PO nobody is named buyer of) is one
    item on the role's list: each member sees what happened and its records, never its
    prepared message, until one takes it; then it is theirs alone and gone from the
    others'. Someone outside the role never sees it."""

    client = AutopilotArea.client
    OUTSIDER = ("zed@plx.example", "66666666-6666-4666-8666-666666666666")

    def setUp(self) -> None:
        AutopilotArea.setUp(self)
        path = Path(self.backend.autopilot_queue)
        (path.parent / autopilot_queue.ROLES_FILE).write_text(json.dumps(
            {"roles": {"purchasing": [OWNER[1], COLLEAGUE[1]]}}))
        item = queued("ap_pool", "pool:purchasing")
        item.update(autopilot="po_followup", title="Supplier PO without confirmation: PO P-80",
                    audiences=[])
        item["body"]["prepared"] = {
            "summary": "PO P-80 was ordered 7 days ago; no confirmation from the supplier.",
            "draft": {"channel": "email", "to": [{"address": "anna@supplier.example"},
                                                 {"address": COLLEAGUE[0]}],
                      "subject": "PO P-80", "body": "Hi Anna, could you confirm PO P-80?"},
            "links": [{"label": "Open the PO",
                       "href": object_href('PurchaseOrder:portal.app_purchase_order:{"id":"p80"}')}]}
        q = ap.Queue(path)
        with q.conn:
            q.add_item(item)
        q.close()
        from integrations.people_exclusion import PeopleExclusion
        from operating.chunking.permissions import Exclusion
        self.backend.autopilot_people = Exclusion(
            PeopleExclusion(["00000000-0000-4000-8000-0000000000e1"]),
            directory={OWNER[0]: OWNER[1], COLLEAGUE[0]: COLLEAGUE[1]},
            internal_domains=["plx.example"])

    def listed(self, client) -> dict:
        got = client.get("/api/autopilot/drafts")
        self.assertEqual(got.status_code, 200, got.text)
        return {i["item_id"]: i for i in got.json()["items"]}

    def test_one_item_on_the_roles_list_until_one_member_takes_it(self):
        ana, sam, zed = self.client(OWNER), self.client(COLLEAGUE), self.client(self.OUTSIDER)
        for who in (ana, sam):
            card = self.listed(who)["ap_pool"]
            self.assertEqual((card["kind"], card["pooled"], card["body"]), ("alert", "purchasing", ""))
            self.assertNotIn("could you confirm", json.dumps(card))
            self.assertEqual([lk["label"] for lk in card["links"]], ["Open the PO"])
        self.assertNotIn("ap_pool", self.listed(zed))
        self.assertEqual(zed.post("/api/autopilot/items/ap_pool/take").status_code, 404)
        got = sam.post("/api/autopilot/items/ap_pool/take")
        self.assertEqual(got.status_code, 200, got.text)
        mine = {i["item_id"]: i for i in got.json()["items"]}["ap_pool"]
        self.assertEqual((mine["kind"], mine["pooled"]), ("reply", ""))
        self.assertIn("could you confirm", mine["body"])
        # addressed again for the taker: their own address is never a recipient
        self.assertEqual(mine["to"], ["anna@supplier.example"])
        self.assertNotIn("ap_pool", self.listed(ana))
        self.assertIn(ana.post("/api/autopilot/items/ap_pool/take").status_code, (404, 409))
        q = ap.Queue(Path(self.backend.autopilot_queue))
        self.assertEqual(q.item("ap_pool")["owner"], COLLEAGUE[1])
        self.assertFalse(q.take("ap_pool", "pool:purchasing", OWNER[1]))   # taken already
        q.close()


class PooledTakeRace(unittest.TestCase):
    """Two people taking one pooled item at the same moment: exactly one gets it."""

    def test_two_takers_at_once_one_gets_it(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ap.QUEUE_FILE
            q = ap.Queue(path)
            with q.conn:
                for n in range(20):
                    q.add_item(queued(f"ap_p{n}", "pool:purchasing"))
            q.close()
            for n in range(20):
                start, got = threading.Barrier(2), {}

                def take(who: str) -> None:
                    mine = ap.Queue(path)
                    try:
                        start.wait()
                        got[who] = mine.take(f"ap_p{n}", "pool:purchasing", who)
                    finally:
                        mine.close()
                threads = [threading.Thread(target=take, args=(w,)) for w in (OWNER[1], COLLEAGUE[1])]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(sorted(got.values()), [False, True], got)
                q = ap.Queue(path)
                winner = next(w for w, ok in got.items() if ok)
                self.assertEqual(q.item(f"ap_p{n}")["owner"], winner)
                q.close()


class ReplyFromLine(unittest.TestCase):
    """``POST /api/autopilot/items/{id}/reply-from-line``: a "your call" card of the reader's
    own gets its reply written from their line (figures only from the line and the card's
    material, the writer's screens read again), kept as their edit; nothing is sent."""

    client = AutopilotArea.client

    def setUp(self) -> None:
        AutopilotArea.setUp(self)
        from operating import autopilot_drafter as ad
        from operating.tests.test_autopilot_drafter import BIN, HOME, Model, People
        self.ad, self.Model, self.People, self.BIN, self.HOME = ad, Model, People, BIN, HOME
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(self.card("ap_card", OWNER[1]))
            q.add_item(self.card("ap_card_sam", COLLEAGUE[1]))
        q.close()
        self.backend.autopilot_people = People()

    @staticmethod
    def card(item_id: str, owner: str) -> dict:
        item = queued(item_id, owner)
        text = "Hi Ana, can you do 12.50 a case on PO P-1 if we take 400 cases?"
        item["body"]["prepared"] = {
            "draft": {"channel": "email", "to": [{"address": "buyer@customer.example"}],
                      "subject": "RE: PO P-1", "body": ""},
            "message": {"text": text}, "drafted_by": "model",
            "your_call": {"ask": "Do you agree to 12.50 a case for 400 cases?",
                          "facts": ["Their PO P-1."],
                          "material": {"reader": "Ana", "channel": "mail", "subject": "PO P-1",
                                       "sender": "Buyer", "text": text, "account": [],
                                       "lines": [], "address": OWNER[0], "relation": ""},
                          "event": {"object_id": "Email:1", "channel": "mail",
                                    "sender": "buyer@customer.example", "subject": "PO P-1",
                                    "cleared": True, "records": []}}}
        return item

    def writer(self, reply: str):
        model = self.Model(reply)
        return self.ad.Writer("opus", binary=self.BIN, home=self.HOME, seconds=5,
                              per_cycle=4, call=model), model

    def ask(self, client, item_id: str, line: str):
        return client.post(f"/api/autopilot/items/{item_id}/reply-from-line",
                           json={"line": line})

    def test_the_reply_is_kept_as_the_persons_edit_and_nothing_is_sent(self) -> None:
        self.backend.autopilot_writer, model = self.writer(
            "Hi,\n\nYes, 12.50 a case works for 400 cases on PO P-1.\n\nAna")
        owner = self.client(OWNER)
        got = self.ask(owner, "ap_card", "yes ok for 400")
        self.assertEqual(got.status_code, 200, got.text)
        item = got.json()
        self.assertEqual(item["state"], "edited")
        self.assertIn("12.50 a case works", item["decision"]["text"])
        self.assertFalse(item["decision"]["sent"])
        self.assertEqual(self.bc.calls, [])
        self.assertIn("yes ok for 400", model.prompts[0])
        # the page is shown the question and facts, never the kept thread or event
        shown = item["prepared"]["your_call"]
        self.assertEqual(set(shown), {"ask", "facts"})
        listed = next(i for i in owner.get("/api/autopilot").json()["items"]
                      if i["item_id"] == "ap_card")
        self.assertEqual(set(listed["prepared"]["your_call"]), {"ask", "facts"})

    def test_a_card_lists_a_reply_per_choice_and_never_its_material(self) -> None:
        item = self.card("ap_opts", OWNER[1])
        item["body"]["prepared"]["your_call"]["options"] = [
            {"label": "Yes", "reply": "Yes, 12.50 a case for 400 works."},
            {"label": "No", "reply": "Sorry, we cannot go below list."},
            {"label": "broken", "reply": ""}, "junk"]
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.add_item(item)
        q.close()
        owner = self.client(OWNER)
        got = next(i for i in owner.get("/api/autopilot/drafts").json()["items"]
                   if i["item_id"] == "ap_opts")
        self.assertEqual((got["kind"], got["body"]), ("your_call", ""))
        self.assertEqual(got["options"], [
            {"label": "Yes", "reply": "Yes, 12.50 a case for 400 works."},
            {"label": "No", "reply": "Sorry, we cannot go below list."}])
        listed = next(i for i in owner.get("/api/autopilot").json()["items"]
                      if i["item_id"] == "ap_opts")
        self.assertEqual(set(listed["prepared"]["your_call"]), {"ask", "facts", "options"})
        self.assertNotIn("can you do 12.50 a case on PO P-1",
                         json.dumps(listed["prepared"]["your_call"]))
        # someone else never sees it
        self.assertNotIn("ap_opts", self.client(COLLEAGUE).get("/api/autopilot/drafts").text)

    def test_the_model_comes_from_the_servers_environment(self) -> None:
        from unittest import mock
        self.backend.autopilot_writer = None
        owner = self.client(OWNER)
        self.assertEqual(self.ask(owner, "ap_card", "yes").status_code, 409)   # unset
        self.backend.writes_environ = {**self.backend.writes_environ,
                                       self.ad.MODEL_ENV: "opus"}
        writer, model = self.writer("Yes, 12.50 a case works for 400 cases.")
        named: list[str] = []

        def make(name, **kw):
            named.append(name)
            return writer
        with mock.patch.object(self.ad, "Writer", make):
            got = self.ask(owner, "ap_card", "yes ok for 400")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(named, ["opus"])
        self.assertEqual(len(model.prompts), 1)

    def test_the_api_units_name_the_model_and_the_autopilots_claude_home(self) -> None:
        root = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
        for name in ("plx-api.service", "plx-api@.service"):    # the warm switch needs both alike
            text = (root / name).read_text()
            self.assertIn(f"Environment={self.ad.MODEL_ENV}=opus", text, name)
            self.assertIn("Environment=PLX_CHAT_ASK_CLAUDE_HOME=/var/lib/plx-autopilot/claude-home",
                          text, name)
            self.assertNotIn("~/.claude", text.replace("never an operator's ~/.claude", ""))

    def test_drafts_list_newest_first_without_thread_text_or_answered_mail(self) -> None:
        owner = self.client(OWNER)
        thread = "The whole earlier thread: quoted history nobody asked to see again."
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            older = self.card("ap_older", OWNER[1])
            older["created_at"] = older["due_at"] = "2026-11-01T10:00:00Z"
            older["body"]["prepared"]["your_call"]["material"]["text"] = thread
            q.add_item(older)
            bare = queued("ap_nodraft", OWNER[1])
            bare["body"]["prepared"] = {"summary": "Nothing to act on."}
            q.add_item(bare)
        q.close()
        got = owner.get("/api/autopilot/drafts")
        self.assertEqual(got.status_code, 200, got.text)
        ids = [i["item_id"] for i in got.json()["items"]]
        self.assertEqual(ids[-1], "ap_older")                     # newest first
        self.assertNotIn("ap_sam", ids)                           # mine only
        self.assertNotIn("ap_nodraft", ids)                       # nothing prepared: not listed
        first = next(i for i in got.json()["items"] if i["item_id"] == "ap_card")
        self.assertEqual((first["kind"], first["channel"], first["to"]),
                         ("your_call", "mail", ["buyer@customer.example"]))
        for path in ("/api/autopilot/drafts", "/api/autopilot", "/api/my-work"):
            self.assertNotIn("quoted history", owner.get(path).text, path)
        # the owner answered that conversation after the message: it is not listed
        q = ap.Queue(self.backend.autopilot_queue)
        with q.conn:
            q.conn.execute(
                "INSERT INTO events (event_id, event_type, object_id, object_type, seen_at, "
                "body) VALUES ('e-ap_card', 'message.received', 'Email:1', 'Email', "
                "'2026-11-01T09:00:00Z', ?)", (json.dumps(
                    {"at": "2026-11-01T09:00:00Z", "conversation_id": "conv-1"}),))
            q.conn.execute("INSERT INTO sent VALUES (?, ?, ?, ?)", (
                OWNER[1], ap._digest("thread", "conv-1"), "x", "2026-11-01T09:30:00Z"))
        q.close()
        ids = [i["item_id"] for i in owner.get("/api/autopilot/drafts").json()["items"]]
        self.assertNotIn("ap_card", ids)

    def test_a_line_reply_is_rate_limited_per_person_and_copy_checked(self) -> None:
        owner = self.client(OWNER)
        self.backend.autopilot_writer, _ = self.writer(
            "The real question is the price. Yes, 12.50 a case works.")
        got = self.ask(owner, "ap_card", "yes")
        self.assertEqual(got.status_code, 422, got.text)       # the copy rules refuse it
        self.assertIn("template", got.json()["error"]["message"])
        from operator_api import autopilot_area as aa
        self.backend.autopilot_writer, model = self.writer("Yes, 12.50 a case works.")
        # every attempt counts, the refused one above included (it called the model too)
        with patch.object(aa, "LINE_REPLIES_PER_HOUR", 2):
            self.assertEqual(self.ask(owner, "ap_card", "yes").status_code, 200)
            self.assertEqual(self.ask(owner, "ap_card", "yes").status_code, 429)
        self.assertEqual(len(model.prompts), 1)               # the limit stops the call
        # many at once never pass the last slot together (counted under one lock)
        from concurrent.futures import ThreadPoolExecutor
        with patch.object(aa, "LINE_REPLIES_PER_HOUR", 5):
            with ThreadPoolExecutor(8) as pool:
                codes = list(pool.map(lambda _: self.ask(owner, "ap_card", "yes").status_code,
                                      range(8)))
        self.assertEqual(sorted(codes), [200, 200, 200, 429, 429, 429, 429, 429])

    def test_a_refused_send_stays_prepared_and_a_teams_send_counts_as_sent(self) -> None:
        from operating import autopilot_outcomes as ao
        q = ap.Queue(self.backend.autopilot_queue)
        item = self.card("ap_teams", OWNER[1])
        prepared = item["body"]["prepared"]
        prepared.pop("your_call")
        prepared["teams_draft"] = {**prepared.pop("draft"), "channel": "teams", "body":
                                   "Yes, 12.50 a case works.", "conversation": "19:abc@thread.v2"}
        prepared["message"] = {"text": "Can you do 12.50?\r\n\r\nFrom: Earlier Person\r\n"
                                       "Sent: Monday\r\nThe quoted history below it."}
        with q.conn:
            q.add_item(item)
        q.close()
        owner = self.client(OWNER)
        listed = {i["item_id"]: i for i in owner.get("/api/autopilot/drafts").json()["items"]}
        # the message beside the reply is the newest one, the quoted thread cut
        self.assertEqual(listed["ap_teams"]["message"], "Can you do 12.50?")

        class Executor:
            status = "refused"

            def live_kinds(self, _item):
                return []

            def run(self, _item, _now, _approval):
                return [{"kind": "send_teams", "status": Executor.status,
                         "reasons": ["not_live"]}]

        with patch.object(type(self.backend), "_apa_executor", lambda *_a: Executor()):
            for status in ("refused", "failed", "killed"):
                Executor.status = status
                got = owner.post("/api/autopilot/items/ap_teams/decision",
                                 json={"decision": "accept"}).json()
                self.assertEqual((got["state"], got["decision"]["outcome"],
                                  got["decision"]["sent"]), ("waiting", status, False))
                ids = [i["item_id"] for i in owner.get("/api/autopilot/drafts").json()["items"]]
                self.assertIn("ap_teams", ids, status)     # still there to confirm again
                decision = self.backend._apa_decisions(self.backend._apa_state())["ap_teams"]
                self.assertEqual(ao.classify(dict(item, state="open"), decision,
                                             datetime.now(timezone.utc))[0], "waiting")
            Executor.status = "done"
            got = owner.post("/api/autopilot/items/ap_teams/decision",
                             json={"decision": "accept"}).json()
        self.assertEqual((got["state"], got["decision"]["sent"]), ("accepted", True))
        ids = [i["item_id"] for i in owner.get("/api/autopilot/drafts").json()["items"]]
        self.assertNotIn("ap_teams", ids)

    def test_a_teams_reply_from_a_line_can_be_confirmed(self) -> None:
        q = ap.Queue(self.backend.autopilot_queue)
        item = self.card("ap_teams", OWNER[1])
        prepared = item["body"]["prepared"]
        prepared["teams_draft"] = {**prepared.pop("draft"), "channel": "teams",
                                   "conversation": "19:abc@thread.v2", "post": False}
        item["body"]["prepared"]["your_call"]["event"]["channel"] = "teams"
        with q.conn:
            q.add_item(item)
        q.close()
        owner = self.client(OWNER)
        self.backend.autopilot_writer, _ = self.writer("Yes, 12.50 a case works.")
        self.assertEqual(self.ask(owner, "ap_teams", "yes").status_code, 200)
        got = owner.post("/api/autopilot/items/ap_teams/decision",
                         json={"decision": "accept"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertFalse(got.json()["decision"]["sent"])

    def test_a_model_reply_is_rewritten_from_a_line_and_never_shows_its_material(self) -> None:
        q = ap.Queue(self.backend.autopilot_queue)
        item = self.card("ap_redo", OWNER[1])
        prepared = item["body"]["prepared"]
        prepared["redo"] = prepared.pop("your_call")
        prepared["redo"].pop("ask"), prepared["redo"].pop("facts")
        prepared["draft"]["body"] = "Hi,\n\nLet me check the price.\n\nAna"
        plain = self.card("ap_plain", OWNER[1])
        plain["body"]["prepared"].pop("your_call")
        plain["body"]["prepared"]["draft"]["body"] = "Thanks, noted."
        with q.conn:
            q.add_item(item)
            q.add_item(plain)
        q.close()
        owner = self.client(OWNER)
        listed = {i["item_id"]: i for i in owner.get("/api/autopilot/drafts").json()["items"]}
        self.assertEqual((listed["ap_redo"]["kind"], listed["ap_redo"]["redo"]), ("reply", True))
        self.assertFalse(listed["ap_plain"]["redo"])
        shown = next(i for i in owner.get("/api/autopilot").json()["items"]
                     if i["item_id"] == "ap_redo")
        self.assertNotIn("redo", shown["prepared"])
        self.assertNotIn("material", json.dumps(shown))
        self.backend.autopilot_writer, model = self.writer(
            "Hi,\n\nYes, 12.50 a case works for 400 cases.\n\nAna")
        got = self.ask(owner, "ap_redo", "say yes to 12.50 for 400")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertIn("12.50 a case works", got.json()["decision"]["text"])
        self.assertFalse(got.json()["decision"]["sent"])
        self.assertIn("say yes to 12.50 for 400", model.prompts[0])
        again = {i["item_id"]: i for i in owner.get("/api/autopilot/drafts").json()["items"]}
        self.assertEqual((again["ap_redo"]["body"], again["ap_redo"]["edited"]),
                         ("Hi,\n\nYes, 12.50 a case works for 400 cases.\n\nAna", True))
        self.assertEqual(self.bc.calls, [])
        # a reply kept without its material: refused, and the model is never asked
        n = len(model.prompts)
        self.assertEqual(self.ask(owner, "ap_plain", "say yes").status_code, 422)
        self.assertEqual(len(model.prompts), n)
        # someone else's: not found
        self.assertEqual(self.ask(self.client(COLLEAGUE), "ap_redo", "yes").status_code, 404)

    def test_refusals_fail_closed(self) -> None:
        owner = self.client(OWNER)
        # not mine: not found, and the model is never asked
        self.backend.autopilot_writer, model = self.writer("Fine.")
        self.assertEqual(self.ask(owner, "ap_card_sam", "yes").status_code, 404)
        self.assertEqual(model.prompts, [])
        # an empty line, an item with a real draft
        self.assertEqual(self.ask(owner, "ap_card", "  ").status_code, 422)
        self.assertEqual(self.ask(owner, "ap_own", "yes").status_code, 422)
        # a figure in the reply that neither the line nor the message states
        self.backend.autopilot_writer, _ = self.writer("Yes, 11.75 a case is fine.")
        got = self.ask(owner, "ap_card", "yes but a bit lower")
        self.assertEqual(got.status_code, 422, got.text)
        self.assertIn("figure", got.json()["error"]["message"])
        # the people screen: an excluded sender never reaches the model
        self.backend.autopilot_people = self.People({"buyer@customer.example"})
        self.backend.autopilot_writer, model = self.writer("Yes.")
        self.assertEqual(self.ask(owner, "ap_card", "yes").status_code, 422)
        self.assertEqual(model.prompts, [])
        # no model set up on the server
        self.backend.autopilot_people = self.People()
        self.backend.autopilot_writer = None
        self.assertEqual(self.ask(owner, "ap_card", "yes").status_code, 409)
        # nothing was recorded for any refusal
        item = next(i for i in owner.get("/api/autopilot").json()["items"]
                    if i["item_id"] == "ap_card")
        self.assertEqual(item["state"], "waiting")
