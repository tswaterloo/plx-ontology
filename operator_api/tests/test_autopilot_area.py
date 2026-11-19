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
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_engine
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_autopilot_write import BC_COMPANY, NEW_ID, Transport, po_item
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

    def score_fresh(self, roles: dict) -> None:
        """A fresh scorer run of ``roles`` (role -> (members, {workflow: (judged, matched)}))
        admitted to the write gate, and the gate synced."""
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
                          lee_role: ([lee[1]], {lee_flow: (40, 38)})})
        owner, them = self.client(OWNER), self.client(lee)
        mine = owner.get("/api/autopilot").json()
        self.assertEqual([(o["workflow"], o["eligible"], o["on"]) for o in mine["my_opt_ins"]],
                         [(FLOW, True, False)])                     # off by default
        self.assertEqual(mine["my_opt_ins"][0]["score"], {
            "judged": 16, "matched": 15, "percent": 93.8, "need_judged": 15,
            "need_percent": 90.0})
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
