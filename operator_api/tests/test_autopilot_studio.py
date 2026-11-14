"""The autopilot studio's contract (``/api/autopilot/workflows/{id}``, ``.../mode``,
``/api/autopilot/catalog``, ``/api/autopilot/schedules``, draft and replay) through the
app's routes: a reader outside a workflow's roles learns nothing of it, only the owner sets
a mode, Auto is refused (and nothing changes) until the fresh gate passed, Suggest and Watch
and the builder are refused on a server without the builder, never faked; the demo studio
(``serve --mock``) keeps Watch closed until a gate passed.

SYNTHETIC queue, gate files and people; no adapter, no transport. Evidence label:
``isolated-fixture``.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operator_api import autopilot_studio as studio
from operating import autopilot_write as aw
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api import mock_studio
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend
from operator_api.tests.test_autopilot_area import COLLEAGUE, ENV, FLOW, OWNER, ROLE, queued


def client_for(backend, who) -> TestClient:
    session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                              subject_email=who[0], subject_oid=who[1])
    client = TestClient(create_app(backend, DevAuth(lambda: session, mode="mock"),
                                   secure_cookie=False), base_url="http://testserver")
    assert client.post("/api/auth/dev-login", json={}).status_code == 200
    return client


class Studio(unittest.TestCase):
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
            q.add_item(queued("ap_sam", COLLEAGUE[1]))
        q.close()
        (path.parent / ap.GATE_FILE).write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": {
            ROLE: {"members": [OWNER[1]], "workflows": {FLOW: {"judged": 40, "matched": 37}}},
            "receivables": {"members": [COLLEAGUE[1]],
                            "workflows": {"ar_collections": {"judged": 20, "matched": 15}}}}}))
        guard = PeopleExclusion(["99999999-9999-4999-8999-999999999999"])
        guard.attach_directory([(OWNER[1], OWNER[0], []), (COLLEAGUE[1], COLLEAGUE[0], [])])
        b = self.backend = MockBackend()
        b.writes_environ = dict(ENV)
        b.autopilot_queue = path
        b.autopilot_write_state = self.state
        b.autopilot_grant = (lambda aud: True, frozenset(), lambda hs: {h: None for h in hs})
        b.autopilot_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        b.autopilot_guard = guard
        b.autopilot_adapters = {}
        self.owner, self.sam = client_for(b, OWNER), client_for(b, COLLEAGUE)

    def switch_files(self) -> dict:
        return {p.name: p.read_text() for p in self.state.glob("*.json")}

    def test_a_workflow_page_shows_only_what_the_reader_may_see(self) -> None:
        page = self.owner.get(f"/api/autopilot/workflows/{FLOW}")
        self.assertEqual(page.status_code, 200, page.text)
        doc = page.json()
        # its steps are the registered autopilot's (customer_po feeds customer_order_intake)
        self.assertIn("customer_po", doc["autopilots"])
        self.assertEqual(doc["trigger"]["kind"], "message.received")
        self.assertEqual([a["kind"] for a in doc["actions"]], ["order_entry"])
        self.assertTrue(doc["conditions"])
        self.assertEqual((doc["gate"]["judged"], doc["gate"]["matched"], doc["gate"]["percent"],
                          doc["gate"]["fresh"], doc["gate"]["passed"]),
                         (40, 37, 92.5, False, False))
        self.assertEqual((doc["caught"], doc["missed"], doc["false_alarms"],
                          doc["hours_saved"]), (37, 3, None, None))
        self.assertEqual((doc["mode"], doc["modes"]), ("draft", ["off", "draft"]))
        # the colleague has an item of it but is in none of its roles: the page, without the
        # owner's role row, gate counts or history
        theirs = self.sam.get(f"/api/autopilot/workflows/{FLOW}").json()
        self.assertEqual((theirs["rows"], theirs["gate"]["judged"], theirs["history"]),
                         ([], 0, []))
        self.assertEqual(theirs["open_items"], 1)
        # a workflow they have nothing of is as unknown as a name that is none
        self.assertEqual(self.sam.get("/api/autopilot/workflows/purchase_order_to_supplier")
                         .status_code, 404)
        self.assertEqual(self.owner.get("/api/autopilot/workflows/purchase_order_to_supplier")
                         .status_code, 200)
        self.assertEqual(self.sam.get("/api/autopilot/workflows/no_such_flow").status_code, 404)
        theirs = self.sam.get("/api/autopilot/workflows/ar_collections").json()
        self.assertEqual([r["role"] for r in theirs["rows"]], ["receivables"])
        self.assertEqual(theirs["history"], [])          # the audit log is the owner's
        # schedules: the colleague sees none of the owner's workflows
        owner_flows = {s["workflow"] for s in
                       self.owner.get("/api/autopilot/schedules").json()["schedules"]}
        sam_flows = {s["workflow"] for s in
                     self.sam.get("/api/autopilot/schedules").json()["schedules"]}
        self.assertIn("purchase_order_to_supplier", owner_flows)
        self.assertLessEqual(sam_flows, {"ar_collections"})
        catalog = self.sam.get("/api/autopilot/catalog").json()
        self.assertFalse(catalog["builder"])
        self.assertIn("message.received", [t["kind"] for t in catalog["triggers"]])

    def test_the_builder_names_every_step_in_words_not_its_key(self) -> None:
        catalog = self.owner.get("/api/autopilot/catalog").json()
        for part in ("triggers", "conditions", "actions"):
            for step in catalog[part]:
                self.assertNotEqual(step["words"], studio._humanize(step["kind"]), (part, step["kind"]))
        words = [a["words"] for a in catalog["actions"]]
        self.assertEqual(len(words), len(set(words)), "two builder actions read the same")

    def test_modes_go_through_the_owner_switches_and_fail_closed(self) -> None:
        path = f"/api/autopilot/workflows/{FLOW}/mode"
        before = self.switch_files()
        # not the owner: refused, nothing written
        for mode in ("off", "draft", "auto", "suggest", "watch"):
            self.assertEqual(self.sam.put(path, json={"mode": mode, "role": ROLE}).status_code,
                             403, mode)
        self.assertEqual(self.switch_files(), before)
        # an unknown mode is refused by the contract
        self.assertEqual(self.owner.put(path, json={"mode": "autonomous"}).status_code, 422)
        # Auto is each person's own switch: the owner is refused, the workflow not switched on
        self.assertEqual(self.owner.put(path, json={"mode": "off"}).status_code, 200)
        off = self.switch_files()
        r = self.owner.put(path, json={"mode": "auto", "role": ROLE})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Nobody can turn it on for them", r.json()["error"]["message"])
        self.assertEqual(self.switch_files(), off)
        self.assertEqual(self.owner.get(f"/api/autopilot/workflows/{FLOW}").json()["mode"],
                         "off")
        # Suggest and Watch are the builder's: refused here, never faked
        for mode in ("suggest", "watch"):
            r = self.owner.put(path, json={"mode": mode, "role": ROLE})
            self.assertEqual(r.status_code, 409, mode)
        self.assertEqual(self.switch_files(), off)
        # Draft switches it back on, not live
        doc = self.owner.put(path, json={"mode": "draft", "role": ROLE}).json()
        self.assertEqual(doc["mode"], "draft")
        self.assertFalse(any(r["switched_on"] for r in doc["rows"]))
        self.assertEqual(json.loads((self.state / aw.WORKFLOW_KILL_FILE).read_text())
                         ["workflows"], [])
        # the builder is refused on this server: nothing kept, nothing replayed
        draft = {"label": "x", "trigger": {"kind": "record.due", "words": "w"},
                 "actions": [{"kind": "po_followup", "words": "w"}]}
        self.assertEqual(self.sam.post("/api/autopilot/workflows", json=draft).status_code, 403)
        self.assertEqual(self.owner.post("/api/autopilot/workflows", json=draft).status_code,
                         409)
        self.assertEqual(self.owner.post(f"/api/autopilot/workflows/{FLOW}/replay",
                                         json={"days": 14}).status_code, 409)
        self.assertEqual(self.owner.post(f"/api/autopilot/workflows/{FLOW}/replay",
                                         json={"days": 0}).status_code, 422)


class DemoStudio(unittest.TestCase):
    """``serve --mock``'s studio: real area code over a seeded synthetic state."""

    def setUp(self) -> None:
        self.backend = MockBackend()
        demo = mock_studio.install(self.backend)
        self.addCleanup(lambda: __import__("shutil").rmtree(demo.root, ignore_errors=True))
        self.client = client_for(self.backend, ("demo@plx-demo.example", None))

    def test_the_demo_owner_sets_neither_auto_nor_watch(self) -> None:
        area = self.client.get("/api/autopilot").json()
        self.assertEqual(len(area["items"]), len(mock_studio._items(
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc))))
        passed = {w["workflow"] for w in area["workflows"]
                  if w["state"] == "passed"}
        self.assertEqual(passed, {"purchase_order_to_supplier", "internal_approval"})
        # Auto and Watch act for a person: only that person switches them on, never the
        # owner, passed gate or not
        for flow in ("meeting_admin", "purchase_order_to_supplier"):
            for mode in ("auto", "watch"):
                r = self.client.put(f"/api/autopilot/workflows/{flow}/mode",
                                    json={"mode": mode})
                self.assertEqual(r.status_code, 409, (flow, mode))
        doc = self.client.get("/api/autopilot/workflows/purchase_order_to_supplier").json()
        self.assertNotIn("watch", doc["modes"])
        self.assertNotIn("auto", doc["modes"])
        # nothing in the demo performs: no adapter, so an accept is a recorded dry run
        mine = next(i for i in area["items"] if i["mine"])
        done = self.client.post(f"/api/autopilot/items/{mine['item_id']}/decision",
                                json={"decision": "accept"}).json()
        self.assertNotEqual(done["decision"]["outcome"], "done")
        self.assertEqual(self.client.get("/api/autopilot/audit").json()["live"], 0)
        kept = self.client.post("/api/autopilot/workflows", json={
            "label": "PO chase", "trigger": {"kind": "record.due", "words": "w"},
            "actions": [{"kind": "po_followup", "words": "w"}]}).json()
        replay = self.client.post(f"/api/autopilot/workflows/{kept['workflow']}/replay",
                                  json={"days": 14}).json()
        self.assertEqual(replay["caught"] + replay["missed"] + replay["false_alarms"],
                         replay["judged"])


if __name__ == "__main__":
    unittest.main()
