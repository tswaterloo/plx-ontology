"""Mail capture volume and throughput (every folder, the whole history, fair turns,
concurrent reads, OCR beside the capture, shared Retry-After) over an injected Graph.

The live store stopped near 1,000 messages in 4 of 166 mailboxes: each scanned PDF was
OCR-read inline (about 60 s each), one large mailbox held every budget (its cut folder
is scheduled first), and only inbox and sent items in a 90-day window were in scope.
These tests pin the fixes at the capture's boundary (what the store holds, what Graph is
asked). No network, no credentials: the fake Graph of ``test_capture_mail`` with folder
listings, child folders and a newest-first message listing. Evidence label:
``injected-transport``.
"""

from __future__ import annotations

import re
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from integrations import capture_mail, graph_read
from integrations.capture_mail import CaptureScope, MailCapture
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, SYNTHETIC,
                                                  UPN_A, UPN_B, CaptureCase, FakeGraph,
                                                  capture_audience, file_attachment, j,
                                                  keyed_delta)

EXCLUDED = ENV["PLX_EXCLUDED_PEOPLE_OIDS"]
EXCLUDED_MAIL = "gone.person@example-plx.com"
CUSTOM, CHILD, EMPTY = "AAMkCUSTOM=", "AAMkCHILD=", "AAMkEMPTY="
OLD = "2026-03-01T09:00:00Z"   # before the 90-day window of the test clock (2026-10-01)


def every_folder_scope(**kwargs) -> CaptureScope:
    return CaptureScope([{"address": UPN_A, "folders": "all"},
                         {"address": UPN_B, "folders": "all"}], window_days=90,
                        internal_domains=["example-plx.com"], allowlist=SYNTHETIC, **kwargs)


