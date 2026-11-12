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

from collections import Counter
import os
from pathlib import Path
import re
import subprocess
import sys
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

    def test_a_backfill_retries_a_failed_attachment_once_per_run_never_at_every_turn(self):
        """Live 2026-11-03: every turn of a history backfill reaches its delta's end, and
        each one retried the folder's degraded versions under the store lock (the same
        100 versions every turn). A run retries a folder's once; the next run again."""
        self.graph.put(BOX_A, "NEW", "CK-1", attachments=[file_attachment("BAD")])
        for n in range(1, 7):
            self.graph.put(BOX_A, f"OLD-{n}", "CK-1", received=f"2025-0{n}-01T09:00:00Z")
        failing = r"/v1\.0/users/[^/]+/messages/NEW/attachments/BAD"
        self.graph.fail_get[failing] = 999
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=SYNTHETIC,
                             history="all")
        run = self.capture(pages_per_turn=1, page_size=1, throttle_waits=()).run(scope)
        tries = 999 - self.graph.fail_get[failing]
        self.assertEqual(len(self.stored()), 7)
        self.assertEqual(run["mailboxes"][0]["folders"][0]["counts"]["backfill_pages"], 6)
        self.assertEqual(self.store.db.execute(
            "SELECT content_status FROM attachments").fetchone()[0], "unavailable")
        # Read with its message, then retried once by the run (six turns; it was seven).
        self.assertEqual(tries, 2)
        self.capture(pages_per_turn=1, page_size=1, throttle_waits=()).run(scope)
        self.assertEqual(999 - self.graph.fail_get[failing], 3)   # the next run retries it

    def test_a_repair_that_recovers_goes_on_at_the_folders_next_turn(self):
        for n in (1, 2):
            self.graph.put(BOX_A, f"NEW-{n}", "CK-1", attachments=[file_attachment(f"BAD{n}")])
            self.graph.fail_get[rf"/v1\.0/users/[^/]+/messages/NEW-{n}/attachments/BAD{n}"] = 1
        for n in range(1, 7):
            self.graph.put(BOX_A, f"OLD-{n}", "CK-1", received=f"2025-0{n}-01T09:00:00Z")
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=SYNTHETIC,
                             history="all")
        with mock.patch.object(capture_mail, "REPAIRS_PER_ROUND", 1):
            run = self.capture(pages_per_turn=1, page_size=1, throttle_waits=()).run(scope)
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(run["mailboxes"][0]["folders"][0]["counts"]["attachments_recovered"],
                         2)
        self.assertEqual({r[0] for r in self.store.db.execute(
            "SELECT content_status FROM attachments")}, {"captured"})

    def test_a_failed_folder_listing_keeps_the_mailbox_incomplete(self):
        self.graph.put(BOX_A, "IN-1", "CK-1")
        self.graph.fail_get[r"/v1\.0/users/[^/]+/mailFolders"] = 99
        run = self.capture(throttle_waits=()).run(every_folder_scope())
        self.assertFalse(run["complete"])
        self.assertIn("folder_listing_incomplete", run["incomplete_reasons"])
        self.assertEqual(self.stored(BOX_A), [("IN-1", "AAMkFOLDER-A=")])   # inbox still

    def test_a_partial_folder_listing_is_listed_again_and_never_held_whole(self):
        # CAPTURE-006: a listing that missed folders is stored marked partial: the next
        # run (well inside FOLDER_RELIST_SECONDS) lists again and the mailbox stays not
        # complete until a whole listing comes back
        self.graph.put(BOX_A, "IN-1", "CK-1")
        real = GraphMailReader.list_folders
        calls = []

        def partial(reader, actor, mailbox, **kwargs):
            calls.append(mailbox)
            found = real(reader, actor, mailbox, **kwargs)
            return dict(found, complete=False)
        with mock.patch.object(GraphMailReader, "list_folders", partial):
            for _ in range(2):
                run = self.capture(throttle_waits=()).run(every_folder_scope())
                self.assertFalse(run["complete"])
                self.assertIn("folder_listing_incomplete", run["incomplete_reasons"])
        # each of the two mailboxes listed by both runs, not read from the store
        self.assertEqual(sorted(Counter(calls).values()), [2, 2])
        self.assertFalse(self.store.folder_listing_whole(BOX_A))
        run = self.capture(throttle_waits=()).run(every_folder_scope())
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertTrue(self.store.folder_listing_whole(BOX_A))
        self.assertEqual(self.stored(BOX_A), [("IN-1", "AAMkFOLDER-A=")])

    def test_a_user_without_an_exchange_mailbox_reads_as_no_mailbox_never_a_gap(self):
        """Graph 404 ``MailboxNotEnabledForRESTAPI``: no mail exists, so the run is not
        held incomplete by it, and every reader of the store says ``no_mailbox``."""
        self.graph.put(BOX_A, "IN-1", "CK-1")
        graph = self.graph
        plain = graph.__call__

        def no_mailbox(method, url, headers, body, timeout, max_bytes):
            if f"/users/{BOX_B}/mailFolders" in unquote(urlsplit(url).path):
                graph.calls.append((method, url, dict(headers)))
                return j(404, {"error": {"code": "MailboxNotEnabledForRESTAPI"}})
            return plain(method, url, headers, body, timeout, max_bytes)
        self.graph = no_mailbox
        run = self.capture().run(every_folder_scope())
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(run["mailboxes_absent"], 1)
        statuses = {m.get("mailbox_id"): m["status"] for m in run["mailboxes"]}
        self.assertEqual(statuses[BOX_B], "no_mailbox")
        self.assertNotIn("not_visible", run["incomplete_reasons"])
        self.assertEqual(self.stored(BOX_A), [("IN-1", "AAMkFOLDER-A=")])
        audience = capture_audience()
        coverage = self.store.mailbox_coverage(BOX_B, CAPTURE, audience)
        self.assertEqual((coverage["status"], coverage["reasons"]),
                         ("no_mailbox", ["no_mailbox"]))
        held = {m["mailbox"]: m for m in self.store.completeness(CAPTURE, audience)
                ["mailboxes"]}
        self.assertEqual((held[BOX_B]["status"], held[BOX_B]["complete"]),
                         ("no_mailbox", True))
        self.assertTrue(held[BOX_A]["complete"])
        # Any other 404 on a folder listing stays what it was: an unreadable mailbox.
        self.assertFalse(graph_read.hidden(graph_read.SourceNotFound("x"))
                         .__class__ is graph_read.MailboxAbsent)

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


