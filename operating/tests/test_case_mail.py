"""Mail inside cases: cited messages, attachments and people (injected transport only).

Evidence label: ``injected-transport``. The real GET-only ``GraphMailReader``
(``build_live_readers``) runs against the in-process ``FakeSender`` of the
adapter tests, the real ``WorkbenchReadApiClient`` against the source-mirror
tests' ``FakeWorkbench`` and the real ``MailCapture`` fills the versioned mail
capture (Graph delta); socket connections are refused for the whole module. Two
mailboxes are captured: the allowlisted shared ``order-confirmation@`` inbox (its UPN differs
from the allowlisted SMTP address) and a mailbox outside the case mail scope
whose mail cites the same PO number; the reader is even granted that mailbox
by the live audience, so only the scope keeps it out.
"""

from __future__ import annotations

import base64
import contextlib
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from integrations.document_understanding import ExtractedDocument
from integrations.live_source_guard import LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_live_source_adapters import (CEO_ID, ENV, VINCE_ID, FakeSender,
                                                          j)
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor, ValidationError
from operating.tests.firewall_view import client_result
from operating import case_mail
from operating.case_mail_scope import MailScope, MailScopeError
from operating.case_runtime import CaseRuntime, build_live_readers
from operating.cases import CitationUnresolvable
from integrations.capture_mail import CaptureScope, MailCapture, MailCaptureStore
from operating.mcp_server import source_read

PO_BOX = "order-confirmation@petrasoap.com"
PO_UPN = "order-confirmation@petrasoap.onmicrosoft.com"
OUTSIDE = "ceo@petralabx.com"
PO_BOX_USER = {"id": VINCE_ID, "userPrincipalName": PO_UPN, "mail": "Order-Confirmation@petrasoap.com",
               "proxyAddresses": ["SMTP:order-confirmation@petrasoap.com"]}
OUTSIDE_USER = {"id": CEO_ID, "userPrincipalName": OUTSIDE, "mail": OUTSIDE,
                "proxyAddresses": [f"SMTP:{OUTSIDE}"]}
DIRECTORY = {VINCE_ID: PO_BOX_USER, PO_UPN: PO_BOX_USER, PO_BOX: PO_BOX_USER,
             CEO_ID: OUTSIDE_USER, OUTSIDE: OUTSIDE_USER}
PO_PATH = f"/v1.0/users/{VINCE_ID}/messages"
OUT_PATH = f"/v1.0/users/{CEO_ID}/messages"

PURCHASING = Actor("plx", frozenset({"staff:purchasing", "staff:operations"}), "restricted")
OPERATIONS = Actor("plx", frozenset({"staff:operations"}), "restricted")
AGENT_ONLY = Actor("plx", frozenset({"agent:case-bot"}), "restricted")
PERSON = {"principal": "employee:0f0f0f0f-0000-4000-8000-00000000000a", "channel": "console",
          "session": "5" * 64}
MAIL_KEYS = [f"mail:{PO_BOX}", f"mail:{PO_UPN}", f"mail:{VINCE_ID}"]
OUT_KEYS = [f"mail:{OUTSIDE}", f"mail:{CEO_ID}"]
LIVE_AUDIENCE = {**{key: ["staff:purchasing", "svc:mail-capture", "agent:case-bot"]
                    for key in MAIL_KEYS},
                 **{key: ["staff:purchasing", "svc:mail-capture"] for key in OUT_KEYS}}
CAPTURE_ACTOR = Actor("plx", frozenset({"svc:mail-capture"}), "restricted")
CAPTURE_SCOPE = {"schema": "plx-mail-capture-scope/v1", "window_days": 90,
                 "internal_domains": ["petrasoap.com"],
                 "mailboxes": [{"address": PO_BOX, "folders": ["inbox"]},
                               {"address": OUTSIDE, "folders": ["inbox"]}]}

PI_PDF = b"%PDF-1.4\n% synthetic supplier proforma for PO100123\n" + b"y" * 120 + b"\n%%EOF\n"
PO_CATALOG = {"columns": ["id", "po_number", "supplier_id", "status", "date_generated",
                          "custom_fields", "updated_at"],
              "key": ["id"], "domain": "purchasing",
              "since": {"column": "updated_at", "mode": "timestamp-utc"}}
SUPPLIER_CATALOG = {"columns": ["id", "code", "name", "email", "website", "updated_at"],
                    "key": ["id"], "domain": "purchasing",
                    "since": {"column": "updated_at", "mode": "timestamp-utc"}}
PO_ROWS = [
    {"id": "po-a", "po_number": "PO100123", "supplier_id": "sup-acme", "status": "SUBMITTED",
     "date_generated": "2026-09-01T00:00:00Z", "custom_fields": None,
     "updated_at": "2026-09-28T08:00:00Z"},
    {"id": "po-b", "po_number": "PO100200", "supplier_id": "sup-acme", "status": "SUBMITTED",
     "date_generated": "2026-09-01T00:00:00Z", "custom_fields": None,
     "updated_at": "2026-09-28T08:00:00Z"}]
SUPPLIERS = [{"id": "sup-acme", "code": "ACME", "name": "Acme Bottles",
              "email": "purchasing@acme-bottles.com", "website": "https://www.acme-bottles.com",
              "updated_at": "2026-09-01T00:00:00Z"}]


def _msg(mid, subject, body, sender, *, conversation, folder="INBOX-V", attachments=False,
         received="2026-09-20T09:00:00Z", cc=()):
    return {"id": mid, "changeKey": "ck-" + mid, "subject": subject, "parentFolderId": folder,
            "lastModifiedDateTime": received, "receivedDateTime": received,
            "sentDateTime": received, "from": {"emailAddress": {"address": sender}},
            "toRecipients": [{"emailAddress": {"address": PO_BOX}}],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
            "conversationId": conversation, "internetMessageId": f"<{mid}@mail.example>",
            "webLink": "https://outlook.office365.com/owa/?ItemID=x",
            "bodyPreview": body[:40], "hasAttachments": attachments,
            "body": {"contentType": "text", "content": body}}


