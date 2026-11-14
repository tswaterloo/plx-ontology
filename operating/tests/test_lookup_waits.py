"""A document question's waits on shared background reads are bounded
(operating/case_tools_host.py): the attachment-mailbox list's background read is waited
for at most ``MAILBOXES_WAIT_SECONDS`` (the list another host kept is taken at once), and
the SharePoint lookup pool has a worker per core, and the SharePoint lookup file is shared
and brought up by one process at a time (GA-F10). Evidence label: ``isolated-fixture``
(made-up mailboxes and files, a stand-in capture path)."""

from __future__ import annotations

import json
import os
import sqlite3
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


class _FakeStore:
    """A capture store's read side as the shared lookup reads it: current versions in an
    ``items`` table (each file's entry is made by a stand-in ``_file_entry``), every
    version listed to the reader."""

    def __init__(self, root: Path):
        import sqlite3
        self.root, self.blobs = root, root / "blobs"
        self.path = root / "capture.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE items (version_id INTEGER PRIMARY KEY, l1_handle TEXT, "
                        "path TEXT, record TEXT, content_status TEXT, content_sha256 TEXT, "
                        "text_status TEXT, text_sha256 TEXT, recorded_at TEXT, "
                        "superseded_at TEXT)")
        self.db.commit()

    def add(self, handle: str, name: str) -> None:
        import sqlite3
        writer = sqlite3.connect(self.path)   # the capture's own connection
        writer.execute("INSERT INTO items (l1_handle, path, record, content_status, "
                        "content_sha256, text_status, text_sha256, recorded_at) VALUES "
                        "(?, '/lots', ?, 'captured', ?, 'extracted', ?, ?)",
                        (handle, json.dumps({"name": name}), "c" + handle, "t" + handle,
                         "r" + handle))
        writer.commit()
        writer.close()

    def index(self, gated, audience, limit=None):
        rows = self.db.execute("SELECT l1_handle, content_sha256, recorded_at FROM items "
                               "WHERE superseded_at IS NULL ORDER BY version_id").fetchall()
        items = [{"l1_handle": r[0], "content_sha256": r[1], "recorded_at": r[2]}
                 for r in rows]
        return {"items": items, "truncated": False, "state": len(items)}

    def content_stamp(self):
        return tuple(self.db.execute("SELECT MAX(version_id), COUNT(*) FROM items").fetchone())


def _entry(blobs, handle, row):
    """Stand-in for the stored-text reading: the file names the PO in its handle (a
    capture other tests of the process left warming is not this fixture's)."""
    if not handle.startswith("sharepoint://") or "/" in handle[len("sharepoint://"):]:
        return _REAL_ENTRY(blobs, handle, row)
    _ENTRIES_MADE.append(handle)
    started = _ENTRY_GATE.get("started")
    if started is not None:
        started.set()
        _ENTRY_GATE["release"].wait(10)
    return {"named": [["petra_po", handle.rsplit("/", 1)[-1].upper(), None]], "why": None,
            "skipped": None}


_ENTRIES_MADE: list[str] = []
_REAL_ENTRY = host._file_entry
_ENTRY_GATE: dict[str, threading.Event] = {}


