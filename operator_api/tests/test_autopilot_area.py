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
        (path.parent / ap.GATE_FILE).write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
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
        (run / ap.GATE_FILE).write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
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
                           (f"/api/autopilot/workflows/{FLOW}", {"on": False}),
                           (f"/api/autopilot/workflows/{FLOW}", {"live": True, "role": ROLE}),
                           ("/api/autopilot/audit/k/undo", None)):
            self.assertEqual(sam.post(path, json=body).status_code, 403, path)
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

    def test_an_empty_page_says_no_workflow_passed_and_the_filter_offers_every_workflow(
            self) -> None:
        """Owner 2026-11-11: an empty Autopilot page did not say why, and the Workflow
        filter offered nothing but "Every workflow". With no workflow over its replay gate
        the page says so first; the filter offers the catalogue, scored or not. A
        workflow that passes keeps the plain notes."""
        from operating import workflows as wf
        from operator_api.autopilot_area import NO_GATE_PASSED
        path = self.state.parent / "empty" / ap.QUEUE_FILE
        ap.Queue(path).close()
        self.backend.autopilot_queue = path
        gate = path.parent / ap.GATE_FILE
        gate.write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER[1]], "workflows": {FLOW: {"judged": 40, "matched": 20}}}}}))
        area = self.client(OWNER).get("/api/autopilot").json()
        self.assertEqual(area["items"], [])
        self.assertEqual(area["notes"][0], NO_GATE_PASSED)
        self.assertEqual(area["known_workflows"], sorted(wf.names()))
        self.assertIn(FLOW, area["known_workflows"])
        # a passing workflow: the page is empty for another reason, not the gate
        gate.write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER[1]], "workflows": {FLOW: {"judged": 40, "matched": 37}}}}}))
        area = self.client(OWNER).get("/api/autopilot").json()
        self.assertNotIn(NO_GATE_PASSED, area["notes"])
        # an unreadable gate file passes nothing: said the same way (fail closed)
        gate.write_text("{not json")
        area = self.client(OWNER).get("/api/autopilot").json()
        self.assertEqual(area["notes"][0], NO_GATE_PASSED)

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

    def test_live_only_after_gate_and_owner_switch_then_undo_once(self) -> None:
        owner = self.client(OWNER)
        live = {"live": True, "role": ROLE}
        refused = owner.post(f"/api/autopilot/workflows/{FLOW}", json=live)
        self.assertEqual(refused.status_code, 409)      # not passed: stays a dry run
        self.pass_fresh_gate()
        row = next(w for w in owner.get("/api/autopilot").json()["workflows"]
                   if w["role"] == ROLE)
        self.assertEqual((row["state"], row["fresh"]["percent"], row["n"]),
                         ("passed_awaiting_owner", 93.8, 16))
        row = next(w for w in owner.post(f"/api/autopilot/workflows/{FLOW}", json=live)
                   .json()["workflows"] if w["role"] == ROLE)
        self.assertEqual(row["state"], "live_held")    # the host's master switch is off
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        # switched off: the accept is recorded killed, nothing written
        owner.post(f"/api/autopilot/workflows/{FLOW}", json={"on": False})
        self.assertEqual(owner.post("/api/autopilot/items/ap_own/decision",
                                    json={"decision": "accept"}).json()["decision"]["outcome"],
                         "killed")
        self.assertEqual(self.bc.calls, [])
        owner.post(f"/api/autopilot/workflows/{FLOW}", json={"on": True})
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
        owner.post(f"/api/autopilot/workflows/{FLOW}", json={"live": False, "role": ROLE})
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
        owner.post(f"/api/autopilot/workflows/{FLOW}", json={"live": True, "role": ROLE})
        self.backend.writes_environ = {**ENV, aw.MASTER_ENV: "1"}
        owner.post(f"/api/autopilot/workflows/{FLOW}", json={"on": True})

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


if __name__ == "__main__":
    unittest.main()