class FolderGraph(FakeGraph):
    """The fake Graph with a folder tree per mailbox (inbox, a custom folder with a child
    folder, an empty folder), a folder-aware delta, a newest-first message listing
    (``receivedDateTime lt``, ``$skip`` paging) and the excluded person in the directory.
    ``slow``: seconds each message GET takes (to make concurrent reads overlap)."""

    def __init__(self) -> None:
        super().__init__()
        self.users[EXCLUDED] = {"id": EXCLUDED, "userPrincipalName": EXCLUDED_MAIL,
                                "mail": EXCLUDED_MAIL, "proxyAddresses": []}
        for user, inbox in ((BOX_A, "AAMkFOLDER-A="), (BOX_B, "AAMkFOLDER-B=")):
            self.folders[user] = {"inbox": inbox, inbox.lower(): inbox}
            for fid in (CUSTOM, CHILD, EMPTY):
                self.folders[user][fid.lower()] = fid
        self.tree = {None: ["inbox", CUSTOM, EMPTY], CUSTOM: [CHILD]}
        self.slow = 0.0
        self.inflight = 0
        self.max_inflight = 0
        self.lock = threading.Lock()
        self.throttle_message_gets = 0   # message GETs answered 429 Retry-After first

    def put(self, user, mid, change_key, *, folder="inbox", sender=None, **kwargs):
        super().put(user, mid, change_key, **kwargs)
        raw = self.messages[user][mid]
        raw["parentFolderId"] = self.folders[user][folder.lower()]
        if sender is not None:
            raw["from"] = {"emailAddress": {"address": sender}}

    def _count(self, user, fid):
        return sum(1 for m in self.messages[user].values() if m["parentFolderId"] == fid)

    def _folder_rows(self, user, parent):
        rows = []
        for key in self.tree.get(parent, []):
            fid = self.folders[user][key.lower()]
            rows.append({"id": fid, "totalItemCount": self._count(user, fid),
                         "childFolderCount": len(self.tree.get(key, []))})
        return rows

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        if parts.hostname == "graph.microsoft.com":
            segs = path.split("/")
            for pattern, left in self.fail_get.items():
                if left and re.fullmatch(pattern, path):
                    self.fail_get[pattern] = left - 1
                    return j(500, {"error": {"code": "generalException"}})
            if len(segs) == 5 and segs[4] == "mailFolders":
                self.calls.append((method, url, dict(headers)))
                return j(200, {"value": self._folder_rows(segs[3], None)})
            if len(segs) == 7 and segs[6] == "childFolders":
                self.calls.append((method, url, dict(headers)))
                return j(200, {"value": self._folder_rows(segs[3], segs[5])})
            if len(segs) == 7 and segs[4] == "mailFolders" and segs[6] == "messages":
                self.calls.append((method, url, dict(headers)))
                return self._listing(segs[3], segs[5], parts, query)
            if len(segs) == 6 and segs[4] == "messages":
                with self.lock:
                    if self.throttle_message_gets:
                        self.throttle_message_gets -= 1
                        self.calls.append((method, url, dict(headers)))
                        return j(429, {"error": {"code": "TooManyRequests"}},
                                 {"Retry-After": "1"})
                    self.inflight += 1
                    self.max_inflight = max(self.max_inflight, self.inflight)
                try:
                    if self.slow:
                        time.sleep(self.slow)
                    return super().__call__(method, url, headers, body, timeout, max_bytes)
                finally:
                    with self.lock:
                        self.inflight -= 1
        with self.lock:
            return super().__call__(method, url, headers, body, timeout, max_bytes)

    def _listing(self, user, fid, parts, query):
        floor = query["$filter"][0].rsplit(" ", 1)[-1]
        assert query["$orderby"] == ["receivedDateTime desc"], query
        top, skip = int(query["$top"][0]), int(query.get("$skip", ["0"])[0])
        found = sorted((self._public(m) for m in self.messages[user].values()
                        if m["parentFolderId"] == fid and m["receivedDateTime"] < floor),
                       key=lambda m: m["receivedDateTime"], reverse=True)
        body = {"value": found[skip:skip + top]}
        if skip + top < len(found):
            kept = "&".join(p for p in parts.query.split("&") if not p.startswith("$skip="))
            body["@odata.nextLink"] = (f"https://graph.microsoft.com{parts.path}?{kept}"
                                       f"&$skip={skip + top}")
        return j(200, body)

    def _delta(self, user, folder, query, headers):
        fid = self.folders[user].get(folder.lower(), folder)
        size = int(headers["Prefer"].rsplit("=", 1)[1])
        if "$skiptoken" in query:
            round_id, offset = query["$skiptoken"][0].split(":")
            entries, final = self.rounds[round_id]
            offset = int(offset)
        else:
            if "$deltatoken" in query:
                since = int(query["$deltatoken"][0])
                changed = list(dict.fromkeys(m for seq, who, m in self.log
                                             if seq > since and who == user))
                entries = [self._public(self.messages[user][m]) for m in changed
                           if m in self.messages[user]
                           and self.messages[user][m]["parentFolderId"] == fid]
            else:
                floor = query.get("$filter", [""])[0].rsplit(" ", 1)[-1]
                entries = [self._public(m) for m in self.messages[user].values()
                           if m["parentFolderId"] == fid and m["receivedDateTime"] >= floor]
            round_id = f"r{len(self.rounds)}"
            self.rounds[round_id] = (entries, self.seq)
            offset = 0
        body = {"value": entries[offset:offset + size]}
        if offset + size < len(entries):
            body["@odata.nextLink"] = keyed_delta(user, fid, f"$skiptoken={round_id}:"
                                                  f"{offset + size}")
        else:
            body["@odata.deltaLink"] = keyed_delta(user, fid,
                                                   f"$deltatoken={self.rounds[round_id][1]}")
        return j(200, body)


