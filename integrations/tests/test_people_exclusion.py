"""The people-exclusion guard (access record 64fca27acc88) over an injected transport.

Mail, Teams chats and OneDrives of excluded people are refused before any request,
keyed by Entra object ID; a message an excluded person sent is dropped before it is
stored or indexed; with no ID list every gated read fails closed. Every ID and
address here is synthetic. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from integrations import graph_read, people_exclusion
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.people_exclusion import (ExclusionUnavailable, PeopleExcluded,
                                           PeopleExclusion)
from integrations.live_source_guard import BoundedBackoff, NotVisible
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint_estate as te
from integrations.tests import test_capture_teams as tc
from integrations.tests import test_live_source_adapters as ta
from integrations.capture_store import CaptureError
from integrations.capture_teams import CONVERSATION, Conversation, TeamsCapture, open_store

EXCLUDED = tm.BOX_B                         # the synthetic excluded person's mailbox
EXCLUDED_UPN = tm.UPN_B
ENV_EXCLUDING_B = {**tm.ENV, people_exclusion.ENV_IDS: EXCLUDED}
ENV_ABSENT = {k: v for k, v in tm.ENV.items() if k != people_exclusion.ENV_IDS}


G1, G2, G3 = (f"abcdef00-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))


class StubApi:
    """``get_pages`` only: the directory listing ``bind`` reads."""

    def __init__(self, users=(), complete=True, fail=None):
        self.users, self.complete, self.fail, self.calls = list(users), complete, fail, 0

    def get_pages(self, url, **_kwargs):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.users, {"complete": self.complete}


class GuardConfigTests(unittest.TestCase):
    def test_absent_empty_or_invalid_lists_fail_closed(self):
        cases = {"absent": ({}, "excluded_people_oids_absent"),
                 "empty": ({people_exclusion.ENV_IDS: " , "}, "excluded_people_oids_absent"),
                 "invalid": ({people_exclusion.ENV_IDS: f"{EXCLUDED},not-an-id"},
                             "excluded_people_oids_invalid")}
        for name, (environ, code) in cases.items():
            with self.subTest(name):
                guard = PeopleExclusion.from_env(environ)
                self.assertEqual(guard.unavailable, code)
                with self.assertRaises(ExclusionUnavailable) as raised:
                    guard.is_excluded(tm.BOX_A)
                self.assertEqual(raised.exception.code, code)
                self.assertIsInstance(raised.exception, NotVisible)

    def test_a_quoted_list_reads_the_same_ids(self):
        # A shell that keeps the env file's quotes must not turn the guard off.
        for raw in (f"'{EXCLUDED}'", f'"{EXCLUDED}"', f"'{EXCLUDED}, {tm.BOX_A}'"):
            with self.subTest(raw):
                guard = PeopleExclusion.from_env({people_exclusion.ENV_IDS: raw})
                self.assertIsNone(guard.unavailable)
                self.assertTrue(guard.is_excluded(EXCLUDED))

    def test_the_committed_config_names_nobody(self):
        config = json.loads(people_exclusion.CONFIG.read_text())
        self.assertEqual(config["schema"], people_exclusion.SCHEMA)
        self.assertEqual(config["object_ids"], [])

    def test_id_file_must_be_absolute_readable_and_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "ids.json"
            outside.write_text(json.dumps({"schema": people_exclusion.SCHEMA,
                                           "object_ids": [EXCLUDED]}))
            guard = PeopleExclusion.from_env({people_exclusion.ENV_FILE: str(outside)})
            self.assertIsNone(guard.unavailable)
            self.assertTrue(guard.is_excluded(EXCLUDED.upper()))
            self.assertFalse(guard.is_excluded(tm.BOX_A))
            self.assertEqual(PeopleExclusion.from_env({people_exclusion.ENV_FILE: "ids.json"})
                             .unavailable, "excluded_people_file_unreadable")
            outside.write_text("{not json")
            self.assertEqual(PeopleExclusion.from_env({people_exclusion.ENV_FILE: str(outside)})
                             .unavailable, "excluded_people_file_unreadable")
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            inside = repo / "ids.json"
            inside.write_text(json.dumps({"schema": people_exclusion.SCHEMA,
                                          "object_ids": [EXCLUDED]}))
            self.assertEqual(PeopleExclusion.from_env({people_exclusion.ENV_FILE: str(inside)})
                             .unavailable, "excluded_people_file_in_git")

    def test_addresses_need_a_complete_directory_listing(self):
        guard = PeopleExclusion([EXCLUDED])
        with self.assertRaises(ExclusionUnavailable):
            guard.is_excluded(EXCLUDED_UPN)          # unbound: an address is undecided
        for api in (StubApi(complete=False), StubApi(fail=RuntimeError("boom"))):
            with self.subTest(api.complete):
                guard = PeopleExclusion([EXCLUDED])
                with self.assertRaises(ExclusionUnavailable):
                    guard.bind(api)
                self.assertEqual(guard.unavailable, "excluded_people_unresolved")
                with self.assertRaises(ExclusionUnavailable):
                    guard.is_excluded(tm.BOX_A)      # one failed bind closes everything
        guard = PeopleExclusion([EXCLUDED])
        guard.bind(StubApi([{"id": EXCLUDED, "userPrincipalName": "B.Person@Example-plx.com",
                             "mail": EXCLUDED_UPN, "proxyAddresses": ["smtp:alias@x.example"]},
                            {"id": tm.BOX_A, "userPrincipalName": tm.UPN_A}]))
        for value in (EXCLUDED_UPN, "b.person@example-plx.com", "ALIAS@x.example", EXCLUDED):
            self.assertTrue(guard.is_excluded(value), value)
        self.assertFalse(guard.is_excluded(tm.UPN_A))
        with self.assertRaises(PeopleExcluded) as raised:
            guard.refuse_onedrive_path("b_person_example-plx_com")
        self.assertEqual(raised.exception.kind, "onedrive_owner")

    def test_nothing_names_an_excluded_person(self):
        guard = PeopleExclusion([EXCLUDED])
        guard.bind(StubApi([{"id": EXCLUDED, "userPrincipalName": EXCLUDED_UPN}]))
        try:
            guard.refuse([EXCLUDED_UPN], "sender")
        except PeopleExcluded as exc:
            refused = exc
        text = " ".join([repr(guard), str(refused), repr(refused),
                         json.dumps(guard.block_part("mail.people_exclusion")),
                         json.dumps(PeopleExclusion().block_part("x"))])
        for secret in (EXCLUDED, EXCLUDED.lower(), EXCLUDED_UPN):
            self.assertNotIn(secret, text)


    def test_a_block_part_carries_a_cause_live_verify_knows(self):
        from verify import live_verify
        absent = PeopleExclusion.from_env({}).block_part("mail.people_exclusion")
        self.assertEqual(live_verify.blocked_cause(declared=absent["cause"]), "later_ask")
        unresolved = PeopleExclusion([EXCLUDED])
        unresolved._bind_failed = True
        self.assertIn(unresolved.block_part("mail.people_exclusion")["cause"],
                      live_verify.CAUSES)


class MailGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.graph = tm.FakeGraph()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MailCaptureStore(Path(self.tmp.name) / "state", clock=tm.Clock())

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def reader(self, environ) -> GraphMailReader:
        api, _transport = graph_read.build_graph(
            environ, sender=self.graph,
            retry=BoundedBackoff(sleep=lambda _s: None, jitter=lambda: 0.0))
        return GraphMailReader(api, tm.capture_audience())

    def capture(self, environ) -> MailCapture:
        return MailCapture(self.store, self.reader(environ), tm.CAPTURE, page_size=2,
                           sleep=lambda _s: None)

    def naming(self, *values: str) -> list[str]:
        return [u for _m, u, _h in self.graph.calls
                if any(v.lower() in unquote(u).lower() for v in values)]

    def test_an_excluded_object_id_is_refused_with_zero_requests(self):
        reader = self.reader(ENV_EXCLUDING_B)
        for call in (lambda: reader.list_messages(tm.CAPTURE, EXCLUDED),
                     lambda: reader.get_message(tm.CAPTURE, EXCLUDED, "MSG-B1"),
                     lambda: reader.open_mailbox(tm.CAPTURE, EXCLUDED.upper())):
            with self.assertRaises(PeopleExcluded) as raised:
                call()
            self.assertEqual(raised.exception.kind, "mailbox")
        self.assertEqual(self.graph.calls, [])          # not even a token request

    def test_an_excluded_address_is_refused_before_any_request_naming_it(self):
        reader = self.reader(ENV_EXCLUDING_B)
        with self.assertRaises(PeopleExcluded):
            reader.list_messages(tm.CAPTURE, EXCLUDED_UPN)
        self.assertEqual(self.naming(EXCLUDED, EXCLUDED_UPN), [])
        self.assertEqual(reader.open_mailbox(tm.CAPTURE, tm.UPN_A)["object_id"],
                         tm.BOX_A)                      # the others still open

    def test_an_absent_list_refuses_every_mail_read_with_zero_requests(self):
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        for environ in (ENV_ABSENT, {**tm.ENV, people_exclusion.ENV_IDS: ""}):
            reader = self.reader(environ)
            for call in (lambda: reader.list_messages(tm.CAPTURE, tm.UPN_A),
                         lambda: reader.list_messages(tm.CAPTURE, tm.BOX_A),
                         lambda: reader.get_message(tm.CAPTURE, tm.UPN_A, "MSG-1"),
                         lambda: reader.mailbox_owner_acl(tm.CAPTURE, tm.UPN_A)):
                with self.assertRaises(ExclusionUnavailable) as raised:
                    call()
                self.assertEqual(raised.exception.code, "excluded_people_oids_absent")
            run = self.capture(environ).run(tm.SCOPE)
            self.assertEqual(run["mailboxes"], [])
            self.assertFalse(run["complete"])
            self.assertEqual(run["people_blocked"]["error_code"], "excluded_people_oids_absent")
            self.assertEqual(run["people_blocked"]["later_ask"], "excluded-people-oids")
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.store.messages(tm.CAPTURE, tm.capture_audience())["total"], 0)

    def test_a_named_excluded_mailbox_is_a_typed_refusal_in_the_run(self):
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        self.graph.put(tm.BOX_B, "MSG-B1", "CKB-1")
        run = self.capture(ENV_EXCLUDING_B).run(tm.SCOPE)
        self.assertEqual(run["people_excluded"], 1)
        self.assertEqual([m.get("status") for m in run["mailboxes"]],
                         ["complete", "people_excluded"])
        self.assertFalse(run["complete"])
        self.assertEqual(self.naming(EXCLUDED, EXCLUDED_UPN), [])
        self.assertNotIn(EXCLUDED, json.dumps(run))
        self.assertNotIn(EXCLUDED_UPN, json.dumps(run))

    def test_an_attachment_carrying_an_excluded_persons_mail_is_never_stored(self):
        """DOC-008: an allowed message's attachment that carries the excluded person's
        mail (an .eml inside a zip, naming them only in Cc) is dropped at capture: no
        bytes, text or readings; the attachment carrying an outside party's mail is kept."""
        from io import BytesIO
        from zipfile import ZipFile

        def zipped(sender, cc):
            out = BytesIO()
            with ZipFile(out, "w") as archive:
                archive.writestr("fwd.eml", (f"From: {sender}\r\nTo: orders@acme.example\r\n"
                                             f"Cc: {cc}\r\nSubject: quote\r\n\r\nx").encode())
                archive.writestr("quote.txt", b"42 boxes")
            return out.getvalue()
        bad, good = zipped(tm.UPN_A, EXCLUDED_UPN.upper()), zipped(
            "sales@acme.example", "orders@acme.example")
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            tm.file_attachment("ATT-BAD", bad, name="mails.zip"),
            tm.file_attachment("ATT-OK", good, name="other.zip")])
        run = self.capture(ENV_EXCLUDING_B).run(tm.SCOPE)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertEqual(folder["counts"]["people_dropped"], 1)
        rows = dict(self.store.db.execute(
            "SELECT attachment_id, content_status FROM attachments").fetchall())
        self.assertEqual(rows, {"ATT-BAD": "excluded:people", "ATT-OK": "captured"})
        digests = {r[0] for r in self.store.db.execute(
            "SELECT content_sha256 FROM attachments WHERE content_sha256 IS NOT NULL")}
        self.assertEqual(digests, {sha256(good).hexdigest()})
        blobs = {p.name.split(".")[0] for p in Path(self.store.blobs).rglob("*") if p.is_file()}
        self.assertNotIn(sha256(bad).hexdigest(), blobs)
        self.assertNotIn(EXCLUDED_UPN, json.dumps(run).lower())

    def test_a_container_stored_before_is_withheld_on_reuse_and_its_mail_never_resolves(self):
        """DOC-008, containers stored before the capture-time drop: a member citation of
        the carried mail of an excluded person (and of a mail the guard cannot clear)
        resolves for nobody while its cleared sibling still does; the next version of
        the message does not store the container again (reused bytes are decided too),
        and the walk kept for the cleared container names nobody excluded."""
        from io import BytesIO
        from unittest import mock
        from zipfile import ZipFile
        from integrations.captured_text import member_handle

        def mail(sender, cc):
            return (f"From: {sender}\r\nTo: orders@acme.example\r\nCc: {cc}\r\n"
                    f"Subject: quote\r\n\r\nx").encode()

        def zipped(data):
            out = BytesIO()
            with ZipFile(out, "w") as archive:
                archive.writestr("fwd.eml", data)
                archive.writestr("quote.txt", b"42 boxes")
            return out.getvalue()
        bad_mail, ok_mail = mail(tm.UPN_A, EXCLUDED_UPN.upper()), mail(
            "sales@acme.example", tm.UPN_A)
        bad, good = zipped(bad_mail), zipped(ok_mail)
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            tm.file_attachment("ATT-BAD", bad, name="mails.zip"),
            tm.file_attachment("ATT-OK", good, name="other.zip")])
        with mock.patch.object(MailCapture, "_carries_withheld_mail",
                               lambda _self, _c, _r: (None, None)):   # before the rule
            self.capture(ENV_EXCLUDING_B).run(tm.SCOPE)
        handles = dict(self.store.db.execute(
            "SELECT attachment_id, l1_handle FROM attachments").fetchall())
        self.assertEqual(set(handles), {"ATT-BAD", "ATT-OK"})
        guard = PeopleExclusion([EXCLUDED])
        guard.attach_directory([(tm.BOX_A, tm.UPN_A, []), (EXCLUDED, EXCLUDED_UPN, [])])
        audience = tm.reader_audience()

        def view(att, path, data, people=guard):
            return self.store.resolve(member_handle(handles[att], path,
                                                    sha256(data).hexdigest()),
                                      tm.READER_A, audience, text=True, people=people)
        self.assertIsNone(view("ATT-BAD", "fwd.eml", bad_mail))
        self.assertEqual(view("ATT-BAD", "quote.txt", b"42 boxes")["row"]["content_text"]
                         .strip(), "42 boxes")
        self.assertIsNotNone(view("ATT-OK", "fwd.eml", ok_mail))
        # fail closed: a guard that cannot decide, or a tenant address it does not hold
        self.assertIsNone(view("ATT-OK", "fwd.eml", ok_mail, PeopleExclusion(
            unavailable="excluded_people_oids_absent")))
        partial = PeopleExclusion([EXCLUDED])
        partial.attach_directory([(EXCLUDED, EXCLUDED_UPN, [])])
        self.assertIsNone(view("ATT-OK", "fwd.eml", ok_mail, partial))
        # the next version (same attachments, reused bytes): the container is not stored
        self.graph.put(tm.BOX_A, "MSG-1", "CK-2", attachments=[
            tm.file_attachment("ATT-BAD", bad, name="mails.zip"),
            tm.file_attachment("ATT-OK", good, name="other.zip")])
        run = self.capture(ENV_EXCLUDING_B).run(tm.SCOPE)
        counts = run["mailboxes"][0]["folders"][0]["counts"]
        self.assertEqual((counts["people_dropped"], counts["attachments_reused"]), (1, 1))
        current = self.store.db.execute(
            "SELECT a.attachment_id, a.content_status, a.content_sha256, a.l1_handle FROM "
            "attachments a JOIN messages m ON m.l1_handle=a.message_handle WHERE "
            "m.superseded_at IS NULL ORDER BY 1").fetchall()
        self.assertEqual([tuple(r)[:3] for r in current],
                         [("ATT-BAD", "excluded:people", None),
                          ("ATT-OK", "captured", sha256(good).hexdigest())])
        self.assertIsNone(current[0][3])
        kept = self.store.db.execute("SELECT content_sha256, walk FROM carried_people"
                                     ).fetchall()
        self.assertEqual([r[0] for r in kept], [sha256(good).hexdigest()])
        self.assertIn("sales@acme.example", kept[0][1])
        self.assertNotIn(EXCLUDED_UPN.lower(), json.dumps([list(r) for r in kept]).lower())

    def test_an_attachment_carrying_withheld_mail_stored_before_resolves_for_nobody(self):
        """DOC-008 review: the attachment citation itself (not only its members) of a
        stored .eml naming the excluded person, or of a zip carrying one, resolves for
        nobody: no text, page, bytes or reading; a clean .eml and a plain file still
        resolve. A guard that cannot read its list clears no carried mail (fail closed)
        yet withholds nothing that carries none."""
        from io import BytesIO
        from unittest import mock
        from zipfile import ZipFile
        from integrations.captured_text import member_handle

        secret = (f"From: {EXCLUDED_UPN}\r\nTo: {tm.UPN_A}\r\nSubject: plan\r\n"
                  "Content-Type: text/plain\r\n\r\nSECRETBODY the plan").encode()
        clean = (f"From: sales@acme.example\r\nTo: {tm.UPN_A}\r\nSubject: quote\r\n"
                 "Content-Type: text/plain\r\n\r\nQUOTEBODY 42 boxes").encode()
        out = BytesIO()
        with ZipFile(out, "w") as archive:
            archive.writestr("fwd.eml", secret)
            archive.writestr("quote.txt", b"42 boxes")
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            {**tm.file_attachment("ATT-EML", secret, name="fwd.eml"),
             "contentType": "message/rfc822"},
            {**tm.file_attachment("ATT-ZIP", out.getvalue(), name="mails.zip"),
             "contentType": "application/zip"},
            {**tm.file_attachment("ATT-OK", clean, name="quote.eml"),
             "contentType": "message/rfc822"},
            tm.file_attachment("ATT-TXT", b"plain 42 boxes", name="note.txt")])
        with mock.patch.object(MailCapture, "_carries_withheld_mail",
                               lambda _self, _c, _r: (None, None)):   # before the rule
            self.capture(ENV_EXCLUDING_B).run(tm.SCOPE)
        handles = dict(self.store.db.execute(
            "SELECT attachment_id, l1_handle FROM attachments").fetchall())
        self.assertEqual(set(handles), {"ATT-EML", "ATT-ZIP", "ATT-OK", "ATT-TXT"})
        guard = PeopleExclusion([EXCLUDED])
        guard.attach_directory([(tm.BOX_A, tm.UPN_A, []), (EXCLUDED, EXCLUDED_UPN, [])])
        audience = tm.reader_audience()

        def resolve(handle, people=guard, **options):
            return self.store.resolve(handle, tm.READER_A, audience, people=people,
                                      **options)
        for att in ("ATT-EML", "ATT-ZIP"):
            with self.subTest(att):
                for options in ({}, {"text": True}, {"text": True, "text_version": 1}):
                    self.assertIsNone(resolve(handles[att], **options))
                self.assertIsNone(resolve(handles[att] + "#page=1", text=True))
                self.store.people = guard   # the serving paths take the store's guard
                try:
                    self.assertIsNone(self.store.attachment_content(
                        handles[att], tm.READER_A, audience))
                    self.assertIsNone(self.store.attachment_reading(
                        handles[att], tm.READER_A, audience))
                finally:
                    self.store.people = None
        # the zip's cleared sibling still resolves as a member; its mail does not
        self.assertEqual(resolve(member_handle(handles["ATT-ZIP"], "quote.txt", sha256(
            b"42 boxes").hexdigest()), text=True)["row"]["content_text"].strip(), "42 boxes")
        self.assertIsNone(resolve(member_handle(handles["ATT-ZIP"], "fwd.eml",
                                                sha256(secret).hexdigest()), text=True))
        ok = resolve(handles["ATT-OK"], text=True)
        self.assertIn("QUOTEBODY", ok["row"]["content_text"])
        self.assertIsNotNone(resolve(handles["ATT-TXT"], text=True))
        # the list cannot be read: no carried mail is cleared, a plain file still resolves
        broken = PeopleExclusion(unavailable="excluded_people_file_unreadable")
        self.assertIsNone(resolve(handles["ATT-OK"], broken, text=True))
        self.assertIsNotNone(resolve(handles["ATT-TXT"], broken, text=True))
        # no guard given: the environment's (none set here: unavailable, fail closed)
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in (people_exclusion.ENV_IDS, people_exclusion.ENV_FILE):
                os.environ.pop(key, None)
            self.assertIsNone(self.store.resolve(handles["ATT-OK"], tm.READER_A, audience))

    def test_a_message_from_an_excluded_sender_is_never_stored_or_indexed(self):
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        self.graph.put(tm.BOX_A, "MSG-2", "CK-2", subject="Sent by the excluded person")
        self.graph.messages[tm.BOX_A]["MSG-2"]["from"] = {
            "emailAddress": {"address": EXCLUDED_UPN.upper()}}
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertEqual(folder["counts"]["people_dropped"], 1)
        self.assertEqual(folder["counts"]["versions_new"], 1)
        audience = tm.capture_audience()
        self.assertEqual([m["message_id"] for m in
                          self.store.index(tm.CAPTURE, audience)["items"]], ["MSG-1"])
        self.assertEqual(self.store.messages(tm.CAPTURE, audience)["total"], 1)
        self.assertEqual(self.naming("MSG-2"), [])       # no body or attachment read
        stored = b"".join(p.read_bytes() for p in Path(self.tmp.name).rglob("*") if p.is_file())
        self.assertNotIn(b"Sent by the excluded person", stored)
        # Read directly, the same message is refused, never returned.
        with self.assertRaises(PeopleExcluded) as raised:
            self.reader(ENV_EXCLUDING_B).get_message(tm.CAPTURE, tm.UPN_A, "MSG-2")
        self.assertEqual(raised.exception.kind, "sender")

    def test_a_message_sent_on_behalf_by_an_excluded_person_is_dropped(self):
        # Graph returns only the $select'ed fields, so every message read must select
        # ``sender`` as well as ``from`` or a send-on-behalf goes unseen live.
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        self.graph.put(tm.BOX_A, "MSG-2", "CK-2", subject="Sent on behalf")
        self.graph.messages[tm.BOX_A]["MSG-2"]["sender"] = {
            "emailAddress": {"address": EXCLUDED_UPN}}
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        self.assertEqual(run["mailboxes"][0]["folders"][0]["counts"]["people_dropped"], 1)
        message_reads = [unquote(u) for _m, u, _h in self.graph.calls if "/messages" in u]
        self.assertTrue(message_reads)
        for url in message_reads:
            selected = parse_qs(urlsplit(url).query).get("$select", [""])[0].split(",")
            self.assertIn("from", selected, url)
            self.assertIn("sender", selected, url)

    def test_a_listing_drops_the_excluded_senders_messages(self):
        sent = ta.message("m2", "ck2")
        sent["from"] = {"emailAddress": {"address": ta.CEO_UPN.upper()}}
        sender = ta.FakeSender({ta.MSG_PATH: ta.j(200, {"value": [ta.message("m1", "ck1"),
                                                                   sent]})})
        api, _transport = ta.graph_api(sender, {**ta.ENV, people_exclusion.ENV_IDS: ta.CEO_ID})
        listed = GraphMailReader(api, ta.audience()).list_messages(ta.ACTOR, ta.MAILBOX)
        self.assertEqual([m["id"] for m in listed["items"]], ["m1"])
        self.assertEqual(listed["completeness"]["people_dropped"], 1)
        self.assertNotIn(ta.CEO_UPN, json.dumps(listed).lower())


    def test_a_recorded_tenant_listing_decides_addresses_offline_and_keeps_excluded_closed(self):
        """A capture run records the listing its guard was bound with; an offline reader
        then decides a colleague's address no captured mailbox holds, while an excluded
        person's address and anything the listing does not hold stay undecided, and the
        store holds nothing of the excluded person."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from unittest import mock
        colleague = "12345678-aaaa-bbbb-cccc-00000000000c"
        self.graph.users[colleague] = {"id": colleague, "userPrincipalName": "c.person@example-plx.com",
                                       "mail": "c.person@example-plx.com", "proxyAddresses": []}
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1")
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        self.capture(ENV_EXCLUDING_B).run(scope)
        state = Path(self.tmp.name) / "state"
        with sqlite3.connect(self.store.path) as db:
            rows = db.execute("SELECT address, object_id, kind FROM tenant_listing").fetchall()
        self.assertTrue(rows)
        self.assertFalse({EXCLUDED_UPN.lower(), EXCLUDED} & {v for r in rows for v in r if v})
        offline = people_exclusion.PeopleExclusion.from_env(ENV_EXCLUDING_B)
        with self.assertRaises(ExclusionUnavailable):
            offline.is_excluded("c.person@example-plx.com")     # nothing attached: undecided
        held = people_exclusion.with_directory(offline, state)
        self.assertFalse(held.is_excluded("c.person@example-plx.com"))
        self.assertFalse(held.is_excluded(tm.UPN_A))
        for address in (EXCLUDED_UPN.upper(), "all-staff@example-plx.com"):  # no groups read
            with self.subTest(undecided=True):
                self.assertFalse(held.decides(address))
                with self.assertRaises(ExclusionUnavailable):
                    held.is_excluded(address)
        # Excluded after the listing was read: their listed address is excluded.
        later = people_exclusion.with_directory(people_exclusion.PeopleExclusion(
            [EXCLUDED, colleague]), state)
        self.assertTrue(later.is_excluded("c.person@example-plx.com"))
        # Past the age bound the listing decides nothing again (fail closed).
        aged = datetime.now(timezone.utc) + people_exclusion.DIRECTORY_MAX_AGE + timedelta(days=1)
        real = people_exclusion.mail_capture_listing
        with mock.patch.object(people_exclusion, "mail_capture_listing",
                               lambda d: real(d, now=aged)), \
                mock.patch.object(people_exclusion, "mail_capture_directory", lambda d: None):
            stale = people_exclusion.with_directory(
                people_exclusion.PeopleExclusion.from_env(ENV_EXCLUDING_B), state)
        with self.assertRaises(ExclusionUnavailable):
            stale.is_excluded("c.person@example-plx.com")

    def test_an_unreadable_list_withholds_only_its_mail_and_the_folder_goes_on(self):
        """One list whose members cannot be read: its message is withheld (counted), the
        rest of the folder is captured, and the groups are listed once for the run. Offline
        a cleared list decides for 24 hours at most."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        self.graph.groups = [
            {"id": G1, "mail": "unreadable-list@example-plx.com", "groupTypes": []},
            {"id": G2, "mail": "clean-list@example-plx.com", "groupTypes": []}]
        self.graph.group_members = {G2: [{"id": tm.BOX_A, "@odata.type": "#microsoft.graph.user"}]}
        for mid, to in (("MSG-1", None), ("MSG-2", "unreadable-list@example-plx.com"),
                        ("MSG-3", "clean-list@example-plx.com")):
            self.graph.put(tm.BOX_A, mid, "CK-" + mid, subject="Subject " + mid)
            if to:
                self.graph.messages[tm.BOX_A][mid]["toRecipients"] = [
                    {"emailAddress": {"address": to}}]
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertEqual((folder["counts"]["people_undecided"],
                          folder["counts"]["people_dropped"]), (1, 0))   # held, not excluded
        self.assertEqual((folder["complete"], folder["reason"]), (False, "people_undecided"))
        self.assertEqual(folder["counts"]["versions_new"], 2)
        self.assertEqual(sorted(m["message_id"] for m in self.store.index(
            tm.CAPTURE, tm.capture_audience())["items"]), ["MSG-1", "MSG-3"])
        self.assertEqual([u for u in self.naming("MSG-2") if "body" in
                          parse_qs(urlsplit(u).query).get("$select", [""])[0].split(",")], [])
        listings = [u for _m, u, _h in self.graph.calls
                    if urlsplit(u).path == "/v1.0/groups"]
        self.assertEqual(len(listings), 1)
        with self.assertRaises(ExclusionUnavailable):
            self.reader(ENV_EXCLUDING_B).get_message(tm.CAPTURE, tm.UPN_A, "MSG-2")
        state = Path(self.tmp.name) / "state"
        with sqlite3.connect(self.store.path) as db:
            kinds = {r[0]: r[1] for r in db.execute(
                "SELECT address, kind FROM tenant_listing").fetchall()}
        self.assertEqual(kinds.get("clean-list@example-plx.com"), "group")
        self.assertNotIn("unreadable-list@example-plx.com", kinds)
        now = datetime.now(timezone.utc)
        for hours, recorded in ((1, True), (25, False)):
            rows = people_exclusion.mail_capture_listing(state, now=now + timedelta(hours=hours))
            groups = [r for r in rows if r[2] == "group"]
            self.assertEqual(bool(groups), recorded, hours)
            self.assertTrue([r for r in rows if r[2] == "user"])   # users: the 7-day bound

    def test_undecided_mail_is_held_and_captured_once_the_listing_succeeds(self):
        """Run 1: the groups listing is throttled throughout, so mail to a list is held by id
        (not dropped as excluded) and the folder is not complete. Run 2: the listing
        succeeds; the message to a cleared list is captured, the one to a list with an
        excluded member is dropped for good, and the folder completes."""
        self.graph.groups = [
            {"id": G1, "mail": "team-list@example-plx.com", "groupTypes": []},
            {"id": G2, "mail": "secret-list@example-plx.com", "groupTypes": []}]
        user = "#microsoft.graph.user"
        self.graph.group_members = {G1: [{"id": tm.BOX_A, "@odata.type": user}],
                                    G2: [{"id": EXCLUDED, "@odata.type": user}]}
        for mid, to in (("MSG-1", None), ("MSG-2", "team-list@example-plx.com"),
                        ("MSG-3", "secret-list@example-plx.com")):
            self.graph.put(tm.BOX_A, mid, "CK-" + mid, subject="Subject " + mid)
            if to:
                self.graph.messages[tm.BOX_A][mid]["toRecipients"] = [
                    {"emailAddress": {"address": to}}]
        scope = tm.CaptureScope([{"address": tm.UPN_A, "folders": ["inbox"]}], window_days=90,
                                internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        audience = tm.capture_audience()

        def captured():
            return sorted(m["message_id"] for m in self.store.index(tm.CAPTURE, audience)["items"])

        self.graph.groups_status = 429
        from unittest import mock
        with mock.patch.object(people_exclusion, "GROUP_LIST_BACKOFF", 0.0):
            run = self.capture(ENV_EXCLUDING_B).run(scope)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertEqual((folder["counts"]["people_undecided"],
                          folder["counts"]["people_dropped"]), (2, 0))
        self.assertEqual((folder["complete"], folder["reason"]), (False, "people_undecided"))
        self.assertEqual(captured(), ["MSG-1"])
        self.graph.groups_status = 200
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertEqual((folder["counts"]["people_recovered"],
                          folder["counts"]["people_dropped"]), (1, 1))
        self.assertEqual((folder["complete"], folder["reason"]), (True, None))
        self.assertEqual(captured(), ["MSG-1", "MSG-2"])
        run = self.capture(ENV_EXCLUDING_B).run(scope)
        self.assertEqual(captured(), ["MSG-1", "MSG-2"])
        self.assertTrue(run["mailboxes"][0]["folders"][0]["complete"])
        stored = b"".join(p.read_bytes() for p in Path(self.tmp.name).rglob("*") if p.is_file())
        self.assertNotIn(b"Subject MSG-3", stored)
        self.assertEqual([u for u in self.naming("MSG-3") if "$select=" in unquote(u)
                          and "body" in parse_qs(urlsplit(u).query)["$select"][0].split(",")],
                         [])   # never a body read

    def test_only_a_listed_user_or_group_address_is_decided_offline(self):
        """Every other form of an excluded person's address stays undecided offline; a
        group's address clears; the recorded rows hold nothing of the excluded person."""
        colleague = "12345678-aaaa-bbbb-cccc-00000000000c"
        users = [{"id": tm.BOX_A, "userPrincipalName": tm.UPN_A,
                  "proxyAddresses": ["smtp:shared@example-plx.com"]},
                 {"id": colleague, "userPrincipalName": "c@example-plx.com",
                  "proxyAddresses": ["smtp:shared@example-plx.com"]},
                 {"id": EXCLUDED, "userPrincipalName": "b.person@example-plx.com",
                  "mail": EXCLUDED_UPN, "proxyAddresses": ["smtp:b.alias@example-plx.com"]}]
        groups = [{"id": G1, "mail": "all-staff@example-plx.com", "groupTypes": [],
                   "proxyAddresses": ["SMTP:everyone@example-plx.com"]}]
        members = {G1: [{"id": tm.BOX_A, "@odata.type": "#microsoft.graph.user"}]}

        class Api:
            def __init__(self, refuse_groups=False):
                self.refuse_groups = refuse_groups

            def get_pages(self, url, **_kwargs):
                if "/members" in url:
                    return members[url.split("/groups/")[1].split("/")[0]], {"complete": True}
                if "/v1.0/groups" in url:
                    if self.refuse_groups:
                        raise RuntimeError("403")
                    return groups, {"complete": True}
                return users, {"complete": True}

        def offline(api):
            guard = PeopleExclusion([EXCLUDED])
            guard.bind(api)
            guard.list_groups(api)
            _at, rows = people_exclusion.tenant_listing_rows(guard)
            held = PeopleExclusion([EXCLUDED])
            held.attach_listing(rows)
            return held, rows

        held, rows = offline(Api())
        stored = {v.lower() for r in rows for v in r if isinstance(v, str)}
        self.assertFalse({EXCLUDED, EXCLUDED_UPN.lower(), "b.person@example-plx.com",
                          "b.alias@example-plx.com"} & stored)
        self.assertFalse(held.is_excluded("ALL-STAFF@example-plx.com"))   # a group clears
        self.assertFalse(held.is_excluded("everyone@example-plx.com"))
        self.assertFalse(held.is_excluded("c@example-plx.com"))
        for value in (EXCLUDED_UPN, "b.person@example-plx.com", "b.alias@example-plx.com",
                      "b.person+orders@example-plx.com",          # plus-address
                      "b.other-alias@example-plx.com",            # unlisted tenant alias
                      "/o=ExchangeLabs/ou=Exchange Administrative Group/cn=Recipients/cn=bperson",
                      "B Person",                                 # display name
                      "b.person@personal-mail.example",           # external personal
                      "gone.user@example-plx.com",                # a deleted user's
                      "shared@example-plx.com"):                  # two holders
            with self.subTest(form=value[:12]):
                self.assertFalse(held.decides(value))
                with self.assertRaises(ExclusionUnavailable):
                    held.is_excluded(value)
        refused, _rows = offline(Api(refuse_groups=True))   # no groups read: none clear
        with self.assertRaises(ExclusionUnavailable):
            refused.is_excluded("all-staff@example-plx.com")
        self.assertIsNone(people_exclusion.tenant_listing_rows(PeopleExclusion([EXCLUDED])))

class DistributionListTests(unittest.TestCase):
    """Mail addressed to a distribution list is decided by who is on it (GroupMember.Read.All):
    released when every member is read and none is excluded, withheld when an excluded person
    is on it (directly or through a nested list) or when its members cannot be read. Synthetic
    ids and addresses only."""

    OTHER = "12345678-aaaa-bbbb-cccc-0000000000d1"
    USERS = [{"id": tm.BOX_A, "userPrincipalName": tm.UPN_A},
             {"id": OTHER, "userPrincipalName": "d1@example-plx.com"},
             {"id": EXCLUDED, "userPrincipalName": "b.person@example-plx.com",
              "mail": EXCLUDED_UPN}]
    GROUPS = [{"id": G1, "mail": "clean-list@example-plx.com", "groupTypes": []},
              {"id": G2, "mail": "with-excluded@example-plx.com", "groupTypes": []},
              {"id": G3, "mail": "nested@example-plx.com", "groupTypes": ["Unified"]}]

    def api(self, members, *, unreadable=(), incomplete=(), groups_fail=False, groups=None,
            throttled=0, groups_cut=False, verified=None):
        users, groups = self.USERS, self.GROUPS if groups is None else groups

        class Api:
            listings = 0
            left = throttled

            def get_pages(self, url, **_kwargs):
                if "/v1.0/organization" in url:
                    if verified is None:
                        raise RuntimeError("403")
                    return [{"verifiedDomains": [{"name": d} for d in verified]}], \
                        {"complete": True}
                if "/members" in url:
                    gid = url.split("/groups/")[1].split("/")[0]
                    if gid in unreadable:
                        raise RuntimeError("403")
                    return members.get(gid, []), {"complete": gid not in incomplete}
                if "/v1.0/groups" in url:
                    self.listings += 1
                    if groups_fail:
                        raise RuntimeError("403")
                    if self.left:
                        self.left -= 1
                        error = RuntimeError("throttled")
                        error.http_status = 429
                        raise error
                    return groups, {"complete": not groups_cut}
                return users, {"complete": True}
        return Api()

    def bound(self, api):
        guard = PeopleExclusion([EXCLUDED])
        guard._sleep = lambda _s: None
        guard.bind(api)
        guard.list_groups(api)
        return guard

    def offline(self, guard):
        _at, rows = people_exclusion.tenant_listing_rows(guard)
        held = PeopleExclusion([EXCLUDED])
        held.attach_listing(rows)
        return held, rows

    def user(self, oid):
        return {"id": oid, "@odata.type": "#microsoft.graph.user"}

    def test_a_list_without_excluded_members_is_released_a_list_with_one_is_withheld(self):
        members = {G1: [self.user(tm.BOX_A), self.user(self.OTHER)],
                   G2: [self.user(tm.BOX_A), self.user(EXCLUDED)],
                   G3: [self.user(self.OTHER),
                        {"id": G2, "@odata.type": "#microsoft.graph.group"}]}
        guard = self.bound(self.api(members))
        held, rows = self.offline(guard)
        self.assertFalse({EXCLUDED, EXCLUDED_UPN.lower()} & {v for r in rows for v in r if v})
        for view in (guard, held):
            self.assertFalse(view.is_excluded("Clean-List@example-plx.com"))
        self.assertTrue(guard.is_excluded("with-excluded@example-plx.com"))
        self.assertTrue(guard.is_excluded("nested@example-plx.com"))   # through a nested list
        for address in ("with-excluded@example-plx.com", "nested@example-plx.com"):
            self.assertFalse(held.decides(address))     # never recorded as cleared offline
            with self.assertRaises(ExclusionUnavailable):
                held.is_excluded(address)

    def test_a_list_whose_members_cannot_be_read_stays_withheld(self):
        members = {G1: [self.user(tm.BOX_A)], G2: [self.user(self.OTHER)],
                   G3: [self.user(self.OTHER)]}
        for name, options in (("refused", {"unreadable": (G1,)}),
                              ("truncated", {"incomplete": (G1,)}),
                              ("member_nobody_can_name", None)):
            with self.subTest(name):
                if options is None:
                    members = {**members, G1: [{"id": "not-an-id"}]}
                    options = {}
                guard = self.bound(self.api(members, **options))
                with self.assertRaises(ExclusionUnavailable):
                    guard.is_excluded("clean-list@example-plx.com")
                held, _rows = self.offline(guard)
                with self.assertRaises(ExclusionUnavailable):
                    held.is_excluded("clean-list@example-plx.com")
                self.assertFalse(guard.is_excluded("with-excluded@example-plx.com"))  # readable

    def test_a_failed_throttled_or_cut_groups_listing_withholds_any_possible_list(self):
        """No complete groups listing: a list with an excluded member must not be released
        online; an address no listed user holds in a tenant domain may be one."""
        members = {G1: [self.user(tm.BOX_A)], G2: [self.user(EXCLUDED)],
                   G3: [self.user(self.OTHER)]}
        for name, options in (("refused", {"groups_fail": True}),
                              ("throttled", {"throttled": 99}),
                              ("cut_short", {"groups_cut": True})):
            with self.subTest(name):
                guard = self.bound(self.api(members, **options))
                for address in ("with-excluded@example-plx.com", "clean-list@example-plx.com",
                                "unlisted@example-plx.com"):
                    with self.assertRaises(ExclusionUnavailable):
                        guard.is_excluded(address)
                self.assertTrue(guard.is_excluded(EXCLUDED_UPN))
                self.assertFalse(guard.is_excluded("d1@example-plx.com"))     # a listed user
                self.assertFalse(guard.is_excluded("someone@elsewhere.example"))  # external
                held, rows = self.offline(guard)
                self.assertFalse([r for r in rows if r[2] == "group"])
                with self.assertRaises(ExclusionUnavailable):
                    held.is_excluded("clean-list@example-plx.com")

    def test_a_failed_listing_withholds_list_domains_no_user_holds_an_address_in(self):
        """A list addressed in a domain no listed user holds an address in: withheld while
        the listing fails when the domain is verified or held a list in an earlier listing."""
        lists = [{"id": G1, "mail": "team@lists.example-plx.net", "groupTypes": []}]
        clean = {G1: [self.user(tm.BOX_A)]}
        guard = self.bound(self.api(clean, groups=lists))
        self.assertEqual(guard.group_domains(), {"lists.example-plx.net"})
        self.assertFalse(guard.is_excluded("team@lists.example-plx.net"))
        later = PeopleExclusion([EXCLUDED])                 # a later run, listing refused
        later._sleep = lambda _s: None
        later.remember_group_domains(guard.group_domains())
        api = self.api(clean, groups_fail=True)
        later.bind(api)
        later.list_groups(api)
        with self.assertRaises(ExclusionUnavailable):
            later.is_excluded("other@lists.example-plx.net")
        verified = self.bound(self.api(clean, groups_fail=True,
                                       verified=["lists.example-plx.net"]))
        with self.assertRaises(ExclusionUnavailable):
            verified.is_excluded("team@lists.example-plx.net")
        self.assertFalse(verified.is_excluded("someone@elsewhere.example"))

    def test_the_groups_listing_is_retried_when_throttled_and_read_once_per_run(self):
        members = {G1: [self.user(tm.BOX_A)], G2: [self.user(EXCLUDED)],
                   G3: [self.user(self.OTHER)]}
        api = self.api(members, throttled=2)
        guard = self.bound(api)
        self.assertEqual(api.listings, 3)                  # two 429s, then the listing
        self.assertTrue(guard.is_excluded("with-excluded@example-plx.com"))
        for _open in range(5):                             # every mailbox open of the run
            guard.list_groups(api)
        self.assertEqual(api.listings, 3)
        guard.forget_groups()                              # the next run lists again
        guard.list_groups(api)
        self.assertEqual(api.listings, 4)
        failed = self.api(members, throttled=99)
        guard = self.bound(failed)
        self.assertEqual(failed.listings, people_exclusion.GROUP_LIST_ATTEMPTS)
        guard.list_groups(failed)                          # failed: unknown for the run
        self.assertEqual(failed.listings, people_exclusion.GROUP_LIST_ATTEMPTS)
        with self.assertRaises(ExclusionUnavailable):
            guard.is_excluded("clean-list@example-plx.com")

    def test_an_unconfirmable_membership_never_clears_a_list(self):
        """Empty, hidden or rule-based membership, an untyped or unknown member (a nested
        list without a type too): the list is withheld online and not recorded offline."""
        clean = [self.user(tm.BOX_A)]
        cases = {
            "empty": ({}, {G1: []}),
            "hidden": ({"hiddenMembership": True}, {G1: clean}),
            "dynamic": ({"groupTypes": ["DynamicMembership"]}, {G1: clean}),
            "rule": ({"membershipRule": "user.department -eq \"x\""}, {G1: clean}),
            "no_group_types": ({"groupTypes": None}, {G1: clean}),
            "untyped_member": ({}, {G1: [{"id": self.OTHER}]}),
            "untyped_nested": ({}, {G1: [self.user(tm.BOX_A), {"id": G3}],
                                    G3: [self.user(EXCLUDED)]}),
            "service_principal": ({}, {G1: [{"id": self.OTHER, "@odata.type":
                                             "#microsoft.graph.servicePrincipal"}]}),
            "nested_empty": ({}, {G1: [{"id": G3, "@odata.type": "#microsoft.graph.group"}],
                                  G3: []}),
        }
        for name, (extra, members) in cases.items():
            with self.subTest(name):
                groups = [{**self.GROUPS[0], **extra}]
                guard = self.bound(self.api(members, groups=groups))
                with self.assertRaises(ExclusionUnavailable):
                    guard.is_excluded("clean-list@example-plx.com")
                held, rows = self.offline(guard)
                self.assertFalse([r for r in rows if r[2] == "group"])
                with self.assertRaises(ExclusionUnavailable):
                    held.is_excluded("clean-list@example-plx.com")

    def test_guests_and_contacts_are_checked_by_address(self):
        contact = "#microsoft.graph.orgContact"
        cases = {
            "contact_of_excluded": ([{"id": self.OTHER, "@odata.type": contact,
                                      "mail": EXCLUDED_UPN.upper()}], True),
            "guest_with_excluded_alias": ([{**self.user(self.OTHER), "proxyAddresses":
                                            ["smtp:" + EXCLUDED_UPN]}], True),
            "external_contact": ([self.user(tm.BOX_A), {"id": self.OTHER, "@odata.type": contact,
                                                        "mail": "x@outside.example"}], False),
            "contact_without_address": ([{"id": self.OTHER, "@odata.type": contact}], False),
        }
        for name, (on_list, excluded) in cases.items():
            with self.subTest(name):
                guard = self.bound(self.api({G1: on_list}, groups=[self.GROUPS[0]]))
                self.assertIs(guard.is_excluded("clean-list@example-plx.com"), excluded)

    def test_a_cleared_list_decides_nothing_offline_once_the_exclusion_list_changed(self):
        members = {G1: [self.user(tm.BOX_A)], G2: [self.user(self.OTHER)],
                   G3: [self.user(self.OTHER)]}
        _held, rows = self.offline(self.bound(self.api(members)))
        later = PeopleExclusion([EXCLUDED, tm.BOX_A])    # a member of the list is excluded now
        later.attach_listing(rows)
        with self.assertRaises(ExclusionUnavailable):
            later.is_excluded("clean-list@example-plx.com")


class TeamsGuardTests(unittest.TestCase):
    """capture_teams uses the same guard. Chats are read over the fake only: no Teams
    chat is ever read live by this module."""

    SENDER = "0000aaaa-0000-0000-0000-000000000009"   # an excluded sender, no member

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = tc.FakeTeamsGraph()
        self.graph.post_root(tc.GENERAL, "1001", replies=2)
        self.graph.post_root(tc.GENERAL, "1002")
        self.graph.post_root(tc.PRIVATE, "2001", "Board-only pricing")
        self.graph.post_chat("3001")
        self.audience = tc.audience()
        self.store = open_store(self.tmp.name, self.audience)
        self.addCleanup(self.store.close)

    def capture(self, excluded=None, *, absent=False):
        env = dict(tc.ENV)
        if absent:
            env.pop(people_exclusion.ENV_IDS)
        elif excluded:
            env[people_exclusion.ENV_IDS] = excluded
        api, _transport = graph_read.build_graph(env, sender=self.graph)
        return TeamsCapture(self.store, api, self.audience, tc.CAPTURE)

    def paths(self, thread):
        kind, unit = ("teams_chat", Conversation("chat", thread).unit) if thread == tc.CHAT \
            else ("teams_channel", Conversation("channel", thread, tc.TEAM).unit)
        # Message ids: the conversation's own record (CONVERSATION) is no
        # message (as the CLI counts it apart).
        return {r.path[-1] for r in self.store.current_records(kind, unit)
                if r.path[-1] != CONVERSATION}

    def test_an_absent_or_empty_list_refuses_every_teams_read_with_zero_requests(self):
        for name, kwargs in {"absent": {"absent": True}, "empty": {"excluded": " , "}}.items():
            with self.subTest(name):
                self.graph.calls.clear()
                run = self.capture(**kwargs).run(tc.scope())
                self.assertEqual(self.graph.calls, [])          # not even the token
                self.assertFalse(run["complete"])
                self.assertEqual(run["discovery"]["status"], "blocked")
                [part] = run["blocked_upstream"]
                self.assertEqual((part["source"], part["error_code"], part["later_ask"]),
                                 ("people-exclusion", "excluded_people_oids_absent",
                                  "excluded-people-oids"))
        self.assertEqual(self.paths(tc.GENERAL) | self.paths(tc.CHAT), set())

    def test_an_unresolved_directory_reads_nothing_else(self):
        self.graph.failing = {r"^/v1\.0/users$": 503}
        run = self.capture(tc.U_VINCE).run(tc.scope())
        self.assertFalse(run["complete"])
        self.assertEqual({unquote(u).split("?")[0] for u in self.graph.gets()},
                         {"https://graph.microsoft.com/v1.0/users"})
        self.assertEqual(run["conversations"], [])

    def test_an_excluded_member_is_refused_before_any_request_naming_them(self):
        run = self.capture(tc.U_VINCE).run(tc.scope())
        self.assertEqual(run["discovery"]["people_excluded"], 1)
        for url in self.graph.gets():
            self.assertNotIn(tc.U_VINCE, unquote(url))
            self.assertNotIn(tc.VINCE, unquote(url).lower())
        self.assertNotIn(tc.U_VINCE, json.dumps(run, default=sorted))
        self.assertNotIn(tc.VINCE, json.dumps(run, default=sorted))

    def test_a_chat_with_an_excluded_member_is_never_read_and_is_withheld(self):
        # Captured before the person was excluded: the store holds the chat.
        self.capture().run(tc.scope())
        self.assertEqual(self.paths(tc.CHAT), {"3001"})
        unit = Conversation("chat", tc.CHAT).unit
        self.assertTrue(self.store.records(tc.COS_ONLY, "teams_chat", unit)["items"])
        self.graph.calls.clear()
        self.graph.post_chat("3002", "after the exclusion")
        run = self.capture(tc.U_OTHER).run(tc.scope())
        chat = [c for c in run["conversations"] if c["kind"] == "chat"]
        self.assertEqual([c["status"] for c in chat], ["people_excluded"])
        self.assertEqual(run["people_excluded"], 1)
        self.assertFalse([u for u in self.graph.gets() if f"/chats/{tc.CHAT}/messages"
                          in unquote(u)])
        self.assertNotIn("3002", self.paths(tc.CHAT))
        # What the earlier capture held loses its membership: nobody sees it.
        try:
            items = self.store.records(tc.COS_ONLY, "teams_chat", unit)["items"]
        except CaptureError:
            items = []
        self.assertEqual(items, [])
        # Withheld every run is settled, not a gap: the channels still complete the pass.
        self.assertTrue(run["complete"], run)

    def test_a_chat_whose_member_list_is_truncated_is_never_read(self):
        # The unlisted members may include an excluded person: the guard cannot decide,
        # so the chat's messages are not requested (fail closed), its run is a gap.
        run = self.capture(self.SENDER).run(tc.scope(max_members=1))
        chat = [c for c in run["conversations"] if c["kind"] == "chat"]
        self.assertEqual([(c["status"], c["reasons"]) for c in chat],
                         [("incomplete", ["cap:member_item_cap"])])
        self.assertFalse([u for u in self.graph.gets() if f"/chats/{tc.CHAT}/messages"
                          in unquote(u)])
        self.assertEqual(self.paths(tc.CHAT), set())
        self.assertFalse(run["complete"])

    def test_messages_an_excluded_person_sent_are_dropped_before_storage(self):
        sender = {"user": {"id": self.SENDER, "displayName": "Excluded"}}
        self.graph.roots[tc.GENERAL]["1002"]["from"] = sender
        self.graph.replies[(tc.GENERAL, "1001")][0]["from"] = sender
        self.graph.chat["3001"]["from"] = sender
        run = self.capture(self.SENDER).run(tc.scope())
        self.assertEqual(run["people_dropped"], 3)
        self.assertEqual(self.paths(tc.GENERAL), {"1001", "10012"})
        self.assertEqual(self.paths(tc.CHAT), set())
        self.assertTrue(run["complete"], run)   # dropped by policy, not a provenance gap
        self.assertNotIn(self.SENDER, json.dumps(run, default=sorted))


class OneDriveGuardTests(te.EstateCase):
    """Estate discovery's personal sites (OneDrives) through the shared guard."""

    EXCLUDED_SITE = (f"{te.MY_HOST},99999999-0000-0000-0000-000000000001,"
                     "99999999-0000-0000-0000-000000000002")

    def setUp(self) -> None:
        super().setUp()
        te.SITES[self.EXCLUDED_SITE] = f"https://{te.MY_HOST}/personal/{te.EXCLUDED_PATH}"
        self.addCleanup(te.SITES.pop, self.EXCLUDED_SITE)
        self.drive.sites.append(self.EXCLUDED_SITE)
        self.drive.libraries = {**te.LIBRARIES,
                                self.EXCLUDED_SITE: [("b!excluded-Drive_77", "Documents")]}

    def capture_with(self, env) -> te.SharePointCapture:
        api, transport = graph_read.build_graph(
            env, sender=self.drive,
            retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

        def reader(drive_id):
            return te.SharePointReader(api, te.ts.capture_audience(), drive_id,
                                       transport=transport, read_permissions=True,
                                       max_acl_reads=te.cs.MAX_ACL_READS)

        return te.SharePointCapture(self.store, reader, te.ts.CAPTURE,
                                    estate=graph_read.SharePointSites(api, te.ts.SP_HOST))

    def requests_naming(self, site: str) -> list[str]:
        return [u for u in self.urls() if site in u or "excluded-Drive_77" in u]

    def test_an_excluded_owners_onedrive_is_refused_before_its_libraries_are_listed(self):
        run = self.capture_with(te.ts.ENV).run(te.estate_scope())
        counts = run["discovery"]["counts"]
        self.assertEqual(self.requests_naming(self.EXCLUDED_SITE), [])
        self.assertEqual((counts["sites_personal"], counts["sites_people_excluded"],
                          counts["sites_people_withheld"]), (2, 1, 0))
        self.assertNotIn("b!excluded-Drive_77", self.roots(run))
        self.assertIn(te.PERSONAL_DRIVE, self.roots(run))   # another OneDrive still read
        self.assertEqual(run["discovery"]["gaps"], ["library_listing:http_403"])
        text = json.dumps(run)
        for secret in (te.EXCLUDED_OID, te.EXCLUDED_UPN, te.EXCLUDED_PATH):
            self.assertNotIn(secret, text)

    def test_an_absent_or_empty_list_withholds_every_onedrive_with_a_typed_gap(self):
        for value in (None, "", " , "):
            env = {k: v for k, v in te.ts.ENV.items() if k != people_exclusion.ENV_IDS}
            if value is not None:
                env[people_exclusion.ENV_IDS] = value
            start = len(self.drive.calls)
            run = self.capture_with(env).run(te.estate_scope())
            urls = self.urls(start)
            counts = run["discovery"]["counts"]
            self.assertEqual([u for u in urls if te.MY_HOST in u.split("?")[0]
                              or "/v1.0/users" in u], [])
            self.assertEqual((counts["sites_people_withheld"],
                              counts["libraries_personal_planned"]), (2, 0))
            self.assertIn("people_exclusion:excluded_people_oids_absent",
                          run["discovery"]["gaps"])
            self.assertFalse(run["discovery"]["complete"])
            self.assertIn(te.ts.DRIVE, self.roots(run))   # site libraries unaffected

    def test_an_unresolved_directory_withholds_every_onedrive(self):
        self.drive.users_status = 503
        run = self.capture_with(te.ts.ENV).run(te.estate_scope())
        counts = run["discovery"]["counts"]
        self.assertEqual([u for u in self.urls() if te.MY_HOST in u.split("?")[0]], [])
        self.assertEqual(counts["sites_people_withheld"], 2)
        self.assertIn("people_exclusion:excluded_people_unresolved", run["discovery"]["gaps"])

    def test_a_personal_site_whose_path_names_no_owner_is_withheld(self):
        te.SITES[self.EXCLUDED_SITE] = f"https://{te.MY_HOST}/"
        run = self.capture_with(te.ts.ENV).run(te.estate_scope())
        self.assertEqual(self.requests_naming(self.EXCLUDED_SITE), [])
        self.assertIn("people_exclusion:onedrive_owner_unresolved", run["discovery"]["gaps"])

    def test_an_estate_without_personal_sites_never_lists_the_directory(self):
        self.drive.sites = [s for s in self.drive.sites if not s.startswith(te.MY_HOST)]
        self.capture_with(te.ts.ENV).run(te.estate_scope())
        self.assertEqual([u for u in self.urls() if "/v1.0/users" in u], [])


class ExclusionEnvFile(unittest.TestCase):
    """A process started outside the passes' units reads the exclusion from their env file;
    anything doubtful about the file leaves the guard unavailable (fail closed)."""

    OID = "00000000-0000-4000-8000-0000000000e7"

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        self.file = self.dir / "proactive.env"
        self.file.write_text(f"# the exclusion only\nPLX_EXCLUDED_PEOPLE_OIDS=\"{self.OID}\"\n"
                             "OTHER_SECRET=never-merged\n", encoding="utf-8")
        os.chmod(self.file, 0o600)

    def guard(self, environ: dict) -> people_exclusion.PeopleExclusion:
        merged = people_exclusion.environ_with_exclusion(environ)
        self.assertNotIn("OTHER_SECRET", merged)       # only the exclusion's own keys
        return people_exclusion.PeopleExclusion.from_env(merged)

    def test_env_file_supplies_the_exclusion_when_the_environment_has_none(self) -> None:
        guard = self.guard({"PLX_EXCLUSION_ENV_FILE": str(self.file)})
        self.assertIsNone(guard.unavailable)
        self.assertEqual(guard.count, 1)

    def test_the_environment_wins_over_the_file(self) -> None:
        other = "00000000-0000-4000-8000-0000000000e8"
        merged = people_exclusion.environ_with_exclusion(
            {"PLX_EXCLUDED_PEOPLE_OIDS": other, "PLX_EXCLUSION_ENV_FILE": str(self.file)})
        self.assertEqual(merged["PLX_EXCLUDED_PEOPLE_OIDS"], other)

    def test_doubtful_file_fails_closed(self) -> None:
        os.chmod(self.file, 0o644)                     # readable by others: not trusted
        self.assertEqual(self.guard({"PLX_EXCLUSION_ENV_FILE": str(self.file)}).unavailable,
                         "excluded_people_oids_absent")
        os.chmod(self.file, 0o600)
        for named in (str(self.dir / "missing.env"), "", str(self.dir)):
            self.assertEqual(self.guard({"PLX_EXCLUSION_ENV_FILE": named}).unavailable,
                             "excluded_people_oids_absent", named)
        self.file.write_text("PLX_EXCLUDED_PEOPLE_OIDS=not-an-id\n", encoding="utf-8")
        self.assertEqual(self.guard({"PLX_EXCLUSION_ENV_FILE": str(self.file)}).unavailable,
                         "excluded_people_oids_invalid")
        link = self.dir / "link.env"
        link.symlink_to(self.file)
        self.assertEqual(self.guard({"PLX_EXCLUSION_ENV_FILE": str(link)}).unavailable,
                         "excluded_people_oids_absent")


if __name__ == "__main__":
    unittest.main()
