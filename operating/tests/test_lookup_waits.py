"""A document question's waits on shared background reads are bounded
(operating/case_tools_host.py): the attachment-mailbox list's background read is waited
for at most ``MAILBOXES_WAIT_SECONDS`` (the list another host kept is taken at once), and
the SharePoint lookup pool has a worker per core. Evidence label: ``isolated-fixture``
(made-up mailboxes, a stand-in capture path)."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from operating import case_tools_host as host

FOUND = [{"mailbox_upn": "buyer@example.test"}]


class MailboxWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name) / "cache"
        cache.mkdir(mode=0o700)
        self.capture = Path(temp.name) / "capture.db"
        self.capture.write_bytes(b"")
        self.key = os.path.realpath(self.capture)
        self.store = SimpleNamespace(path=self.capture)
        self.running = threading.Event()   # a background read that never ends here
        for patched in (mock.patch.dict(os.environ, {host.MAILBOXES_CACHE_ENV: str(cache)}),
                        mock.patch.dict(host._MAILBOXES, clear=True),
                        mock.patch.dict(host._MAILBOXES_READING, {self.key: self.running}),
                        mock.patch.object(host, "MAILBOXES_WAIT_SECONDS", 0.3)):
            patched.start()
            self.addCleanup(patched.stop)

    def test_a_slow_background_read_is_waited_for_a_bounded_time(self):
        with mock.patch.object(host, "_mailbox_list", return_value=FOUND) as listed:
            began = time.monotonic()
            self.assertEqual(host._attachment_mailboxes(self.store), FOUND)
        self.assertLess(time.monotonic() - began, 3.0)
        listed.assert_called_once_with(self.key)

    def test_the_list_another_host_kept_is_taken_without_waiting(self):
        host._keep_mailboxes(self.key, FOUND)
        with mock.patch.object(host, "_mailbox_list",
                               side_effect=AssertionError("read again")):
            began = time.monotonic()
            self.assertEqual(host._attachment_mailboxes(self.store), FOUND)
        self.assertLess(time.monotonic() - began, 0.25)


class LookupPoolTests(unittest.TestCase):
    def test_the_lookup_pool_has_a_worker_per_core(self):
        self.assertGreaterEqual(host._AHEAD_POOL._max_workers,
                                max(2, min(16, os.cpu_count() or 2)))


class _StaleStore:
    path = "/nonexistent/stale-lookup.sqlite"

    def __init__(self):
        import sqlite3
        self.db = sqlite3.connect(":memory:")
        self.items = [{"l1_handle": "sharepoint://old", "content_sha256": "a",
                       "recorded_at": "t0"}]

    def index(self, gated, audience, limit=None):
        return {"items": list(self.items), "truncated": False, "state": len(self.items)}

    def content_stamp(self):
        return len(self.items)


class StaleLookupTests(unittest.TestCase):
    """A lookup built while the warm-up reads the new state serves the previous snapshot:
    files added or changed since are named not searched, and that lookup is never kept."""

    def test_a_stale_snapshot_marks_new_files_unsearched_and_is_not_kept(self):
        store = _StaleStore()
        entry = {"named": [("po_number", "PO123", None)], "why": None, "skipped": None}
        fresh = {"sharepoint://old": entry,
                 "sharepoint://new": dict(entry)}
        stale_key = (store.path, "index")
        host._SNAPSHOTS[stale_key] = (("s0",), {"sharepoint://old": entry})
        host._SNAPSHOTS[(store.path, "names")] = (("s0",), {})
        self.addCleanup(lambda: [host._SNAPSHOTS.pop(k, None)
                                 for k in (stale_key, (store.path, "names"))])
        store.items.append({"l1_handle": "sharepoint://new", "content_sha256": "b",
                            "recorded_at": "t1"})
        locks = [host._snapshot_lock(store, k) for k in ("index", "names")]
        held, release = threading.Event(), threading.Event()

        def warm():
            for lock in locks:
                lock.acquire()
            held.set()
            release.wait()
            for lock in locks:
                lock.release()
        thread = threading.Thread(target=warm)
        thread.start()
        held.wait()
        me = SimpleNamespace(_lookup=None)
        try:
            lookup = host.CaptureCaseHostTools._identifier_lookup(me, store, "r", "aud")
        finally:
            release.set()
            thread.join()
        pending = [lookup.handles[at] for at, why in lookup.unsearched
                   if why == host.STALE_INDEX]
        self.assertIn("sharepoint://new", pending)
        self.assertFalse(lookup.entries)   # no text of the new file was matched
        self.assertIsNone(me._lookup)   # not kept under the new state
        # The warm-up is done: the next question builds again, and searches the new file.
        with mock.patch.object(host, "_file_index", return_value=fresh), \
                mock.patch.object(host, "_file_names", return_value={}):
            again = host.CaptureCaseHostTools._identifier_lookup(me, store, "r", "aud")
        self.assertIsNot(again, lookup)
        self.assertFalse([1 for _at, why in again.unsearched if why == host.STALE_INDEX])
        self.assertEqual(sorted(again.handles), ["sharepoint://new", "sharepoint://old"])
        self.assertIs(me._lookup, again)


if __name__ == "__main__":
    unittest.main()
