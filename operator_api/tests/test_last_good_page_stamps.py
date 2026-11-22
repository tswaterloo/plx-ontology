"""The last-good-page tests that are about stamps and switches (the inbox digest off the
request, the change-refresh switch, the marks and own-issued stamps): split from
test_last_good_page.py, no change to a test."""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operator_api import backend as be
from operator_api.tests.test_last_good_page import page, reader, slow


class InboxDigestOffTheRequest(unittest.TestCase):
    """Every "seen" mark writes the gap inbox; its content digest reads all of it (2.7 s
    live). A request's inputs stamp never waits for it: a state not read yet is
    "pending" (equal to no digest) and is read once in the background; a seen mark still
    leaves the digest as it was."""

    def test_the_request_path_never_reads_the_inbox_and_a_seen_mark_changes_nothing(self):
        import sqlite3
        import tempfile
        from operator_api import page_parts as pp
        inbox = Path(tempfile.mkdtemp()) / "x.gap-inbox.sqlite"
        self.addCleanup(__import__("shutil").rmtree, inbox.parent, True)
        conn = sqlite3.connect(inbox)
        conn.executescript("CREATE TABLE gaps (id, kind); CREATE TABLE visits (who, at);"
                           "INSERT INTO gaps VALUES (1, 'po_late');")
        conn.commit()
        reads = []
        real = pp._inbox_digest
        gate = threading.Event()        # cleared: a digest read waits until set again
        gate.set()

        def slow(*args):
            reads.append(1)
            gate.wait(30)
            time.sleep(0.3)
            return real(*args)
        with mock.patch.object(pp, "_inbox_digest", slow):
            gate.clear()                # a request that waited for the digest takes 30 s
            started = time.monotonic()
            try:
                first = pp.inbox_state(inbox, wait=False)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(first[0], "pending")
                self.assertEqual(pp.inbox_state(inbox, wait=False)[0], "pending")  # one read
            finally:
                gate.set()
            digest = pp.inbox_state(inbox)          # a page key waits for that one read
            self.assertEqual((digest[0], len(reads)), ("content", 1))
            self.assertEqual(pp.inbox_state(inbox, wait=False), digest)
            conn.execute("INSERT INTO visits VALUES ('a', 1)")      # a "seen" mark
            conn.commit()
            self.assertEqual(pp.inbox_state(inbox, wait=False)[0], "pending")
            self.assertEqual(pp.inbox_state(inbox), digest)        # the same findings
            conn.execute("INSERT INTO gaps VALUES (2, 'po_late')")   # a new finding
            conn.commit()
            self.assertNotEqual(pp.inbox_state(inbox), digest)
            # the backend's inputs stamp, as a request reads it, does not wait either
            store = inbox.parent / "x"
            store.write_bytes(b"")
            conn.execute("INSERT INTO gaps VALUES (3, 'po_late')")
            conn.commit()
            backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
            backend._lock, backend._inputs = threading.RLock(), None
            backend.store_path, backend.pg_dsn = store, None
            from operating import (ai_watcher, delta_overlay, ontology_host,  # noqa: F401
                                   proactive_engine)
            gate.clear()                # a stamp that waited for the digest takes 30 s
            started = time.monotonic()
            try:
                stamp = backend.inputs_stamp()
            finally:
                gate.set()
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(stamp[0][2][0], "pending")
        conn.close()


class ChangeRefreshSwitch(unittest.TestCase):
    def test_the_change_refresh_starts_only_when_switched_on(self):
        """Off by default (the live inputs move every few minutes): reading the inputs
        stamp starts no refresh thread; PLX_SNAPSHOT_REFRESH=1 starts it once."""
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend._lock = threading.RLock()
        backend.store_path = Path("/nonexistent/store.sqlite")
        backend._served_state = lambda wait=True: ("store", 1)
        backend._pg_load_stamp = lambda: None
        started: list = []
        backend._refresh_on_change = lambda: started.append(1)
        self.assertEqual(be.WorkspaceBackend.inputs_stamp(backend), (("store", 1), None))
        self.assertIsNone(getattr(backend, "_inputs_watch", None))
        with mock.patch.object(be, "SNAPSHOT_REFRESH", True):
            backend._inputs = None
            be.WorkspaceBackend.inputs_stamp(backend)
            backend._inputs_watch.join(5)
        self.assertEqual(started, [1])


class MarksStamp(unittest.TestCase):
    """The page key's fingerprint of the documents marked beyond a reader: worked out once
    per withheld set (11-18 profile: ~11 s of CPU sorting and hashing a staff reader's ids
    on every access check of a 3-visit walk), and still new on every re-mark (a new set),
    every change to the PLX-issued documents, and for every other reader's set."""

    def test_one_digest_per_set_and_a_new_one_on_any_change(self):
        from operator_api import backend_pages as bp
        from operating.search import document_marks as dm
        view = SimpleNamespace(access=SimpleNamespace(grant=lambda: (None, ["m:a"])))
        sets = {frozenset({"m:a"}): (frozenset({"Doc:1", "Doc:2"}), frozenset({"h1"}))}
        own = [frozenset()]
        sorted_calls = []
        real_dumps = bp.json.dumps

        def dumps(value, *a, **k):
            sorted_calls.append(1)
            return real_dumps(value, *a, **k)
        with mock.patch.object(dm, "withheld_set", lambda held: sets[frozenset(held)]), \
                mock.patch.object(dm, "_own_issued", lambda: own[0]), \
                mock.patch.object(bp.json, "dumps", dumps), \
                mock.patch.dict(bp._MARK_DIGESTS, clear=True):
            first = bp._marks_stamp(view)
            self.assertEqual([bp._marks_stamp(view) for _ in range(50)], [first] * 50)
            self.assertEqual(len(sorted_calls), 1)
            self.assertEqual(first, bp.hashlib.sha256(real_dumps(
                [["Doc:1", "Doc:2"], ["h1"]]).encode()).hexdigest()[:16])
            sets[frozenset({"m:a"})] = (frozenset({"Doc:1"}), frozenset({"h1"}))   # re-mark
            remarked = bp._marks_stamp(view)
            self.assertNotEqual(remarked, first)
            own[0] = frozenset({"Doc:9"})                       # a PLX-issued doc found
            self.assertNotIn(bp._marks_stamp(view), (first, remarked))
            other = SimpleNamespace(access=SimpleNamespace(grant=lambda: (None, ["m:b"])))
            sets[frozenset({"m:b"})] = (frozenset({"Doc:3"}), frozenset())
            self.assertNotEqual(bp._marks_stamp(other), bp._marks_stamp(view))


class OwnIssuedStamp(unittest.TestCase):
    def test_an_equal_set_reloaded_keeps_its_digest_and_a_changed_one_moves_it(self):
        """The PLX-issued ids' digest is the same for an equal set loaded again (a new
        object) and moves when an id is added or removed."""
        from operator_api import backend_pages as bp
        first = frozenset({"Item:a", "Item:b"})
        stamp = bp._own_stamp(first)
        self.assertEqual(bp._own_stamp(first), stamp)
        self.assertEqual(bp._own_stamp(frozenset(set(first))), stamp)   # reloaded
        added = bp._own_stamp(first | {"Item:c"})
        self.assertNotEqual(added, stamp)
        self.assertNotEqual(bp._own_stamp(frozenset({"Item:a"})), stamp)
        self.assertEqual(bp._own_stamp(first), stamp)
        self.assertEqual(bp._own_stamp(frozenset()), "")
