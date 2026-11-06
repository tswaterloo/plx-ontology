"""The reading budget of a row-delta cycle (``ontology/reading_budget.py``): a file not
read in time is pending (logged, not remembered, not kept), never unreadable."""

from __future__ import annotations

import functools
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from integrations import captured_text as ct
from ontology import reading_budget as rb


class ReadingBudgetTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.blobs = self.root / "blobs"
        self.content = b"Packing list PL-0042 for PO900001, made-up text"
        self.digest = sha256(self.content).hexdigest()
        path = ct.blob_path(self.blobs, self.digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content)
        self.row = {"content_sha256": self.digest, "content_type": "text/plain",
                    "name": "list.txt"}
        self.log = self.root / "pending.tsv"
        # a memo of this test's own, so no other test's readings answer
        memo = ct._CACHE.empty()
        patch = mock.patch.object(ct, "_CACHE", memo)
        patch.start()
        self.addCleanup(patch.stop)
        rb.install()
        self.kept = ct.KeptReadings(self.root / "readings.sqlite", "code-1")
        memo.backing = self.kept
        self.addCleanup(self.kept.close, prune=False)

    def budget(self, **config):
        config.setdefault("log", str(self.log))
        return mock.patch.dict(os.environ, {rb.BUDGET_ENV: json.dumps(config)})

    def test_past_the_deadline_a_file_is_pending_and_read_once_the_budget_allows(self):
        with self.budget(deadline=time.time() - 1, limit=30):
            self.assertIsNone(ct.captured_text(self.blobs, self.row))
        self.assertEqual(rb.pending(self.log), {self.digest: {rb.BUDGET}})
        self.assertNotIn((self.digest, "text/plain", "list.txt", None), ct._CACHE)
        self.assertIsNone(self.kept.get((self.digest, "text/plain", "list.txt", None)))
        with self.budget(deadline=time.time() + 60, limit=30):
            found = ct.captured_text(self.blobs, self.row)
        self.assertIn("PL-0042", found.text)
        # known now: served past the deadline too, nothing more pending
        with self.budget(deadline=time.time() - 1, limit=30, log=str(self.root / "b.tsv")):
            self.assertEqual(ct.captured_text(self.blobs, self.row), found)
        self.assertEqual(rb.pending(self.root / "b.tsv"), {})

    def test_a_read_past_its_limit_is_timed_out_even_when_the_extractor_swallows_it(self):
        calls = []

        def slow(content, mime, **kwargs):
            # as pypdf does per page: every failure is a page "unreadable", reading goes on
            calls.append(1)
            pages = []
            for _ in range(40):
                try:
                    time.sleep(0.05)
                    pages.append("text")
                except BaseException:   # noqa: BLE001 - the extractor's own habit
                    pages.append("")
            return ct.CapturedText(self.digest, mime, "extracted", "test",
                                   tuple(enumerate(pages, 1)), frozenset(), True)

        with self.budget(deadline=time.time() + 60, limit=0.3), \
                mock.patch.object(ct, "read_text", slow):
            started = time.monotonic()
            self.assertIsNone(ct.captured_text(self.blobs, self.row))
            self.assertLess(time.monotonic() - started, 5)
            # timed out in this cycle: not tried again in it
            self.assertIsNone(ct.captured_text(self.blobs, self.row))
        self.assertEqual(len(calls), 1)
        self.assertEqual(rb.pending(self.log), {self.digest: {rb.TIMED_OUT, rb.DEFERRED}})
        self.assertIsNone(self.kept.get((self.digest, "text/plain", "list.txt", None)))
        self.assertEqual(len(ct._CACHE), 0)

    def test_an_interrupt_in_the_markitdown_probe_leaves_later_reads_unchanged(self):
        from integrations import doc_convert

        @functools.lru_cache(maxsize=1)
        def probe():
            try:
                time.sleep(0.6)   # an import the timer may cut short
                return "markitdown"
            except BaseException:   # noqa: BLE001 - as _import_backend: "not installed"
                return None
        cold = [True]

        def read(content, mime, **kwargs):
            if cold:
                cold.pop()
                probe.cache_clear()   # a worker whose probe ran inside this read
                time.sleep(0.2)
            method = doc_convert.installed_backend() or "stdlib"
            return ct.CapturedText(sha256(content).hexdigest(), mime, "extracted", method,
                                   ((None, "text"),), frozenset(), False)

        other = b"Commercial invoice CI-0007, made-up text"
        path = ct.blob_path(self.blobs, sha256(other).hexdigest())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(other)
        row = dict(self.row, content_sha256=sha256(other).hexdigest(), name="b.txt")
        with mock.patch.object(doc_convert, "_import_backend", probe), \
                mock.patch.object(ct, "read_text", read), \
                self.budget(deadline=time.time() + 60, limit=0.4):
            self.assertIsNone(ct.captured_text(self.blobs, self.row))   # timed out
            self.assertEqual(rb.pending(self.log), {self.digest: {rb.TIMED_OUT}})
            # the next file in this process is read with MarkItDown, as in a clean one
            self.assertEqual(ct.captured_text(self.blobs, row).method, "markitdown")
            self.assertEqual(doc_convert.installed_backend(), "markitdown")

    def test_a_file_left_to_the_generation_build_is_never_read_by_a_cycle(self):
        limits = self.root / "limits.json"
        limits.write_text(json.dumps({self.digest: 0}))
        with self.budget(deadline=time.time() + 60, limit=30, limits=str(limits)), \
                mock.patch.object(ct, "read_text", side_effect=AssertionError("read")):
            self.assertIsNone(ct.captured_text(self.blobs, self.row))
        self.assertEqual(rb.pending(self.log), {self.digest: {rb.GENERATION}})


if __name__ == "__main__":
    unittest.main()
