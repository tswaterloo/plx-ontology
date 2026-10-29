"""Compact SharePoint storage (``SharePointCaptureStore.settle_original`` / ``compact``).

Live 2026-10-27: ``<state>/sharepoint-capture`` held 4 GB and grew. As the mail store
(550f151de1), the capture now drops an original over ``DROP_ORIGINALS_ABOVE`` once every
reading of it is kept (``.dropped``), keeps texts compressed, keeps what a pending OCR
still needs, and a store captured before is compacted once (``compact``). Every reader of
the text (resolve's page citations, the typed-field citation reader) reads the same
before and after a drop; a restricted reader sees nothing more; a dropped original's
bytes come back only through one guarded GET that hashes to the pin. Fixtures: the fake
Graph drive of ``test_capture_sharepoint``. Evidence label: injected-transport.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout
import unittest

from integrations import capture_sharepoint as cs, captured_text as ct, graph_read
from integrations.graph_read import SharePointReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests import test_capture_sharepoint as tsp
from ontology import document_fields

QUALITY, EMPLOYEE, OUTSIDER = tsp.QUALITY, tsp.EMPLOYEE, tsp.OUTSIDER


def fresh_memos():
    """Empty process-wide reading memos, so a reading is made again from the store."""
    stack = contextlib.ExitStack()
    for module, name in ((ct, "_CACHE"), (ct, "_MEMBER_READINGS"), (document_fields, "_FIELDS")):
        stack.enter_context(mock.patch.object(module, name, getattr(module, name).empty()))
    return stack


def pdf(pages):
    from operating.tests.test_attested_missing import pdf as make
    return make(pages)


def big_pdf(pages: int) -> bytes:
    """A text PDF over ``DROP_ORIGINALS_ABOVE``: a few lines of text per page and, on the
    first, many small drawn rectangles (uncompressed bytes, no text)."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pageCompression=0)
    for number in range(1, pages + 1):
        for line in range(5):
            page.drawString(40, 800 - 17 * line, f"Page {number} line {line} lot "
                            f"L{number:04d}{line:03d} quantity {number * line} units filled")
        if number == 1:
            for k in range(16000):
                page.rect(40 + k % 500, 100 + (k // 500) % 500, 1.5, 1.5)
        page.showPage()
    page.save()
    return buffer.getvalue()


def dropped_items(store):
    return sorted(r["item_id"] for r in store.db.execute(
        "SELECT item_id, content_sha256 FROM items WHERE superseded_at IS NULL AND "
        "content_sha256 IS NOT NULL") if store.original_dropped(r["content_sha256"]))


def forms(store, digest):
    return {suffix: ct.blob_path(store.blobs, digest, suffix).exists()
            for suffix in ("", ct.COMPRESSED_SUFFIX, ct.DROPPED_SUFFIX)}


class CompactTests(tsp.CaptureCase):
    def reader(self):
        api, transport = graph_read.build_graph(tsp.ENV, sender=self.drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        return SharePointReader(api, tsp.capture_audience(), tsp.DRIVE, transport=transport,
                                read_permissions=True)

    def views(self, handle, digest):
        """What every reader of a version's text gets (memos emptied first)."""
        with fresh_memos():
            audience = tsp.reader_audience()
            page = self.store.resolve(handle + "#page=2", QUALITY, audience)
            whole = self.store.resolve(handle, QUALITY, audience)
            fields = document_fields.capture_citation_reader(self.store, QUALITY, audience)(
                f"{handle}#sha256={digest}")
            return {"page": page["row"]["content_text"], "text": whole["row"]["content_text"],
                    "fields": fields}

    def test_a_dropped_original_reads_the_same_text_and_refetches_verified(self):
        packet = pdf([["PURCHASE ORDER", "PO Number: 4500012345"],
                      ["Purchase order PO100235", "Qty 1,502"]])
        self.drive.file("01BIG", "packet.pdf", "01AOS", packet)
        self.drive.file("01ZIP", "bundle.zip", "01AOS", _zip({"a.txt": b"PO100236"}),
                        mime="application/zip")
        huge = big_pdf(3)
        self.assertGreater(len(huge), cs.DROP_ORIGINALS_ABOVE)
        self.drive.file("01HUGE", "lots.pdf", "01AOS", huge)
        run = self.capture().run(tsp.scope())
        # At capture only the original over 256 KiB goes; the store holds far less of it.
        self.assertEqual(self.root(run)["counts"]["originals_dropped"], 1)
        self.assertEqual(dropped_items(self.store), ["01HUGE"])
        self.assertLess(self.store.store_bytes(), len(huge))
        with fresh_memos():
            page = self.store.resolve(self.handle("01HUGE") + "#page=2", QUALITY,
                                      tsp.reader_audience())
        self.assertIn("lot L0002001", page["row"]["content_text"])
        handle, digest = self.handle("01BIG"), self.store.current(tsp.DRIVE, "01BIG")[
            "content_sha256"]
        self.assertEqual(self.store.settle_original(digest), "kept_small")
        before = self.views(handle, digest)
        self.assertIn("PO100235", before["page"])
        self.assertTrue(before["fields"]["ok"], before["fields"])
        # Compact under a 10-byte bound: the packet and the seeded PDFs go, the zip stays.
        with mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10):
            result = self.store.compact(vacuum=False)
        self.assertEqual(result["outcomes"].get("kept_container"), 1, result)
        self.assertEqual(dropped_items(self.store), ["01BIG", "01FILEA", "01FILEC", "01HUGE"])
        self.assertEqual(forms(self.store, digest), {"": False, ".z": False, ".dropped": True})
        # Every citation re-reads the same text; the typed-field row is the same row.
        self.assertEqual(self.views(handle, digest), before)
        # The identifier lookup's stored text (no extraction) is that same reading.
        found = self.store.db.execute(
            "SELECT * FROM items WHERE l1_handle=? AND superseded_at IS NULL",
            (handle,)).fetchone()
        stored = ct.stored_text(self.store.blobs, {**json.loads(found["record"]), **{
            key: found[key] for key in ("content_status", "content_sha256", "text_status",
                                        "text_sha256")}})
        with fresh_memos():
            self.assertEqual(stored.text, self.store.resolve(
                handle, QUALITY, tsp.reader_audience())["row"]["content_text"])
        self.assertIn("PO100235", stored.text)
        self.assertIsNone(self.store.content(handle, QUALITY, tsp.reader_audience()))
        self.assertTrue(self.store.original_verified(f"{handle}#sha256={digest}", QUALITY,
                                                     tsp.reader_audience()))
        # A restricted reader sees nothing more of it.
        for actor in (EMPLOYEE, OUTSIDER):
            self.assertIsNone(self.store.resolve(handle + "#page=2", actor,
                                                 tsp.reader_audience()))
            self.assertIsNone(self.store.original_reading(handle, actor, tsp.reader_audience()))
            self.assertIsNone(self.store.refetch_original(handle, actor, tsp.reader_audience(),
                                                          self.reader(), tsp.CAPTURE))
        # The refetch handle: one guarded GET, returned only when it hashes to the pin.
        downloads = self.drive.downloads()
        self.assertEqual(self.store.refetch_original(handle, QUALITY, tsp.reader_audience(),
                                                     self.reader(), tsp.CAPTURE), packet)
        self.assertEqual(self.drive.downloads(), downloads + 1)
        self.assertEqual(forms(self.store, digest)[""], False)   # never stored
        self.drive.change("01BIG", pdf([["changed"]]))
        self.assertIsNone(self.store.refetch_original(handle, QUALITY, tsp.reader_audience(),
                                                      self.reader(), tsp.CAPTURE))
        # The text that is its own bytes is kept, compressed, and still reads.
        text_digest = self.store.current(tsp.DRIVE, "01FILEB")["text_sha256"]
        self.assertTrue(forms(self.store, text_digest)[".z"])
        with fresh_memos():
            row = self.store.resolve(self.handle("01FILEB"), QUALITY, tsp.reader_audience())
        self.assertEqual(row["row"]["content_text"], tsp.TEXT_SECRET)
        # A second compact finds it dropped and drops nothing twice.
        with mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10):
            again = self.store.compact(vacuum=False)["outcomes"]
        self.assertEqual(again.get("dropped"), 4)

    def test_new_captures_drop_large_originals_at_cardinality(self):
        """1, 10 and 60 files over the bound: each dropped at capture, none lost or
        twice, each one's page citation still reads its own text."""
        for n in (1, 10, 60):
            with self.subTest(files=n):
                if n != 1:   # a fresh store and drive
                    self.tearDown()
                    self.setUp()
                for i in range(n):
                    self.drive.file(f"01N{i:03d}", f"po-{i}.pdf", "01AOS",
                                    pdf([["cover"], [f"Purchase order PO{200000 + i}"]]))
                with mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10):
                    run = self.capture().run(tsp.scope())
                mine = [f"01N{i:03d}" for i in range(n)]
                self.assertEqual(dropped_items(self.store),   # with the 2 seeded PDFs
                                 sorted(mine + ["01FILEA", "01FILEC"]))
                self.assertEqual(self.root(run)["counts"]["originals_dropped"], n + 2)
                with fresh_memos():
                    for i in range(n):
                        page = self.store.resolve(self.handle(f"01N{i:03d}") + "#page=2",
                                                  QUALITY, tsp.reader_audience())
                        self.assertEqual(page["row"]["content_text"].strip(),
                                         f"Purchase order PO{200000 + i}")

    def test_deferred_ocr_drops_a_pending_scan_and_a_later_walk_refetches_it(self):
        scan = tsp._scanned_or_skip(self)
        self.drive.file("01SCAN", "scan.pdf", "01AOS", scan)
        short = cs.OCR_RESERVE_SECONDS - 1
        self.capture(budget_seconds=short, monotonic=lambda: 0.0).run(tsp.scope())
        self.assertEqual(self.store.current(tsp.DRIVE, "01SCAN")["text_status"], ct.OCR_PENDING)
        digest = self.store.current(tsp.DRIVE, "01SCAN")["content_sha256"]
        with mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10):
            self.assertEqual(self.store.compact(vacuum=False)["outcomes"]["kept_pending"], 1)
            result = self.store.compact(vacuum=False, defer_ocr=True)
        self.assertEqual((result["ocr_deferred"], dropped_items(self.store)),
                         (1, ["01FILEA", "01FILEC", "01SCAN"]))
        self.assertEqual(self.store.current(tsp.DRIVE, "01SCAN")["text_status"], ct.OCR_DEFERRED)
        handle = self.handle("01SCAN")
        with fresh_memos():   # a named gap, never an empty or absent reading
            found = document_fields.capture_citation_reader(
                self.store, QUALITY, tsp.reader_audience())(f"{handle}#sha256={digest}")
            reading = self.store.original_reading(handle, QUALITY, tsp.reader_audience())
        self.assertTrue(reading.ocr_deferred)
        self.assertNotEqual(found.get("row", {}).get("status"), "classified")
        downloads = self.drive.downloads()
        with mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10):
            run = self.capture(budget_seconds=cs.OCR_RESERVE_SECONDS,
                               monotonic=lambda: 0.0).run(tsp.scope())
        counts = self.root(run)["counts"]
        self.assertEqual((counts["texts_reextracted"], counts["texts_ocr"]), (1, 1))
        self.assertEqual(self.drive.downloads(), downloads + 1)   # the one refetch
        self.assertEqual(self.store.current(tsp.DRIVE, "01SCAN")["text_status"], ct.TEXT_OCR)
        self.assertEqual(forms(self.store, digest)[""], False)   # dropped again, OCR kept
        with fresh_memos():
            page = self.store.resolve(handle + "#page=2", QUALITY, tsp.reader_audience())
        self.assertIn("PO100235", page["row"]["content_text"])

    def test_compact_cli_holds_the_cycle_lock_and_fails_closed(self):
        self.capture().run(tsp.scope())
        self.store.close()
        out = io.StringIO()
        lock_dir = self.state / "host"
        lock_dir.mkdir(exist_ok=True)
        with open(lock_dir / "cycle.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with redirect_stdout(out):
                code = cs.main(["compact", "--state-dir", str(self.state)])
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual((code, json.loads(out.getvalue())),
                         (2, {"ok": False, "error": "host_cycle_running"}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = cs.main(["compact", "--state-dir", str(self.state), "--defer-ocr"])
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["ok"], report["vacuum"]), (0, True, "done"))
        self.assertNotIn(str(self.state), out.getvalue())   # counts and bytes only
        out = io.StringIO()
        with redirect_stdout(out):
            code = cs.main(["compact", "--state-dir", str(Path(self.tmp.name) / "none")])
        self.assertEqual((code, json.loads(out.getvalue())["error"]),
                         (2, "sharepoint_capture_missing"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cs.main(["unknown"]), 2)
        self.store = cs.SharePointCaptureStore(self.state)


def _zip(members):
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