class OcrBacklogDrainTests(OcrBesideTheCaptureTests):
    """Live 2026-11-04: four OCR threads took 2,000 scans at a run's start and no more,
    the deferred PDFs grew to 248k while most cores idled. A run reads its whole backlog
    a page at a time, each scan once, and goes on after its last folder turn."""

    def test_a_run_reads_its_whole_ocr_backlog_each_scan_once(self):
        for n in (1, 10, 60):
            with self.subTest(scans=n):
                self.store.close()
                self.store = capture_mail.MailCaptureStore(Path(self.tmp.name) / f"s{n}",
                                                           clock=self.clock)
                self.graph = FolderGraph()
                for m in range(n):
                    self.graph.put(BOX_A, f"M-{m}", "CK-1", attachments=[
                        file_attachment(f"SCAN-{m}", f"scan {m} ".encode() * 40)])
                with mock.patch.object(capture_mail, "_ocr_engine", lambda: False), \
                        mock.patch("integrations.document_understanding."
                                   "local_ocr_available", lambda: False):   # no OCR yet
                    first = self.capture(defer_ocr=True, ocr_workers=2).run(self.scope)
                self.assertTrue(first["complete"], first["incomplete_reasons"])
                read: list[str] = []
                plain = capture_mail._OcrQueue._read

                def counted(queue, digest, metadata):
                    read.append(metadata["id"])
                    return plain(queue, digest, metadata)
                with mock.patch.object(capture_mail, "OCR_BACKLOG_PER_RUN", 3), \
                        mock.patch.object(capture_mail, "OCR_REFILL_SECONDS", 0.0), \
                        mock.patch.object(capture_mail._OcrQueue, "_read", counted):
                    run = self.capture(defer_ocr=True, ocr_workers=2).run(self.scope)
                self.assertEqual(run["ocr"]["read"], n)
                self.assertEqual(sorted(read), sorted(f"SCAN-{m}" for m in range(n)))
                self.assertEqual(self.store.db.execute(
                    "SELECT text_status, COUNT(*) FROM attachments GROUP BY 1").fetchall()
                    [0][:], ("extracted_ocr", n))

    def test_ocr_readings_run_in_processes_at_a_lower_priority(self):
        queue = capture_mail._Extractors(1, 30.0, nice=capture_mail.OCR_NICE)
        try:
            pid = queue.pids()[0]
            queue.read(b"plain", {"name": "a.txt", "content_type": "text/plain"},
                       defer_ocr=True, defer_all=False)   # it has started
            own = os.getpriority(os.PRIO_PROCESS, 0)   # 19 is the lowest priority
            self.assertEqual(os.getpriority(os.PRIO_PROCESS, pid),
                             min(19, own + capture_mail.OCR_NICE))
            group = Path(f"/proc/{pid}/autogroup")
            if group.exists():   # its session's scheduling group is lowered too
                self.assertEqual(int(group.read_text().split("nice")[1]),
                                 capture_mail.OCR_NICE)
        finally:
            queue.close()
        self.assertEqual(capture_mail.ocr_workers_for(16, 61 << 30), 14)
        self.assertEqual(capture_mail.ocr_workers_for(16, 8 << 30), 4)   # memory bound
        self.assertEqual(capture_mail.ocr_workers_for(2, None), 1)
        self.assertEqual(capture_mail.ocr_workers_for(64, None), capture_mail.MAX_WORKERS)