def _extractor(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
    text = "PROFORMA PI-SWC024613 for PO100123"
    return ExtractedDocument("application/pdf", "x", "extracted", "stub", text, (), (), (),
                             ({"page": 1, "start": 0, "end": len(text)},))


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


class CaseMailTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.mirror_audience = self.root / "mirror-audience.json"
        self.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.app_purchase_order": ["staff:operations"],
                          "public.app_supplier": ["staff:operations"]}}), encoding="utf-8")
        self.fake = FakeWorkbench()
        self.fake.catalog["public.app_purchase_order"] = dict(PO_CATALOG)
        self.fake.tables["public.app_purchase_order"] = [dict(r) for r in PO_ROWS]
        self.fake.catalog["public.app_supplier"] = dict(SUPPLIER_CATALOG)
        self.fake.tables["public.app_supplier"] = [dict(r) for r in SUPPLIERS]
        self.clock = Clock()
        self.pdf = PI_PDF
        self.messages = {
            "m-confirm": _msg("m-confirm", "Order confirmation PO100123",
                              "Dear Petra,\nWe confirm your PO100123.\nOur order no. SO-88812.\n",
                              "sales@acme-bottles.com", conversation="conv-1", attachments=True,
                              cc=("buyer@petrasoap.com",)),
            "m-reply": _msg("m-reply", "RE: Order confirmation", "Goods ship next week.",
                            "sales@acme-bottles.com", conversation="conv-1",
                            received="2026-09-22T09:00:00Z"),
            "m-other": _msg("m-other", "PO100200 query", "About PO100200 only.",
                            "ops@other.example", conversation="conv-2")}
        self.outside = {"m-ceo": _msg("m-ceo", "PO100123 private note",
                                      "Confidential PO100123 note.", "sales@acme-bottles.com",
                                      conversation="conv-1", folder="INBOX-C")}

        def listing(messages):
            return lambda url: j(200, {"value": [{k: v for k, v in m.items() if k != "body"}
                                                 for m in messages.values()]})

        def delta(user, folder, messages):
            link = (f"https://graph.microsoft.com/v1.0/users/{user}/mailFolders/{folder}"
                    "/messages/delta?$deltatoken=t1")
            return lambda url: j(200, {"value": [{k: v for k, v in m.items() if k != "body"}
                                                 for m in messages.values()],
                                       "@odata.deltaLink": link})

        routes = {PO_PATH: listing(self.messages),
                  f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages": listing(self.messages),
                  OUT_PATH: listing(self.outside),
                  f"/v1.0/users/{CEO_ID}/mailFolders/INBOX-C/messages": listing(self.outside),
                  f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages/delta":
                      delta(VINCE_ID, "INBOX-V", self.messages),
                  f"/v1.0/users/{CEO_ID}/mailFolders/INBOX-C/messages/delta":
                      delta(CEO_ID, "INBOX-C", self.outside)}
        for mid in self.messages:
            routes[f"{PO_PATH}/{mid}"] = (lambda m: lambda url: j(200, self.messages[m]))(mid)
        routes[f"{OUT_PATH}/m-ceo"] = lambda url: j(200, self.outside["m-ceo"])
        routes[f"{PO_PATH}/m-confirm/attachments"] = lambda url: j(200, {"value": [
            {"@odata.type": "#microsoft.graph.fileAttachment", "id": "att-pdf",
             "name": "PI-SWC024613.pdf", "contentType": "application/pdf",
             "size": len(self.pdf) + 300, "isInline": False},
            {"@odata.type": "#microsoft.graph.itemAttachment", "id": "att-item",
             "name": "Forwarded quote", "contentType": None, "size": 900,
             "isInline": False}]})
        routes[f"{PO_PATH}/m-confirm/attachments/att-pdf"] = lambda url: j(200, {
            "@odata.type": "#microsoft.graph.fileAttachment", "id": "att-pdf",
            "name": "PI-SWC024613.pdf", "contentType": "application/pdf",
            "size": len(self.pdf) + 300, "isInline": False,
            "contentBytes": base64.b64encode(self.pdf).decode()})
        self.sender = FakeSender(routes, directory=DIRECTORY)
        self.capture()

    def live(self, audiences=None):
        audience = LiveSourceAudience(LIVE_AUDIENCE if audiences is None else audiences,
                                      tenant="plx")
        return build_live_readers(audience, ENV, graph_sender=self.sender)

    def capture(self) -> None:
        store = MailCaptureStore(self.state)
        try:
            run = MailCapture(store, self.live().mail, CAPTURE_ACTOR).run(
                # The outside mailbox is captured on purpose (a store holding mail the
                # case scope must keep out); the shipped capture never requests it.
                CaptureScope.from_json(CAPTURE_SCOPE, allowlist=MailScope.unrestricted()))
        finally:
            store.close()
        self.assertTrue(run["complete"], run)

    def runtime(self, *, scope=True, audiences=None, live_mail=False) -> CaseRuntime:
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        # These readers run over an injected sender, which carries the named
        # opt-out; name the scope explicitly either way.
        options = {"mail_scope": MailScope.builtin() if scope else MailScope.unrestricted()}
        if live_mail:
            # The live reader's own rules (the state directory's capture is read first).
            options["mail_capture_dir"] = self.root / "no-mail-capture"
        runtime = CaseRuntime(self.state, client=client, audience=self.mirror_audience,
                              tenant="plx", live=self.live(audiences),
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
                              originals_options={"extractor": _extractor},
                              # The guard the host reads from the environ its live
                              # readers are built from (case_mail.main, host.py).
                              people_exclusion=PeopleExclusion.from_env(ENV), **options)
        self.addCleanup(runtime.close)
        return runtime

    def po_citation(self, runtime, ident="po-a") -> str:
        page = runtime.mirror.rows(PURCHASING, "public.app_purchase_order", limit=50)
        return next(item["citation"] for item in page["rows"] if item["key"]["id"] == ident)

    def po_case(self, runtime) -> str:
        return runtime.cases.open(PURCHASING, "Which mail belongs to this purchase order?",
                                  roots=[self.po_citation(runtime)],
                                  audience=["staff:purchasing", "staff:operations"])

    def project(self, runtime) -> None:
        """Project the state directory (mirror, mail capture, review store) into the
        store whose mention links relate mail to purchase orders."""
        from operating.case_workspace_live import project_state
        from operating.host import projection_path

        runtime.mirror.rows(PURCHASING, "public.app_supplier", limit=50)
        project_state(self.state, projection_path(self.state), tenant="plx",
                      internal_domains=["petrasoap.com"])

    def evidence(self, runtime, case_id, actor=PURCHASING, **kwargs):
        self.project(runtime)
        return case_mail.case_mail_evidence(runtime, actor, case_id,
                                            mail_capture_dir=self.state, **kwargs)

    def edit_links(self, message_id: str, change) -> None:
        """Rewrite the stored mention links of one message in the built projection
        (``change(row)``: the columns to set), as a later build could have written them."""
        from operating.host import projection_path

        with contextlib.closing(sqlite3.connect(projection_path(self.state))) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM links WHERE link_type = ? AND from_object LIKE ?",
                              ("Email.mentions_purchase_order",
                               f'%"{message_id}"%')).fetchall()
            self.assertTrue(rows)
            for row in rows:
                columns = change(row)
                db.execute(f"UPDATE links SET {', '.join(f'{c} = ?' for c in columns)} "
                           "WHERE link_id = ?", (*columns.values(), row["link_id"]))
            db.commit()

    @staticmethod
    def mention_relations(report, message_id):
        return [r for m in report["messages"] if f"/{message_id}@" in m["metadata_citation"]
                for r in m["relations"] if r["basis"] == "mention_link"]

    def calls_since(self, mark: int) -> list[str]:
        return [unquote(urlsplit(call[1]).path) for call in self.sender.calls[mark:]
                if "login.microsoftonline.com" not in call[1]]


