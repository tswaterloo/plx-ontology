"""Prepared writes in the app: the Ask's ``plx.prepare_write`` bound to the signed-in
person, the answer naming what it prepared, ``/api/prepared...`` (preview, two-step
confirm, dismiss, undo) and "could the person do this themselves" per system.

SYNTHETIC people and plans; fake transports only: nothing reaches a network and no test
presses a live confirm against a real system. Evidence label: injected-transport.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_autopilot_write import Transport
from operator_api import simple_ask
from operator_api.app import create_app
from operator_api.auth import DevAuth, Reader
from operator_api.mock import MockBackend

OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
STAFF = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")
HIDDEN = ("hidden@plx.example", "99999999-9999-4999-8999-999999999999")
PLAN = "plan-0001"
TASK = {"system": "planner", "operation": "task_create", "target": {},
        "change": {"planId": PLAN, "title": "Chase the carrier"}, "reason": "You asked."}


def session(who):
    return fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                           subject_email=who[0], subject_oid=who[1])


class PreparedRoutes(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fake = Transport()
        guard = PeopleExclusion([HIDDEN[1]])
        guard.attach_directory([(OWNER[1], OWNER[0], []), (STAFF[1], STAFF[0], []),
                                (HIDDEN[1], HIDDEN[0], [])])
        self.env = {aw.APPROVED_ENV: "planner_write"}
        b = self.backend = MockBackend()
        b.writes_environ = self.env
        b.autopilot_write_state = self.root / "autopilot-write"
        b.autopilot_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        b.autopilot_guard = guard
        b.autopilot_adapters = {"planner_write": wa.PlannerAdapter(self.fake, environ=self.env)}
        b.prepared_roster = {OWNER[0]: {"mc_role": "owner"}, STAFF[0]: {"mc_role": None}}

    def client(self, who) -> TestClient:
        s = session(who)
        client = TestClient(create_app(self.backend, DevAuth(lambda: s, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def tool(self, who):
        reader = Reader(session=session(who), mode="mock", display_name=who[0], email=who[0])
        tools = {t.spec["name"]: t for t in self.backend.prepared_tools(reader)}
        return tools["plx.prepare_write"]

    def test_ask_prepares_and_only_the_person_confirms_in_two_steps(self):
        got = self.tool(OWNER).call(TASK)
        pid = got["prepared_id"]
        self.assertIn("never say it was done", got["note"])
        self.assertEqual(self.fake.calls, [])
        # the answer carries the id the session's tool answered, and nothing it did not
        events = [{"type": "tool_call", "name": "plx.prepare_write", "is_error": False,
                   "result": json.dumps(got)},
                  {"type": "tool_call", "name": "mcp__plx__plx_prepare_write",
                   "is_error": True, "result": json.dumps({"prepared_id": "pw_x"})}]
        self.assertEqual(simple_ask.prepared_ids(events), [pid])
        ana, sam = self.client(OWNER), self.client(STAFF)
        self.assertEqual([i["prepared_id"] for i in ana.get("/api/prepared").json()["items"]],
                         [pid])
        # another person never sees, confirms, dismisses or undoes it
        self.assertEqual(sam.get("/api/prepared").json()["items"], [])
        for call in (lambda: sam.get(f"/api/prepared/{pid}"),
                     lambda: sam.post(f"/api/prepared/{pid}/dismiss"),
                     lambda: sam.post(f"/api/prepared/{pid}/confirm",
                                      json={"preview_hash": "a" * 64, "confirm": True})):
            self.assertEqual(call().status_code, 404)
        shown = ana.get(f"/api/prepared/{pid}").json()
        self.assertTrue(shown["live"], shown["missing"])
        self.assertEqual(ana.post(f"/api/prepared/{pid}/confirm", json={
            "preview_hash": shown["preview_hash"], "confirm": False}).status_code, 409)
        self.assertEqual(ana.post(f"/api/prepared/{pid}/confirm", json={
            "preview_hash": "b" * 64, "confirm": True}).status_code, 409)
        self.assertEqual(self.fake.calls, [])
        self.fake.answers = [(201, {"id": "pt_1", "@odata.etag": 'W/"e2"'})]
        done = ana.post(f"/api/prepared/{pid}/confirm", json={
            "preview_hash": shown["preview_hash"], "confirm": True}).json()
        self.assertEqual((done["state"], [o["status"] for o in done["outcome"]]),
                         ("confirmed", ["done"]))
        self.assertEqual(len(self.fake.calls), 1)
        self.fake.answers = [(204, {})]
        self.assertEqual(ana.post(f"/api/prepared/{pid}/undo").json()["state"], "undone")

    def test_a_document_the_ask_made_goes_out_attached_only_after_the_confirm(self):
        """``plx.make_document`` then ``plx.prepare_write`` mail with the file's id: the
        card shows the attachment (name, size, the link that opens it, never the bytes);
        the send carries exactly those bytes; another person's file, or a file handed over
        as bytes, is refused."""
        sent = []

        class Sender:
            def dispatch(self, kind, sender, payload):
                sent.append((kind, sender, payload))
                return {"status": "sent", "http_status": 202}
        b = self.backend
        b.people_guard = lambda: b.autopilot_guard
        b.instruct_uploads = None
        b.state_dir = self.root
        self.env[aw.APPROVED_ENV] = "planner_write,send_mail"
        b.autopilot_adapters["send_mail"] = wa.MailAdapter(Sender(), environ=self.env)

        def own(who):   # each person their own reader key (their files are theirs)
            return fixture_session("plx", [f"staff:{who[0].split('@')[0]}"],
                                   channel=CHANNEL_CONSOLE, subject_email=who[0],
                                   subject_oid=who[1])

        def tools(who):
            reader = Reader(session=own(who), mode="mock", display_name=who[0], email=who[0])
            return {t.spec["name"]: t for t in b.prepared_tools(reader)
                    + b.assistant_tools(reader)}
        ana, sam = tools(OWNER), tools(STAFF)
        made = ana["plx.make_document"].call({"name": "Late POs", "kind": "docx",
                                              "content": "Late POs\nPO 1 is 9 days late."})
        self.assertEqual(made["name"], "Late POs.docx")
        self.assertIn("refused", ana["plx.make_document"].call(
            {"name": "Carrier", "kind": "docx", "content": "Book it with Freightcom."}))
        # the Markdown the Ask writes becomes Word's own table, never pipes on the page
        import io
        import zipfile
        table = ana["plx.make_document"].call({"name": "T", "kind": "docx", "content":
                                               "# Late\n| PO | Days |\n|---|---|\n| 1 | 9 |"})
        page = zipfile.ZipFile(io.BytesIO(b._uploads().file(
            b._uploads()._rows()[-1]["reader"], table["file_id"]))).read("word/document.xml")
        self.assertEqual((page.count(b"<w:tbl>"), page.count(b"<w:tr>"), b"|" in page), (1, 2, False))
        mail = {"system": "mail", "operation": "send_email", "target": {},
                "change": {"to": [STAFF[0]], "subject": "Late POs", "body": "Attached.",
                           "attachments": [made["file_id"]]}, "reason": "You asked."}
        for refused in (sam["plx.prepare_write"].call(mail), ana["plx.prepare_write"].call(
                {**mail, "change": {**mail["change"], "attachments": [
                    {"file_id": made["file_id"], "name": "x.docx", "content_b64": "AA=="}]}})):
            self.assertIn("refused", refused)
        # a file naming someone excluded since it was made is never attached
        later = ana["plx.make_document"].call({"name": "Note", "kind": "docx",
                                               "content": f"Ask {STAFF[0]} first."})
        now_excluded = PeopleExclusion([STAFF[1]])
        now_excluded.attach_directory([(OWNER[1], OWNER[0], []), (STAFF[1], STAFF[0], [])])
        kept, b.people_guard = b.people_guard, lambda: now_excluded
        self.assertIn("refused", ana["plx.prepare_write"].call(
            {**mail, "change": {**mail["change"], "attachments": [later["file_id"]]}}))
        b.people_guard = kept
        # saved to SharePoint beside a file the person opens: that file's folder; one they
        # cannot open names none
        from operator_api import models as m
        anchor = "sharepoint://drive-1/item-9@e1"
        opens = {anchor: m.SourceView(handle=anchor, system="File", label="Specs.docx",
                                      kind="record", fields={"parent_id": "folder-7"})}
        b.source = lambda who, handle, offset: opens[handle]
        b.autopilot_adapters["sharepoint_write"] = wa.SharePointAdapter(self.fake,
                                                                         environ=self.env)
        save = {"system": "sharepoint", "operation": "file_upload", "reason": "You asked.",
                "change": {"file_id": made["file_id"]}, "target": {"beside": anchor}}
        card = ana["plx.prepare_write"].call(save)
        self.assertEqual(card["actions"][0]["target"], {"drive_id": "drive-1",
                                                        "parent_id": "folder-7"})
        self.assertEqual(card["actions"][0]["fields"]["name"], "Late POs.docx")
        shown = self.client(OWNER).get(f"/api/prepared/{card['prepared_id']}").json()
        self.assertEqual(shown["title"], "Save Late POs.docx in (the folder of Specs.docx)")
        self.assertIn("refused", ana["plx.prepare_write"].call(
            {**save, "target": {"beside": "sharepoint://drive-1/other@e2"}}))
        # an autopilot job's proposal takes files the same way: bytes handed over are refused
        reader = Reader(session=own(OWNER), mode="mock", display_name=OWNER[0], email=OWNER[0])
        inline = {**mail, "change": {**mail["change"], "attachments": [
            {"file_id": made["file_id"], "name": "x.docx", "content_b64": "AA=="}]}}
        self.assertIsNotNone(b._pw_resolve(reader, inline)[1])
        # nothing of the assistant, and no write tool, for a session not the person's own
        for not_own in ({"view_as": True}, {"canary": True}):
            viewing = Reader(session=type("Session", (), not_own)(), mode="mock",
                             display_name="x", email=OWNER[0])
            self.assertEqual((b.assistant_tools(viewing), b.prepared_tools(viewing)), ([], []))
        # someone the file names, excluded between prepare and confirm: nothing is sent
        named = ana["plx.make_document"].call({"name": "Ask Sam", "kind": "docx",
                                               "content": f"Ask {STAFF[0]} first."})
        later = ana["plx.prepare_write"].call(
            {**mail, "change": {**mail["change"], "to": [OWNER[0]],
                                "attachments": [named["file_id"]]}})
        pending = TestClient(create_app(b, DevAuth(lambda: own(OWNER), mode="mock"),
                                        secure_cookie=False), base_url="http://testserver")
        pending.post("/api/auth/dev-login", json={})
        before = pending.get(f"/api/prepared/{later['prepared_id']}").json()
        kept_guard = b.autopilot_guard
        b.autopilot_guard = PeopleExclusion([STAFF[1]])
        b.autopilot_guard.attach_directory([(OWNER[1], OWNER[0], []), (STAFF[1], STAFF[0], [])])
        ran = pending.post(f"/api/prepared/{later['prepared_id']}/confirm", json={
            "preview_hash": before["preview_hash"], "confirm": True})
        b.autopilot_guard = kept_guard
        self.assertNotIn("done", [o.get("status") for o in ran.json().get("outcome") or ()])
        self.assertEqual(sent, [])
        pid = ana["plx.prepare_write"].call(mail)["prepared_id"]
        mine = own(OWNER)
        client = TestClient(create_app(b, DevAuth(lambda: mine, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        client.post("/api/auth/dev-login", json={})
        shown = client.get(f"/api/prepared/{pid}").json()
        (action,) = shown["actions"]
        self.assertEqual(action["message"]["attachments"], [
            {"name": "Late POs.docx", "size": made["size"], "file_id": made["file_id"]}])
        self.assertNotIn("UEsDB", json.dumps(shown))      # a docx's bytes, base64
        file = client.get(made["download_url"])
        self.assertEqual((file.status_code, file.content[:2]), (200, b"PK"))
        self.assertEqual(sent, [])
        done = client.post(f"/api/prepared/{pid}/confirm", json={
            "preview_hash": shown["preview_hash"], "confirm": True}).json()
        self.assertEqual([o["status"] for o in done["outcome"]], ["done"])
        ((kind, _sender, payload),) = sent
        self.assertEqual((kind, [a["name"] for a in payload["attachments"]]),
                         ("send_email", ["Late POs.docx"]))
        self.assertEqual(payload["attachments"][0]["content"], file.content)
        # the audit keeps what went where, never the file's bytes
        journal = "".join(p.read_text() for p in self.root.rglob("*.jsonl"))
        self.assertNotIn("UEsDB", journal)

    def test_a_csv_the_ask_makes_holds_the_rows_and_never_runs_a_formula(self):
        """``plx.make_document`` kind csv: the rows the Ask wrote (a Markdown table's outer
        pipes and rule row dropped, a comma kept inside its cell), downloaded as text/csv
        with a byte-order mark; a cell a spreadsheet would run as a formula is text, a
        negative number stays a number; charts are refused (a CSV never drops one
        silently), so is the carrier never written about."""
        import csv
        import io
        b = self.backend
        b.people_guard = lambda: b.autopilot_guard
        b.instruct_uploads = None
        b.state_dir = self.root
        mine = fixture_session("plx", ["staff:ana"], channel=CHANNEL_CONSOLE,
                               subject_email=OWNER[0], subject_oid=OWNER[1])
        reader = Reader(session=mine, mode="mock", display_name=OWNER[0], email=OWNER[0])
        make = {t.spec["name"]: t for t in b.assistant_tools(reader)}["plx.make_document"]
        self.assertIn("csv", make.spec["inputSchema"]["properties"]["kind"]["enum"])
        made = make.call({"name": "Open orders.csv", "kind": "csv", "content":
                          "| Order | Customer | Balance | Note |\n|---|:--|--:|---|\n"
                          "| SO-1 | Acme, Inc. | -12.5 | =HYPERLINK(\"http://x\") |\n"
                          "SO-2 | Beta | 1,200.00 | @SUM(A1)\n\n"})
        self.assertEqual((made["name"], made["kind"]), ("Open orders.csv", "csv"))
        client = TestClient(create_app(b, DevAuth(lambda: mine, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        client.post("/api/auth/dev-login", json={})
        got = client.get(made["download_url"])
        self.assertEqual(got.status_code, 200)
        self.assertTrue(got.headers["content-type"].startswith("text/csv"))
        self.assertTrue(got.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(got.content.decode("utf-8-sig"))))
        self.assertEqual(rows, [["Order", "Customer", "Balance", "Note"],
                                ["SO-1", "Acme, Inc.", "-12.5", "'=HYPERLINK(\"http://x\")"],
                                ["SO-2", "Beta", "1,200.00", "'@SUM(A1)"]])
        self.assertIn("refused", make.call({"name": "C", "kind": "csv", "content": "a | b",
                                            "charts": [{"sql": "SELECT 1", "type": "bar",
                                                        "y": ["x"]}]}))
        self.assertIn("refused", make.call({"name": "C", "kind": "csv",
                                            "content": "Carrier | Freightcom"}))
        self.assertEqual(len(b._uploads()._rows()), 1)   # nothing made for a refusal

    def test_staff_may_only_what_their_own_grants_allow(self):
        refused = self.tool(STAFF).call(TASK)["refused"]
        self.assertIn("cannot read your planner membership", " ".join(refused))
        grants = self.root / "grants.json"
        grants.write_text(json.dumps({"schema": "plx-write-grants/v1",
                                      "grants": {STAFF[0]: [f"planner:{PLAN}"]}}))
        self.env["PLX_WRITE_GRANTS_FILE"] = str(grants)
        self.assertIn("prepared_id", self.tool(STAFF).call(TASK))
        # another plan is still refused; BC without a grant too
        other = {**TASK, "change": {**TASK["change"], "planId": "plan-0002"}}
        self.assertIn("refused", self.tool(STAFF).call(other))
        bc = {"system": "bc", "operation": "sales_order_comment",
              "target": {"company_name": "PLX", "document_id": "x"},
              "change": {"description": "note"}, "reason": "r"}
        self.assertIn("refused", self.tool(STAFF).call(bc))

    def test_mc_gate_decisions_need_an_mc_admin_role_and_never_ones_own_request(self):
        desk = self.backend._pw_desk(Reader(session=session(STAFF), mode="mock",
                                            display_name="s", email=STAFF[0]))
        may = desk.may
        from operating.prepared_writes import Person
        gate = {"kind": "mc_approval", "target": {}, "people": ["lee@plx.example"]}
        self.assertIn("admins and owners", may(Person(STAFF), gate))
        self.assertIsNone(may(Person(OWNER), gate))
        own = {"kind": "mc_approval", "target": {}, "people": [OWNER[0].upper()]}
        self.assertIn("nobody approves", may(Person(OWNER), own))
        self.assertIn("not on the Mission Control", may(
            Person(("x@plx.example",)), {"kind": "mc_task", "target": {}}))
        # a system the desk cannot check refuses
        self.assertIsNotNone(may(Person(STAFF), {"kind": "something_new", "target": {}}))

    def test_document_edit_is_a_diff_then_an_if_match_replace_and_its_undo(self):
        import base64
        from integrations import live_source_guard as g
        etag = '"{A1B2},3"'
        before = b"Ship to Unit 4 by Friday\nThanks\n" + b"x" * 2000
        self.backend.prepared_doc_read = lambda reader, cite: (
            ("notes.txt", before, etag) if cite == "sharepoint://b!d1/01ITEM@v3" else None)
        self.env[aw.APPROVED_ENV] = "sharepoint_write"
        self.backend.autopilot_adapters = {
            "sharepoint_write": wa.SharePointAdapter(self.fake, environ=self.env)}
        reader = Reader(session=session(OWNER), mode="mock", display_name="a", email=OWNER[0])
        tools = {t.spec["name"]: t for t in self.backend.prepared_tools(reader)}
        edit = tools["plx.edit_document"]
        got = edit.call({"citation": "sharepoint://b!d1/01ITEM@v3", "find": "Unit 4",
                         "replace": "Dock 9", "reason": "New address."})
        self.assertEqual(got["diff"], [{"field": "line 1", "before": "Ship to Unit 4 by Friday",
                                        "after": "Ship to Dock 9 by Friday"}])
        # the bytes stay out of the journal; a citation the reader cannot read is refused
        journal = (self.root / "autopilot-write" / "prepared-writes" / "journal.jsonl").read_text()
        self.assertNotIn(base64.b64encode(before).decode()[:200], journal)
        self.assertIn("refused", edit.call({"citation": "sharepoint://b!d1/02OTHER@v1",
                                            "find": "a", "replace": "b", "reason": "r"}))
        ana = self.client(OWNER)
        shown = ana.get(f"/api/prepared/{got['prepared_id']}").json()
        self.fake.answers = [(200, {"id": "01ITEM", "eTag": '"{A1B2},4"'})]
        ana.post(f"/api/prepared/{got['prepared_id']}/confirm",
                 json={"preview_hash": shown["preview_hash"], "confirm": True})
        sent = self.fake.calls[-1][0]
        self.assertEqual((sent["method"], sent["headers"]["If-Match"]), ("PUT", etag))
        self.assertTrue(sent["url"].endswith("/drives/b!d1/items/01ITEM/content"))
        self.assertEqual(base64.b64decode(sent["body_b64"]),
                         before.replace(b"Unit 4", b"Dock 9"))
        audit = (self.root / "autopilot-write" / aw.AUDIT_FILE).read_text()
        self.assertNotIn(base64.b64encode(before).decode()[:200], audit)
        # the prior bytes wait in one owner-only file; the undo store names its digest
        store = (self.root / "autopilot-write" / aw.UNDO_FILE).read_text()
        self.assertNotIn(base64.b64encode(before).decode()[:200], store)
        blobs = list((self.root / "autopilot-write" / aw.UNDO_BLOBS).iterdir())
        self.assertEqual([b.stat().st_mode & 0o777 for b in blobs], [0o600])
        self.assertIn(f"blob:sha256:{blobs[0].name}", store)
        self.fake.answers = [(200, {"id": "01ITEM", "eTag": '"{A1B2},5"'})]
        self.assertEqual(ana.post(f"/api/prepared/{got['prepared_id']}/undo").json()["state"],
                         "undone")
        back = self.fake.calls[-1][0]
        self.assertEqual((back["headers"]["If-Match"], base64.b64decode(back["body_b64"])),
                         ('"{A1B2},4"', before))
        self.assertEqual(list((self.root / "autopilot-write" / aw.UNDO_BLOBS).iterdir()), [])
        # the egress guard: this shape only with a real If-Match
        env = {g.WRITES_FLAG: "sharepoint"}
        with self.assertRaises(g.ReadOnlyGuardError):
            g.check_write_request("PUT", "https://graph.microsoft.com" + sent["url"].split(
                "graph.microsoft.com", 1)[1], {"If-Match": "*"}, b"x", environ=env)
        self.assertEqual(g.check_write_request("PUT", sent["url"], {"If-Match": etag}, b"x",
                                               environ=env), "sharepoint")
        # staff with no recorded role on the file: refused before anything is prepared
        staff = Reader(session=session(STAFF), mode="mock", display_name="s", email=STAFF[0])
        self.backend.folder_acls = lambda drive, item: None
        refused = {t.spec["name"]: t for t in self.backend.prepared_tools(staff)}[
            "plx.edit_document"].call({"citation": "sharepoint://b!d1/01ITEM@v3",
                                       "find": "Thanks", "replace": "Regards", "reason": "r"})
        self.assertIn("permissions are not known", " ".join(refused["refused"]))
        # the document's bytes go with the item: kept while it may be retried, then pruned
        desk = self.backend._pw_desk(reader)
        blobs = self.root / "autopilot-write" / "prepared-writes" / "blobs"
        self.assertTrue(any(blobs.iterdir()))
        from datetime import datetime, timezone
        from operating import prepared_writes as pw
        desk.prune(datetime.now(timezone.utc) + pw.EXPIRES + pw.EXPIRES)
        self.assertEqual(list(blobs.iterdir()), [])

    @staticmethod
    def docx(paragraphs, header=""):
        import io
        import zipfile
        out = io.BytesIO()
        body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
            if header:
                z.writestr("word/header1.xml",
                           f"<w:hdr><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>")
        return out.getvalue()

    def test_document_edit_reads_through_the_same_filters_as_every_read(self):
        """Freightcom anywhere in the decoded document, or an excluded person in it: the
        edit is refused and no part of the text reaches the model (review probe)."""
        cite = "sharepoint://b!d1/01ITEM@v3"
        docs = {
            "freightcom line": ("notes.txt", b"Ship to Unit 4\nBook the pallet with "
                                b"Freightcom, quote FC-1234\nThanks\n", "FC-1234"),
            "freightcom in a docx header": ("plan.docx", self.docx(
                ["Ship to Unit 4", "Thanks"], header="Freightcom account 77"), "account 77"),
            "excluded person": ("notes.txt", b"Ship to Unit 4\ncc " + HIDDEN[0].encode()
                                + b"\nThanks\n", HIDDEN[0]),
            "excluded id": ("notes.txt", b"Ship to Unit 4 for " + HIDDEN[1].encode()
                            + b"\nThanks\n", HIDDEN[1]),
        }
        self.backend.autopilot_adapters = {
            "sharepoint_write": wa.SharePointAdapter(self.fake, environ=self.env)}
        reader = Reader(session=session(OWNER), mode="mock", display_name="a", email=OWNER[0])
        for name, (file, data, secret) in docs.items():
            with self.subTest(name):
                self.backend.prepared_doc_read = lambda r, c, f=file, d=data: (f, d, '"e,1"')
                edit = {t.spec["name"]: t for t in self.backend.prepared_tools(reader)}[
                    "plx.edit_document"]
                for args in ({"find": "Thanks", "replace": "Regards"},
                             {"find": "Unit 4", "replace": "Dock 9"}):
                    got = json.dumps(edit.call({"citation": cite, "reason": "r", **args}))
                    self.assertIn("refused", got)
                    for word in ("Freightcom", secret, "Ship to"):
                        self.assertNotIn(word, got)
        self.assertEqual(self.client(OWNER).get("/api/prepared").json()["items"], [])
        # an unavailable exclusion list refuses every document edit
        self.backend.autopilot_guard = PeopleExclusion(unavailable="excluded_people_oids_absent")
        self.backend.prepared_doc_read = lambda r, c: ("notes.txt", b"Ship to Unit 4\n", '"e"')
        edit = {t.spec["name"]: t for t in self.backend.prepared_tools(reader)}[
            "plx.edit_document"]
        self.assertIn("not loaded", " ".join(edit.call(
            {"citation": cite, "find": "Unit 4", "replace": "Dock 9", "reason": "r"})["refused"]))

    def test_a_word_edit_gives_the_edited_copy_now_and_writes_back_only_on_confirm(self):
        """The prepared edit of a .docx also keeps the edited file for the reader: the
        answer lists it to download at once (``assistant_tools.documents``), it holds the
        change and nothing else, no one else can fetch it, and SharePoint is written only
        by the confirm. A text file's edit keeps no copy (not one of the answer's kinds)."""
        import io
        import zipfile
        from operating import doc_edit
        from operator_api import assistant_tools
        b = self.backend
        b.people_guard = lambda: b.autopilot_guard
        b.instruct_uploads = None
        b.state_dir = self.root
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>The report "
                       "must be prepared by the client.</w:t></w:r></w:p><w:p><w:r><w:t>Notes"
                       "</w:t></w:r></w:p></w:body></w:document>")
        before = out.getvalue()
        b.autopilot_adapters = {
            "sharepoint_write": wa.SharePointAdapter(self.fake, environ=self.env)}

        def reader_of(who):
            return Reader(session=fixture_session("plx", [f"staff:{who[0].split('@')[0]}"],
                                                  channel=CHANNEL_CONSOLE,
                                                  subject_email=who[0], subject_oid=who[1]),
                          mode="mock", display_name=who[0], email=who[0])
        ana, sam = reader_of(OWNER), reader_of(STAFF)
        calls = len(self.fake.calls)

        def edit(name, data, find, replace):
            b.prepared_doc_read = lambda r, c: (name, data, '"e,1"')
            tool = {t.spec["name"]: t for t in b.prepared_tools(ana)}["plx.edit_document"]
            return tool.call({"citation": "sharepoint://b!d1/01ITEM@v3", "find": find,
                              "replace": replace, "reason": "You asked."})
        got = edit("Report May.docx", before, "prepared by the client.",
                   "prepared by the client and reviewed by the lead.")
        self.assertTrue(got["prepared_id"])
        copy = got["edited_copy"]
        self.assertEqual((copy["name"], copy["kind"]), ("Report May (edited).docx", "docx"))
        file_id = copy["download_url"].rsplit("/", 1)[-1]
        data = b._uploads().file(ana.key, file_id)
        self.assertEqual(doc_edit.text_of("x.docx", data).split(), (
            "The report must be prepared by the client and reviewed by the lead. Notes"
        ).split())
        self.assertEqual(copy["size"], len(data))
        self.assertIsNone(b._uploads().file(sam.key, file_id))
        self.assertEqual(len(self.fake.calls), calls)   # nothing written to SharePoint
        listed = assistant_tools.documents([
            {"type": "tool_call", "name": "mcp__plx__plx.edit_document",
             "result": json.dumps(got)},
            {"type": "tool_call", "name": "mcp__plx__plx.edit_document", "is_error": True,
             "result": json.dumps(got)}])
        self.assertEqual(listed, [{"name": "Report May (edited).docx", "kind": "docx",
                                   "size": len(data), "download_url": copy["download_url"]}])
        plain = edit("notes.txt", b"Ship to Unit 4\n", "Unit 4", "Dock 9")
        self.assertTrue(plain["prepared_id"])
        self.assertNotIn("edited_copy", plain)

    def test_an_attached_file_is_kept_as_the_persons_own_and_edited_into_a_copy(self):
        """An attached Word/PowerPoint file is kept for the person who attached it (the
        made documents' store) and the Ask's ``plx.edit_document`` edits it by the
        ``file:`` citation it is given: each edit is a copy for them only, a second edit
        goes on the copy and the answer lists the last copy once; nothing is prepared or
        written. Another reader, a session not the person's own, a file naming an
        excluded person or Freightcom, or no exclusion list: nothing kept, nothing
        edited."""
        import base64
        import io
        import zipfile
        from operating import ask_attachments, doc_edit
        from operator_api import assistant_tools
        from operator_api import models as m
        import types
        from operator_api.backend_ask import AskMixin
        b = self.backend
        # the Ask's own reading of attachments (the real backend's), on this backend
        b._attached = types.MethodType(AskMixin._attached, b)
        b._keep_attached = types.MethodType(AskMixin._keep_attached, b)
        b.people_guard = lambda: b.autopilot_guard
        b.instruct_uploads = None
        b.state_dir = self.root

        office = "http://schemas.openxmlformats.org/"
        W = office + "wordprocessingml/2006/main"
        P, A = office + "presentationml/2006/main", office + "drawingml/2006/main"
        R = office + "officeDocument/2006/relationships"
        PR = office + "package/2006/relationships"

        def docx(*paras):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>' + "".join(
                    f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
                    + "</w:body></w:document>")
            return out.getvalue()

        def pptx(*lines):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{P}" '
                           f'xmlns:r="{R}">'
                           '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                           "</p:presentation>")
                z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{PR}">'
                           '<Relationship Id="rId2" Target="slides/slide1.xml"/>'
                           '</Relationships>')
                z.writestr("ppt/slides/slide1.xml", f'<p:sld xmlns:p="{P}" xmlns:a="{A}">'
                           "<p:cSld><p:spTree>" + "".join(
                    f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
                    + "</p:spTree></p:cSld></p:sld>")
            return out.getvalue()

        contract = docx("1. Parties. PLX and Acme agree.",
                        "2. Payment terms. Invoices are payable Net 30 from the invoice date.",
                        "3. Delivery. FOB origin.")
        deck = pptx("Orders placed: 31", "On-time delivery: 97%")
        named = docx("Ask " + HIDDEN[0] + " about Net 30.")
        freight = docx("Freightcom quote: Net 30.")

        def request(*files):
            return m.AskRequest(question="change it", attachments=[
                {"name": n, "data": base64.b64encode(d).decode()} for n, d in files])

        def reader_of(who, **extra):
            return Reader(session=fixture_session("plx", [f"staff:{who[0].split('@')[0]}"],
                                                  channel=CHANNEL_CONSOLE,
                                                  subject_email=who[0], subject_oid=who[1],
                                                  **extra),
                          mode="mock", display_name=who[0], email=who[0])
        ana, sam = reader_of(OWNER), reader_of(STAFF)
        calls = len(self.fake.calls)
        batch = b._attached(request(("Supply agreement.docx", contract),
                                    ("review.pptx", deck), ("notes.txt", b"Net 30 here"),
                                    ("names.docx", named), ("fc.docx", freight)), ana)
        kept = [f.kept for f in batch.files]
        self.assertTrue(all(f.data is None for f in batch.files), "bytes outlived the read")
        self.assertTrue(kept[0] and kept[0].startswith("file:f_"))
        self.assertTrue(kept[1] and kept[1].startswith("file:f_"))
        # text is not an edited kind; an excluded person's or Freightcom's file is not kept
        self.assertEqual(kept[2:], [None, None, None])
        self.assertEqual(batch.files[4].unread, ask_attachments.WITHHELD)
        self.assertEqual(batch.files[4].text, "")
        lines = "\n".join(simple_ask.attached(batch.files)[0])
        self.assertIn(json.dumps(kept[0]), lines)
        self.assertNotIn("Freightcom", lines)
        # the meta the saved Ask keeps holds no handle to the file
        self.assertNotIn("f_", json.dumps([f.meta for f in batch.files]))

        def tool(who):
            return {t.spec["name"]: t for t in b.prepared_tools(who)}["plx.edit_document"]
        first = tool(ana).call({"citation": kept[0], "find": "Net 30", "replace": "Net 45",
                                "reason": "You asked."})
        self.assertNotIn("prepared_id", first)
        self.assertEqual(first["diff"][0]["after"],
                         "2. Payment terms. Invoices are payable Net 45 from the invoice date.")
        self.assertEqual(first["edited_copy"]["name"], "Supply agreement (edited).docx")
        second = tool(ana).call({"citation": first["citation"], "find": "FOB origin",
                                 "replace": "FOB destination", "reason": "You asked."})
        self.assertEqual(second["edited_copy"]["name"], "Supply agreement (edited).docx")
        last = b._uploads().file(ana.key, second["citation"][len("file:"):])
        self.assertEqual(doc_edit.text_of("x.docx", last).split(), (
            "1. Parties. PLX and Acme agree. 2. Payment terms. Invoices are payable Net 45 "
            "from the invoice date. 3. Delivery. FOB destination.").split())
        listed = assistant_tools.documents([
            {"type": "tool_call", "name": "mcp__plx__plx.edit_document",
             "result": json.dumps(r)} for r in (first, second)])
        self.assertEqual([d["download_url"] for d in listed],
                         [second["edited_copy"]["download_url"]])
        slide = tool(ana).call({"citation": kept[1], "find": "97%", "replace": "91%",
                                "reason": "You asked."})
        self.assertEqual(slide["diff"], [{"field": "slide 1", "before": "On-time delivery: 97%",
                                          "after": "On-time delivery: 91%"}])
        self.assertEqual(slide["edited_copy"]["kind"], "pptx")
        self.assertEqual(len(self.fake.calls), calls)        # nothing written anywhere
        # someone else's file, a made-up id, an unknown form: refused, nothing made
        for who, citation in ((sam, kept[0]), (ana, "file:f_" + "0" * 24), (ana, "file:x")):
            with self.subTest(citation=citation):
                got = tool(who).call({"citation": citation, "find": "Net 30",
                                      "replace": "Net 45", "reason": "You asked."})
                self.assertIn("refused", got)
                self.assertNotIn("edited_copy", got)
        # a change that would put an excluded person into the copy: refused
        got = tool(ana).call({"citation": kept[0], "find": "Net 30",
                              "replace": "Net 30, ask " + HIDDEN[0], "reason": "You asked."})
        self.assertIn("refused", got)
        # a session not the person's own keeps nothing
        from unittest import mock
        with mock.patch.object(type(ana.session), "view_as", new_callable=mock.PropertyMock,
                               return_value=True):
            self.assertIsNone(b._attached(request(("a.docx", contract)), ana).files[0].kept)
        # no exclusion list: nothing read, nothing kept
        b.people_guard = lambda: None
        off = b._attached(request(("a.docx", contract)), ana).files[0]
        self.assertIsNone(off.kept)
        self.assertIsNone(off.data)
        self.assertEqual(off.text, "")

    def test_a_word_file_whose_body_is_not_word_document_xml_is_read_and_edited_whole(self):
        """Live 11-18: a .docx keeps its body in word/document2.xml (its [Content_Types].xml
        names it): every edit of it was refused as "not in the document" and its text read
        as empty. The parts are taken from the package's own content types, for the edit
        and for the filters alike; a package with no main part is refused."""
        import io
        import zipfile
        wp = "application/vnd.openxmlformats-officedocument.wordprocessingml."

        def package(body, header="", main=True):
            out = io.BytesIO()
            types = ('<Types><Override PartName="/word/document2.xml" ContentType="'
                     + wp + ('document.main+xml' if main else 'styles+xml') + '"/>'
                     '<Override PartName="/word/header3.xml" ContentType="' + wp
                     + 'header+xml"/></Types>')
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("[Content_Types].xml", types)
                z.writestr("word/document2.xml", "<w:document><w:body>" + "".join(
                    f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in body)
                    + "</w:body></w:document>")
                z.writestr("word/header3.xml",
                           f"<w:hdr><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>")
            return out.getvalue()
        cite = "sharepoint://b!d1/01ITEM@v3"
        self.backend.autopilot_adapters = {
            "sharepoint_write": wa.SharePointAdapter(self.fake, environ=self.env)}
        reader = Reader(session=session(OWNER), mode="mock", display_name="a", email=OWNER[0])

        def edit(data, find="would constitute", replace="would count as"):
            self.backend.prepared_doc_read = lambda r, c: ("report.docx", data, '"e,1"')
            tool = {t.spec["name"]: t for t in self.backend.prepared_tools(reader)}[
                "plx.edit_document"]
            return tool.call({"citation": cite, "find": find, "replace": replace,
                              "reason": "r"})
        got = edit(package(["A Firm must declare it, which would constitute Stacking."]))
        self.assertEqual(got["diff"], [{
            "field": "paragraph",
            "before": "A Firm must declare it, which would constitute Stacking.",
            "after": "A Firm must declare it, which would count as Stacking."}])
        # the renamed header is read by the filters too: Freightcom there refuses it all
        refused = json.dumps(edit(package(["which would constitute it"],
                                          header="Freightcom account 77")))
        self.assertIn("refused", refused)
        self.assertNotIn("account 77", refused)
        # no main document part named: refused, never read as an empty document
        self.assertIn("could not be found", " ".join(
            edit(package(["which would constitute it"], main=False))["refused"]))

    def test_a_grant_revoked_after_prepare_is_refused_at_confirm_and_retry(self):
        grants = self.root / "grants.json"
        grants.write_text(json.dumps({"schema": "plx-write-grants/v1",
                                      "grants": {STAFF[0]: [f"planner:{PLAN}"]}}))
        self.env["PLX_WRITE_GRANTS_FILE"] = str(grants)
        pid = self.tool(STAFF).call(TASK)["prepared_id"]
        sam = self.client(STAFF)
        shown = sam.get(f"/api/prepared/{pid}").json()
        # The grant file emptied between prepare and confirm
        grants.write_text(json.dumps({"schema": "plx-write-grants/v1", "grants": {}}))
        got = sam.post(f"/api/prepared/{pid}/confirm",
                       json={"preview_hash": shown["preview_hash"], "confirm": True})
        self.assertEqual((got.status_code, self.fake.calls), (403, []))
        self.assertEqual(sam.get(f"/api/prepared/{pid}").json()["state"], "prepared")
        # dismissed, then prepared again: a new item checked anew (refused: no grant now)
        self.assertEqual(sam.post(f"/api/prepared/{pid}/dismiss").json()["state"], "dismissed")
        self.assertEqual(sam.post(f"/api/prepared/{pid}/retry").status_code, 422)
        grants.write_text(json.dumps({"schema": "plx-write-grants/v1",
                                      "grants": {STAFF[0]: [f"planner:{PLAN}"]}}))
        again = sam.post(f"/api/prepared/{pid}/retry").json()
        self.assertEqual(again["state"], "prepared")
        self.assertNotEqual(again["prepared_id"], pid)
        self.assertEqual(self.client(OWNER).post(f"/api/prepared/{pid}/retry").status_code, 404)

    def test_the_warm_spare_session_gets_the_readers_write_tools(self):
        from unittest import mock
        warm = simple_ask.WarmSessions()
        tool = self.tool(OWNER)
        got = {}

        class Session:
            alive = False
            def turn(self, prompt, on_line, cancel):
                return type("Ran", (), {"usage": {}})()
            def close(self):
                pass

        class Agent:
            def open_session(self, host):
                return Session()
        with mock.patch.object(simple_ask, "scope_digest", lambda a, s: "d"), \
                mock.patch.object(simple_ask, "host_for", lambda a, s: object()), \
                mock.patch.object(simple_ask, "agent_for", lambda a, o=None, t=(): Agent()), \
                mock.patch.object(warm, "prepare",
                                  lambda asker, session, **kw: got.update(kw)):
            asker = type("Asker", (), {"_reader_key": lambda self, s: "ana"})()
            warm.turn(asker, session(OWNER), "c1", "Rename the task", [],
                      lambda line: None, None, tools=[tool])
        self.assertEqual(list(got.get("tools") or ()), [tool])

    def test_a_line_asking_for_something_done_goes_to_the_ask_with_every_tool(self):
        """The simple Ask takes a do-line unless it is one of the instruction engine's own
        flows; its tools are the reader's write, memory, template and document tools."""
        from unittest import mock
        from operator_api import models as m
        b = self.backend
        b.agent_ask = type("A", (), {"serves": lambda self, s: True})()
        reader = Reader(session=session(OWNER), mode="mock", display_name=OWNER[0],
                        email=OWNER[0])
        self.assertEqual(sorted(t.spec["name"] for t in simple_ask.write_tools(b, reader)),
                         ["plx.edit_document", "plx.make_document", "plx.prepare_write",
                          "plx.remember", "plx.template"])
        ask = lambda q, **kw: b.ask_does(reader, m.AskRequest(question=q, **kw))  # noqa: E731
        with mock.patch.dict("os.environ", {"PLX_ASK_SIMPLE": "1"}):
            self.assertTrue(ask("Write a memo of our late POs and email it to Sam"))
            self.assertTrue(ask("Save a template for supplier ship-date chasers",
                                context={"kind": "record", "object_id": "PurchaseOrder:1"}))
            for engine in (dict(context={"kind": "email", "handle": "graph://m/1"}),
                           dict(source="graph://m/1")):
                self.assertFalse(ask("Reply that it ships Friday", **engine))
            self.assertTrue(ask("Use my quote template for SO 5512"))
            for line in ("Create an order from this PO", "Every Monday, chase the late POs",
                         "Reply to Kim's email that PO 4411 ships Friday"):
                self.assertFalse(ask(line), line)
            with mock.patch.dict("os.environ", {"PLX_ASK_SIMPLE_DO": "0"}):
                self.assertFalse(ask("Write a memo of our late POs"))
        with mock.patch.dict("os.environ", {"PLX_ASK_SIMPLE": "0"}):
            self.assertFalse(ask("Write a memo of our late POs"))

    def test_agent_binds_the_readers_tools(self):
        class Agent:
            local_tools: dict = {}

        class Asker:
            def _agent_for(self, on_line):
                return Agent()
        tool = self.tool(OWNER)
        agent = simple_ask.agent_for(Asker(), tools=[tool])
        self.assertIs(agent.local_tools["plx.prepare_write"], tool)
        self.assertIn("plx.prepare_write", simple_ask.WRITE_RULE)


if __name__ == "__main__":
    unittest.main()
