"""A later mail exclusion.

A person excluded, or a mailbox denied, after their mail was captured: every store read
hides it at once (``MailCaptureStore._visible``, every reader, grant holders included),
the next capture purges it and reports what it purged, and mail sent to or copying an
excluded person stays out of every other mailbox. Injected transport only (the fake
Graph of ``test_capture_mail_all_mailboxes``); synthetic ids and ``example-plx.com``
addresses. Evidence label: ``injected-transport``; the live check
``graph.mail_all_mailboxes`` (``exclusion_check``) is live-unverified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from datetime import datetime
from unittest import mock

from integrations import capture_mail
from integrations.capture_mail import MailCaptureStore
from integrations.live_source_guard import LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.tests.test_capture_mail import ENV, file_attachment
from integrations.tests.test_capture_mail_all_mailboxes import (
    AUDIENCE, EXCLUDED, EXCLUDED_ALIAS, EXCLUDED_UPN, SPEC, AllMailboxesCase, DirectoryGraph,
    lv, oid)
from ontology.types import Actor

# Run 1 excludes someone outside the directory, so the person EXCLUDED is captured; run 2
# (ENV) excludes them: the exclusion came later.
OTHER_ENV = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e2"}
LATER = {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED}
X_BODY = "Only the excluded person's mailbox holds this body."
X_BYTES = b"only the excluded person's attachment bytes\n" * 8
TABLES = ("mailboxes", "folders", "messages", "attachments", "mailbox_directory")


def cc(graph: DirectoryGraph, box: str, mid: str, *, to=(), copy=()) -> None:
    """A message in ``box`` addressed (``to``) or copied (``copy``) to these addresses."""
    graph.put(box, mid, "CK-1")
    raw = graph.messages[box][mid]
    raw["toRecipients"] += [{"emailAddress": {"address": a}} for a in to]
    raw["ccRecipients"] = [{"emailAddress": {"address": a}} for a in copy]


class LaterExclusionTests(AllMailboxesCase):
    def setUp(self) -> None:
        super().setUp()
        self.audience = LiveSourceAudience.from_file(AUDIENCE)
        self.actor = Actor(self.audience.tenant, frozenset({"svc:mail-capture"}),
                           "restricted")
        # Read-time decisions below use the process environment: start from none.
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
            os.environ.pop(name, None)
        self.addCleanup(self.env.stop)

    def captured(self, graph: DirectoryGraph | None = None) -> DirectoryGraph:
        """Run 1: the person EXCLUDED is not excluded yet; their mailbox (a message with
        its own body and attachment) and a message copying them in box 1 are captured."""
        graph = graph or DirectoryGraph(members=8)
        graph.put(EXCLUDED, "M-x-own", "CK-1", body=X_BODY,
                  attachments=[file_attachment("AX", content=X_BYTES)])
        cc(graph, oid(1), "M-cc-before", copy=[EXCLUDED_ALIAS])
        code, report = self.main(graph, "--runs", "1", environ=OTHER_ENV)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["directory"]["mailboxes_enumerated"], 10)
        self.assertEqual(report["summary"]["mailboxes"], 10)
        return graph

    def raw(self, sql: str, *params) -> list:
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def x_handles(self) -> list[str]:
        return [h for (h,) in self.raw(
            "SELECT l1_handle FROM messages WHERE mailbox_id=? UNION "
            "SELECT l1_handle FROM attachments WHERE mailbox_id=? AND l1_handle IS NOT NULL",
            EXCLUDED, EXCLUDED)]

    def served(self, store: MailCaptureStore, actor: Actor,
               audience: LiveSourceAudience) -> dict:
        """What every store consumer gives one reader of the EXCLUDED mailbox."""
        handles = self.x_handles()
        attachment = next(h for h in handles if "/attachments/" in h or "attachment" in h)
        message = attachment.split("#", 1)[0]
        moment = datetime.fromisoformat(json.loads(self.raw(
            "SELECT record FROM messages WHERE l1_handle=?", message)[0][0]
        )["received_at"].replace("Z", "+00:00"))
        rows = store.messages(actor, audience, limit=1000)["items"]
        return {
            "messages": [r for r in rows if r["l1_handle"] in handles],
            "completeness": [m for m in store.completeness(actor, audience)["mailboxes"]
                             if m["mailbox"] == EXCLUDED],
            "index": [i for i in store.index(actor, audience)["items"]
                      if i["mailbox"] == EXCLUDED],
            "resolved": [h for h in handles if store.resolve(h, actor, audience, text=True)],
            "history": store.history(actor, audience, message),
            "attachments": store.attachments(message, actor, audience),
            "attachment_content": store.attachment_content(attachment, actor, audience),
            "text_versions": store.text_versions(attachment, actor, audience),
            "received_at": [h for h in store.received_at(actor, audience, moment)
                            if h in handles],
            "audience_check": store.audience_check(audience)(actor, EXCLUDED),
            "item_gate": [h for h in handles if store.item_gate(audience)(actor, h)],
            "coverage": store.mailbox_coverage(EXCLUDED, actor, audience)["status"],
        }

    def owner(self) -> tuple[Actor, LiveSourceAudience]:
        audience = LiveSourceAudience(
            {"mail:*": ["staff:owner"]}, tenant="plx", classifications={"mail:*": "restricted"},
            identities={"staff:owner": [EXCLUDED]}, source_acl_required=["mail:*"])
        return Actor("plx", frozenset({"staff:owner"}), "restricted"), audience

    def test_a_later_exclusion_hides_the_mailbox_at_read_before_any_recapture(self):
        graph = self.captured()
        requests = len(graph.calls)
        nothing = {"messages": [], "completeness": [], "index": [], "resolved": [],
                   "history": [], "attachments": [], "attachment_content": None,
                   "text_versions": [], "received_at": [], "audience_check": False,
                   "item_gate": [], "coverage": "not_captured"}
        owner, owner_audience = self.owner()
        store = MailCaptureStore(self.state)
        try:
            # Before the exclusion, every consumer serves it (the probe's control).
            before = self.served(store, self.actor, self.audience)
            self.assertTrue(all(before[k] not in ([], None, False, "not_captured")
                                for k in nothing), before)
            self.assertTrue(self.served(store, owner, owner_audience)["resolved"])
            marking = store.owner_marking(self.actor, self.audience)
            counts = store.counts(self.actor, self.audience)
            x_rows = len(before["messages"])
            # The exclusion arrives in the serving process's environment: no capture runs.
            os.environ.update(LATER)
            for reader in ((self.actor, self.audience), (owner, owner_audience)):
                self.assertEqual(self.served(store, *reader), nothing)
            self.assertEqual(store.owner_marking(self.actor, self.audience),
                             {"with": marking["with"] - x_rows, "without": 0})
            self.assertEqual(store.owner_marking(owner, owner_audience),
                             {"with": 0, "without": 0})
            self.assertEqual(store.counts(self.actor, self.audience)["mailboxes"],
                             counts["mailboxes"] - 1)
            # A configured list that cannot decide withholds every mailbox.
            os.environ["PLX_EXCLUDED_PEOPLE_OIDS"] = "not-an-object-id"
            self.assertEqual(store.completeness(self.actor, self.audience)["mailboxes"], [])
            self.assertEqual(store.messages(self.actor, self.audience)["total"], 0)
        finally:
            store.close()
        # A guard given to the store decides the same, whatever the environment says.
        os.environ.pop("PLX_EXCLUDED_PEOPLE_OIDS")
        store = MailCaptureStore(self.state, people=PeopleExclusion.from_env(ENV))
        try:
            self.assertEqual(self.served(store, self.actor, self.audience), nothing)
        finally:
            store.close()
        self.assertEqual(len(graph.calls), requests)   # offline: no request at all
        self.assertTrue(self.x_handles())             # hidden, not yet purged

    def test_the_next_capture_purges_it_and_mail_naming_the_person_stays_out(self):
        graph = self.captured()
        store = MailCaptureStore(self.state)
        try:   # an attachment text read again: its history is append-only otherwise
            [attachment] = [h for (h,) in self.raw(
                "SELECT l1_handle FROM attachments WHERE mailbox_id=?", EXCLUDED)]
            store.fill_text(attachment, "text", store.put_text("excluded-only reading"))
        finally:
            store.close()
        x_blobs = {d for (d,) in self.raw(
            "SELECT body_sha256 FROM messages WHERE mailbox_id=? UNION SELECT content_sha256 "
            "FROM attachments WHERE mailbox_id=? UNION SELECT text_sha256 FROM "
            "attachment_text_versions", EXCLUDED, EXCLUDED) if d}
        # A body other mailboxes hold too stays: only what nothing else references goes.
        shared = x_blobs & {d for (d,) in self.raw(
            "SELECT body_sha256 FROM messages WHERE mailbox_id<>? AND message_id<>"
            "'M-cc-before'", EXCLUDED)}
        x_blobs -= shared
        kept = self.raw("SELECT COUNT(*) FROM messages WHERE mailbox_id<>? AND "
                        "message_id<>'M-cc-before'", EXCLUDED)[0][0]
        self.assertGreaterEqual(len(x_blobs), 3)
        # After the exclusion, new mail to and copying the person arrives in box 2.
        cc(graph, oid(2), "M-to-after", to=[EXCLUDED_UPN])
        cc(graph, oid(2), "M-cc-after", copy=[EXCLUDED_ALIAS.upper()])
        code, report = self.main(graph)       # ENV: excluded now; run 2 resumes
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"], report)
        # The reproduced case: 10 stored against 9 enumerated; now 9 and 9.
        self.assertEqual(report["directory"]["mailboxes_enumerated"], 9)
        self.assertEqual(report["summary"]["mailboxes"], 9)
        self.assertEqual(report["summary"]["purged"], {
            "mailboxes": 1, "messages": 3, "versions": 3, "attachments": 1,
            "blobs": len(x_blobs)})
        # The first run purges; the second finds nothing more to purge.
        self.assertEqual([r["purged"] for r in report["runs"]],
                         [report["summary"]["purged"],
                          dict.fromkeys(capture_mail.PURGE_COUNTS, 0)])
        self.assertEqual(report["exclusion_check"], {
            "mailboxes_stored": 0, "items_stored": 0, "items_readable": 0,
            "mailboxes_readable": 0})
        dropped = sum(f["counts"]["people_dropped"] for m in report["runs"][0]["mailboxes"]
                      for f in m["folders"])
        self.assertEqual(dropped, 2)
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])
        # Nothing of the person is left in any table, record or blob file.
        for table in TABLES:
            self.assertEqual(self.raw(f"SELECT COUNT(*) FROM {table} WHERE mailbox_id=?",
                                      EXCLUDED), [(0,)], table)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM messages WHERE message_id IN "
                                  "('M-cc-before', 'M-to-after', 'M-cc-after')"), [(0,)])
        text = " ".join(r for (r,) in self.raw("SELECT record FROM messages")).lower()
        for token in (EXCLUDED, EXCLUDED_UPN, EXCLUDED_ALIAS):
            self.assertNotIn(token, text)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM attachment_text_versions"), [(0,)])
        blobs = self.state / "mail-capture" / "blobs" / "sha256"

        def forms(digest):   # the bytes, compressed (bodies, texts), a dropped original
            return [blobs / digest[:2] / (digest + suffix) for suffix in ("", ".z", ".dropped")]
        for digest in x_blobs:
            self.assertFalse(any(f.exists() for f in forms(digest)), digest)
            self.assertEqual(self.raw("SELECT COUNT(*) FROM blobs WHERE sha256=?", digest),
                             [(0,)])
        for digest in shared:
            self.assertTrue(any(f.exists() for f in forms(digest)), digest)
        # Every other mailbox's mail is intact, and text history is append-only again.
        self.assertEqual(report["summary"]["messages_current"], kept)
        db = sqlite3.connect(self.state / "mail-capture" / "capture.db")
        try:
            db.execute("INSERT INTO attachment_text_versions VALUES ('h', 1, 'text', NULL, "
                       "'2026-10-21T00:00:00Z')")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM attachment_text_versions")
        finally:
            db.close()

    def test_a_mailbox_added_to_the_deny_list_is_hidden_then_purged(self):
        graph = self.captured()
        scope = json.loads(Path(capture_mail.REPO_ROOT / "integrations" /
                                "mail_all_mailboxes_scope.json").read_text())
        scope["deny"]["address_tokens"].append("box3")
        path = Path(self.tmp.name) / "scope.json"
        path.write_text(json.dumps(scope))
        from integrations import mail_directory_scope as mds
        deny = mds.DirectoryScope.from_file(path).deny
        store = MailCaptureStore(self.state, deny=deny)
        try:
            seen = {m["mailbox"] for m in store.completeness(
                self.actor, self.audience)["mailboxes"]}
            self.assertNotIn(oid(3), seen)
            self.assertIn(oid(4), seen)
            self.assertEqual(len(seen), 9)
        finally:
            store.close()
        code, report = self.main(graph, "--runs", "1", "--scope", str(path),
                                 environ=OTHER_ENV)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["directory"]["mailboxes_denied"], 8)
        self.assertEqual(report["summary"]["mailboxes"], 9)
        self.assertEqual(report["summary"]["purged"]["mailboxes"], 1)
        self.assertEqual(report["exclusion_check"]["items_stored"], 0)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM messages WHERE mailbox_id=?",
                                  oid(3)), [(0,)])

    def test_purging_mail_that_names_the_person_scales_without_loss(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.state = Path(self.tmp.name) / f"state-{n}"
                graph = DirectoryGraph(members=2)
                for k in range(n):
                    cc(graph, oid(1), f"M-cc-{k}", copy=[EXCLUDED_UPN])
                self.main(graph, "--runs", "1", environ=OTHER_ENV)
                kept = self.raw("SELECT message_id FROM messages WHERE mailbox_id IN (?,?) "
                                "AND message_id NOT LIKE 'M-cc-%' ORDER BY message_id",
                                oid(1), oid(2))
                started = time.monotonic()
                code, report = self.main(graph, "--runs", "1")
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["summary"]["purged"]["messages"], n + 1)
                self.assertEqual(self.raw("SELECT COUNT(*) FROM messages WHERE message_id "
                                          "LIKE 'M-cc-%'"), [(0,)])
                self.assertEqual(self.raw(
                    "SELECT message_id FROM messages WHERE mailbox_id IN (?,?) "
                    "ORDER BY message_id", oid(1), oid(2)), kept)

    def test_without_the_purge_and_the_read_filter_the_check_fails(self):
        # The negative control of exclusion_check: the store left as it was.
        graph = self.captured()
        with mock.patch.object(MailCaptureStore, "purge_withheld",
                               lambda self, people: dict.fromkeys(capture_mail.PURGE_COUNTS, 0)), \
                mock.patch.object(MailCaptureStore, "_withholds",
                                  lambda self, people=None, undecided=True: lambda row: False):
            code, report = self.main(graph, "--runs", "1")
        check = report["exclusion_check"]
        self.assertEqual(check["mailboxes_stored"], 1)
        self.assertGreater(check["items_readable"], 0)
        self.assertEqual(check["items_readable"], check["items_stored"])
        self.assertEqual(check["mailboxes_readable"], 1)
        self.assertFalse(report["ok"])
        self.assertTrue(any("exclusion_check" in f
                            for f in lv.check_expectations(SPEC, code, report)))


if __name__ == "__main__":
    unittest.main()
