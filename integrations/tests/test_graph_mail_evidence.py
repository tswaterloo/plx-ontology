"""Case mail evidence shapes in graph_evidence and the gated attachment read (injected).

Evidence label: ``injected-transport`` for ``GraphMailReader.get_attachment``
(the real guarded client over the adapter tests' ``FakeSender``); the
citation grammar, attachment decoding and people projection are pure.
"""

from __future__ import annotations

import base64
from hashlib import sha256
import unittest
from urllib.parse import unquote, urlsplit

from integrations import graph_read
from integrations.graph_evidence import (FILE_ATTACHMENT_TYPE, decode_file_attachment,
                                         is_mail_attachment_citation,
                                         mail_attachment_citation, mail_people,
                                         parse_mail_attachment_citation)
from integrations.http import SourceUnavailable
from integrations.live_source_guard import LiveSourceAudience, NotVisible
from integrations.tests.test_live_source_adapters import (ENV, MAILBOX, MSG_PATH, VINCE_ID,
                                                          FakeSender, j, message)
from ontology.core import Actor, ValidationError

BYTES = b"%PDF-1.4\n% attachment\n" + b"z" * 64
DIGEST = sha256(BYTES).hexdigest()
BASE = f"graph-mail://{VINCE_ID}/m1@ck1"
ACTOR = Actor("plx", frozenset({"staff:vince"}), "restricted")
OTHER = Actor("plx", frozenset({"staff:ops"}), "restricted")


def file_attachment(**overrides):
    raw = {"@odata.type": FILE_ATTACHMENT_TYPE, "id": "att-1", "name": "PI.pdf",
           "contentType": "application/pdf", "size": len(BYTES) + 200, "isInline": False,
           "contentBytes": base64.b64encode(BYTES).decode()}
    raw.update(overrides)
    return raw


class AttachmentCitationTests(unittest.TestCase):
    def test_citation_round_trips_and_pins_the_bytes(self):
        handle = mail_attachment_citation(BASE, "att-1", DIGEST)
        self.assertEqual(handle, f"{BASE}#attachment=att-1&sha256={DIGEST}")
        parsed = parse_mail_attachment_citation(handle)
        self.assertEqual((parsed["mailbox"], parsed["message_id"], parsed["change_key"],
                          parsed["attachment_id"], parsed["sha256"]),
                         (VINCE_ID, "m1", "ck1", "att-1", DIGEST))
        self.assertEqual(parsed["message_citation"], BASE)
        # Graph attachment ids end in '=' padding: still one stable handle.
        padded = mail_attachment_citation(BASE, "AAMkAD-att_1==", DIGEST)
        self.assertEqual(parse_mail_attachment_citation(padded)["attachment_id"],
                         "AAMkAD-att_1==")

    def test_malformed_handles_are_refused(self):
        for bad in (BASE, f"{BASE}#body_sha256={DIGEST}",
                    f"{BASE}#attachment=att-1", f"{BASE}#attachment=att-1&sha256=abc",
                    f"{BASE}#attachment=att-1&sha256={DIGEST}&x=1",
                    f"graph-mail://{MAILBOX}/m1@ck1#attachment=att-1&sha256={DIGEST}",
                    f"graph-mail://{VINCE_ID.upper()}/m1@ck1#attachment=a&sha256={DIGEST}",
                    f"sharepoint://d/i@e#attachment=a&sha256={DIGEST}"):
            self.assertFalse(is_mail_attachment_citation(bad), bad)
        with self.assertRaises(ValueError):
            mail_attachment_citation(f"{BASE}#body_sha256={DIGEST}", "att-1", DIGEST)
        with self.assertRaises(ValueError):
            mail_attachment_citation(BASE, "att/1", DIGEST)


class DecodeTests(unittest.TestCase):
    def test_file_attachment_decodes_and_hashes(self):
        metadata, content = decode_file_attachment(file_attachment(), "att-1")
        self.assertEqual(content, BYTES)
        self.assertEqual(metadata["content_sha256"], DIGEST)
        self.assertEqual(metadata["bytes"], len(BYTES))
        self.assertNotIn("contentBytes", metadata)

    def test_only_verified_file_attachments_carry_bytes(self):
        cases = {
            "another identity": file_attachment(id="att-2"),
            "item attachment": file_attachment(**{"@odata.type":
                                                  "#microsoft.graph.itemAttachment"}),
            "reference attachment": file_attachment(**{
                "@odata.type": "#microsoft.graph.referenceAttachment"}),
            "no bytes": {k: v for k, v in file_attachment().items() if k != "contentBytes"},
            "not base64": file_attachment(contentBytes="@@not-base64@@"),
            "bigger than reported": file_attachment(size=10),
        }
        for label, raw in cases.items():
            with self.assertRaises(SourceUnavailable, msg=label):
                decode_file_attachment(raw, "att-1")
        with self.assertRaises(SourceUnavailable):
            decode_file_attachment(file_attachment(), "att-1", max_bytes=10)