class DeferredFolderTests(ThroughputCase):
    """Live 2026-11-04: every folder round's end counted its mailbox's whole deferred
    pile (248k) under the store lock every lane waits on. The per-folder answer comes
    from the folders known to hold one, kept by each write; it must equal the count."""

    def test_whether_a_folder_holds_a_deferred_attachment_matches_the_count(self):
        import random

        store, deferred = self.store, capture_mail.ATTACHMENT_DEFERRED
        boxes, folders = (BOX_A, BOX_B), tuple(f"F{n}" for n in range(8))
        for box in boxes:
            store.db.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                             (box, f"{box}@x", "[]", "[]", "{}", "t", "t"))
        store.db.commit()
        rng = random.Random(7)
        current: dict[tuple[str, str], tuple[str, str]] = {}   # (box, id) -> handle, folder
        versions = 0

        def add(box, mid, folder, statuses):
            nonlocal versions
            versions += 1
            handle = f"graph-mail://{box}/{mid}@CK{versions}"
            store.add_version(
                {"mailbox": box, "id": mid, "citation": handle, "change_key": f"CK{versions}",
                 "content_sha256": "d"}, folder_id=folder, body_status="captured",
                attachments=[{"id": f"A{i}", "metadata": {"id": f"A{i}"},
                              "content_status": st} for i, st in enumerate(statuses)])
            current[(box, mid)] = (handle, folder)

        def status():
            return deferred if rng.random() < 0.1 else "captured"

        def check(step):
            for box in boxes:
                for folder in (*folders, "F-NONE"):
                    self.assertEqual(store.has_deferred_attachments(box, folder),
                                     store.deferred_attachments(box, folder) > 0,
                                     (step, box, folder))
        for n in range(20):   # held before the run
            add(rng.choice(boxes), f"M{n}", rng.choice(folders), [status()])
        for loaded in (False, True):
            if loaded:
                store.load_deferred_folders()
            else:
                store.__dict__.pop("_deferred_folders", None)
            for step in range(300):
                check(step)
                op = rng.choice((0, 1, 1, 2, 3, 3, 4))
                box, mid = rng.choice(sorted(current))
                handle, folder = current[(box, mid)]
                if op == 0:   # a new message, or a new version of one
                    add(box, rng.choice((mid, f"N{step}")), rng.choice(folders),
                        [status(), status()])
                elif op == 1:   # moved, same changeKey
                    to = rng.choice(folders)
                    store.observe(handle, to)
                    current[(box, mid)] = (handle, to)
                elif op == 2:   # a deferred attachment read later
                    store.fill_attachment(handle, {"id": "A0", "metadata": {"id": "A0"},
                                                   "content_status": "unavailable"})
                elif op == 3:   # removed, then listed again elsewhere
                    store.remove(box, mid, folder, "deleted")
                    to = rng.choice(folders)
                    store.observe(handle, to)
                    current[(box, mid)] = (handle, to)
                elif rng.random() < 0.3:   # deferred again on a retry
                    store.fill_attachment(handle, {"id": "A0", "metadata": {"id": "A0"},
                                                   "content_status": deferred})
            check("end")

    def test_a_folder_round_end_never_counts_the_mailbox_deferred_pile(self):
        """The check reads one folder through its index, not the mailbox's pile."""
        store, deferred = self.store, capture_mail.ATTACHMENT_DEFERRED
        store.db.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                         (BOX_A, "a@x", "[]", "[]", "{}", "t", "t"))
        with store.db:
            for n in range(3000):
                handle = f"graph-mail://{BOX_A}/P{n}@CK"
                store.db.execute(
                    "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                    "folder_id, record, digest, body_status, recorded_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (handle, BOX_A, f"P{n}", "CK", "PILE" if n else "SMALL", "{}", "d",
                     "captured", "t", "t"))
                store.db.execute(
                    "INSERT INTO attachments (message_handle, attachment_id, mailbox_id, "
                    "metadata, content_status, recorded_at) VALUES (?,?,?,?,?,?)",
                    (handle, "A", BOX_A, "{}", deferred, "t"))
        store.load_deferred_folders()
        steps: list[int] = []
        store.db.set_progress_handler(lambda: steps.append(1), 1000)
        try:
            self.assertTrue(store.has_deferred_attachments(BOX_A, "SMALL"))
            self.assertFalse(store.has_deferred_attachments(BOX_A, "EMPTY"))
        finally:
            store.db.set_progress_handler(None, 0)
        before = len(steps)
        store.db.set_progress_handler(lambda: steps.append(1), 1000)
        store.deferred_attachments(BOX_A, "SMALL")   # the count: the whole pile
        store.db.set_progress_handler(None, 0)
        self.assertLess(before * 20, len(steps) - before, (before, len(steps)))


def lane_scope(upns, **kwargs) -> CaptureScope:
    return CaptureScope([{"address": u, "folders": "all"} for u in upns], window_days=90,
                        internal_domains=["example-plx.com"],
                        allowlist=capture_mail.MailScope(upns), **kwargs)


class Killed(BaseException):
    """A capture process ended from outside mid-run (no handler runs)."""


