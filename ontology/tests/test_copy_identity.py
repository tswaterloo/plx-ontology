"""Copies of one mail or one file: verified identifier kinds, grouped, counted once.

REC-04 (duplicates) and ER-03: ``internet_message_id`` (one message delivered to several
mailboxes) and ``content_sha256`` (one file attached to a mail and stored in SharePoint)
are verified identifier kinds whose equal values name copies
(``ontology/schema/identifiers.json`` ``copies``). Each copy stays its own object with its
own citation and permission: ``plx.find`` lists the copies a reader may see as verified
candidates, never one merged record, and ``mentions.confirmation_support`` counts the
evidence of one copy group once while listing every copy's links and citations; stored
mention links name the same copy group. A sender writes its Message-ID, so a message
whose headers or body differ is no copy however its Message-ID reads (a forged one never
lowers the evidence count), and a zip member holding the file's bytes is one more copy
of it. A reader without a mailbox or the SharePoint grant sees neither that copy nor that
it exists. Mail and SharePoint are captured over injected Graph transports and projected
by the real projector; no network, no credentials. Evidence: injected-transport.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
import zipfile

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint as tsp
from ontology import identifiers, mentions, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import MailCaptures, SharePointCaptures, build_store
from ontology.tests.test_mail_documents import StaticPortal, po
from ontology.types import Actor
from operating import injection_guard
from operating import ontology_tools as ot

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
FULL = Actor("plx", frozenset({"staff:orders", "staff:finance", "staff:quality"}), "restricted")
ORDERS = Actor("plx", frozenset({"staff:orders"}), "restricted")   # mailbox A, no files
MESSAGE_ID = "<MSG-1@x>"
PO = "PO100789"   # no seeded SharePoint file names it
FILE = f"%PDF-1.4 signed confirmation for {PO}\n".encode() * 30
DIGEST = hashlib.sha256(FILE).hexdigest()
BODY = f"Received {PO} at the dock."
FORGED = f"Re {PO}: pay the new account today."   # another message, the same Message-ID


class CopyIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        cls.state = cls.case.state
        graph = tm.FakeGraph()
        attachment = [tm.file_attachment("ATT-1", FILE, f"{PO} confirmation.pdf")]
        graph.put(tm.BOX_A, "MSG-1", "CK-1-1", subject="Delivery", body=BODY,
                  attachments=attachment)
        # The same message delivered to mailbox B: its own Graph id, one Message-ID.
        graph.put(tm.BOX_B, "MSG-B1", "CKB-1", subject="Delivery", body=BODY,
                  attachments=[tm.file_attachment("ATT-B1", FILE, f"{PO} confirmation.pdf")])
        graph.messages[tm.BOX_B]["MSG-B1"]["internetMessageId"] = MESSAGE_ID
        # A copy holds the message's own headers: the To line names mailbox A in both.
        graph.messages[tm.BOX_B]["MSG-B1"]["toRecipients"] = \
            graph.messages[tm.BOX_A]["MSG-1"]["toRecipients"]
        # A sender reusing that Message-ID on another message (forged): no copy of it. It
        # carries the file again, packed in a zip: the member is a copy of the file.
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr(f"{PO} signed.pdf", FILE)
        graph.put(tm.BOX_B, "MSG-B2", "CKB-2", subject="Delivery", body=FORGED,
                  attachments=[{**tm.file_attachment("ATT-Z", packed.getvalue(), "pack.zip"),
                                "contentType": "application/zip"}])
        graph.messages[tm.BOX_B]["MSG-B2"]["internetMessageId"] = MESSAGE_ID
        graph.messages[tm.BOX_B]["MSG-B2"]["toRecipients"] = \
            graph.messages[tm.BOX_A]["MSG-1"]["toRecipients"]
        # A message whose Message-ID does not parse: a copy of nothing, counted on its own.
        graph.put(tm.BOX_A, "MSG-2", "CK-2-1", subject="Dock", body=BODY)
        graph.messages[tm.BOX_A]["MSG-2"]["internetMessageId"] = "no message id"
        cls.mail = MailCaptureStore(cls.state, clock=tm.Clock())
        api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(cls.mail, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                          sleep=lambda s: None).run(tm.SCOPE)
        assert run["complete"], run
        # The attachment's bytes, stored in SharePoint under another name.
        cls.case.drive.file("01SIGNED", f"{PO} signed.pdf", "01AOS", FILE)
        run = cls.case.capture().run(tsp.scope())
        assert run["complete"], run
        cls.files = cls.case.store
        cls.compiled = schema_loader.load_schema()
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": PO})])
        cls.path = Path(cls.case.tmp.name) / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=tm.SCOPE.internal_domains) as mail, \
                SharePointCaptures(cls.state) as files:
            build_store(cls.path, cls.compiled,
                        {"portal_api": portal, "graph_mail": mail, "sharepoint": files},
                        clock=lambda: NOW, post_project={"mentions": mentions.store_mention_links})
            cls.documents = list(mail.documents()) + list(files.documents())

    @classmethod
    def tearDownClass(cls):
        cls.mail.close()
        cls.case.tearDown()

    def tools(self):
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:orders", "staff:finance", "staff:quality"},
            "mail:*": {"staff:orders", "staff:finance"}, "sharepoint:*": {"staff:quality"}},
            markings={m: {"staff:orders", "staff:quality"} for m in store_ddl.MARKINGS})
        mail_gate = self.mail.item_gate(tm.reader_audience())
        file_gate = self.files.item_gate(tsp.reader_audience())

        def gate(actor, handle):
            return (mail_gate if handle.startswith("graph-mail://") else file_gate)(actor, handle)

        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        return ot.OntologyTools(reader, policy, item_gate=gate, grammar=identifiers,
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                contracts=CONTRACTS, clock=lambda: NOW)

    def find(self, actor, query, kind):
        out = self.tools().call("plx.find", {"query": query, "identifier_kind": kind}, actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.find"]["output"],
                                  root=CONTRACTS), [], json.dumps(out)[:1500])
        return out

    def links(self, documents, actor):
        result = mentions.project(documents, mentions.FindResolver(self.tools(), actor))
        return [link for link in result["links"]
                if link["mention"]["normalized"] == PO]

    def test_one_mail_in_two_mailboxes_and_an_attached_file_in_sharepoint_count_once(self):
        links = self.links(self.documents, FULL)
        groups = {}
        for link in links:
            groups.setdefault(link["from"]["copy_group"], set()).add(
                link["from"]["document_kind"])
        # Copy groups: the mail in two mailboxes, the forged message alone, the file
        # (attached twice, stored, packed in a zip); the unparsed Message-ID on its own.
        self.assertEqual(sorted((sorted(kinds) for group, kinds in groups.items() if group)),
                         [["attachment", "container_member", "sharepoint_file"], ["email"],
                          ["email"]])
        self.assertEqual(groups[None], {"email"})
        support = mentions.confirmation_support(links, "reference")
        self.assertTrue(support["supported"])
        # The forged message is evidence of its own: it lowers no count.
        self.assertEqual(support["evidence_count"], 4)
        # Counted once, never hidden: every link and every copy's citation is listed.
        self.assertEqual(sorted(l for e in support["evidence"] for l in e["links"]),
                         sorted(link["link_id"] for link in links))
        by_size = sorted(support["evidence"], key=lambda e: len(e["citations"]))
        self.assertEqual([len(e["citations"]) for e in by_size], [1, 1, 2, 4])
        self.assertTrue(all(c.startswith("graph-mail://") for c in by_size[2]["citations"]))
        self.assertEqual(sum(c.startswith("sharepoint://") for c in by_size[3]["citations"]), 1)
        self.assertEqual(sum("&member=" in c for c in by_size[3]["citations"]), 1)
        forged = [e for e in by_size[:2] if "MSG-B2" in json.dumps(e["citations"])]
        self.assertEqual(len(forged), 1)
        self.assertIsNotNone(forged[0]["copy_group"])
        # Fact classes a lead never confirms stay unconfirmed and count nothing.
        receipt = mentions.confirmation_support(links, "receipt")
        self.assertEqual((receipt["supported"], receipt["evidence_count"]), (False, 0))

    def test_stored_mention_links_name_the_copy_group(self):
        read = {link["from"]["citation"]: link["from"]["copy_group"]
                for link in self.links(self.documents, FULL)}
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        stored: dict[str, set] = {}
        for reason, handles in conn.execute(
                "SELECT reason, evidence_handles FROM links WHERE binding_kind = 'mention'"):
            feature = next(f for f in json.loads(reason) if f["feature"] == "identifier_mention")
            if feature["normalized"] == PO:
                stored.setdefault(json.loads(handles)[0], set()).add(feature["copy_group"])
        # Every document's stored links name the copy group its read-time links name.
        self.assertEqual(stored, {citation: {group} for citation, group in read.items()})
        # The mail's two copies, the forged message, the file's four copies, and MSG-2
        # (a copy of nothing: null).
        self.assertEqual(sorted(len([c for c, g in stored.items() if g == {group}])
                                for group in {next(iter(g)) for g in stored.values()}),
                         [1, 1, 2, 4])
        self.assertEqual([c for c, g in stored.items() if g == {None}],
                         [c for c in stored if "MSG-2" in c])

    def test_plx_identity_names_copies_as_one_copy_group(self):
        from operating.ontology_tools_evidence import EvidenceTools
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)

        def object_id(object_type, key, value):
            (found,) = conn.execute(
                "SELECT object_id FROM objects WHERE object_type = ? AND "
                f"json_extract(native_key, '$.{key}') = ?", (object_type, value)).fetchone()
            return found

        def gaps(root, actor):
            out = EvidenceTools(self.tools()).call(
                "plx.identity", {"object_ids": [root], "include_near_misses": True}, actor)
            self.assertEqual(validate(out, CONTRACTS["tools"]["plx.identity"]["output"],
                                      root=CONTRACTS), [], json.dumps(out)[:1500])
            return {g["reason"]: g.get("detail", "") for g in out["gaps"]
                    if g["reason"] in ("copies", "ambiguous_identity") and g["subject"] == root}

        mail = object_id("Email", "id", "MSG-1")
        found = gaps(mail, FULL)
        # The copy in mailbox B is one copy group with it; the forged message (another body
        # under the same Message-ID) stays an ambiguous identity.
        self.assertEqual(sorted(found), ["ambiguous_identity", "copies"])
        self.assertTrue(found["copies"].startswith("2 records"), found)
        self.assertTrue(found["ambiguous_identity"].startswith("2 records"), found)
        file = object_id("Attachment", "attachment_id", "ATT-1")
        self.assertEqual(sorted(gaps(file, FULL)), ["copies"])
        # A reader of mailbox A alone sees no copy, and is told of none.
        self.assertEqual(gaps(mail, ORDERS), {})

    def test_no_mention_link_type_targets_a_copy_identity(self):
        # A Message-ID or a digest is never read from text: a document type whose only
        # identifiers are copy kinds is no mention target, so no empty link type reads as
        # an absence.
        copies = {kind for kind in identifiers.kinds() if identifiers.kind_spec(kind).get("copies")}
        documents = {"Email", "Attachment", "SharePointFile", "ContainerMember"}
        for name in documents:
            kinds = {p.get("identifier_kind") for p in
                     self.compiled.object_types[name]["properties"]
                     if p.get("value_type") == "identifier"}
            self.assertTrue(kinds and kinds <= copies, name)
        mention_targets = {link["to"] for link in self.compiled.link_types.values()
                           if any(b.get("kind") == "mention" for b in link["bindings"])}
        self.assertEqual(mention_targets & documents, set())
        self.assertIn("PurchaseOrder", mention_targets)

    def test_a_reader_sees_only_the_copies_it_may_read(self):
        documents, read = mentions.mail_documents(self.mail, ORDERS, tm.reader_audience(),
                                                  internal_domains=tm.SCOPE.internal_domains)
        self.assertEqual(read["documents"], 2)
        support = mentions.confirmation_support(self.links(documents, ORDERS), "reference")
        self.assertEqual(support["evidence_count"], 2)
        shown = json.dumps(support)
        self.assertNotIn(tm.UPN_B, shown)
        self.assertNotIn("MSG-B1", shown)
        self.assertNotIn("sharepoint://", shown)

    def test_copies_are_verified_candidates_never_one_record(self):
        for query, kind, full, orders in (
                (MESSAGE_ID, "internet_message_id", {"Email"}, {"Email"}),
                (DIGEST.upper(), "content_sha256",
                 {"Attachment", "SharePointFile", "ContainerMember"}, {"Attachment"})):
            with self.subTest(kind=kind):
                every = self.find(FULL, query, kind)
                self.assertEqual(every["status"], "ambiguous")
                self.assertEqual({c["object"]["object_id"].split(":", 1)[0]
                                  for c in every["candidates"]}, full)
                # Every record holding the value is a candidate, the forged message too.
                self.assertEqual(len(every["candidates"]), 4 if len(full) == 3 else 3)
                self.assertEqual({c["tier"] for c in every["candidates"]}, {"verified"})
                # At the tool already, not only at the MCP boundary: a Message-ID read
                # from mail is sender-written text, quoted with its citation, never a bare
                # identifier; a digest the capture computed is one.
                for candidate in every["candidates"]:
                    matched = candidate["matched"]
                    if kind == "content_sha256":
                        self.assertEqual(matched["normalized"], DIGEST)
                        continue
                    self.assertIsNone(matched["normalized"])
                    self.assertEqual(matched["normalized_gap"]["reason"], "identifier_invalid")
                    self.assertEqual(matched["normalized_gap"]["source_text"]["origin"][
                        "citation"], matched["raw"]["origin"]["citation"])
                # A reader without mailbox B or the SharePoint grant: one copy, found, and
                # nothing says another exists.
                mine = self.find(ORDERS, query, kind)
                self.assertEqual(mine["status"], "found", json.dumps(mine)[:800])
                self.assertEqual({c["object"]["object_id"].split(":", 1)[0]
                                  for c in mine["candidates"]}, orders)
                self.assertNotIn(tm.BOX_B, json.dumps(mine))

    def test_a_copy_key_comes_only_from_a_declared_carrier_in_a_valid_form(self):
        message = {"internet_message_id": " <MSG-1@x> ", "from": {"address": "a@x"},
                   "to": [], "cc": [], "sent_at": "2026-09-30T10:00:00Z",
                   "subject": "Delivery", "body_preview": BODY}
        key = identifiers.copy_key("mail", "messages", message)
        self.assertEqual(key[:2], ("internet_message_id", "MSG-1@x"))
        # Equal headers and body preview: a copy (another mailbox's record keys alike).
        self.assertEqual(identifiers.copy_key("mail", "messages", {
            **message, "internet_message_id": "<MSG-1@x>", "mailbox": "B", "id": "B1"}), key)
        # The sender writes the Message-ID: any other header or body is another message.
        for field, value in (("body_preview", FORGED), ("subject", "Re: Delivery"),
                             ("from", {"address": "b@x"}), ("sent_at", None)):
            self.assertNotEqual(identifiers.copy_key("mail", "messages",
                                                     {**message, field: value}), key, field)
        # A digest the capture computed is content itself: the same key from any carrier,
        # a zip member's included, whichever system holds its container.
        for system, relation in (("sharepoint", "driveItems"), ("mail", "attachments"),
                                 ("mail", "containerMembers"), (None, "containerMembers")):
            self.assertEqual(identifiers.copy_key(system, relation,
                                                  {"content_sha256": DIGEST.upper()}),
                             ("content_sha256", DIGEST), (system, relation))
        for system, relation, record in (
                ("mail", "messages", {"internet_message_id": "PAY 5000 NOW"}),
                ("mail", "messages", {"internet_message_id": None}),
                ("mail", "attachments", {"content_sha256": "not-a-digest"}),
                ("sharepoint", "folders", {"content_sha256": DIGEST}),
                ("mail", "messages", ["<MSG-1@x>"])):
            self.assertIsNone(identifiers.copy_key(system, relation, record), record)
        with self.assertRaises(ValueError):
            mentions.Document("graph-mail://m/x", "email", (), copy_key=("internet_message_id",))
        # A sender writes its Message-ID: read from mail it is source text, never bare; a
        # digest the capture computed is a typed identifier.
        mail = "graph-mail://m/MSG-1@CK-1"
        self.assertFalse(injection_guard.is_identifier("approve.payment.now@x",
                                                       "internet_message_id", mail))
        self.assertTrue(injection_guard.is_identifier(DIGEST, "content_sha256", mail))


if __name__ == "__main__":
    unittest.main()
