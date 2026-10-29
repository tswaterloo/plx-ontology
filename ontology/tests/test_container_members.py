"""Members of captured zip / .msg / .eml files are cited documents (gap-e10f050d25a3).

A zip mailed as an attachment or stored in SharePoint (a PO packet) is no longer one
textless file: each member is a ``ContainerMember`` object and a mention document of its
own, cited by the member's SHA-256 inside the container's pinned bytes; a classified
member is a typed document (``documentFields``); every member or part that was not read
is a member with its reason. A mailed ``.eml``/``.msg`` is read once: its own text under
its handle, each attachment (nested mail included) only under its member handle
(direction finding 11e322b9). Projected through the host's entry point over injected
Graph transports: no network, no credentials.
"""

from __future__ import annotations

from contextlib import ExitStack
from email.message import EmailMessage
from hashlib import sha256
import io
import json
import os
import sqlite3
import unittest
import zipfile

from integrations import captured_text as ct
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint as tsp
from integrations.tests.test_doc_convert import make_msg, msg_tree
from ontology.tests import test_graph_projection as tg
from operating import host

MEMBER_LINK = "ContainerMember.mentions_purchase_order"
CONFIRMATION = (b"ORDER CONFIRMATION\nConfirmation No: SC-77\nTotal Amount: USD 1,250.00\n"
                b"PO: PO100235\n")
NOTE = b"Re PO100236 freight booked"
LOGO = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
SECRET = b"%PDF-1.4 encrypted member"
BROKEN = b"PK\x03\x04 not a zip after all"