class PeopleTests(unittest.TestCase):
    def test_addresses_only_never_merged_and_mailbox_marked(self):
        records = [
            {"citation": "c1", "record": {"from": "Sales@Acme.example", "to": [MAILBOX],
                                          "cc": ["buyer@petralabx.com", "not an address"],
                                          "mailbox_upn": MAILBOX,
                                          "source_acl": {"identities": [VINCE_ID, MAILBOX]}}},
            {"citation": "c2", "record": {"from": "sales@acme.example",
                                          "to": ["sales2@acme.example"], "cc": [],
                                          "mailbox_upn": MAILBOX}}]
        people = {p["address"]: p for p in mail_people(
            records, internal_domains=frozenset({"petralabx.com"}))}
        self.assertEqual(sorted(people), ["buyer@petralabx.com", "sales2@acme.example",
                                          "sales@acme.example", MAILBOX])
        self.assertEqual(people["sales@acme.example"]["roles"], {"from": 2, "to": 0, "cc": 0})
        self.assertEqual(people["sales@acme.example"]["citations"], ["c1", "c2"])
        self.assertTrue(people[MAILBOX]["is_mailbox"])
        self.assertTrue(people["buyer@petralabx.com"]["internal"])
        self.assertFalse(people["sales@acme.example"]["internal"])
        self.assertTrue(all(p["identity_basis"] == "email_address"
                            and p["person_resolved"] is False for p in people.values()))
        with self.assertRaises(ValidationError):
            mail_people([{"record": {}}])


class GetAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = file_attachment()
        self.sender = FakeSender({
            f"{MSG_PATH}/m1": j(200, message("m1", "ck1")),
            f"{MSG_PATH}/m1/attachments/att-1": lambda url: j(200, self.raw),
            f"{MSG_PATH}/m-elsewhere": j(200, message("m-elsewhere", "ck1", folder="OTHER"))})
        api, _transport = graph_read.build_graph(ENV, sender=self.sender)
        audience = LiveSourceAudience({f"mail:{MAILBOX}": ["staff:vince"]}, tenant="plx")
        self.reader = graph_read.GraphMailReader(api, audience)

    def paths(self):
        return [unquote(urlsplit(c[1]).path) for c in self.sender.api_calls()]

    def test_bytes_and_citation_after_the_mailbox_and_message_checks(self):
        read = self.reader.get_attachment(ACTOR, MAILBOX, "m1", "att-1")
        self.assertEqual(read["content"], BYTES)
        self.assertEqual(read["citation"], f"{BASE}#attachment=att-1&sha256={DIGEST}")
        self.assertEqual(read["message"]["id"], "m1")
        paths = self.paths()
        # Directory, message metadata (provenance), then the attachment: GET only.
        self.assertLess(paths.index(f"{MSG_PATH}/m1"),
                        paths.index(f"{MSG_PATH}/m1/attachments/att-1"))
        self.assertTrue(all(c[0] == "GET" for c in self.sender.api_calls()))
        self.assertTrue(all(c[2].get("Prefer") == 'IdType="ImmutableId"'
                            for c in self.sender.api_calls() if "/attachments/" in c[1]))

    def test_no_attachment_request_unless_the_actor_may_read_the_mailbox(self):
        with self.assertRaises(NotVisible):
            self.reader.get_attachment(OTHER, MAILBOX, "m1", "att-1")
        with self.assertRaises(NotVisible):
            self.reader.get_attachment(ACTOR, MAILBOX, "m-elsewhere", "att-1")
        with self.assertRaises(NotVisible):
            self.reader.get_attachment(ACTOR, MAILBOX, "m1", "att/../1")
        self.assertFalse([p for p in self.paths() if "/attachments/" in p])

    def test_attachment_that_is_not_a_file_is_refused(self):
        self.raw = file_attachment(**{"@odata.type": "#microsoft.graph.itemAttachment"})
        with self.assertRaises(SourceUnavailable):
            self.reader.get_attachment(ACTOR, MAILBOX, "m1", "att-1")


if __name__ == "__main__":
    unittest.main()
