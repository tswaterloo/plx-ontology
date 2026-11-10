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

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_autopilot_write import BC_COMPANY, NEW_ID, Transport, po_item
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
        item = owner.post("/api/autopilot/items/ap_own/decision",
                          json={"decision": "accept"}).json()
        self.assertEqual(item["decision"]["outcome"], "done")
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


if __name__ == "__main__":
    unittest.main()


SAM2 = ("sam.ortiz@plx.example", "66666666-6666-4666-8666-666666666666")
LEE = ("lee@plx.example", "99999999-9999-4999-8999-999999999999")   # excluded


class AssistMode(unittest.TestCase):
    """``POST /api/autopilot/assist``: a typed line drafted as the reader; recipients
    cleared by the people exclusion (fail closed), facts only from the reader's gated Ask
    answer with their sources, nothing sent."""

    def setUp(self) -> None:
        from operating.chunking.permissions import Exclusion
        guard = PeopleExclusion([LEE[1]])
        directory = {a: o for a, o in (OWNER, COLLEAGUE, SAM2, LEE)}
        b = self.backend = MockBackend()
        b.assist_people = Exclusion(guard, directory=directory, internal_domains=["plx.example"])
        self.sent = [{"at": "2026-10-20T10:00:00Z", "to": ["rita@maple.example",
                                                          "orders@maple.example"], "cc": []},
                     {"at": "2026-10-19T10:00:00Z", "to": [COLLEAGUE[0]], "cc": []}]
        b.assist_sent = lambda oid: self.sent if oid == OWNER[1] else []
        b.assist_party = lambda name, side: {"@maple.example"} if name.lower() == "maple" \
            and side == "supplier" else set()

    def client(self, who) -> TestClient:
        return AutopilotArea.client(self, who)  # type: ignore[arg-type]

    def assist(self, line: str, who=OWNER):
        return self.client(who).post("/api/autopilot/assist", json={"intent": line})

    def test_drafts_with_cited_facts_and_cleared_recipients(self) -> None:
        r = self.assist("tell Sam the PO 90412 is late")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        # two people are named Sam; the one the reader writes to is meant
        self.assertEqual([t["address"] for t in d["to"]], [COLLEAGUE[0]])
        self.assertEqual((d["channel"], d["sent"], d["withheld"]), ("mail", False, 0))
        self.assertTrue(d["subject"] and "90412" in d["subject"])
        self.assertTrue(d["facts"] and all(f["sources"] for f in d["facts"]))
        self.assertEqual(d["questions"], ["the PO 90412 is late"])   # the fast path takes it
        for fact in d["facts"]:
            self.assertIn(fact["text"], d["body"])
        self.assertTrue(d["body"].startswith("Hi Sam,"))
        # someone outside: the addresses the reader's own last mail to that supplier went to
        d = self.assist("ask supplier Maple for an ETA on PO 90412").json()
        self.assertEqual(sorted(t["address"] for t in d["to"]),
                         ["orders@maple.example", "rita@maple.example"])
        self.assertTrue(d["body"] and "Could you send me an ETA on PO 90412?" in d["body"])
        # going outside: only the facts naming the PO asked about, never the customer order
        # it feeds
        self.assertTrue(d["facts"] and all("90412" in f["text"] for f in d["facts"]))
        self.assertNotIn("SO 77120", d["body"])
        # on Teams: no subject
        self.assertIsNone(self.assist("ping Sam on teams about PO 90412").json()["subject"])

    def test_fails_closed(self) -> None:
        # an excluded person is never a recipient and never named
        d = self.assist("tell Lee the PO 90412 is late").json()
        self.assertEqual((d["to"], d["withheld"]), ([], 1))
        self.assertNotIn("Lee", d["body"] or "")
        # nobody of that name, nobody the reader wrote to: a gap, never a guess
        d = self.assist("ask supplier Nowhere for an ETA on PO 90412").json()
        self.assertEqual(d["to"], [])
        self.assertTrue(any("Nowhere" in g for g in d["gaps"]))
        # two people of that name, neither written to by this reader: choices
        self.sent = []
        d = self.assist("tell Sam the PO 90412 is late").json()
        self.assertEqual((d["to"], sorted(o["address"] for o in d["choices"][0]["options"])),
                         ([], sorted([COLLEAGUE[0], SAM2[0]])))
        for line, status in (("   ", 422), ("x" * 301, 422), ("12345", 422)):
            self.assertEqual(self.assist(line).status_code, status, line)
        self.backend.assist_people = PeopleExclusion(unavailable="excluded_people_oids_absent")
        r = self.assist("tell Sam the PO 90412 is late")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "assist_refused"))
