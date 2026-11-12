"""The readers' pages kept on disk (``page_store``): found again by another process or
after a restart under the same reader state and code, decided again for the reader before
they are served, never under another key; the warm-up sent to its own process; the
attachment page counts kept by digest."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from operator_api import backend as be
from operator_api import page_store
from operator_api.tests.test_backend_fixture import _View

HANDLE = "sharepoint://site/drive/item-1@g-1"


def view_of(built: list, digest: str = "sha256:x") -> _View:
    view = _View(())

    class Policy:
        unrestricted = False

        def may_read(self, actor, key):
            return True

    view.access.reader_policy = lambda: (object(), Policy())
    view.access.store_meta = lambda: {"content_digest": digest}
    view._request = contextlib.nullcontext
    view.investigate = lambda family, oid: {}

    def workspace(oid):
        built.append(oid)
        return {"object_id": oid, "status": "read", "card": {
            "object": {"object_type": "PurchaseOrder", "display": "PO 1"},
            "values": [{"property": "status", "value": "open",
                        "citation": {"l1_handle": HANDLE}}]}}
    view.workspace = workspace
    return view


class KeptPages(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pages-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        patch = mock.patch("operating.pg.permissions._fingerprint", lambda p, a: "fp-1")
        patch.start()
        self.addCleanup(patch.stop)

    def backend(self):
        return be.WorkspaceBackend(lambda s: None, data_label="t", data_dir=self.dir,
                                   keep_pages=True)

    def page(self, backend, view):
        return be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")

    def test_a_page_read_once_is_served_after_a_restart_and_decided_again(self):
        built = []
        first = self.page(self.backend(), view_of(built))
        self.assertEqual(len(built), 1)
        restarted = self.backend()                         # a new process, the same disk
        again = self.page(restarted, view_of(built))
        self.assertEqual(len(built), 1)                    # served from the disk
        self.assertEqual(again.model_dump(), first.model_dump())
        closed = view_of(built)
        closed.closed.add(HANDLE)                          # its item ACL changed since
        self.page(self.backend(), closed)
        self.assertEqual(len(built), 2)                    # never served stale

    def test_another_generation_reader_or_code_is_another_page(self):
        built = []
        self.page(self.backend(), view_of(built))
        self.page(self.backend(), view_of(built, digest="sha256:y"))   # a new generation
        self.assertEqual(len(built), 2)
        with mock.patch("operating.pg.permissions._fingerprint", lambda p, a: "fp-2"):
            self.page(self.backend(), view_of(built))                 # another reader
        self.assertEqual(len(built), 3)
        with mock.patch.dict("os.environ", {"PLX_EXCLUDED_PEOPLE_OIDS":
                                            "33333333-3333-4333-8333-333333333333"}):
            self.page(self.backend(), view_of(built))                 # another exclusion
        self.assertEqual(len(built), 4)
        with mock.patch.object(page_store, "_CODE", "other-code"):
            self.page(self.backend(), view_of(built))                 # another code
        self.assertEqual(len(built), 5)
        self.page(self.backend(), view_of(built))                     # the first again
        self.assertEqual(len(built), 5)

    def test_an_expired_or_process_bound_entry_is_never_kept_or_served(self):
        store = page_store.PageStore(self.dir / "s")
        store.put(("k", 1), {"v": 1}, seconds=0.01)
        time.sleep(0.05)
        self.assertIsNone(store.get(("k", 1)))
        store.put(("k", time.monotonic()), {"v": 2}, seconds=60)       # a monotonic time
        store.put(("k", object()), {"v": 3}, seconds=60)               # an object
        self.assertEqual(store.puts, 1)                                # the first only
        store.put(("k", frozenset({"b", "a"})), {"v": 4}, seconds=60)
        self.assertEqual(page_store.PageStore(self.dir / "s").get(
            ("k", frozenset({"a", "b"})))[1], {"v": 4})
        self.assertEqual((self.dir / "s" / page_store.FILE).stat().st_mode & 0o077, 0)

    def test_the_warm_up_process_reads_entries_near_their_expiry_again(self):
        cache = be.ReaderCache(store=page_store.PageStore(self.dir / "s"))
        cache.put(("k",), "old")
        warm = be.ReaderCache(store=page_store.PageStore(self.dir / "s"))
        warm.fresh_for = 0.0
        time.sleep(0.01)
        self.assertIsNone(warm.get(("k",)))
        self.assertEqual(be.ReaderCache(store=page_store.PageStore(self.dir / "s")).get(
            ("k",)), "old")


class WarmUpProcess(unittest.TestCase):
    def test_the_api_sends_the_read_ahead_to_its_process_never_its_read_workers(self):
        backend = be.WorkspaceBackend(lambda s: None, data_label="t")
        sent = []

        class Process:
            def warm(self, reader, recent, today=True):
                sent.append(("warm", reader, list(recent), today))
                return True

            def warm_briefing(self, reader):
                sent.append(("briefing", reader))
                return True

            def stop(self):
                pass
        backend.warm_process = Process()
        reader = mock.Mock(key="r1")
        reader.session.view_as = False   # a Mock attribute is truthy: not the owner viewing as someone
        backend._pages_seen["r1"] = [("PurchaseOrder", "PurchaseOrder:demo:1")]
        self.assertTrue(backend.warm(reader))
        self.assertFalse(backend.warm(reader))              # once per WARM_SECONDS
        self.assertTrue(backend.warm_briefing(reader))
        self.assertEqual(sent, [("warm", reader, [("PurchaseOrder", "PurchaseOrder:demo:1")],
                                 True), ("briefing", reader)])
        self.assertTrue(backend.reads.jobs.empty())         # nothing read here


class PageCounts(unittest.TestCase):
    def test_a_page_count_is_kept_by_digest_and_survives_a_restart(self):
        from integrations import captured_text as ct
        from integrations import page_counts as pc
        tmp = Path(tempfile.mkdtemp(prefix="counts-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        blobs = tmp / "blobs"
        reading = mock.Mock(text="one" + ct.PAGE_BREAK + "two", paged=True)
        row = {"content_sha256": "a" * 64, "content_type": "application/pdf",
               "name": "po.pdf"}
        with mock.patch.object(pc, "captured_text", return_value=reading) as read, \
                mock.patch.object(pc, "_blob_kept", return_value=True):
            counts = pc.PageCounts(tmp / "counts.sqlite")
            self.assertEqual([counts.has_page(blobs, row, n) for n in (1, 2, 3)],
                             [True, True, False])
            self.assertEqual(read.call_count, 1)             # one reading counts them
            again = pc.PageCounts(tmp / "counts.sqlite")     # after a restart
            self.assertTrue(again.has_page(blobs, row, 2))
            self.assertEqual(read.call_count, 1)
        # its bytes gone: no kept count is served, and the reading finds nothing
        with mock.patch.object(pc, "captured_text", return_value=None), \
                mock.patch.object(pc, "_blob_kept", return_value=False):
            self.assertIsNone(pc.PageCounts(tmp / "counts.sqlite").count(blobs, row))


class DeltaLayerKey(unittest.TestCase):
    def test_a_new_delta_layer_is_a_new_page_key(self):
        view = view_of([])
        layers = [("/d/a.sqlite",)]
        view.access.delta_layer_stamp = lambda: layers[0]
        with mock.patch("operating.pg.permissions._fingerprint", lambda p, a: "fp-1"):
            first = be.page_key(view, "o")
            self.assertIsNotNone(first)
            self.assertEqual(first, be.page_key(view, "o"))
            layers[0] = ("/d/b.sqlite", "/d/a.sqlite")
            self.assertNotEqual(first, be.page_key(view, "o"))


class MarkedSetKey(unittest.TestCase):
    def test_a_re_mark_is_a_new_page_key(self):
        from operating.search import document_marks
        view = view_of([])
        marked = [(frozenset(), frozenset())]
        with mock.patch("operating.pg.permissions._fingerprint", lambda p, a: "fp-1"), \
                mock.patch.object(document_marks, "withheld_set",
                                  side_effect=lambda held: marked[0]):
            first = be.page_key(view, "o")
            self.assertEqual(first, be.page_key(view, "o"))
            marked[0] = (frozenset({"SharePointFile:x"}), frozenset())
            second = be.page_key(view, "o")
            self.assertNotEqual(first, second)
            marked[0] = (frozenset(), frozenset())
            self.assertEqual(first, be.page_key(view, "o"))      # un-marked: the old key
            marked[0] = None                                      # unreadable: its own key
            self.assertNotIn(be.page_key(view, "o"), (first, second))


if __name__ == "__main__":
    unittest.main()