class ThroughputCase(CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.graph = FolderGraph()

    def capture(self, **kwargs) -> MailCapture:
        api, self.transport = graph_read.build_graph(
            ENV, sender=self.graph,
            retry=BoundedBackoff(sleep=self.sleeps.append, jitter=lambda: 0.0))
        reader = GraphMailReader(api, capture_audience())
        kwargs.setdefault("sleep", self.sleeps.append)
        kwargs.setdefault("page_size", 2)
        return MailCapture(self.store, reader, CAPTURE, **kwargs)

    def stored(self, box: str | None = None) -> list[tuple[str, str]]:
        """(message id, folder id) of every current version, in the order stored."""
        return [(r["message_id"], r["folder_id"]) for r in self.store.db.execute(
            "SELECT message_id, folder_id FROM messages WHERE superseded_at IS NULL "
            "AND (? IS NULL OR mailbox_id=?) ORDER BY version_id", (box, box))]

    def requested(self, fragment: str) -> list[str]:
        return [url for _m, url, _h in self.graph.calls
                if fragment in unquote(url.replace("+", " "))]


class EveryFolderAndHistoryTests(ThroughputCase):
    def test_every_folder_and_the_whole_history_are_captured_newest_first(self):
        self.graph.put(BOX_A, "IN-NEW", "CK-1")
        self.graph.put(BOX_A, "IN-OLD-1", "CK-1", received="2026-02-01T09:00:00Z")
        self.graph.put(BOX_A, "IN-OLD-2", "CK-1", received="2026-05-01T09:00:00Z")
        self.graph.put(BOX_A, "IN-OLD-3", "CK-1", received="2025-11-01T09:00:00Z")
        self.graph.put(BOX_A, "CU-NEW", "CK-1", folder=CUSTOM)
        self.graph.put(BOX_A, "CH-OLD", "CK-1", folder=CHILD, received=OLD)
        # An excluded person's mail, in the window and in the history: never stored.
        self.graph.put(BOX_A, "X-NEW", "CK-1", folder=CUSTOM, sender=EXCLUDED_MAIL)
        self.graph.put(BOX_A, "X-OLD", "CK-1", sender=EXCLUDED_MAIL, received=OLD)
        self.graph.put(BOX_B, "B-OLD", "CK-1", folder=CHILD, received=OLD)
        run = self.capture().run(every_folder_scope(history="all"))
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        stored = self.stored(BOX_A)
        self.assertEqual(sorted(m for m, _f in stored),
                         ["CH-OLD", "CU-NEW", "IN-NEW", "IN-OLD-1", "IN-OLD-2", "IN-OLD-3"])
        self.assertEqual(self.stored(BOX_B), [("B-OLD", CHILD)])
        self.assertEqual(dict(stored)["CH-OLD"], CHILD)          # the child folder's own id
        # The window first, then the history newest first.
        inbox = [m for m, f in stored if f == "AAMkFOLDER-A="]
        self.assertEqual(inbox, ["IN-NEW", "IN-OLD-2", "IN-OLD-1", "IN-OLD-3"])
        dropped = sum(f["counts"]["people_dropped"] for m in run["mailboxes"]
                      for f in m["folders"])
        self.assertEqual(dropped, 2)
        self.assertFalse(self.store.db.execute(
            "SELECT 1 FROM messages WHERE message_id LIKE 'X-%'").fetchall())
        # The empty folder is never read; the mailbox's folders are each one unit.
        self.assertFalse([u for u in self.requested("/messages") if EMPTY in u])
        self.assertEqual(len(run["mailboxes"][0]["folders"]), 3)
        # The next run reads only deltas and no history again; new mail arrives.
        self.graph.put(BOX_A, "CU-NEXT", "CK-1", folder=CUSTOM)
        before = len(self.requested("$orderby=receivedDateTime desc"))
        again = self.capture().run(every_folder_scope(history="all"))
        self.assertTrue(again["complete"], again["incomplete_reasons"])
        self.assertIn(("CU-NEXT", CUSTOM), self.stored(BOX_A))
        self.assertEqual(len(self.requested("$orderby=receivedDateTime desc")), before)
        self.assertEqual(len(self.stored()), 8)

    def test_a_history_cut_by_the_budget_resumes_from_its_cursor(self):
        for n in range(1, 8):
            self.graph.put(BOX_A, f"OLD-{n}", "CK-1", received=f"2026-0{n}-01T09:00:00Z"
                           if n < 4 else f"2025-0{n}-01T09:00:00Z")
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=SYNTHETIC,
                             history="all")
        first = self.capture(page_budget=2).run(scope)
        folder = first["mailboxes"][0]["folders"][0]
        self.assertEqual((folder["complete"], folder["reason"]), (False, "page_budget"))
        held = [m for m, _f in self.stored()]
        self.assertEqual(held, ["OLD-3", "OLD-2"])    # one delta page, one history page
        done = self.capture().run(scope)
        self.assertTrue(done["complete"], done["incomplete_reasons"])
        self.assertEqual([m for m, _f in self.stored()],
                         ["OLD-3", "OLD-2", "OLD-1", "OLD-7", "OLD-6", "OLD-5", "OLD-4"])

    def test_a_failed_folder_listing_keeps_the_mailbox_incomplete(self):
        self.graph.put(BOX_A, "IN-1", "CK-1")
        self.graph.fail_get[r"/v1\.0/users/[^/]+/mailFolders"] = 99
        run = self.capture(throttle_waits=()).run(every_folder_scope())
        self.assertFalse(run["complete"])
        self.assertIn("folder_listing_incomplete", run["incomplete_reasons"])
        self.assertEqual(self.stored(BOX_A), [("IN-1", "AAMkFOLDER-A=")])   # inbox still

    def test_the_shipped_scope_is_every_folder_and_the_whole_history(self):
        shipped = CaptureScope.from_file(capture_mail.DEFAULT_SCOPE)
        self.assertEqual((shipped.all_mailboxes, shipped.all_mailbox_folders,
                          shipped.history), (True, ("all",), "all"))
        with self.assertRaises(capture_mail.CaptureError):
            CaptureScope([{"address": UPN_A, "folders": "every"}], allowlist=SYNTHETIC)
        with self.assertRaises(capture_mail.CaptureError):
            CaptureScope([{"address": UPN_A}], allowlist=SYNTHETIC, history="some")