class PoCaseMailTests(CaseMailTestCase):
    def test_po_case_cites_messages_attachments_and_people(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        report = self.evidence(runtime, case_id, record=True)
        by_id = {m["metadata_citation"].split("/")[-1].split("@")[0]: m
                 for m in report["messages"]}
        # The confirmation (exact PO number) and its thread reply (no number) belong
        # to the case; the other PO's mail does not.
        self.assertEqual(sorted(by_id), ["m-confirm", "m-reply"])
        confirm = by_id["m-confirm"]
        # The stored mention link decides: the number from the supplier's distinctive
        # domain is two agreeing features, a corroborated link (number identity).
        self.assertEqual({(r["basis"], r["status"], r["identity"], r["identity_asserted"],
                           r["requires_human_confirmation"])
                          for r in confirm["relations"] if r["basis"] == "mention_link"},
                         {("mention_link", "corroborated", "corroborated", True, False)})
        self.assertTrue(all(r["identity_asserted"] is False for r in confirm["relations"]
                            if r["basis"] != "mention_link"))
        self.assertEqual([r["basis"] for r in by_id["m-reply"]["relations"]],
                         ["same_conversation"])
        # Body-pinned message citation by the mailbox object id (not the address).
        self.assertTrue(confirm["citation"].startswith(f"graph-mail://{VINCE_ID}/m-confirm@"))
        self.assertIn("#body_sha256=", confirm["citation"])
        # The PDF attachment is cited by the hash of its bytes; the attached item is
        # listed but has no bytes to cite.
        attachments = {a["id"]: a for a in confirm["attachments"]}
        self.assertEqual(attachments["att-pdf"]["citation"],
                         f"graph-mail://{VINCE_ID}/m-confirm@ck-m-confirm"
                         f"#attachment=att-pdf&sha256={sha256(PI_PDF).hexdigest()}")
        self.assertIsNone(attachments["att-item"]["citation"])
        self.assertEqual(attachments["att-item"]["not_cited"], "not_a_file_attachment")
        # People: sender and recipients by address, the inbox marked as the mailbox.
        people = {p["address"]: p for p in report["people"]}
        self.assertEqual(sorted(people), ["buyer@petrasoap.com", PO_BOX,
                                          "sales@acme-bottles.com"])
        self.assertEqual(people["sales@acme-bottles.com"]["roles"], {"from": 2, "to": 0, "cc": 0})
        self.assertFalse(people["sales@acme-bottles.com"]["internal"])
        self.assertTrue(people[PO_BOX]["is_mailbox"])
        self.assertTrue(people["buyer@petrasoap.com"]["internal"])
        self.assertTrue(all(p["person_resolved"] is False for p in people.values()))
        self.assertEqual(len(people["sales@acme-bottles.com"]["citations"]), 2)
        # The case itself now cites the message, its attachment and the PO row.
        self.assertEqual(report["counts"]["findings_recorded"], 2)
        view = runtime.cases.get(PURCHASING, case_id)
        cited = {c["handle"] for f in view["findings"]["supported"] for c in f["citations"]}
        self.assertIn(confirm["citation"], cited)
        self.assertIn(attachments["att-pdf"]["citation"], cited)
        self.assertIn(self.po_citation(runtime), cited)
        statement = next(f["statement"] for f in view["findings"]["supported"]
                         if confirm["citation"] in {c["handle"] for c in f["citations"]})
        self.assertIn("PO100123 by mention_link (corroborated; identity: corroborated)",
                      statement)
        self.assertIn("not proof of the quantities, prices or dates", statement)
        # Recording again adds nothing (already cited).
        again = self.evidence(runtime, case_id, record=True)
        self.assertEqual(again["counts"]["findings_recorded"], 0)
        self.assertEqual(again["counts"]["findings_already_cited"], 2)

    def test_a_stored_corroborated_link_relates_mail_and_a_bare_mention_is_a_lead(self):
        # ER-09: case mail reads the projection's stored mention links (and the review
        # store), the one path plx.object and plx.around read. The confirmation from the
        # supplier's domain is corroborated (two features agree); the other PO's mail names
        # its number from an unknown domain: the mention alone, a lead for a person.
        runtime = self.runtime()
        case_a = self.po_case(runtime)
        case_b = runtime.cases.open(PURCHASING, "Which mail belongs to this purchase order?",
                                    roots=[self.po_citation(runtime, "po-b")],
                                    audience=["staff:purchasing", "staff:operations"])
        self.project(runtime)
        first = case_mail.case_mail_evidence(runtime, PURCHASING, case_a,
                                             mail_capture_dir=self.state)
        second = case_mail.case_mail_evidence(runtime, PURCHASING, case_b,
                                              mail_capture_dir=self.state)

        def stored(report, message_id):
            [message] = [m for m in report["messages"]
                         if f"/{message_id}@" in m["metadata_citation"]]
            return [r for r in message["relations"] if r["basis"] == "mention_link"]

        [confirm] = stored(first, "m-confirm")
        self.assertEqual((confirm["status"], confirm["identity"], confirm["identity_asserted"]),
                         ("corroborated", "corroborated", True))
        self.assertEqual(confirm["features"],
                         ["identifier_mention", "identity_candidate", "party_domain"])
        self.assertEqual(confirm["review"]["link_type"], "Email.mentions_purchase_order")
        self.assertEqual(first["mail_source"]["po_links"], "read")
        [other] = stored(second, "m-other")
        self.assertEqual((other["status"], other["identity_asserted"],
                          other["requires_human_confirmation"]), ("lead", False, True))
        self.assertNotIn("identity", other)
        self.assertIn("uncorroborated", other["why_not"])
        self.assertEqual(other["features"], ["identifier_mention", "identity_candidate"])

    def test_without_a_projection_no_po_relation_is_read_and_nothing_is_complete(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.state,
                                              ontology_store=self.root / "none.sqlite")
        self.assertEqual(report["mail_source"]["po_links"], "projection_not_built")
        self.assertFalse(report["mail_source"]["complete"])
        self.assertEqual(report["messages"], [])

    def test_citations_are_stable_across_reads(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        first = self.evidence(runtime, case_id)
        second = self.evidence(runtime, case_id)
        self.assertEqual([m["citation"] for m in first["messages"]],
                         [m["citation"] for m in second["messages"]])
        self.assertEqual([a["citation"] for m in first["messages"] for a in m["attachments"]],
                         [a["citation"] for m in second["messages"] for a in m["attachments"]])

    def test_rereading_a_citation_returns_the_original_through_the_get_only_reader(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        report = self.evidence(runtime, case_id, record=True)
        confirm = next(m for m in report["messages"] if "/m-confirm@" in m["citation"])
        attachment = next(a for a in confirm["attachments"] if a["citation"])
        mark = len(self.sender.calls)
        read = runtime.read_mail_original(attachment["citation"], PURCHASING)
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["content"], PI_PDF)
        self.assertEqual(read["attachment"]["content_sha256"], sha256(PI_PDF).hexdigest())
        self.assertIs(read["mailbox_in_scope"], True)
        # The bytes came from a fresh GET of that attachment, nothing cached.
        self.assertIn(f"{PO_PATH}/m-confirm/attachments/att-pdf", self.calls_since(mark))
        self.assertTrue(all(call[0] == "GET" for call in self.sender.api_calls()))
        body = runtime.read_mail_original(confirm["citation"], PURCHASING)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["body_text"], self.messages["m-confirm"]["body"]["content"])
        self.assertEqual(body["record"]["from"], "sales@acme-bottles.com")
        # The roundtrip helper agrees: every citation reads back its cited bytes/body.
        result = case_mail.roundtrip(runtime, PURCHASING, report)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["bytes_match"] + result["body_match"], 3)
        self.assertEqual(result["outside_scope"], 0)
        # A bare message citation pins nothing; an agent identity gets no whole original.
        bare = confirm["metadata_citation"]
        self.assertEqual(runtime.read_mail_original(bare, PURCHASING)["error"],
                         "mail_citation_not_pinned")
        self.assertEqual(runtime.read_mail_original(attachment["citation"], AGENT_ONLY),
                         {"ok": False, "error": "citation_unresolvable",
                          "citation": attachment["citation"]})

    def test_changed_attachment_bytes_read_as_changed_and_never_support(self):
        runtime = self.runtime(live_mail=True)
        case_id = self.po_case(runtime)
        report = self.evidence(runtime, case_id, record=True)
        cited = next(a["citation"] for m in report["messages"] for a in m["attachments"]
                     if a["citation"])
        self.pdf = PI_PDF + b"\n% revised"
        self.messages["m-confirm"]["changeKey"] = "ck-m-confirm-2"  # Exchange bumps it
        view = runtime.cases.get(PURCHASING, case_id)
        self.assertEqual(len([f for f in view["findings"]["citation_content_changed"]
                              if cited in {c.get("handle") for c in f["citations"]}]), 1)
        read = runtime.read_mail_original(cited, PURCHASING)
        self.assertEqual(read["error"], "citation_changed_since")
        self.assertTrue(read["current_citation"].endswith(sha256(self.pdf).hexdigest()))
        view = runtime.cases.get(PURCHASING, case_id)
        changed = [f for f in view["findings"]["citation_content_changed"]
                   if cited in {c.get("handle") for c in f["citations"]}]
        self.assertEqual(len(changed), 1)
        self.assertNotIn(cited, {c["handle"] for f in view["findings"]["supported"]
                                 for c in f["citations"]})

    def test_case_views_do_not_redownload_an_unchanged_attachment(self):
        runtime = self.runtime(live_mail=True)
        case_id = self.po_case(runtime)
        report = self.evidence(runtime, case_id, record=True)
        cited = next(a["citation"] for m in report["messages"] for a in m["attachments"]
                     if a["citation"])
        path = f"{PO_PATH}/m-confirm/attachments/att-pdf"
        mark = len(self.sender.calls)
        for _ in range(3):
            view = runtime.cases.get(PURCHASING, case_id)
        self.assertNotIn(path, self.calls_since(mark))
        # The message itself is still re-read for the reader on every view.
        self.assertIn(f"{PO_PATH}/m-confirm", self.calls_since(mark))
        self.assertIn(cited, {c["handle"] for f in view["findings"]["supported"]
                              for c in f["citations"]})
        # A reader who may not read the mailbox is still refused (no cache bypass).
        self.assertEqual(runtime.cases.get(OPERATIONS, case_id)["findings"]["supported"], [])
        # A new changeKey downloads again; read_mail_original always downloads.
        self.messages["m-confirm"]["changeKey"] = "ck-m-confirm-2"
        mark = len(self.sender.calls)
        runtime.cases.get(PURCHASING, case_id)
        self.assertIn(path, self.calls_since(mark))
        mark = len(self.sender.calls)
        self.assertTrue(runtime.read_mail_original(cited, PURCHASING)["ok"])
        self.assertIn(path, self.calls_since(mark))

    def test_pdf_attachment_becomes_an_original_gated_by_the_mailbox(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        report = self.evidence(runtime, case_id, record=True, attach_pdf_originals=True)
        attachment = next(a for m in report["messages"] for a in m["attachments"]
                          if a.get("original"))
        original = attachment["original"]
        self.assertEqual(original["sha256"], sha256(PI_PDF).hexdigest())
        page = runtime.resolve_citation(original["page_citations"][0], PURCHASING)
        self.assertIn("PI-SWC024613", page["text"])
        self.assertEqual(page["provenance"]["source"], "graph-mail-attachment")
        self.assertEqual(page["provenance"]["source_citation"], attachment["citation"])
        # Operations shares the original's audience principal but cannot read the
        # mailbox: the live gate refuses the original too.
        with self.assertRaises(CitationUnresolvable):
            runtime.resolve_citation(original["citation"], OPERATIONS)
        view = runtime.cases.get(PURCHASING, case_id)
        cited = {c["handle"] for f in view["findings"]["supported"] for c in f["citations"]}
        self.assertIn(original["citation"], cited)


class PermissionTests(CaseMailTestCase):
    def test_reader_without_mailbox_access_sees_withheld_markers_only(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        self.evidence(runtime, case_id, record=True)
        report = self.evidence(runtime, case_id, actor=OPERATIONS)
        self.assertEqual(report["messages"], [])
        self.assertEqual(report["people"], [])
        self.assertEqual(report["counts"]["messages_visible"], 0)
        scopes = [m["scope"] for m in report["withheld"]["markers"]]
        self.assertIn("mailbox", scopes)
        self.assertEqual(scopes.count("case_finding"), 2)
        self.assertTrue(all(m["content"] == case_mail.WITHHELD
                            for m in report["withheld"]["markers"]))
        text = json.dumps(report)
        for secret in ("PO100123", "acme-bottles", "Order confirmation", "graph-mail://",
                       "PI-SWC", "SO-88812", PO_BOX):
            self.assertNotIn(secret, text)
        # The case shows the mail findings as withheld (no statement, no handle).
        view = runtime.cases.get(OPERATIONS, case_id)
        self.assertEqual(len(view["findings"]["withheld"]), 2)
        self.assertEqual(view["findings"]["supported"], [])
        self.assertNotIn("graph-mail://", json.dumps(view))

    def test_cited_mail_only_case_follows_the_capture_completeness(self):
        # With no PO root, the thread index alone decides whether
        # the related mail can be complete.
        runtime = self.runtime()
        citation = runtime.mail_message(PURCHASING, PO_BOX, "m-confirm")["citation"]
        case_id = runtime.cases.open(PURCHASING, "Which mail belongs to this message?",
                                     roots=[citation],
                                     audience=["staff:purchasing", "staff:operations"])
        report = self.evidence(runtime, case_id)
        self.assertEqual(report["counts"]["po_roots"], 0)
        self.assertEqual(report["mail_source"]["mail_capture"], "read")
        self.assertTrue(report["mail_source"]["complete"])
        self.assertEqual(report["counts"]["thread_siblings_read"], 1)   # m-reply
        store = MailCaptureStore(self.state)
        with store.db:
            store.db.execute("UPDATE folders SET status = 'incomplete', reason = 'page_budget'")
        store.close()
        report = self.evidence(runtime, case_id)
        self.assertFalse(report["mail_source"]["complete"])
        # No capture at all for this reader's state: not complete either.
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.root / "none")
        self.assertEqual(report["mail_source"]["mail_capture"], "store_absent")
        self.assertFalse(report["mail_source"]["complete"])
        # No mail_capture_dir configured: not_configured, never complete.
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=None)
        self.assertEqual(report["mail_source"]["mail_capture"], "not_configured")
        self.assertFalse(report["mail_source"]["complete"])
        # Without thread siblings to search, the cited message alone is complete.
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=None, thread=False)
        self.assertEqual(report["mail_source"]["mail_capture"], "not_used")
        self.assertTrue(report["mail_source"]["complete"])

    def test_mail_outside_the_scope_never_enters_a_case(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        mark = len(self.sender.calls)
        report = self.evidence(runtime, case_id, record=True)
        # The outside mailbox cites the same PO number and the same conversation
        # id, and the reader's audience grants it: it is still never read.
        paths = self.calls_since(mark)
        # Not even its directory entry is requested: the capture index is read
        # narrowed to the case mail scope (identities recorded at capture).
        self.assertFalse([p for p in paths if p.startswith(f"/v1.0/users/{CEO_ID}")])
        self.assertNotIn(CEO_ID, json.dumps(report))
        self.assertEqual(report["mail_source"]["index_mailboxes_outside_scope"], 1)
        self.assertEqual(report["counts"]["messages_outside_scope"], 0)
        view = runtime.cases.get(PURCHASING, case_id)
        self.assertNotIn(CEO_ID, json.dumps(view))
        # Directly: a scoped runtime refuses the mailbox before any message read ...
        mark = len(self.sender.calls)
        with self.assertRaises(CitationUnresolvable):
            runtime.mail_message(PURCHASING, OUTSIDE, "m-ceo")
        with self.assertRaises(CitationUnresolvable):
            runtime.list_live(PURCHASING, "mail", mailbox=OUTSIDE)
        self.assertEqual([p for p in self.calls_since(mark) if "/messages" in p], [])
        # ... and a citation minted without a scope cannot be recorded on a case.
        unscoped = self.runtime(scope=False)
        minted = unscoped.mail_message(PURCHASING, OUTSIDE, "m-ceo")["citation"]
        with self.assertRaises((CitationUnresolvable, ValidationError)):
            runtime.cases.finding(PURCHASING, case_id, "outside mail", "fact", "supported",
                                  [minted])
        self.assertEqual(runtime.read_live(minted, PURCHASING)["error"],
                         "citation_unresolvable")

    def test_scope_is_rechecked_on_the_record_read(self):
        # The directory lookup said in scope, the message record's mailbox
        # identities do not (directory changed between the two reads): refused.
        runtime = self.runtime()
        reader = runtime.live.mail
        real = reader.get_message

        def drifted(*args, **kwargs):
            record = real(*args, **kwargs)
            return {**record, "mailbox_upn": OUTSIDE,
                    "source_acl": {**record["source_acl"], "identities": [OUTSIDE]}}

        with patch.object(reader, "get_message", drifted):
            with self.assertRaises(CitationUnresolvable):
                runtime.mail_message(PURCHASING, PO_BOX, "m-confirm")

    def test_case_mail_refuses_an_unrestricted_runtime(self):
        runtime = self.runtime(scope=False)
        case_id = self.po_case(runtime)
        with self.assertRaises(case_mail.CaseMailError) as caught:
            self.evidence(runtime, case_id)
        self.assertEqual(caught.exception.code, "mail_scope_unrestricted")
        self.assertTrue(runtime.mail_scope.allows(["anything@example.com"]))
        self.assertFalse(self.runtime().mail_scope.is_unrestricted)

    def test_agent_identity_never_gets_attachment_bytes(self):
        runtime = self.runtime()
        with self.assertRaises(CitationUnresolvable):
            runtime.mail_attachment(AGENT_ONLY, PO_BOX, "m-confirm", "att-pdf")
        from operating.case_originals import OriginalError
        with self.assertRaises(OriginalError):
            runtime.attach_mail_attachment_original(AGENT_ONLY, PO_BOX, "m-confirm", "att-pdf")
        mixed = Actor("plx", frozenset({"agent:case-bot", "staff:purchasing"}), "restricted")
        read = runtime.mail_attachment(mixed, PO_BOX, "m-confirm", "att-pdf")
        self.assertEqual(read["content"], PI_PDF)  # granted by the person principal

    def test_a_corroboration_the_reader_may_not_read_is_shown_as_its_lead(self):
        # The link is corroborated by the supplier record; a reader the mirror audience
        # no longer grants the supplier relation sees the mention as a lead, and nothing
        # of the supplier.
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        self.project(runtime)
        self.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.app_purchase_order": ["staff:operations"],
                          "public.app_supplier": ["staff:finance"]}}), encoding="utf-8")
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.state)
        [confirm] = [r for m in report["messages"] if "/m-confirm@" in m["metadata_citation"]
                     for r in m["relations"] if r["basis"] == "mention_link"]
        self.assertEqual((confirm["status"], confirm["identity_asserted"]), ("lead", False))
        self.assertIn("corroboration_not_visible", confirm["why_not"])
        self.assertNotIn("party_domain", confirm["features"])
        self.assertNotIn("app_supplier", json.dumps(report))

    def test_an_unreadable_record_outside_the_corroboration_keeps_the_link_corroborated(self):
        # One rule with plx.object / plx.around: only the records the corroborating
        # features cite decide what this reader is shown. The link also cites a message
        # outside the reader's scope (no feature rests on it): it stays corroborated.
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        self.project(runtime)
        outside = f"graph-mail://{CEO_ID}/m-ceo@ck-m-ceo"
        self.edit_links("m-confirm", lambda row: {"evidence_handles": json.dumps(
            json.loads(row["evidence_handles"]) + [outside])})
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.state)
        [confirm] = self.mention_relations(report, "m-confirm")
        self.assertEqual((confirm["status"], confirm["identity_asserted"]),
                         ("corroborated", True))
        self.assertIn("party_domain", confirm["features"])
        self.assertNotIn("corroboration_not_visible", confirm["why_not"])

    def test_a_marked_link_is_counted_as_withheld_never_shown(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        self.project(runtime)
        self.edit_links("m-confirm", lambda row: {"markings": json.dumps(["finance"])})
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.state)
        # Its subject and its body each name the order: two stored links, both withheld,
        # so the message (and its thread) is not related at all.
        self.assertEqual(report["messages"], [])
        self.assertEqual(report["withheld"]["po_links"], 2)
        self.assertIn({"scope": "po_link", "content": case_mail.WITHHELD},
                      report["withheld"]["markers"])
        counted = case_mail.counts_only(report, evidence_label="injected-transport")
        self.assertEqual(counted["withheld"]["po_links"], 2)

    def test_mailbox_not_granted_by_the_audience_is_withheld(self):
        audiences = {key: ["svc:mail-capture"] for key in MAIL_KEYS + OUT_KEYS}
        runtime = self.runtime(audiences=audiences)
        case_id = self.po_case(runtime)
        report = self.evidence(runtime, case_id)
        self.assertEqual(report["messages"], [])
        self.assertGreaterEqual(report["withheld"]["mailboxes"], 1)


