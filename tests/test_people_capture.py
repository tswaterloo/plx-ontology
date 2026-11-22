"""The captures include the two excluded people: with
``PLX_CAPTURE_PEOPLE=include`` a mail capture opens their mailbox and keeps mail they send
or receive, recording their addresses offline as before (the people set decides who reads
it after capture: the owner alone); without it, the capture keeps the old guard. A list
that does not load still captures nothing; a OneDrive stays refused either way. The
re-capture plan counts what the capture still holds back. Evidence label:
injected-transport (the capture tests' in-process FakeGraph, made-up ids and addresses)."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_capture import ENV as INCLUDE_ENV, IncludingExclusion, capture_guard
from integrations.people_exclusion import (ExclusionUnavailable, PeopleExcluded,
                                           PeopleExclusion, address_digest)
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, SCOPE, UPN_B,
                                                   Clock, FakeGraph, capture_audience)

EXCLUDED_B = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B}   # the finance mailbox's owner


def capture(env):
    tmp = tempfile.TemporaryDirectory()
    state = Path(tmp.name) / "mail"
    graph = FakeGraph()
    graph.put(BOX_A, "MSG-CC", "CK-1", subject="Copied to the excluded person")
    graph.messages[BOX_A]["MSG-CC"]["ccRecipients"] = [{"emailAddress": {"address": UPN_B}}]
    graph.put(BOX_A, "MSG-OK", "CK-2", subject="Plain")
    graph.put(BOX_B, "MSG-OWN", "CK-3", subject="In the excluded person's own mailbox")
    store = MailCaptureStore(state, clock=Clock())
    api, _ = graph_read.build_graph(env, sender=graph, capture=True, retry=BoundedBackoff(
        sleep=lambda s: None, jitter=lambda: 0.0))
    run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                      sleep=lambda s: None).run(SCOPE)
    ids = {r[0] for r in store.db.execute("SELECT message_id FROM messages")}
    record = dict(store.db.execute("SELECT key, value FROM meta WHERE key IN "
                                   "('tenant_withheld_at', 'tenant_listing_at')").fetchall())
    kinds = dict(store.db.execute("SELECT digest, kind FROM tenant_withheld").fetchall()) \
        if record.get("tenant_withheld_at") else {}
    store.close()
    return tmp, run, ids, kinds


class PeopleCaptureTests(unittest.TestCase):
    def test_include_keeps_their_mail_and_mailbox_and_still_records_their_addresses(self):
        tmp, run, ids, kinds = capture({**EXCLUDED_B, INCLUDE_ENV: "include"})
        self.addCleanup(tmp.cleanup)
        self.assertEqual(ids, {"MSG-CC", "MSG-OK", "MSG-OWN"})
        # offline the capture still says whose the addresses are (the set decides by them)
        self.assertEqual(kinds.get(address_digest(UPN_B)), "excluded")

    def test_without_include_the_old_guard_refuses_and_drops(self):
        tmp, run, ids, _kinds = capture(EXCLUDED_B)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(ids, {"MSG-OK"})

    def test_the_guard_excludes_no_one_but_fails_closed_and_keeps_onedrive_out(self):
        guard = capture_guard({**EXCLUDED_B, INCLUDE_ENV: "include"})
        self.assertIsInstance(guard, IncludingExclusion)
        self.assertFalse(guard.is_excluded(BOX_B))
        guard.refuse([BOX_B, UPN_B], "mailbox")   # never refuses
        self.assertFalse(guard.excluded([BOX_B]))
        guard.attach_directory([(BOX_B, UPN_B, [UPN_B])])
        with self.assertRaises(PeopleExcluded):   # their OneDrive stays out
            guard.refuse_onedrive_path("finance_example-plx_com")
        self.assertNotIsInstance(capture_guard(EXCLUDED_B), IncludingExclusion)
        for value in ("", "yes", "INCLUDED"):
            self.assertNotIsInstance(capture_guard({**EXCLUDED_B, INCLUDE_ENV: value}),
                                     IncludingExclusion)
        unloaded = capture_guard({INCLUDE_ENV: "include"})
        self.assertTrue(unloaded.unavailable)
        with self.assertRaises(ExclusionUnavailable):
            unloaded.is_excluded(BOX_B)
        self.assertIsInstance(PeopleExclusion.from_env(EXCLUDED_B), PeopleExclusion)


class RecapturePlanTests(unittest.TestCase):
    def test_plan_counts_what_is_held_back_and_apply_refuses_without_include_or_disk(self):
        from scripts import partners_recapture as pr
        tmp, _run, _ids, _kinds = capture(EXCLUDED_B)
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "mail"
        db = pr._db(state, write=False)
        try:
            found = pr.counts(db)
        finally:
            db.close()
        self.assertGreaterEqual(found["withheld_messages"], 1)   # the copy to finance
        out = pr.apply(state, Path(tmp.name) / "backup", "postgresql:///nonexistent-db")
        self.assertFalse(out["ok"])
        self.assertIn("include_on", out["refused"])
        self.assertFalse((Path(tmp.name) / "backup" / pr.BACKUP).exists())

    def test_apply_backs_up_then_forgets_and_undo_restores(self):
        from unittest import mock
        from scripts import partners_recapture as pr
        tmp, _run, _ids, _kinds = capture(EXCLUDED_B)
        self.addCleanup(tmp.cleanup)
        state, backup = Path(tmp.name) / "mail", Path(tmp.name) / "backup"
        ok = {"include_on": True, "disk_ok": True, "lag_ok": True}
        with mock.patch.object(pr, "guards", lambda *a: ok):
            before = pr.counts(pr._db(state, write=False))
            out = pr.apply(state, backup, None)
            self.assertTrue(out["ok"], out)
            self.assertEqual(oct((backup / pr.BACKUP).stat().st_mode)[-3:], "600")
            self.assertEqual(pr.counts(pr._db(state, write=False))["withheld_messages"], 0)
            again = pr.apply(state, backup, None)   # never over an earlier backup
            self.assertEqual(again["refused"], ["backup_exists"])
        pr.undo(state, backup)
        self.assertEqual(pr.counts(pr._db(state, write=False)), before)



class NothingPurgedWhileIncludedTests(unittest.TestCase):
    def test_the_purge_refuses_whatever_is_scheduled_once_the_captures_include(self):
        import contextlib
        import io
        from operating import mail_exclusion_purge as mp
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["scan", "--state-dir", tmp, "--live-audience", "x.json"],
                         ["purge", "--state-dir", tmp, "--apply"],
                         ["carried", "--state-dir", tmp, "--apply"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mp.main(argv, environ={**EXCLUDED_B, INCLUDE_ENV: "include"})
                self.assertEqual(code, 2, argv)
                self.assertIn("nothing is purged", out.getvalue())


class StyleMiningBarTests(unittest.TestCase):
    def test_a_sent_document_bcc_to_an_excluded_person_is_never_mined(self):
        import sqlite3
        from operating import doc_style_mine as dm
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mail-capture"
            root.mkdir()
            db = sqlite3.connect(root / "capture.db")
            db.execute("CREATE TABLE messages (l1_handle, record, mailbox_id, superseded_at)")
            db.execute("CREATE TABLE attachments (message_handle, metadata, l1_handle, "
                       "content_sha256, content_status)")
            me = "staff@example-plx.com"
            for n, bcc in (("plain", []), ("bcc", ["partner@example-plx.com"])):
                rec = {"from": me, "mailbox_upn": me, "to": ["buyer@outside.example"],
                       "cc": [], "bcc": bcc, "sent_at": "2026-11-01T10:00:00Z",
                       "internet_message_id": f"<{n}>", "subject": n}
                db.execute("INSERT INTO messages VALUES (?, ?, ?, NULL)",
                           (f"m-{n}", json.dumps(rec), BOX_A))
                db.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, 'captured')",
                           (f"m-{n}", json.dumps({"name": "Quote.docx", "size": 10}),
                            f"a-{n}", "d" * 64))
            db.commit()
            db.close()
            guard = PeopleExclusion([BOX_B])
            guard.attach_directory([(BOX_B, "partner@example-plx.com",
                                     ["partner@example-plx.com"]),
                                    (BOX_A, me, [me])])
            guard.known_domains = lambda: {"example-plx.com"}
            import re
            got = [d["subject"] for d in dm.mail_documents(
                Path(tmp), guard, [("quote", re.compile("quote"))])]
            self.assertEqual(got, ["plain"])

if __name__ == "__main__":
    unittest.main()