class TurnsAndConcurrencyTests(ThroughputCase):
    def test_turns_give_every_mailbox_its_newest_pages_before_a_second_turn(self):
        for n in range(1, 9):
            self.graph.put(BOX_A, f"A-{n}", "CK-1")
        self.graph.put(BOX_B, "B-1", "CK-1")
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]},
                              {"address": UPN_B, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=SYNTHETIC)
        run = self.capture(pages_per_turn=1).run(scope)
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        boxes = [BOX_A if BOX_A in u else BOX_B for u in self.requested("/messages/delta")]
        self.assertEqual(boxes[:3], [BOX_A, BOX_B, BOX_A])   # B's page before A's second
        self.assertEqual(len(self.stored()), 9)
        self.assertEqual(run["mailboxes"][0]["folders"][0]["counts"]["versions_new"], 8)

    def test_a_budget_spent_mid_turns_cuts_resumably_and_loses_nothing(self):
        for n in range(1, 9):
            self.graph.put(BOX_A, f"A-{n}", "CK-1")
        self.graph.put(BOX_B, "B-1", "CK-1")
        self.graph.put(BOX_B, "B-2", "CK-1")
        self.graph.put(BOX_B, "B-3", "CK-1")
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]},
                              {"address": UPN_B, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=SYNTHETIC)
        # The clock: one second per delta page read, so the budget ends after three.
        cut = self.capture(pages_per_turn=1, budget_seconds=3,
                           monotonic=lambda: float(len(self.graph.delta_calls()))).run(scope)
        self.assertFalse(cut["complete"])
        self.assertEqual({f["reason"] for m in cut["mailboxes"] for f in m["folders"]},
                         {capture_mail.TIME_BUDGET})
        self.assertTrue(any(m.startswith("A-") for m, _f in self.stored()))
        self.assertTrue(any(m.startswith("B-") for m, _f in self.stored()))
        done = self.capture(pages_per_turn=1).run(scope)
        self.assertTrue(done["complete"], done["incomplete_reasons"])
        ids = [m for m, _f in self.stored()]
        self.assertEqual(sorted(ids), sorted({*ids}))   # nothing stored twice
        self.assertEqual(len(ids), 11)

    def test_concurrent_reads_store_every_message_once_in_page_order(self):
        for n in (1, 10, 60):
            with self.subTest(messages=n):
                self.graph = FolderGraph()
                self.graph.slow = 0.01
                self.store.db.execute("DELETE FROM attachments")
                self.store.db.execute("DELETE FROM messages")
                self.store.db.execute("DELETE FROM folders")
                self.store.db.commit()
                for m in range(n):
                    self.graph.put(BOX_A, f"M-{m:03d}", "CK-1",
                                   attachments=[file_attachment(f"A{m}")] if m % 3 == 0
                                   else None)
                started = time.monotonic()
                run = self.capture(workers=4, page_size=7, pages_per_turn=1).run(
                    CaptureScope([{"address": UPN_A, "folders": ["inbox"]}],
                                 allowlist=SYNTHETIC, internal_domains=["example-plx.com"]))
                elapsed = time.monotonic() - started
                self.assertTrue(run["complete"], run["incomplete_reasons"])
                ids = [m for m, _f in self.stored()]
                self.assertEqual(ids, [f"M-{m:03d}" for m in range(n)])   # page order
                attachments = self.store.db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE content_status='captured'"
                ).fetchone()[0]
                self.assertEqual(attachments, len(range(0, n, 3)))
                if n >= 10:
                    self.assertGreater(self.graph.max_inflight, 1)
                self.assertLessEqual(self.graph.max_inflight, 4)
                # Each message read once: no read repeated by the concurrent path (a
                # wall-clock bound is not kept: the shared test machine's load varies).
                reads = [u for _m, u, _h in self.graph.calls
                         if re.fullmatch(r"/v1\.0/users/[^/]+/messages/[^/]+",
                                         unquote(urlsplit(u).path))]
                self.assertEqual(len(reads), n)
                del elapsed

    def test_a_throttled_read_waits_retry_after_and_loses_nothing(self):
        for m in range(12):
            self.graph.put(BOX_A, f"M-{m:02d}", "CK-1")
        self.graph.throttle_message_gets = 3
        run = self.capture(workers=4, page_size=6).run(
            CaptureScope([{"address": UPN_A, "folders": ["inbox"]}], allowlist=SYNTHETIC,
                         internal_domains=["example-plx.com"]))
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(len(self.stored()), 12)
        self.assertTrue(self.sleeps)                    # Retry-After was waited

    def test_a_retry_after_holds_the_other_threads_sharing_the_backoff(self):
        slept: list[float] = []
        backoff = BoundedBackoff(sleep=slept.append, jitter=lambda: 0.0)
        told = threading.Thread(target=backoff.pause, args=(0, {"Retry-After": "3"}))
        told.start()
        told.join()
        waited = backoff.hold()                  # another thread: it waits too
        self.assertGreater(waited, 2.0)
        self.assertLessEqual(waited, 4.0)
        backoff.pause(0, {"Retry-After": "3"})   # this thread waited itself: no second wait
        self.assertEqual(backoff.hold(), 0.0)
        self.assertEqual(BoundedBackoff(sleep=slept.append).hold(), 0.0)