class CaseMailReviewTests(CaseMailTestCase):
    """ER-09: case mail reads a person's link decisions through the one review store."""

    def reject(self, runtime, report) -> dict:
        """A person rejects m-confirm's link to its order, citing the versions the
        projection holds (as plx.review does)."""
        from ontology import reviews as rv
        from operating.host import projection_path

        [message] = [m for m in report["messages"] if "/m-confirm@" in m["metadata_citation"]]
        [relation] = self.mention_relations(report, "m-confirm")
        review = relation["review"]
        [row] = [item for item in runtime.mirror.rows(
            PURCHASING, "public.app_purchase_order", limit=50)["rows"]
            if item["key"]["id"] == "po-a"]
        with contextlib.closing(sqlite3.connect(projection_path(self.state))) as db:
            [(digest,)] = db.execute("SELECT digest FROM object_versions WHERE l1_handle = ?",
                                     (message["metadata_citation"],)).fetchall()
        return rv.ReviewStore(rv.store_path(self.state)).record(
            tenant="plx", author=PERSON, decision="reject_link", subject=review["subject"],
            target=review["target"], link_type=review["link_type"], evidence=[
                {"object_id": review["subject"], "l1_handle": message["metadata_citation"],
                 "digest": digest},
                {"object_id": review["target"], "l1_handle": relation["po_citation"],
                 "digest": row["content_sha256"]}])[0]

    def test_case_mail_honours_a_person_s_rejected_link(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        [relation] = self.mention_relations(self.evidence(runtime, case_id), "m-confirm")
        self.assertEqual(relation["identity"], "corroborated")
        self.reject(runtime, self.evidence(runtime, case_id))
        # Recorded after the projection's build: read from the review store now ...
        after = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                             mail_capture_dir=self.state)
        self.assertEqual(self.mention_relations(after, "m-confirm"), [])
        self.assertFalse(after["identity_asserted"])
        # ... and the next build ends the stored link (ontology.reviews.link_step).
        self.assertEqual(self.mention_relations(self.evidence(runtime, case_id), "m-confirm"),
                         [])

    def test_a_rejection_after_the_build_is_compared_as_an_instant(self):
        # The build time written with an offset reads later as text than the rejection
        # recorded an hour after it in UTC: compared as text, the rejection would be taken
        # as already built in and the rejected link shown.
        from datetime import datetime, timedelta, timezone

        from operating.host import projection_path

        runtime = self.runtime()
        case_id = self.po_case(runtime)
        review = self.reject(runtime, self.evidence(runtime, case_id))
        recorded = datetime.fromisoformat(review["recorded_at"].replace("Z", "+00:00"))
        built = (recorded - timedelta(hours=1)).astimezone(timezone(timedelta(hours=5)))
        self.assertGreater(built.isoformat(), review["recorded_at"])
        # The projection still holds the link: it was built before the rejection.
        with contextlib.closing(sqlite3.connect(projection_path(self.state))) as db:
            db.execute("UPDATE store_meta SET value = ? WHERE key = 'built_at'",
                       (built.isoformat(),))
            db.commit()
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.state)
        self.assertEqual(self.mention_relations(report, "m-confirm"), [])
        # An unreadable build time fails closed: every rejection in force applies.
        with contextlib.closing(sqlite3.connect(projection_path(self.state))) as db:
            db.execute("UPDATE store_meta SET value = 'yesterday' WHERE key = 'built_at'")
            db.commit()
        report = case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                              mail_capture_dir=self.state)
        self.assertEqual(self.mention_relations(report, "m-confirm"), [])