class SharedLookupTests(unittest.TestCase):
    """GA-F10: one lookup file per capture store, shared by every process and brought up
    by one at a time; a lookup made while another brings it up serves the last committed
    state, names the files added since not searched, and is never kept."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = _FakeStore(Path(temp.name))
        self.addCleanup(self.store.db.close)
        host._lookup_code()   # digested from the real sources before the stand-in
        _ENTRIES_MADE.clear()
        _ENTRY_GATE.clear()
        for patched in (mock.patch.object(host, "_file_entry", _entry),
                        mock.patch.dict(host._LOOKUPS, clear=True)):
            patched.start()
            self.addCleanup(patched.stop)

    def lookup(self, me):
        return host.CaptureCaseHostTools._identifier_lookup(me, self.store, "r", "aud")

    def test_a_lookup_made_while_another_brings_the_file_up_is_not_kept(self):
        self.store.add("sharepoint://old", "old.txt")
        me = SimpleNamespace(_lookup=None)
        first = self.lookup(me)
        self.assertIs(me._lookup, first)
        self.assertEqual(first.found({("petra_po", "OLD")}), {0: {None}})
        self.assertEqual(first.unsearched, [])
        self.store.add("sharepoint://new", "new.txt")
        shared = host._shared_lookup(self.store)
        held, release = threading.Event(), threading.Event()

        def bring_up():   # another process holding the file's lock
            with shared.locked(wait=True):
                held.set()
                release.wait(10)
        thread = threading.Thread(target=bring_up)
        thread.start()
        held.wait(10)
        try:
            began = time.monotonic()
            stale = self.lookup(me)
            self.assertLess(time.monotonic() - began, 5.0)   # never waits for it
        finally:
            release.set()
            thread.join(10)
        self.assertEqual([(stale.handles[at], why) for at, why in stale.unsearched],
                         [("sharepoint://new", host.STALE_INDEX)])
        self.assertEqual(stale.found({("petra_po", "NEW"), ("petra_po", "OLD")}),
                         {0: {None}})   # the new file's text is not matched yet
        self.assertIs(me._lookup, first)   # not kept under the new state
        # Brought up now: the next question searches the new file and keeps its lookup.
        again = self.lookup(me)
        self.assertEqual(again.unsearched, [])
        self.assertEqual(again.found({("petra_po", "NEW")}), {1: {None}})
        self.assertIs(me._lookup, again)
        self.assertEqual(sorted(_ENTRIES_MADE), ["sharepoint://new", "sharepoint://old"])

    def test_a_file_never_brought_up_is_waited_for_and_made_once(self):
        # Two processes start together: one reads the stored text, the other waits for
        # its lock and then finds the file current (no file's text is read twice).
        for number in range(10):
            self.store.add(f"sharepoint://po{number}", f"po{number}.txt")
        _ENTRY_GATE.update(started=threading.Event(), release=threading.Event())
        first_host = host._shared_lookup(self.store)
        warm = threading.Thread(target=first_host.sync, args=(self.store,),
                                kwargs={"wait": True})
        warm.start()
        _ENTRY_GATE["started"].wait(10)
        host._LOOKUPS.clear()   # a second process: its own objects, the same file
        answer: list = []
        asked = threading.Thread(target=lambda: answer.append(
            self.lookup(SimpleNamespace(_lookup=None))))
        asked.start()
        asked.join(0.5)
        self.assertTrue(asked.is_alive())   # waiting for the first one's bring-up
        _ENTRY_GATE["release"].set()
        warm.join(10)
        asked.join(10)
        lookup = answer[0]
        self.assertIsNot(host._shared_lookup(self.store), first_host)
        self.assertEqual(lookup.unsearched, [])
        self.assertEqual(lookup.found({("petra_po", "PO7")}), {7: {None}})
        self.assertEqual(sorted(_ENTRIES_MADE), sorted(f"sharepoint://po{n}" for n in range(10)))

    def test_a_first_build_is_waited_for_a_bounded_time_then_nothing_is_searched_yet(self):
        for number in range(3):
            self.store.add(f"sharepoint://po{number}", f"po{number}.txt")
        shared = host._shared_lookup(self.store)
        held, release = threading.Event(), threading.Event()

        def stuck():   # another process's first build that does not finish
            with shared.locked(wait=True):
                held.set()
                release.wait(10)
        thread = threading.Thread(target=stuck)
        thread.start()
        held.wait(10)
        me = SimpleNamespace(_lookup=None)
        try:
            with mock.patch.object(host, "FIRST_BUILD_WAIT_SECONDS", 0.5):
                began = time.monotonic()
                lookup = self.lookup(me)
                self.assertLess(time.monotonic() - began, 5.0)
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(lookup.unsearched, [(n, host.STALE_INDEX) for n in range(3)])
        self.assertEqual(lookup.found({("petra_po", "PO1")}), {})
        self.assertIsNone(me._lookup)
        self.assertEqual(_ENTRIES_MADE, [])

    def test_a_failed_bring_up_keeps_the_last_build_and_a_remade_file_drops_held_lookups(self):
        self.store.add("sharepoint://po1", "po1.txt")
        me = SimpleNamespace(_lookup=None)
        first = self.lookup(me)
        shared = host._shared_lookup(self.store)
        generation = shared.generation()
        self.assertIsNotNone(generation)
        self.store.add("sharepoint://po2", "po2.txt")
        # A bring-up failing midway (I/O) leaves the file as the last one made: nothing
        # of the new state is half-written and the file is not made anew.
        with mock.patch.object(host, "_sync_entries",
                               side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertRaises(sqlite3.OperationalError):
            shared.sync(self.store, wait=True)
        self.assertEqual(shared.generation(), generation)
        conn = shared.reader()
        self.assertEqual([r[0] for r in conn.execute("SELECT handle FROM files")],
                         ["sharepoint://po1"])
        self.assertEqual(first.found({("petra_po", "PO1")}), {0: {None}})
        # A file made anew (another store, another code's file) has a new generation: a
        # lookup held from the old one is never reused, even with no commit to the store.
        again = self.lookup(me)
        self.assertIs(me._lookup, again)
        writer = sqlite3.connect(shared.path)
        with writer:
            writer.execute("UPDATE meta SET value = 'other' WHERE key = 'code'")
        writer.close()
        shared.sync(self.store, wait=True)   # another process makes it anew
        remade = self.lookup(me)
        self.assertIsNot(remade, again)
        self.assertNotEqual(shared.generation(), generation)
        self.assertEqual(remade.found({("petra_po", "PO2")}), {1: {None}})

    def test_names_and_identifiers_are_only_those_of_the_listed_files(self):
        # The file is shared by every reader: a reader's lookup answers only for the files
        # the store lists for it.
        for handle, name in (("sharepoint://a1", "LOT240306 spec.pdf"),
                             ("sharepoint://b2", "LOT240306 specification.pdf"),
                             ("sharepoint://c3", "LOT1240306.pdf")):
            self.store.add(handle, name)
        listed = self.store.index
        self.store.index = lambda gated, audience, limit=None: dict(
            listed(gated, audience), items=[i for i in listed(gated, audience)["items"]
                                            if i["l1_handle"] != "sharepoint://b2"])
        lookup = self.lookup(SimpleNamespace(_lookup=None))
        self.assertEqual([row[0] for row in lookup.named(["240306", "spec"])],
                         ["sharepoint://a1"])
        self.assertEqual([row[0] for row in lookup.named(["1240306"])], ["sharepoint://c3"])
        self.assertEqual(lookup.found({("petra_po", "B2"), ("petra_po", "C3")}), {1: {None}})


if __name__ == "__main__":
    unittest.main()