def zipped(members, encrypt=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    data = bytearray(buffer.getvalue())
    for name in encrypt:   # the encryption flag (bit 0), local and central headers
        for signature, flags, header in ((b"PK\x03\x04", 6, 30), (b"PK\x01\x02", 8, 46)):
            at = data.find(signature)
            while at >= 0:
                if data[at + header:at + header + len(name)] == name.encode():
                    data[at + flags] |= 1
                at = data.find(signature, at + 1)
    return bytes(data)


INNER = zipped([("note.txt", NOTE)])
EML = EmailMessage()
EML["From"], EML["To"], EML["Subject"] = "a@outside.example", "b@outside.example", "Fwd"
EML.set_content("See attached.")
EML.add_attachment(NOTE, maintype="text", subtype="plain", filename="fwd-note.txt")
PACKET = zipped([("confirmation.txt", CONFIRMATION), ("logo.png", LOGO),
                 ("nested/inner.zip", INNER), ("secret.pdf", SECRET),
                 ("forward.eml", EML.as_bytes())], encrypt=("secret.pdf",))
# A mailed .eml and .msg, each with a confirmation naming PO100234, a nested mail of
# the other format whose attachment names it again, and a carried mail (forwarded as an
# attachment: a message/rfc822 entity, an embedded .msg item) whose body names it.
DEEP = b"Re PO100234 freight booked"
CARRIED = "Re PO100234 pallets loaded"


def message(attachments, body="See attached."):
    found = EmailMessage()
    found["From"], found["To"], found["Subject"] = ("a@outside.example",
                                                    "b@outside.example", "Fwd")
    found.set_content(body)
    for name, content, mime in attachments:
        if isinstance(content, EmailMessage):   # carried: a message/rfc822 entity, no name
            found.add_attachment(content)
            continue
        maintype, subtype = mime.split("/")
        found.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    return found


def mail(attachments):
    return message(attachments).as_bytes()


def confirmation(number):
    return CONFIRMATION.replace(b"SC-77", number).replace(b"PO100235", b"PO100234")


OUTER_EML = mail([("conf-e.txt", confirmation(b"SC-78"), "text/plain"),
                  ("inner.msg", make_msg([("deep-m.txt", "text/plain", DEEP)]),
                   "application/vnd.ms-outlook"),
                  ("", message([], CARRIED), "message/rfc822")])
OUTER_MSG = make_msg([("conf-m.txt", "text/plain", confirmation(b"SC-79")),
                      ("inner.eml", "message/rfc822",
                       mail([("deep-e.txt", DEEP, "text/plain")])),
                      ("forwarded", "", msg_tree([], body=CARRIED, header=24))])


class ContainerMemberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        # N=60 cardinality probe: one SharePoint zip, every member a document.
        cls.notes = [(f"notes/n{i:02d}.txt", f"Re PO100236 batch {i}".encode())
                     for i in range(60)]
        cls.case.drive.file("01PACKET", "packet.zip", "01AOS", zipped(cls.notes),
                            mime="application/zip", acl=(tsp.RICARDO, tsp.SALES))
        # Item permissions: Ricardo only, so its members are withheld from every reader here.
        cls.case.drive.file("01PRIVATE", "private-packet.zip", "01AOS",
                            zipped([("private-terms.txt", b"Re PO100236 private terms")]),
                            mime="application/zip", acl=(tsp.RICARDO,))
        run = cls.case.capture().run(tsp.scope())
        assert run["complete"], run
        cls.state = cls.case.state
        graph = tm.FakeGraph()
        graph.put(tm.BOX_A, "MSG-Z", "CK-Z", subject="PO packet", body="Originals attached.",
                  attachments=[tg.attachment("ATT-Z", "po-packet.zip", PACKET,
                                             "application/zip"),
                               tg.attachment("ATT-X", "broken.zip", BROKEN,
                                             "application/zip")])
        graph.put(tm.BOX_A, "MSG-E", "CK-E", subject="Forwarded mail", body="Two attached.",
                  attachments=[tg.attachment("ATT-E", "fwd.eml", OUTER_EML, "message/rfc822"),
                               tg.attachment("ATT-M", "fwd.msg", OUTER_MSG,
                                             "application/vnd.ms-outlook")])
        cls.mail = tg.capture_mail(cls.state, graph)
        with ExitStack() as stack:
            cls.built = host.build_projection(cls.state, tg.sources(stack, cls.state))
        cls.path = host.projection_path(cls.state)
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.mail.close()
        cls.case.tearDown()

    def members(self, where, value):
        rows = self.conn.execute(
            "SELECT o.object_id, v.l1_handle, o.audience_key, o.native_key FROM objects o "
            "JOIN object_versions v ON v.object_id = o.object_id WHERE o.object_type = "
            f"'ContainerMember' AND json_extract(o.native_key, '$.{where}') = ?", (value,))
        return {json.loads(r["native_key"])["path"]: r for r in rows}

    def value(self, object_id, name):
        row = self.conn.execute("SELECT value, currency FROM object_values WHERE object_id = ? "
                                "AND property = ?", (object_id, name)).fetchone()
        return None if row is None else row["value"] if row["currency"] is None else (
            row["value"], row["currency"])

    def po(self, key):
        return self.conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                 "'PurchaseOrder' AND json_extract(native_key, '$.id') = ?",
                                 (key,)).fetchone()[0]

    def links(self, object_id, link_type=MEMBER_LINK):
        return self.conn.execute("SELECT to_object, reason FROM links WHERE link_type = ? AND "
                                 "from_object = ?", (link_type, object_id)).fetchall()

    def test_zip_attachment_members_are_cited_documents_and_unread_parts_are_gaps(self):
        self.assertEqual(self.built["post_project"]["mentions"]["counts"]["undeclared_target"],
                         0)
        found = self.members("attachment_id", "ATT-Z")
        self.assertEqual(set(found), {
            "confirmation.txt", "logo.png", "nested/inner.zip", "nested/inner.zip!/note.txt",
            "secret.pdf", "forward.eml", "forward.eml!/2"})
        states = {path: (self.value(r["object_id"], "state"), self.value(r["object_id"],
                                                                          "reason"))
                  for path, r in found.items()}
        self.assertEqual(states, {
            "confirmation.txt": ("read", None), "nested/inner.zip": ("container", None),
            "nested/inner.zip!/note.txt": ("read", None), "forward.eml": ("read", None),
            "forward.eml!/2": ("read", None),
            "logo.png": ("unread", "text_unsupported"),     # an image: no OCR at projection
            "secret.pdf": ("unread", "encrypted")})          # never decrypted, never dropped
        self.assertTrue(all(r["audience_key"] == f"mail:{tm.BOX_A}" for r in found.values()))
        # A member is cited by its own bytes inside the container's pinned bytes.
        confirmation = found["confirmation.txt"]
        container = (f"{tm.graph_read.mail_citation(tm.BOX_A, 'MSG-Z', 'CK-Z')}"
                     f"#attachment=ATT-Z&sha256={sha256(PACKET).hexdigest()}")
        self.assertEqual(confirmation["l1_handle"], ct.member_handle(
            container, "confirmation.txt", sha256(CONFIRMATION).hexdigest()))
        attachment = self.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'Attachment' AND "
            "json_extract(native_key, '$.attachment_id') = 'ATT-Z'").fetchone()[0]
        self.assertEqual([r["to_object"] for r in self.links(
            confirmation["object_id"], "ContainerMember.in_attachment")], [attachment])
        # Mentions: page/span cited to the member, resolvable only by a mailbox reader.
        rows = self.links(confirmation["object_id"])
        self.assertEqual({r["to_object"] for r in rows}, {self.po("p2"), self.po("b2")})
        [cited] = [f for f in json.loads(rows[0]["reason"])
                   if f["feature"] == "identifier_mention"]
        [mention] = cited["citations"]
        self.assertEqual(mention["l1_handle"], confirmation["l1_handle"])
        resolved = self.mail.resolve(mention["l1_handle"], tm.READER_A, tm.reader_audience(),
                                     text=True)
        start, end = cited["span"]
        self.assertEqual(resolved["row"]["content_text"][start:end], "PO100235")
        self.assertIsNone(self.mail.resolve(mention["l1_handle"], tg.OUTSIDER,
                                            tm.reader_audience(), text=True))
        # A container that cannot be opened is itself a visible gap, never nothing.
        [broken] = self.members("attachment_id", "ATT-X").values()
        self.assertEqual((self.value(broken["object_id"], "state"),
                          self.value(broken["object_id"], "reason")),
                         ("unread", "container_unreadable"))
        self.assertIsNotNone(self.mail.resolve(broken["l1_handle"], tm.READER_A,
                                               tm.reader_audience(), text=True))
        # Nested containers: an inner zip's member and an .eml's attachment link too.
        for path in ("nested/inner.zip!/note.txt", "forward.eml!/2"):
            self.assertEqual([r["to_object"] for r in self.links(found[path]["object_id"])],
                             [self.po("p3")], path)
        # The classified member is a typed document cited by the member's bytes.
        [typed] = [r for r in self.typed() if r["l1_handle"] == confirmation["l1_handle"]]
        self.assertEqual(self.value(typed["object_id"], "amount"), ("1250.00", "USD"))

    def typed(self):
        return self.conn.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id WHERE o.object_type = 'SupplierConfirmation'"
        ).fetchall()

    def test_a_mailed_eml_or_msg_cites_each_attachment_once_under_its_member(self):
        # Direction finding 11e322b9: the container's text inlined its attachments, so
        # PO100234 was a lead (and the confirmation a typed document) under the
        # container's handle and again under the member's. A carried mail (forwarded as
        # an attachment) is a member read for its own text too (gap-0d37a104a3db); with
        # no file name it is shown as <Subject>.eml.
        p1 = self.po("p1")
        rows = self.conn.execute("SELECT link_type, from_object FROM links WHERE to_object = ?",
                                 (p1,)).fetchall()
        cited = sorted((r["link_type"], r["from_object"]) for r in rows
                       if "MSG-E" in r["from_object"])   # this message's objects only
        expected, confirmations = [], set()
        for aid, conf, nested, deep, carried in (
                ("ATT-E", "conf-e.txt", "inner.msg", "deep-m.txt", "Fwd.eml"),
                ("ATT-M", "conf-m.txt", "inner.eml", "deep-e.txt", "forwarded")):
            found = {self.value(r["object_id"], "name") or path: (r, path)
                     for path, r in self.members("attachment_id", aid).items()}
            self.assertEqual(set(found), {conf, nested, deep, carried}, aid)
            # The nested mail is a member read for its own text; its attachment a member
            # of it, its text read under its own handle only.
            for mail_member in (nested, carried):
                self.assertEqual(self.value(found[mail_member][0]["object_id"], "state"),
                                 "read", aid)
            self.assertTrue(found[deep][1].startswith(found[nested][1] + "!/"), aid)
            self.assertEqual(self.mail.resolve(found[deep][0]["l1_handle"], tm.READER_A,
                                               tm.reader_audience(), text=True)
                             ["row"]["content_text"], DEEP.decode())
            # The carried mail's identifier is cited by its member handle and span.
            member = found[carried][0]
            [reason] = [json.loads(r["reason"]) for r in self.links(member["object_id"])]
            [mention] = [f for f in reason if f["feature"] == "identifier_mention"]
            self.assertEqual([c["l1_handle"] for c in mention["citations"]],
                             [member["l1_handle"]])
            text = self.mail.resolve(member["l1_handle"], tm.READER_A, tm.reader_audience(),
                                     text=True)["row"]["content_text"]
            self.assertIn(CARRIED, text, aid)
            self.assertEqual(text[slice(*mention["span"])], "PO100234", aid)
            found = {name: row for name, (row, _path) in found.items()}
            expected += [found[conf]["object_id"], found[deep]["object_id"],
                         member["object_id"]]
            confirmations.add(found[conf]["l1_handle"])
        # Each identifier once, under its member, never the container's (nor the mail's);
        # each typed confirmation confirms the PO once.
        self.assertEqual(sorted(c for t, c in cited if t == MEMBER_LINK), sorted(expected))
        self.assertEqual(sorted(t for t, _ in cited),
                         sorted([MEMBER_LINK] * 6 + ["SupplierConfirmation.confirms"] * 2))
        self.assertEqual({r["l1_handle"] for r in self.typed()} - {
            r["l1_handle"] for r in self.members("attachment_id", "ATT-Z").values()},
            confirmations)

    def test_sharepoint_zip_members_keep_every_row_and_the_item_permissions(self):
        found = self.members("item_id", "01PACKET")
        self.assertEqual(sorted(found), [name for name, _ in self.notes])   # none lost
        p3 = self.po("p3")
        packet = self.conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                   "'SharePointFile' AND json_extract(native_key, '$.id') = "
                                   "'01PACKET'").fetchone()[0]
        for name, content in self.notes:
            self.assertEqual([r["to_object"] for r in self.links(
                found[name]["object_id"], "ContainerMember.in_file")], [packet])
            member = found[name]
            self.assertEqual(member["l1_handle"].rsplit("member_sha256=", 1)[1],
                             sha256(content).hexdigest())
            self.assertEqual([r["to_object"] for r in self.links(member["object_id"])], [p3])
        handle = found["notes/n07.txt"]["l1_handle"]
        resolved = self.case.store.resolve(handle, tsp.QUALITY, tsp.reader_audience())
        self.assertEqual(resolved["row"]["content_text"], "Re PO100236 batch 7")
        self.assertIsNone(self.case.store.resolve(handle, tsp.OUTSIDER, tsp.reader_audience()))
        # A member path or digest the container does not hold resolves nowhere.
        for forged in (handle.replace("n07", "n99"), handle[:-4] + "0000",
                       handle + "&member=x"):
            self.assertIsNone(self.case.store.resolve(forged, tsp.QUALITY,
                                                      tsp.reader_audience()), forged)

    def test_a_restricted_reader_sees_no_member_through_any_consumer(self):
        base = tg.tools(self, self.path, self.mail, self.case.store)
        traverse = tg.tt.TraverseTools(base)
        evidence = tg.oe.EvidenceTools(base, traverse=traverse)
        process = tg.op.ProcessTools(base)
        aggregate = tg.oa.AggregateTools(base, traverse)
        members = [r["object_id"] for aid in ("ATT-Z", "ATT-E", "ATT-M")
                   for r in self.members("attachment_id", aid).values()]
        [private] = [r["object_id"] for r in self.members("item_id", "01PRIVATE").values()]
        for actor, visible in ((tg.ORDERS, True), (tg.OUTSIDER, False)):
            outputs = []
            for root in (self.po("p1"), self.po("p2"), self.po("p3")):
                around = traverse.call("plx.around", {"from": root, "path": [
                    {"link_type": MEMBER_LINK, "direction": "reverse"}],
                    "tier_floor": "lead"}, actor)
                outputs.append(around)
                reached = {o["object_id"] for o in around.get("objects", [])}
                self.assertEqual(bool(reached & set(members)), visible, actor)
                self.assertNotIn(private, reached, actor)
                outputs.append(evidence.call("plx.documents", {"object_id": root}, actor))
                outputs.append(process.call("plx.process", {"from": root}, actor))
                outputs.append(traverse.call("plx.timeline", {"from": root}, actor))
            outputs.append(base.call("plx.find", {"query": "confirmation",
                                                  "object_type": "ContainerMember"}, actor))
            outputs.append(aggregate.call("plx.aggregate", {
                "set": base.sets.new(actor, [*members, private]), "measure": {"op": "count"},
                "group_by": ["state"]}, actor))
            for member in members:
                shown = base.call("plx.object", {"object_id": member}, actor)
                self.assertEqual("error" not in shown, visible, (actor, shown))
            denied = base.call("plx.object", {"object_id": private}, actor)
            self.assertEqual(denied["error"]["code"], "object_not_found")
            text = json.dumps(outputs)
            for marker in ("01PRIVATE", "private-terms", "private-packet"):
                self.assertNotIn(marker, text, actor)
            if not visible:
                for marker in ("graph-mail://", "member=", "confirmation.txt", "secret.pdf"):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