class McpHostScopeTests(CaseMailTestCase):
    """The MCP host builds CaseRuntime without naming a scope (operating/mcp_server.py):
    the built-in scope applies there by default."""

    def server(self, runtime):
        from operating.mcp_server import MCPServer

        server = MCPServer(case_runtime=runtime, actor=PURCHASING)
        # The session's case-find store and working directory, never left to the GC
        # (it ran inside a later test's warning recorder: test_eval_unfamiliar_sample).
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "case-mail-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server, name, arguments):
        self.request_id = getattr(self, "request_id", 100) + 1
        response = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
        self.assertNotIn("error", response, response)
        return client_result(name, response["result"])  # through the output boundary

    def test_real_readers_never_carry_the_opt_out(self):
        from integrations.live_source_guard import urllib_sender

        audience = LiveSourceAudience(LIVE_AUDIENCE, tenant="plx")
        for live in (build_live_readers(audience, ENV),
                     build_live_readers(audience, ENV, graph_sender=urllib_sender)):
            self.assertIsNotNone(live.mail)
            self.assertIsNone(live.mail_scope)
            runtime = CaseRuntime(self.state, client=None, audience=self.mirror_audience,
                                  tenant="plx", live=live)
            self.addCleanup(runtime.close)
            self.assertFalse(runtime.mail_scope.is_unrestricted)
            self.assertEqual(runtime.mail_scope.addresses, MailScope.builtin().addresses)
        self.assertTrue(self.live().mail_scope.is_unrestricted)  # injected sender only

    def test_audience_granted_mailbox_outside_the_scope_is_refused_with_no_message_get(self):
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        live = self.live()
        live.mail_scope = None  # exactly what build_live_readers gives a real host
        runtime = CaseRuntime(self.state, client=client, audience=self.mirror_audience,
                              tenant="plx", live=live,
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
                              people_exclusion=PeopleExclusion.from_env(ENV))
        self.addCleanup(runtime.close)
        server = self.server(runtime)
        case_id = self.po_case(runtime)
        mark = len(self.sender.calls)
        for mailbox in (OUTSIDE, CEO_ID):
            # plx.read (the way a message enters a case) reads nothing outside the scope.
            read = self.call(server, "plx.read", {
                "citation": f"graph-mail://{mailbox.lower()}/m-ceo@ck-ceo"})["structuredContent"]
            self.assertEqual(read["status"], "unresolvable", read)
            self.assertNotIn("content", read)
            with self.assertRaises(RuntimeError):  # the driver read is refused too
                source_read(server, "live.list", {"source": "mail", "mailbox": mailbox})
        self.assertEqual([p for p in self.calls_since(mark) if "/messages" in p
                          or "/mailFolders" in p], [])
        view = runtime.cases.get(PURCHASING, case_id)
        self.assertNotIn(CEO_ID, json.dumps(view))
        self.assertNotIn("PO100123 private", json.dumps(view))
        # The allowlisted inbox still works through the same tools.
        listed = source_read(server, "live.list", {"source": "mail", "mailbox": PO_BOX})
        self.assertEqual(listed["source"], "mail")
        confirm = next(item["citation"] for item in listed["items"]
                       if "/m-confirm@" in item["citation"])
        read = self.call(server, "plx.read", {"citation": confirm})["structuredContent"]
        self.assertEqual(read["status"], "resolved", read)
        # The state directory's capture holds that version: read from it (no body pin to
        # cite instead) and citable as it is, through the one resolver.
        self.assertNotIn("cite_as", read)
        cited = self.call(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "The supplier confirmed the order by mail.",
            "kind": "fact", "status": "supported", "citations": [confirm]})
        self.assertFalse(cited["isError"], cited)


