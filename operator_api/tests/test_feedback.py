"""Feedback on anything: what is attached and stored, who sees it, triage, the eval candidate,
the nightly digest and the per-item signal the watcher and the engine read."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import feedback_signals
from operator_api import feedback as fb
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend

OID = "11111111-2222-4333-8444-555555555555"


def client_for(backend: MockBackend, principal: str) -> TestClient:
    app = create_app(backend, DevAuth(fixture_identity("plx", [principal]), mode="mock"),
                     secure_cookie=False)
    client = TestClient(app, base_url="http://testserver")
    assert client.post("/api/auth/dev-login", json={}).status_code == 200
    return client


def asked(client: TestClient, question: str = "where is PO 90412, is it late") -> str:
    text = client.post("/api/ask", json={"question": question}).text
    last = [b for b in text.strip().split("\n\n") if b.startswith("event: answer")][-1]
    return json.loads(last.split("data: ", 1)[1])["answer_id"]


class Guard:
    """A people exclusion naming one object id and one address."""
    unavailable = None

    def is_excluded(self, value):
        return value.lower() in (OID, "excluded.person@plx.example")

    def decides(self, address):
        return address.endswith("@plx.example")

    def known_domains(self):
        return {"plx.example"}


class Grants(dict):
    """The fixture's grants by principal: a reader key holds what its principal holds."""
    def get(self, key, default=None):
        return next((v for k, v in self.items() if k in key.split(",")
                     or key.endswith(k)), default)


class FeedbackLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "feedback"
        env = mock.patch.dict(os.environ, {"PLX_FEEDBACK_DIR": str(self.dir),
                                           "PLX_FEEDBACK_ADMINS": "staff:owner"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.backend = MockBackend()
        self.backend.feedback_store = fb.FeedbackStore(self.dir)
        self.addCleanup(self.backend.feedback_store.close)
        self.ana = client_for(self.backend, "staff:ana")
        self.ben = client_for(self.backend, "staff:ben")
        self.owner = client_for(self.backend, "staff:owner")

    def test_answer_feedback_attaches_the_answer_and_stays_with_its_reader(self):
        answer_id = asked(self.ana)
        sent = self.ana.post("/api/feedback", json={
            "answer_id": answer_id, "rating": -1, "note": "wrong date; key sk-abcdefghijklmnopqrstuv",
            "expected": "2026-11-20", "claims": [0], "page": "/ask", "app_version": "0.1.0"})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertIn("feedback_store", sent.json()["stored_in"])
        # another reader can neither rate this answer nor see the entry
        self.assertEqual(self.ben.post("/api/feedback", json={
            "answer_id": answer_id, "rating": 1}).status_code, 404)
        self.assertEqual(self.ben.get("/api/feedback").json()["total"], 0)
        mine = self.ana.get("/api/feedback").json()
        self.assertFalse(mine["all_readers"])
        entry = mine["entries"][0]
        self.assertEqual((entry["kind"], entry["rating"], entry["expected"]),
                         ("answer", -1, "2026-11-20"))
        self.assertTrue(entry["question"] and entry["answer"] and entry["claims"])
        self.assertEqual(entry["flagged"], [0])
        self.assertEqual((entry["served_version"], entry["app_version"]), ("fixture", "0.1.0"))
        self.assertNotIn("sk-abc", entry["note"])
        self.assertNotIn("candidate", entry)   # keys are made offline, never here
        # the admin sees every reader's; readers cannot triage
        every = self.owner.get("/api/feedback").json()
        self.assertTrue(every["all_readers"])
        self.assertTrue(self.owner.get("/api/me").json()["feedback_admin"])
        self.assertFalse(self.ana.get("/api/me").json()["feedback_admin"])
        fid = entry["id"]
        self.assertEqual(self.ana.post(f"/api/feedback/{fid}/status",
                                       json={"status": "fixed"}).status_code, 403)
        done = self.owner.post(f"/api/feedback/{fid}/status", json={"status": "accepted"})
        self.assertEqual(done.json()["status"], "accepted")
        self.assertFalse((self.dir / "eval-candidates.json").exists())
        filtered = self.owner.get("/api/feedback", params={"status": "new"}).json()
        self.assertEqual((filtered["total"], filtered["counts"]["accepted"]), (0, 1))

    def test_item_feedback_suppresses_for_its_reader_and_owner_wrong_for_everyone(self):
        for client, item, verdict in ((self.ana, "gap-1", "not_useful"),
                                      (self.ben, "gap-2", "wrong"),
                                      (self.owner, "gap-3", "wrong"),
                                      (self.ana, "gap-4", "useful")):
            self.assertEqual(client.post("/api/feedback/item", json={
                "item_id": item, "verdict": verdict, "note": "duplicate of yesterday",
                "label": "Late purchase order"}).status_code, 200)
        hide = feedback_signals.suppressor(None)
        ana, ben = "plx:staff:ana", "plx:staff:ben"   # briefing reader keys
        self.assertIn("not useful", hide("gap-1", ana))
        self.assertIsNone(hide("gap-1", ben))
        self.assertIsNotNone(hide("gap-2", ben))
        self.assertIsNone(hide("gap-2", ana))
        self.assertEqual(hide("gap-3", ana), "Marked wrong: duplicate of yesterday")
        self.assertIsNone(hide("gap-4", ana))
        # accepted in triage: everyone's; won't fix: nobody's
        fid = next(e["id"] for e in self.owner.get("/api/feedback").json()["entries"]
                   if e["item_id"] == "gap-2")
        self.owner.post(f"/api/feedback/{fid}/status", json={"status": "accepted"})
        self.assertIsNotNone(feedback_signals.suppressor(None)("gap-2", ana))
        self.owner.post(f"/api/feedback/{fid}/status", json={"status": "wont_fix"})
        self.assertIsNone(feedback_signals.suppressor(None)("gap-2", ben))

    def test_nightly_digest_holds_thumbs_down_and_wrong_alerts(self):
        answer_id = asked(self.ana)
        self.ana.post("/api/feedback", json={"answer_id": answer_id, "rating": -1,
                                             "note": "missed the receipt"})
        self.ana.post("/api/feedback", json={"answer_id": asked(self.ana), "rating": 1})
        self.ben.post("/api/feedback/item", json={"item_id": "w-9", "verdict": "wrong"})
        self.ben.post("/api/feedback/note", json={"kind": "ui", "note": "the list jumps",
                                                  "page": "/late"})
        day = fb.eastern_day(datetime.now(timezone.utc))
        path = self.backend.feedback_store.write_digest(day)
        self.assertEqual(path, self.dir / f"digest-{day.isoformat()}.json")
        digest = json.loads(path.read_text("utf-8"))
        self.assertEqual(digest["counts"]["thumbs_down"], 1)
        self.assertEqual(digest["counts"]["thumbs_up"], 1)
        self.assertEqual([a["item_id"] for a in digest["alerts"]], ["w-9"])
        self.assertEqual(digest["notes"][0]["page"], "/late")
        self.assertTrue(digest["thumbs_down"][0]["reader"].startswith("r-"))
        self.assertNotIn("staff:", json.dumps(digest))
        self.assertNotIn('"expected"', json.dumps(digest))   # only the offline tool carries it
        # 0600 files in a 0700 folder, whatever the umask
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        for path in self.dir.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_an_existing_open_folder_is_closed_and_files_are_0600(self):
        folder = Path(self.tmp.name) / "open"
        folder.mkdir(mode=0o755)
        old = os.umask(0o002)
        try:
            store = fb.FeedbackStore(folder)
            store.add({"kind": "alert", "reader": "r", "item_id": "i", "verdict": "wrong"})
            store.close()
        finally:
            os.umask(old)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual({p.name: p.stat().st_mode & 0o777 for p in folder.iterdir()
                          if p.stat().st_mode & 0o777 != 0o600}, {})

    def test_general_feedback_needs_no_page_or_record(self):
        sent = self.ana.post("/api/feedback/note", json={"note": "the app is slow"})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.ana.post("/api/feedback/note", json={"kind": "general",
                                                  "note": "I wish it could do X"})
        got = self.owner.get("/api/feedback", params={"kind": "general"}).json()
        self.assertEqual(sorted(e["note"] for e in got["entries"]),
                         ["I wish it could do X", "the app is slow"])
        self.assertTrue(all(e["page"] is None and e["object_id"] is None
                            for e in got["entries"]))

    def test_rating_and_its_record_are_one_write(self):
        answer_id = asked(self.ana)
        with mock.patch.object(self.backend, "feedback", side_effect=LookupError("down")):
            failed = self.ana.post("/api/feedback", json={"answer_id": answer_id,
                                                          "rating": -1, "expected": "3"})
        self.assertEqual(failed.status_code, 404)
        self.assertEqual(self.ana.get("/api/feedback").json()["total"], 0)   # no orphan

    def test_an_admin_reads_another_readers_answer_as_their_markings_allow(self):
        self.backend.held_markings = Grants(
            {"staff:ana": frozenset({"finance", "purchasing_prices"})})
        self.ana.post("/api/feedback", json={"answer_id": asked(self.ana), "rating": -1,
                                             "expected": "$4,821.75", "claims": [0]})
        seen = self.owner.get("/api/feedback").json()["entries"][0]
        self.assertEqual(seen["withheld"], ["finance", "purchasing_prices"])
        self.assertIsNone(seen["answer"])
        self.assertIsNone(seen["expected"])
        self.assertTrue(seen["claims"] and all(c["text"] is None and not c["citations"]
                                               for c in seen["claims"]))
        self.assertTrue(seen["question"])
        # the reader sees their own whole
        mine = self.ana.get("/api/feedback").json()["entries"][0]
        self.assertEqual((mine["expected"], mine["withheld"]), ("$4,821.75", []))
        # an admin holding finance reads it whole
        self.backend.held_markings["staff:owner"] = frozenset({"finance"})
        whole = self.owner.get("/api/feedback").json()["entries"][0]
        self.assertEqual((whole["expected"], whole["withheld"]), ("$4,821.75", []))
        # a citation the admin may not open withholds its claim
        with mock.patch.object(self.backend, "feedback_grant", lambda r, h: (
                frozenset({"finance"}), {x: {"reason": "item_refused"} for x in h})):
            trimmed = self.owner.get("/api/feedback").json()["entries"][0]
        self.assertIn("source", trimmed["withheld"])
        self.assertTrue(all(c["text"] is None for c in trimmed["claims"] if c["i"] == 0))
        # the owner (full-read grant) reads everything
        del self.backend.held_markings["staff:owner"]
        with mock.patch.object(fb, "owner_open", lambda session: True):
            owner = self.owner.get("/api/feedback").json()["entries"][0]
        self.assertEqual((owner["expected"], owner["withheld"]), ("$4,821.75", []))

    def test_an_excluded_persons_own_feedback_names_a_colleague(self):
        # no usable exclusion: nobody can be told apart, so nobody is named (fail closed)
        self.ana.post("/api/feedback/note", json={"note": "hidden author"})
        entry = self.owner.get("/api/feedback").json()["entries"][0]
        self.assertEqual(entry["reader_name"], "a colleague")
        stored = self.backend.feedback_store.get(entry["id"])
        self.assertIsNone(stored["reader_name"])
        self.assertNotIn("session_ref", stored["context"])
        # a usable exclusion that does not name the reader: named
        with mock.patch.object(fb, "exclusion", Guard):
            self.ben.post("/api/feedback/note", json={"note": "named author"})
        named = next(e for e in self.owner.get("/api/feedback").json()["entries"]
                     if e["note"] == "named author")
        self.assertNotEqual(named["reader_name"], "a colleague")
        self.assertTrue(named["reader_name"])
        self.assertTrue(fb.reader_excluded(mock.Mock(
            email="Excluded.Person@plx.example",
            session=mock.Mock(identities=(), principals=())), Guard()))

    def test_notes_on_a_record_list_by_type_and_page(self):
        self.ana.post("/api/feedback/note", json={
            "kind": "record", "note": "supplier name is old", "page": "/po/90412",
            "object_type": "PurchaseOrder", "object_id": "90412"})
        self.ana.post("/api/feedback/note", json={"kind": "general", "note": "faster please",
                                                  "page": "/"})
        got = self.ana.get("/api/feedback", params={"kind": "record"}).json()
        self.assertEqual([e["object_id"] for e in got["entries"]], ["90412"])
        self.assertEqual(self.ana.get("/api/feedback", params={"page": "/po"}).json()["total"], 1)
        self.assertEqual(self.ana.post("/api/feedback/note", json={"note": ""}).status_code, 422)


class NeverStored(unittest.TestCase):
    def test_excluded_people_and_secrets_are_withheld(self):
        text = (f"mail from excluded.person@plx.example ({OID}) and ok.person@plx.example; "
                "password=hunter2 and vendor@supplier.example")
        out = fb.clean(text, Guard())
        self.assertNotIn("excluded.person", out)
        self.assertNotIn(OID, out)
        self.assertIn("ok.person@plx.example", out)
        self.assertIn("vendor@supplier.example", out)
        self.assertNotIn("hunter2", out)
        self.assertIsNone(fb.clean_handle(f"graph-mail://{OID}/messages/AAMk", Guard()))
        self.assertIsNone(fb.clean_handle(
            "graph-mail://excluded.person%40plx.example/messages/AAMk", Guard()))
        self.assertEqual(fb.clean_handle("graph-mail://ok.person@plx.example/m/1", Guard()),
                         "graph-mail://ok.person@plx.example/m/1")
        self.assertEqual(fb.clean_handle("bc://plx/purchaseOrders/90412", Guard()),
                         "bc://plx/purchaseOrders/90412")
        # no usable exclusion: every address and object id is withheld (fail closed)
        self.assertNotIn("@", fb.clean("ok.person@plx.example", None))
        self.assertIsNone(fb.clean_handle(f"graph-mail://{OID}/messages/AAMk", None))

    def test_the_product_never_imports_eval_code(self):
        source = Path(fb.__file__).read_text("utf-8")
        for name in ("evals", "scripts.", "cards.json", "live_ops"):
            self.assertNotIn(name, source.replace("scripts.eval_feedback_candidates", ""))


if __name__ == "__main__":
    unittest.main()
