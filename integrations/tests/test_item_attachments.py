"""Attached emails (Graph item attachments, ``message/rfc822``): fetched by one guarded GET of
``.../attachments/{id}/$value`` and read as ``<subject>.eml``.

Boundaries: the new operation is a GET of a document only (the guard still refuses every
write verb on it and every other operation as a document); the downloader stops under the
disk floor and leaves the rows as they were; reopen lists only current item attachments; the
fetched message lands through the capture's own write path (text read, carried-mail exclusion
decided). Evidence label: injected-transport (live-unverified until
live check ``mail.item_attachment`` passes)."""

from __future__ import annotations

import json
import unittest
from email.message import EmailMessage
from urllib.parse import urlsplit

from integrations import attach_fetch, capture_mail, graph_read
from integrations import live_source_grammar as grammar
from integrations import live_source_guard as guard
from integrations.tests import test_attach_fetch as taf
from integrations.tests import test_capture_mail as tm

USER, MSG, ATT = tm.BOX_A, "MSG-1", "ATT-ITEM"
URL = f"https://graph.microsoft.com/v1.0/users/{USER}/messages/{MSG}/attachments/{ATT}/$value"


def eml(subject="RE: Lot 26233A certificate", body="Please find the certificate of analysis."):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "lab@vendor.example", "ops@petra.example", subject
    msg.set_content(body)
    return bytes(msg)


class OperationGuard(unittest.TestCase):
    def test_the_raw_attachment_is_a_get_of_a_document_and_nothing_else(self):
        self.assertEqual(grammar.match("graph.microsoft.com", urlsplit(URL).path),
                         "graph.message_attachment_raw")
        guard.check_request("GET", URL, environ={})                       # no raise
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(verb=verb), self.assertRaises(guard.ReadOnlyGuardError):
                guard.check_request(verb, URL, body=b"{}", environ={})
        # only declared documents are read as documents: a file attachment's JSON is not
        plain = URL.rsplit("/$value", 1)[0]
        self.assertEqual(grammar.match("graph.microsoft.com", urlsplit(plain).path),
                         "graph.message_attachment")
        self.assertNotIn("graph.message_attachment", guard.DOCUMENT_OPERATIONS)
        self.assertIn("graph.message_attachment_raw", guard.DOCUMENT_OPERATIONS)
        # a path that only looks like it is no operation
        for bad in (URL + "/x", URL.replace("/$value", "/$VALUE/x"),
                    URL.replace("attachments", "attachment")):
            with self.subTest(url=bad), self.assertRaises(guard.ReadOnlyGuardError):
                guard.check_request("GET", bad, environ={})


class Rows:
    """Work rows as the fetch store hands them to the downloader."""

    def __init__(self, items):
        self.items = items


class FakeApi:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get_document(self, url, *, max_bytes):
        self.asked.append(url)
        got = self.answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got


class FakeFetch:
    def __init__(self, tmp):
        from pathlib import Path
        self.root, self.put = Path(tmp), []

    def put_bytes(self, content):
        import hashlib
        digest = hashlib.sha256(content).hexdigest()
        self.put.append(digest)
        return digest


def row(aid="A1", size=1000, kind=attach_fetch.ITEM_KIND, name="RE: Lot 26233A certificate"):
    return {"message_handle": "graph-mail://m/x@ck", "attachment_id": aid, "mailbox_id": "m",
            "message_id": "MSG-1", "version_id": 1,
            "metadata": json.dumps({"kind": kind, "name": name, "size": size, "id": aid})}