class ScopeTests(unittest.TestCase):
    def test_builtin_scope_is_the_reported_allowlist(self):
        scope = MailScope.builtin()
        self.assertEqual(len(scope.addresses), 14)
        self.assertEqual(scope.excluded, frozenset({"accounting1@petrasoap.com"}))
        self.assertTrue(scope.allows(["x-id", "purchase-order@petrasoap.com"]))
        self.assertTrue(scope.allows(["Shipmentconfirmation@PetraSoap.com"]))
        self.assertFalse(scope.allows(["accounting1@petrasoap.com"]))  # pending: fail closed
        self.assertFalse(scope.allows(["ceo@petralabx.com"]))
        self.assertFalse(scope.allows("purchase-order@petrasoap.com"))  # not an identity set
        self.assertTrue(scope.allows_record({"mailbox_upn": PO_UPN, "source_acl": {
            "identities": [VINCE_ID, PO_UPN, PO_BOX]}}))
        self.assertFalse(scope.allows_record({"mailbox_upn": PO_UPN}))

    def test_invalid_scope_documents_are_refused(self):
        base = {"schema": "plx-case-mail-scope/v1",
                "mailboxes": [{"address": "a@petrasoap.com", "status": "reported_allowlisted"}]}
        MailScope.from_json(base)
        for broken in ({**base, "schema": "x"}, {**base, "extra": 1},
                       {**base, "mailboxes": []},
                       {**base, "mailboxes": [{"address": "a@petrasoap.com",
                                               "status": "granted"}]},
                       {**base, "mailboxes": [{"address": "not-an-address",
                                               "status": "reported_allowlisted"}]},
                       {**base, "mailboxes": [{"address": "a@petrasoap.com",
                                               "status": "pending_confirmation"}]}):
            with self.assertRaises(MailScopeError):
                MailScope.from_json(broken)