def _fake_text(content, record, *, max_chars=None, defer_ocr=False):
    if defer_ocr:
        return "ocr_pending", "text layer only"
    return "extracted_ocr", "the scanned pages read"


class OcrBesideTheCaptureTests(ThroughputCase):
    def setUp(self) -> None:
        super().setUp()
        for target in ("capture_text", "_ocr_engine"):
            patcher = mock.patch.object(capture_mail, target,
                                        _fake_text if target == "capture_text"
                                        else (lambda: True))
            patcher.start()
            self.addCleanup(patcher.stop)
        local = mock.patch("integrations.document_understanding.local_ocr_available",
                           lambda: True)
        local.start()
        self.addCleanup(local.stop)
        self.scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]}],
                                  allowlist=SYNTHETIC, internal_domains=["example-plx.com"])

    def texts(self) -> list[tuple[str, int, str]]:
        return [tuple(r) for r in self.store.db.execute(
            "SELECT a.attachment_id, v.version, v.text_status FROM attachment_text_versions v "
            "JOIN attachments a ON a.l1_handle=v.l1_handle ORDER BY 1, 2")]

    def test_a_scan_is_stored_at_once_and_ocr_read_beside_the_capture(self):
        self.graph.put(BOX_A, "M-1", "CK-1", attachments=[file_attachment("SCAN")])
        self.graph.put(BOX_A, "M-2", "CK-1")
        run = self.capture(defer_ocr=True, ocr_workers=2).run(self.scope)
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(run["ocr"]["read"], 1)
        self.assertEqual(self.texts(), [("SCAN", 1, "ocr_pending"), ("SCAN", 2, "extracted_ocr")])
        self.assertEqual(self.store.db.execute(
            "SELECT text_status FROM attachments").fetchone()[0], "extracted_ocr")

    def test_a_reading_the_budget_left_pending_is_read_first_by_the_next_run(self):
        ticks = {"now": 0.0}

        def deferred(content, record, *, max_chars=None, defer_ocr=False):
            ticks["now"] = 1_000.0   # the budget ends as the scan is stored
            return _fake_text(content, record, defer_ocr=defer_ocr)
        self.graph.put(BOX_A, "M-1", "CK-1", attachments=[file_attachment("SCAN")])
        with mock.patch.object(capture_mail, "capture_text", deferred):
            cut = self.capture(defer_ocr=True, ocr_workers=1, budget_seconds=10,
                               monotonic=lambda: ticks["now"]).run(self.scope)
        self.assertEqual(cut["ocr"]["read"], 0)
        self.assertEqual(self.store.db.execute(
            "SELECT text_status FROM attachments").fetchone()[0], "ocr_pending")
        later = self.capture(defer_ocr=True, ocr_workers=1).run(self.scope)
        self.assertEqual(later["ocr"]["read"], 1)
        self.assertEqual(self.texts(), [("SCAN", 1, "ocr_pending"), ("SCAN", 2, "extracted_ocr")])


if __name__ == "__main__":
    unittest.main()