class LanesAndPriorityTests(ThroughputCase):
    """Mailboxes captured at once by lanes (one writer to the store), a Retry-After that
    holds only its own mailbox, text read in extraction processes, and The priority mailboxes' recent mail, then the others', then older history, then
    notification-heavy folders."""

    def add_mailbox(self, n: int) -> tuple[str, str]:
        box, upn = f"12345678-aaaa-bbbb-cccc-{n:012d}", f"box{n}@example-plx.com"
        self.graph.users[box] = {"id": box, "userPrincipalName": upn, "mail": upn,
                                 "proxyAddresses": [f"SMTP:{upn}"]}
        inbox = f"AAMkIN-{n}="
        self.graph.folders[box] = {"inbox": inbox, inbox.lower(): inbox,
                                   **{f.lower(): f for f in (CUSTOM, CHILD, EMPTY)}}
        self.graph.messages[box] = {}
        return box, upn

    def lane_capture(self, upns, **kwargs) -> MailCapture:
        api, self.transport = graph_read.build_graph(
            ENV, sender=self.graph,
            retry=BoundedBackoff(sleep=self.sleeps.append, jitter=lambda: 0.0))
        audience = capture_mail.LiveSourceAudience(
            {f"mail:{u}": ["svc:mail-capture"] for u in upns}, tenant="plx",
            classifications={"mail:*": "restricted"})
        kwargs.setdefault("sleep", self.sleeps.append)
        kwargs.setdefault("page_size", 2)
        return MailCapture(self.store, GraphMailReader(api, audience), CAPTURE, **kwargs)

    def test_lanes_capture_mailboxes_at_once_through_one_writer_losing_nothing(self):
        writing = {"now": 0, "most": 0}
        per_box: dict[str, int] = {}
        most_per_box: dict[str, int] = {}
        plain = MailCapture._commit_message_locked

        def counted(capture, *args, **kwargs):
            with self.graph.lock:
                writing["now"] += 1
                writing["most"] = max(writing["most"], writing["now"])
            try:
                return plain(capture, *args, **kwargs)
            finally:
                with self.graph.lock:
                    writing["now"] -= 1
        graph = self.graph
        read = FolderGraph.__call__

        def per_mailbox(fake, method, url, headers, body, timeout, max_bytes):
            path = unquote(urlsplit(url).path)
            box = path.split("/")[3] if re.fullmatch(r"/v1\.0/users/[^/]+/messages/[^/]+",
                                                     path) else None
            if box is not None:
                with fake.lock:
                    per_box[box] = per_box.get(box, 0) + 1
                    most_per_box[box] = max(most_per_box.get(box, 0), per_box[box])
            try:
                return read(fake, method, url, headers, body, timeout, max_bytes)
            finally:
                if box is not None:
                    with fake.lock:
                        per_box[box] -= 1
        for size in (1, 10, 60):
            with self.subTest(mailboxes=size), mock.patch.object(
                    MailCapture, "_commit_message_locked", counted), mock.patch.object(
                    FolderGraph, "__call__", per_mailbox):
                self.store.close()
                self.store = capture_mail.MailCaptureStore(self.state / f"n{size}",
                                                           clock=self.clock)
                self.graph = FolderGraph()
                self.graph.slow = 0.002
                upns, expected = [], set()
                for n in range(size):
                    box, upn = self.add_mailbox(n)
                    upns.append(upn)
                    for m in range(5):
                        self.graph.put(box, f"M-{n}-{m}", "CK-1",
                                       folder=CUSTOM if m % 2 else "inbox",
                                       received=OLD if m == 4 else None)
                        expected.add(f"M-{n}-{m}")
                run = self.lane_capture(upns, lanes=4, workers=2, pages_per_turn=1).run(
                    lane_scope(upns, history="all"))
                self.assertTrue(run["complete"], run["incomplete_reasons"])
                held = [m for m, _f in self.stored()]
                self.assertEqual(sorted(held), sorted(expected))      # none lost, none twice
                self.assertEqual(writing["most"], 1)                  # one writer at a time
                self.assertLessEqual(max(most_per_box.values()), 2)   # workers per mailbox
                if size >= 10:
                    self.assertGreater(self.graph.max_inflight, 2)    # mailboxes at once
        del graph

    def test_a_retry_after_holds_only_the_mailbox_it_was_given_for(self):
        from integrations.live_source_guard import HOLD_SCOPE
        slept: list[float] = []
        backoff = BoundedBackoff(sleep=slept.append, jitter=lambda: 0.0)
        token = HOLD_SCOPE.set("mailbox-a")
        try:
            backoff.pause(0, {"Retry-After": "3"})
        finally:
            HOLD_SCOPE.reset(token)
        waits = {}

        def other(scope):
            token = HOLD_SCOPE.set(scope)
            try:
                waits[scope] = backoff.hold()
            finally:
                HOLD_SCOPE.reset(token)
        for scope in ("mailbox-a", "mailbox-b"):
            thread = threading.Thread(target=other, args=(scope,))
            thread.start()
            thread.join()
        self.assertGreater(waits["mailbox-a"], 0)    # its own mailbox waits it out
        self.assertEqual(waits["mailbox-b"], 0.0)    # another mailbox reads on

    def test_text_read_in_extraction_processes_matches_inline_and_holds_no_credential(self):
        text = b"Order confirmation PO 4711 for 120 cases.\n"
        attachment = {"name": "note.txt", "contentType": "text/plain",
                      "content_type": "text/plain", "size": len(text)}
        inline = capture_mail.extract_attachment(text, attachment, True, False)
        secrets = {"MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t-value-x",
                   "PLX_WORKBENCH_API_KEY": "k3y-value-x", "GH_TOKEN": "t0ken-value-x",
                   "PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e9"}
        with mock.patch.dict(os.environ, secrets):
            extractors = capture_mail._Extractors(1, 60)
        try:
            self.assertEqual(extractors.read(text, attachment, defer_ocr=True,
                                             defer_all=False), inline)
            (pid,) = extractors.pids()
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        finally:
            extractors.close()
        names = {e.split(b"=", 1)[0].decode() for e in environ if e}
        self.assertEqual(inline[0], "extracted")
        self.assertLessEqual(names, set(capture_mail.EXTRACTION_ENV))   # nothing else
        self.assertFalse([v for v in secrets.values()
                          if v.encode() in b"\0".join(environ)])
        # A capture with extraction processes stores the same text as an inline one.
        self.graph.put(BOX_A, "M-1", "CK-1", attachments=[{**file_attachment(
            "T1", text, name="note.txt"), "contentType": "text/plain"}])
        run = self.capture(extract_workers=1).run(
            CaptureScope([{"address": UPN_A, "folders": ["inbox"]}], allowlist=SYNTHETIC,
                         internal_domains=["example-plx.com"]))
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(self.store.db.execute(
            "SELECT text_status FROM attachments").fetchone()[0], "extracted")

    def test_a_reading_that_hangs_ends_its_process_alone_and_the_others_are_read(self):
        attachment = {"name": "note.txt", "contentType": "text/plain",
                      "content_type": "text/plain"}
        real = capture_mail._Extractors._start
        started: list = []

        def start(extractors):
            # The first process never answers (a file its parser never finishes).
            process = (subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        start_new_session=True)
                       if not started else real(extractors))
            started.append(process)
            return process
        with mock.patch.object(capture_mail._Extractors, "_start", start):
            extractors = capture_mail._Extractors(2, 5)
        try:
            hung = started[0]
            results: dict[str, tuple] = {}

            def read(name: str, content: bytes) -> None:
                results[name] = extractors.read(content, attachment, defer_ocr=True,
                                                defer_all=False)
            with mock.patch.object(capture_mail._Extractors, "_start", start):
                threads = [threading.Thread(target=read, args=(f"r{n}", b"words %d" % n))
                           for n in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
            statuses = sorted(r[0] for r in results.values())
            # One reading met the hung process: its own gap; every other one was read.
            self.assertEqual(statuses, ["extracted"] * 3 + [capture_mail.EXTRACTION_TIMED_OUT])
            self.assertIsNotNone(hung.poll())                  # the hung process was ended
            self.assertEqual(len(started), 3)                  # and replaced
            self.assertEqual(extractors.read(b"after", attachment, defer_ocr=True,
                                             defer_all=False)[0], "extracted")
        finally:
            extractors.close()

    def test_a_reading_left_unfinished_is_a_named_gap_its_original_kept_and_retried(self):
        big = b"%PDF-1.4 " + os.urandom(300_000)
        self.graph.put(BOX_A, "M-1", "CK-1", attachments=[file_attachment("P1", big,
                                                                          name="scan.pdf")])
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]}], allowlist=SYNTHETIC,
                             internal_domains=["example-plx.com"])

        def parsed(*_args, **_kwargs):
            raise AssertionError("parsed in the capture process")
        gap = (capture_mail.EXTRACTION_TIMED_OUT, None, None)
        with mock.patch.object(MailCapture, "_extract", lambda *a, **k: gap), \
                mock.patch.object(MailCapture, "_read_again", lambda *a: gap), \
                mock.patch.object(capture_mail, "readings_of", parsed), \
                mock.patch.object(capture_mail, "capture_text", parsed), \
                mock.patch("integrations.document_understanding.local_ocr_available",
                           lambda: True):
            run = self.capture(extract_workers=1).run(scope)
            self.assertTrue(run["mailboxes"][0]["folders"][0]["complete"], run)
            row = self.store.db.execute("SELECT l1_handle, content_sha256, text_status "
                                        "FROM attachments").fetchone()
            self.assertEqual(row["text_status"], capture_mail.EXTRACTION_TIMED_OUT)
            self.assertEqual(self.store.blob(row["content_sha256"]), big)   # kept
            # Read again (a round's re-read, then later runs) until EXTRACTION_RETRIES more
            # readings were left unfinished too; then a named gap for good, still kept.
            for _ in range(capture_mail.EXTRACTION_RETRIES + 1):
                self.capture(extract_workers=1).run(scope)
        retries = self.store.db.execute(
            "SELECT COUNT(*) FROM message_events WHERE l1_handle=? AND "
            "event='text_reextracted'", (row["l1_handle"],)).fetchone()[0]
        self.assertEqual(retries, capture_mail.EXTRACTION_RETRIES)
        self.assertEqual(self.store.db.execute("SELECT text_status FROM attachments")
                         .fetchone()[0], capture_mail.EXTRACTION_TIMED_OUT)
        self.assertEqual(self.store.blob(row["content_sha256"]), big)
        self.assertEqual(self.store.text_unread(None, None, 10), [])
        self.assertEqual(self.store.settle_original(row["content_sha256"]), "kept_pending")
        self.assertEqual(self.store.blob(row["content_sha256"]), big)

    def test_a_large_reextraction_backlog_never_stalls_new_mail(self):
        """Live 2026-11-01: every lane waited on the store lock while one lane's round-end
        re-extraction scanned the whole store (text_unread at 636k messages) under it.
        A backlog of re-readable attachments is found through indexes, each round reads
        a bounded share of it, and new mail in every mailbox is captured beside it."""
        backlog = 5000
        db = self.store.db
        with db:
            db.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                       ("old-box", "old@example-plx.com", "[]", "[]", "{}", "t", "t"))
            for n in range(backlog):
                handle = f"graph-mail://old-box/OLD-{n}@CK"
                db.execute("INSERT INTO messages (l1_handle, mailbox_id, message_id, "
                           "change_key, folder_id, record, digest, body_status, recorded_at, "
                           "last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (handle, "old-box", f"OLD-{n}", "CK", "OLD-F", "{}", "d",
                            "captured", "t", "t"))
                db.execute("INSERT INTO attachments (message_handle, attachment_id, l1_handle, "
                           "mailbox_id, metadata, content_sha256, content_status, recorded_at, "
                           "text_status) VALUES (?,?,?,?,?,?,?,?,?)",
                           (handle, "A", handle + "#a", "old-box",
                            '{"content_type": "application/pdf", "name": "s.pdf"}',
                            "0" * 64, "captured", "t", "ocr_pending"))
        plans = []
        for folder in (None, "AAMkFOLDER-A="):
            query = self.store.db.execute
            seen: list[str] = []

            def explain(sql, params=(), _query=query, _seen=seen):
                if sql.lstrip().upper().startswith("SELECT"):
                    _seen.extend(r[3] for r in _query("EXPLAIN QUERY PLAN " + sql, params))
                return _query(sql, params)
            with mock.patch.object(self.store, "db", mock.Mock(execute=explain)):
                self.store.text_unread(None if folder is None else BOX_A, folder, 100)
            plans.append(seen)
        # Never a scan of the messages, attachments or events tables.
        self.assertEqual([p for plan in plans for p in plan
                          if p.startswith("SCAN") and "CONSTANT" not in p], [], plans)
        for m in range(6):
            self.graph.put(BOX_A, f"NEW-A-{m}", "CK-1")
            self.graph.put(BOX_B, f"NEW-B-{m}", "CK-1")
        pages: list[int] = []
        page = capture_mail.MailCaptureStore.text_unread_page

        def paged(store, mailbox_id, folder_id, limit, after=None):
            if mailbox_id is None:
                pages.append(limit)
            return page(store, mailbox_id, folder_id, limit, after)
        with mock.patch("integrations.document_understanding.local_ocr_available",
                        lambda: True), mock.patch.object(capture_mail, "_ocr_engine",
                                                         lambda: True), \
                mock.patch.object(capture_mail.MailCaptureStore, "text_unread_page", paged):
            run = self.capture(lanes=2, workers=2, pages_per_turn=1, ocr_workers=1,
                               defer_ocr=True).run(every_folder_scope())
        held = {m for m, _f in self.stored()}
        self.assertTrue({f"NEW-A-{m}" for m in range(6)} | {f"NEW-B-{m}" for m in range(6)}
                        <= held)
        # The backlog is taken a bounded page at a time (each under the store lock),
        # never all of it at once; each scan is queued once.
        self.assertTrue(pages)
        self.assertLessEqual(max(pages), capture_mail.OCR_BACKLOG_PER_RUN)
        self.assertLessEqual(run["ocr"].get("queued", 0), backlog)

    def test_each_stored_message_reads_the_store_through_its_own_keys(self):
        """Live 2026-11-03 (2.1M messages): the read of a new message's captured
        attachments walked the status index's every captured attachment of its mailbox
        (70-150 ms each, under the store lock every lane waits on: about 36k messages an
        hour), its OCR queueing every attachment awaiting OCR, and a large original's
        settle every text version. Each reads its own rows by key."""
        db = self.store.db
        with db:
            db.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                       (BOX_A, UPN_A, "[]", "[]", "{}", "t", "t"))
            for n in range(300):
                handle = f"graph-mail://{BOX_A}/M-{n}@CK"
                db.execute("INSERT INTO messages (l1_handle, mailbox_id, message_id, "
                           "change_key, folder_id, record, digest, body_status, recorded_at, "
                           "last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (handle, BOX_A, f"M-{n}", "CK", "F", "{}", "d", "captured", "t",
                            "t"))
                db.execute("INSERT INTO attachments (message_handle, attachment_id, l1_handle, "
                           "mailbox_id, metadata, content_sha256, content_status, recorded_at, "
                           "text_status) VALUES (?,?,?,?,?,?,?,?,?)",
                           (handle, "A", handle + "#a", BOX_A, '{"size": 1}', "0" * 64,
                            "captured", "t", "ocr_pending" if n % 2 else "extracted"))
        seen: list[str] = []
        query = db.execute

        def explain(sql, params=()):
            if sql.lstrip().upper().startswith(("SELECT", "WITH")):
                seen.extend(r[3] for r in query("EXPLAIN QUERY PLAN " + sql, params))
            return query(sql, params)
        capture = self.capture()
        capture._ocr = mock.Mock()
        with mock.patch.object(self.store, "db", mock.Mock(execute=explain)):
            held = self.store.captured_attachments(BOX_A, "M-7")
            known = self.store.known_attachment(BOX_A, "M-7", "A", {"size": 1})
            capture._queue_ocr_of(f"graph-mail://{BOX_A}/M-7@CK")
            self.store._referenced_as_text("0" * 64)
        self.assertEqual((len(held), known["attachment_id"]), (1, "A"))
        self.assertEqual(capture._ocr.submit.call_count, 1)
        self.assertEqual([p for p in seen if p.startswith("SCAN")
                          or "attachments_by_status" in p or "attachments_by_text_status" in p],
                         [], seen)

    def test_priority_mailboxes_recent_mail_first_older_history_and_bulk_folders_last(self):
        a, upn_a = self.add_mailbox(1)
        b, upn_b = self.add_mailbox(2)
        for box, tag in ((a, "A"), (b, "B")):
            for m in range(3):
                self.graph.put(box, f"{tag}-NEW-{m}", "CK-1", received=f"2026-0{7 + m}-01T09:00:00Z")
                self.graph.put(box, f"{tag}-OLD-{m}", "CK-1", received=f"2023-0{1 + m}-01T09:00:00Z")
        upns = [upn_a, upn_b]
        scope = lane_scope(upns, history="all",
                           priority={"mailboxes": [upn_b], "recent_days": 365})
        self.lane_capture(upns, lanes=1, pages_per_turn=1, page_size=1).run(scope)
        held = [m for m, _f in self.stored()]
        self.assertEqual(len(held), 12)
        # The priority mailbox first; every mailbox's last year before older mail (a
        # folder's band is taken per turn: at most its one page past the horizon).
        self.assertEqual(held[0][:1], "B")
        last_recent = max(held.index(m) for m in held if "-NEW-" in m)
        self.assertLessEqual(sum(1 for m in held[:last_recent] if "-OLD-" in m), 2)
        self.assertLess(max(held.index(m) for m in held if m.startswith("B-OLD")),
                        max(held.index(m) for m in held if m.startswith("A-OLD")))
        # Lanes read the same mail, every message once, and never the excluded person.
        self.assertFalse(self.requested(EXCLUDED))
        # A folder most of whose held mail one sender sent is read after every other.
        c, upn_c = self.add_mailbox(3)
        for m in range(6):
            self.graph.put(c, f"N-{m}", "CK-1", folder=CUSTOM, sender="alerts@vendor.example",
                           received=f"2026-0{1 + m}-01T09:00:00Z")
            self.graph.put(c, f"R-{m}", "CK-1", sender=f"person{m}@outside.example",
                           received=f"2026-0{1 + m}-02T09:00:00Z")
        only_c = lane_scope([upn_c], history="all", priority={})
        before = len(self.stored())
        with mock.patch.object(capture_mail, "BULK_SAMPLE", 2):
            first = self.lane_capture([upn_c], pages_per_turn=1, page_size=1,
                                      page_budget=4).run(only_c)   # samples both folders
            self.assertFalse(first["complete"])
            sampled = len(self.stored())
            self.lane_capture([upn_c], pages_per_turn=1, page_size=1).run(only_c)
        self.assertEqual(sampled - before, 4)
        later = [m for m, _f in self.stored()][sampled:]
        self.assertEqual(len(later), 8)
        self.assertLess(max(later.index(m) for m in later if m.startswith("R-")),
                        min(later.index(m) for m in later if m.startswith("N-")))
    def test_a_held_notification_folder_new_mail_is_read_before_other_mailboxes_backlog(self):
        # A complete notification-heavy folder's delta, once due, is read in the first band
        # with every other held folder's, never after the backlog of mailboxes not yet
        # read (2026-11-02: 30 such folders went unread for three days while the history
        # bands filled every budget). Every folder a run read, a turn cut included, is
        # stamped as read then (the source's freshness is its stalest such folder).
        from datetime import timedelta
        c, upn_c = self.add_mailbox(3)
        for m in range(6):
            self.graph.put(c, f"N-{m}", "CK-1", folder=CUSTOM, sender="alerts@vendor.example",
                           received=f"2026-0{1 + m}-01T09:00:00Z")
            self.graph.put(c, f"R-{m}", "CK-1", sender=f"person{m}@outside.example",
                           received=f"2026-0{1 + m}-02T09:00:00Z")
        with mock.patch.object(capture_mail, "BULK_SAMPLE", 2):
            first = self.lane_capture([upn_c], lanes=1, pages_per_turn=1, page_size=1).run(
                lane_scope([upn_c], history="all", priority={}))
            self.assertTrue(first["complete"], first["incomplete_reasons"])
            self.clock.now += timedelta(seconds=capture_mail.FRESH_SECONDS + 60)
            self.graph.put(c, "N-NEW", "CK-1", folder=CUSTOM, sender="alerts@vendor.example",
                           received="2026-09-30T09:00:00Z")
            d, upn_d = self.add_mailbox(4)
            e, upn_e = self.add_mailbox(5)
            for m in range(8):
                for box, tag in ((d, "D"), (e, "E")):
                    self.graph.put(box, f"{tag}-{m}", "CK-1", sender=f"p{m}@outside.example",
                                   received=f"2026-0{1 + m}-03T09:00:00Z")
            before = len(self.stored())
            started = capture_mail._stamp(self.clock.now)
            # The run is killed (the host's child timeout) at its first read of mailbox E.
            graph = self.graph

            def killed(method, url, *rest):
                if "/messages/delta" in url and e in url:
                    raise Killed()
                return graph(method, url, *rest)
            self.graph = mock.Mock(wraps=graph, side_effect=killed,
                                   delta_calls=graph.delta_calls)
            with self.assertRaises(Killed):
                self.lane_capture([upn_c, upn_d, upn_e], lanes=1, pages_per_turn=1,
                                  page_size=1).run(
                    lane_scope([upn_c, upn_d, upn_e], history="all", priority={}))
            self.graph = graph
        later = [m for m, _f in self.stored()][before:]
        self.assertIn("N-NEW", later)             # before the new mailboxes' backlog
        self.assertLess(len([m for m in later if m.startswith("D-")]), 8)   # one turn
        # Mailbox D's inbox gave up its turn and the run never came back to it: it is
        # still stamped as read by this run.
        inbox = self.store.db.execute(
            "SELECT reason, last_run_at FROM folders WHERE mailbox_id=? AND "
            "folder_id='AAMkIN-4='", (d,)).fetchone()
        self.assertEqual(inbox["reason"], capture_mail.TURN)
        self.assertGreaterEqual(inbox["last_run_at"] or "", started, dict(inbox))

    def test_a_backfilling_folder_new_mail_is_read_before_other_mailboxes_backlog(self):
        # A folder whose round reached its deltaLink while ``history`` all still reads its
        # older mail is incomplete, not complete: its delta, once due, is read in the first
        # band all the same (2026-11-02: 156 such folders' new mail went unread from 30 Oct
        # while new mailboxes' recent mail filled every budget).
        from datetime import timedelta
        c, upn_c = self.add_mailbox(3)
        self.graph.put(c, "C-RECENT", "CK-1", received="2026-09-20T09:00:00Z")
        for m in range(4):
            self.graph.put(c, f"C-OLD-{m}", "CK-1", received=f"2021-0{1 + m}-01T09:00:00Z")
        scope_c = lane_scope([upn_c], history="all", priority={})
        self.lane_capture([upn_c], lanes=1, pages_per_turn=1, page_size=1,
                          page_budget=2).run(scope_c)
        inbox = self.store.db.execute(
            "SELECT * FROM folders WHERE mailbox_id=? AND folder_id='AAMkIN-3='", (c,)
        ).fetchone()
        self.assertNotEqual(inbox["status"], "complete", dict(inbox))
        self.assertTrue(inbox["delta_url"] and inbox["backfill_next_url"], dict(inbox))
        self.assertIsNone(inbox["next_url"])
        self.clock.now += timedelta(seconds=capture_mail.FRESH_SECONDS + 60)
        self.graph.put(c, "C-NEW", "CK-1", received="2026-09-30T09:00:00Z")
        d, upn_d = self.add_mailbox(4)
        e, upn_e = self.add_mailbox(5)
        for m in range(8):
            for box, tag in ((d, "D"), (e, "E")):
                self.graph.put(box, f"{tag}-{m}", "CK-1", sender=f"p{m}@outside.example",
                               received=f"2026-09-0{1 + m}T09:00:00Z")
        before = len(self.stored())
        graph = self.graph

        def killed(method, url, *rest):
            if "/messages/delta" in url and e in url:
                raise Killed()
            return graph(method, url, *rest)
        self.graph = mock.Mock(wraps=graph, side_effect=killed, delta_calls=graph.delta_calls)
        with self.assertRaises(Killed):
            self.lane_capture([upn_c, upn_d, upn_e], lanes=1, pages_per_turn=1,
                              page_size=1).run(
                lane_scope([upn_c, upn_d, upn_e], history="all", priority={}))
        self.graph = graph
        later = [m for m, _f in self.stored()][before:]
        self.assertIn("C-NEW", later)
        # Its turn read the delta, not the rest of its backlog ahead of the new mailboxes.
        self.assertLessEqual(len([m for m in later if m.startswith("C-OLD")]), 1)


if __name__ == "__main__":
    unittest.main()