class CliTests(CaseMailTestCase):
    def scan(self) -> tuple[int, str]:
        """The scan CLI over a capture of this world: (exit code, stdout)."""
        (self.root / "scope.json").write_text(json.dumps(CAPTURE_SCOPE), encoding="utf-8")
        (self.root / "live.json").write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": LIVE_AUDIENCE}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = case_mail.main(["scan", "--state-dir", str(self.root / "cli-state"),
                                   "--mail-capture-scope", str(self.root / "scope.json"),
                                   "--mirror-audience", str(self.mirror_audience),
                                   "--live-audience", str(self.root / "live.json"),
                                   "--principal", "staff:purchasing",
                                   "--principal", "staff:operations",
                                   "--withheld-principal", "staff:operations",
                                   "--attach-pdf-originals"],
                                  environ={**ENV, "PLX_WORKBENCH_API_URL": BASE,
                                           "PLX_WORKBENCH_API_KEY": KEY},
                                  graph_sender=self.sender, workbench_fetch=self.fake)
        return code, out.getvalue()

    def test_scan_prints_counts_only_and_checks_the_other_reader(self):
        code, text = self.scan()
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["capture"]["mailboxes_outside_scope"], 1)
        self.assertEqual(report["capture"]["mailboxes_in_scope"], 1)
        self.assertTrue(report["capture"]["complete"])
        self.assertEqual(report["totals"]["cases"], 2)  # PO100123 and PO100200 have mail
        self.assertGreaterEqual(report["totals"]["messages_visible"], 3)
        self.assertEqual(report["totals"]["attachments_cited"], 1)
        self.assertEqual(report["totals"]["roundtrip_mismatch"], 0)
        self.assertEqual(report["totals"]["outside_scope"], 0)
        self.assertEqual(report["totals"]["other_reader_messages_visible"], 0)
        self.assertTrue(all(c["other_reader"]["withheld_markers"] >= 1
                            for c in report["cases"]))
        for secret in ("PO100123", "PO100200", "acme-bottles", "graph-mail://", "api://",
                       "Order confirmation", "@", VINCE_ID, CEO_ID, "PI-SWC", "SO-88812"):
            self.assertNotIn(secret, text)

    def test_a_citation_unreadable_at_the_finding_write_refuses_that_finding_only(self):
        # Graph stops serving the attachment once its original is attached (throttled):
        # the finding citing it cannot re-read it, so that finding alone is refused and
        # counted and the scan prints its counts (live regression: the scan ended in an
        # uncaught CitationUnresolvable, exit 1, stdout not JSON).
        route = f"{PO_PATH}/m-confirm/attachments/att-pdf"
        served, throttled = self.sender.routes[route], []
        self.sender.routes[route] = lambda url: j(503, {}) if throttled else served(url)
        attach = CaseRuntime.attach_mail_attachment_original

        def attach_then_throttle(*args, **kwargs):
            try:
                return attach(*args, **kwargs)
            finally:
                throttled.append(True)

        captured = CaseRuntime.mail_capture

        def attachment_not_captured_once_throttled(runtime, handle, *args, **kwargs):
            # The capture read first would serve it: the attachment is read live here.
            if throttled and "#attachment=att-pdf" in handle:
                return None
            return captured(runtime, handle, *args, **kwargs)

        with patch.object(CaseRuntime, "attach_mail_attachment_original",
                          attach_then_throttle), \
                patch.object(CaseRuntime, "mail_capture", attachment_not_captured_once_throttled):
            code, text = self.scan()
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["totals"]["cases"], 2)
        self.assertEqual(report["totals"]["findings_not_recorded"], 1)
        self.assertGreaterEqual(report["totals"]["findings_recorded"], 2)
        self.assertEqual(sum(c["counts"]["findings_not_recorded"] for c in report["cases"]), 1)

    def test_the_live_check_spec_passes_on_the_injected_world(self):
        from verify import live_verify as lv

        root = Path(lv.ROOT)
        spec = json.loads((root / "verify/live-checks/case.mail_evidence.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec)
        argv = [part.replace("{tmp}", str(self.root / "spec-tmp")) for part in spec["command"]]
        argv = [str(root / part) if part.startswith("examples/") else part for part in argv]
        self.assertEqual(argv[:3], ["python3", "-m", "operating.case_mail"])
        out = io.StringIO()
        self.calls_before_spec = len(self.sender.calls)
        with contextlib.redirect_stdout(out):
            code = case_mail.main(argv[3:], environ={**ENV, "PLX_WORKBENCH_API_URL": BASE,
                                                     "PLX_WORKBENCH_API_KEY": KEY},
                                  graph_sender=self.sender, workbench_fetch=self.fake)
        document = json.loads(out.getvalue())
        # An injected transport can never pass as a live read; with the live label
        # every other expectation holds on this world.
        self.assertEqual(document["evidence_label"], "injected-transport")
        self.assertEqual(lv.check_expectations(spec, code, document),
                         ["evidence_label != expected"])
        self.assertEqual(lv.check_expectations(
            spec, code, {**document, "evidence_label": "live-read"}), [])
        self.assertGreaterEqual(document["totals"]["findings_recorded"], 2)
        # Cost bound: every Graph request of the run is counted, and directory
        # lookups are shared, so lookups stay far below the message reads.
        sent = document["graph_requests_sent"]
        self.assertEqual(sent, len(self.sender.calls) - self.calls_before_spec)
        users = [c for c in self.sender.calls[self.calls_before_spec:]
                 if urlsplit(c[1]).path.count("/") == 3 and "/v1.0/users/" in c[1]]
        reads = [c for c in self.sender.calls[self.calls_before_spec:]
                 if "/messages/" in c[1]]
        # One lookup set per mailbox in scope (the spec captures several), never
        # per message.
        self.assertLessEqual(len(users), document["capture"]["mailboxes_in_scope"] + 3)
        self.assertGreater(len(reads), len(users))
        recorded = json.dumps(lv._recorded(spec, document))
        for secret in ("PO100123", "acme-bottles", "graph-mail://", "@", VINCE_ID):
            self.assertNotIn(secret, recorded)


    def test_mail_refused_on_every_mailbox_is_blocked_upstream_never_a_product_fail(self):
        # Live 2026-10-17 on (case.mail_evidence daily): every in-scope mailbox's
        # directory lookup (graph.user) answered 403 Authorization_RequestDenied once
        # MICROSOFT_GRAPH_CLIENT_ID named an app without User.Read.All or Mail.Read; the
        # check failed as a product (no-source-read, blocked_upstream []). Now the refused
        # call and the missing grant are blocked parts and expect_blocked checks the rest,
        # which must still read every input no part stopped.
        from integrations.http import SourceHttpError
        from verify import live_verify as lv

        root = Path(lv.ROOT)
        spec = json.loads((root / "verify/live-checks/case.mail_evidence.json")
                          .read_text(encoding="utf-8"))
        lookup = {"part": "mail.mailbox_lookup", "source": "microsoft-graph",
                  "http_status": 403, "error_code": "missing_permission",
                  "permission": "User.Read.All", "cause": "host_misconfiguration"}

        def refusing(method, url, *rest):
            if (urlsplit(url).hostname == "graph.microsoft.com"
                    and unquote(urlsplit(url).path).count("/") == 3
                    and "/v1.0/users/" in url):
                return j(403, {"error": {"code": "Authorization_RequestDenied"}})
            return self.sender(method, url, *rest)

        def portal_down(url, headers, *, max_bytes):
            if "app_purchase_order/rows" in url:
                raise SourceHttpError("read-only source GET failed: HTTPError", status=503,
                                      error_code="database-login-failed")
            return self.fake(url, headers, max_bytes=max_bytes)

        def run(name, sender, fetch):
            argv = [part.replace("{tmp}", str(self.root / name)) for part in spec["command"]]
            argv = [str(root / part) if part.startswith("examples/") else part
                    for part in argv]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = case_mail.main(argv[3:], environ={**ENV, "PLX_WORKBENCH_API_URL": BASE,
                                                         "PLX_WORKBENCH_API_KEY": KEY},
                                      graph_sender=sender, workbench_fetch=fetch)
            return code, json.loads(out.getvalue())

        code, document = run("refused", refusing, self.fake)
        self.assertEqual(code, 0)
        self.assertEqual(document["capture"]["mailboxes_in_scope"], 6)
        self.assertEqual(document["capture"]["reasons"], ["forbidden"])
        self.assertEqual(document["blocked_upstream"], [lookup])
        self.assertEqual(document["source_read"]["unblocked_unread"], [])
        self.assertEqual(document["source_read"]["inputs_read"], 1)   # the PO relation
        self.assertNotEqual(lv.check_expectations(spec, code, document), [])
        self.assertEqual(lv.check_expectations(spec, code, document, spec["expect_blocked"]),
                         [])
        self.assertEqual([p["cause"] for p in lv.blocked_parts(document)],
                         ["host_misconfiguration"])
        recorded = lv._recorded(spec, document)
        self.assertEqual(recorded["blocked_upstream[*].permission"], ["User.Read.All"])
        self.assertNotIn("@", json.dumps(recorded))

        # The rest must still read: refused mail beside an empty PO relation fails.
        self.fake.tables["public.app_purchase_order"] = []
        code, empty = run("refused-empty", refusing, self.fake)
        self.assertEqual(empty["source_read"]["unblocked_unread"], ["po_relation"])
        self.assertIn("source_read.unblocked_unread not empty",
                      lv.check_expectations(spec, code, empty, spec["expect_blocked"]))
        self.fake.tables["public.app_purchase_order"] = [dict(r) for r in PO_ROWS]

        # The Portal blocked and mail read: blocked, and mail is what was measured.
        code, portal = run("portal-down", self.sender, portal_down)
        self.assertEqual([p["part"] for p in portal["blocked_upstream"]], ["po_source"])
        self.assertGreaterEqual(portal["source_read"]["mail_messages"], 1)
        self.assertEqual(lv.check_expectations(spec, code, portal, spec["expect_blocked"]),
                         [])
        # Both blocked: nothing was measured, so the blocked expectation fails too.
        code, both = run("both-down", refusing, portal_down)
        self.assertEqual([p["part"] for p in both["blocked_upstream"]],
                         ["po_source", "mail.mailbox_lookup"])
        self.assertIn("source_read.inputs_read < 1",
                      lv.check_expectations(spec, code, both, spec["expect_blocked"]))


if __name__ == "__main__":
    unittest.main()