class DownloaderItems(unittest.TestCase):
    def downloader(self, answers, free=1 << 50, **kw):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        api = FakeApi(answers)
        d = attach_fetch.Downloader.__new__(attach_fetch.Downloader)
        d.api, d.fetch, d.max_bytes = api, FakeFetch(tmp.name), 20_000_000
        d.min_free, d.item_min_free = kw.get("min_free", 0), kw.get("item_min_free", 75 << 30)
        d.free = lambda path: free
        d.stats, d.stats_lock = {}, __import__("threading").Lock()
        return d, api

    box = type("Box", (), {"object_id": tm.BOX_A})()

    def test_a_fetched_message_is_stored_and_named_for_the_reader_as_an_eml(self):
        raw = eml()
        d, api = self.downloader([raw], free=100 << 30)
        settled, again, wait = d._item_rows(self.box, [row()])
        self.assertEqual(again, [])
        (r, result), = settled
        self.assertEqual(result["status"], attach_fetch.FETCHED)
        meta = result["read_meta"]
        self.assertEqual((meta["name"], meta["content_type"]), (
            "RE: Lot 26233A certificate.eml", "message/rfc822"))
        self.assertEqual(meta["size"], len(raw))
        self.assertIn("/messages/MSG-1/attachments/A1/$value", api.asked[0])

    def test_under_the_disk_floor_nothing_is_requested_and_the_rows_wait(self):
        d, api = self.downloader([eml()], free=74 << 30)   # 75 GB floor: stops
        settled, again, _ = d._item_rows(self.box, [row("A1"), row("A2")])
        self.assertEqual((settled, len(again), api.asked), ([], 2, []))

    def test_every_answer_is_a_state_never_lost(self):
        from integrations.live_source_guard import SourceNotFound, SourceUnavailable
        throttled = SourceUnavailable("429")
        throttled.http_status, throttled.retry_after_seconds = 429, 7
        d, api = self.downloader([SourceNotFound("gone"), throttled, eml()], free=100 << 30)
        settled, again, wait = d._item_rows(self.box, [row("A1"), row("A2"), row("A3")])
        self.assertEqual([s[1]["status"] for s in settled],
                         [attach_fetch.NOT_VISIBLE, attach_fetch.FETCHED])
        self.assertEqual([r["attachment_id"] for r in again], ["A2"])
        self.assertEqual(wait, 7)
        big, _ = self.downloader([], free=100 << 30)
        settled, again, _ = big._item_rows(self.box, [row("A9", size=30_000_000)])
        self.assertEqual(settled[0][1], {"status": attach_fetch.FILLED,
                                         "content_status": "exceeds_cap"})

    def test_only_item_attachments_take_this_path(self):
        self.assertTrue(attach_fetch._is_item(row()))
        self.assertFalse(attach_fetch._is_item(row(kind="#microsoft.graph.fileAttachment")))
        self.assertFalse(attach_fetch._is_item({"metadata": "not json"}))


class Reopen(taf.FastPathCase):
    """Attached emails the capture left as not-a-file are listed again as deferred, once, so
    the fast path (plan, download, read, apply) takes them; files and links are untouched."""

    def setUp(self):
        super().setUp()
        self.raw = eml(body="Lot Number: 26233A passed.")

    def test_only_current_item_attachments_are_reopened_once(self):
        # (the base fixture already stored its three PDFs as deferred; add the listing's item)
        meta = json.dumps({"kind": attach_fetch.ITEM_KIND, "name": "RE: Lot 26233A certificate",
                           "size": len(self.raw), "id": ATT, "content_type": "message/rfc822"})
        handle = graph_read.mail_citation(tm.BOX_A, "MSG-1", "CK-1")
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO attachments (message_handle, attachment_id, mailbox_id, "
                "metadata, content_status, recorded_at) VALUES (?,?,?,?, 'not_file_attachment', "
                "'2026-11-01')", (handle, ATT, tm.BOX_A, meta))
        self.assertEqual(self.store.reopen_item_attachments(), 1)
        self.assertEqual(self.store.reopen_item_attachments(), 0)   # nothing left to reopen
        status = self.store.db.execute("SELECT content_status FROM attachments WHERE "
                                       "attachment_id=?", (ATT,)).fetchone()[0]
        self.assertEqual(status, capture_mail.ATTACHMENT_DEFERRED)


if __name__ == "__main__":
    unittest.main()
