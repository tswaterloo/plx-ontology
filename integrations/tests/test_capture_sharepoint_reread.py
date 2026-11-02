"""A stored version whose text an earlier reader could not read (a large workbook that
failed whole, a legacy .xls/.doc) is read again once by the current reader: from its
stored bytes, or a dropped original by one guarded GET checked against its pin; never
offered twice per reader generation, never for a type the reader does not read, and
its text still gated per reader. Fixtures: the fake Graph drive of
``test_capture_sharepoint``. Evidence label: injected-transport."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock
import unittest

from integrations import capture_sharepoint as cs, captured_text as ct
from integrations.tests import test_capture_sharepoint as tsp
from integrations.tests.test_capture_sharepoint_compact import fresh_memos
from integrations.tests.test_partial_and_legacy_extraction import XLSX, doc, rows, workbook

QUALITY, OUTSIDER = tsp.QUALITY, tsp.OUTSIDER


class ReaderRereadTests(tsp.CaptureCase):
    def seed(self, n: int) -> list[str]:
        ids = []
        for i in range(n):
            self.drive.file(f"01W{i:03d}", f"ledger-{i}.xlsx", "01AOS",
                            workbook([("Ledger", rows(3_000, label=f"L{i}"))]), mime=XLSX)
            ids.append(f"01W{i:03d}")
        self.drive.file("01DOC", "po.doc", "01AOS", doc(), mime="application/msword")
        self.drive.file("01PNG", "logo.png", "01AOS", b"\x89PNG\r\n\x1a\n" + bytes(40),
                        mime="image/png")
        return ids + ["01DOC"]

    def old_reader_capture(self) -> None:
        """A capture by the earlier reader: these types failed or were unsupported, and
        their originals were dropped (compact storage)."""
        real = cs.capture_text

        def old(content, record, **kwargs):
            name = record.get("name", "")
            if name.endswith(".xlsx"):
                return "failed", None
            if name.endswith(".doc"):
                return "unsupported", None
            return real(content, record, **kwargs)
        with mock.patch.object(cs, "capture_text", old), \
                mock.patch.object(cs.SharePointCapture, "_reread_stale",
                                  lambda *a, **k: None), \
                mock.patch.object(cs, "DROP_ORIGINALS_ABOVE", 10):
            self.capture().run(tsp.scope())

    def test_failed_and_unsupported_versions_are_reread_once_and_refetched_when_dropped(self):
        for n in (1, 10, 60):
            with self.subTest(files=n):
                if n != 1:
                    self.tearDown()
                    self.setUp()
                ids = self.seed(n)
                self.old_reader_capture()
                statuses = {i: self.store.current(tsp.DRIVE, i)["text_status"] for i in ids}
                self.assertEqual(set(statuses.values()), {"failed", "unsupported"})
                dropped = [i for i in ids if self.store.original_dropped(
                    self.store.current(tsp.DRIVE, i)["content_sha256"])]
                self.assertTrue(dropped)
                downloads = self.drive.downloads()
                with mock.patch.object(cs, "REPAIRS_PER_ROOT", 100):
                    run = self.capture().run(tsp.scope())
                counts = self.root(run)["counts"]
                self.assertEqual(counts["texts_reread"], n + 1)   # none lost or twice
                self.assertEqual(counts["texts_reread_refetch_failed"], 0)
                self.assertEqual(self.drive.downloads(), downloads + len(dropped))
                for i in ids:
                    current = self.store.current(tsp.DRIVE, i)
                    self.assertEqual(current["text_status"], ct.TEXT_EXTRACTED, i)
                # An image is now OCR-read locally (tesseract): the fixture's PNG is no
                # real image, so its re-read ends in a typed OCR failure (no engine: a
                # typed ocr_unavailable), never extracted text and never re-offered below.
                from integrations.document_understanding import local_image_ocr_available
                self.assertEqual(self.store.current(tsp.DRIVE, "01PNG")["text_status"],
                                 f"{ct.OCR_FAILED}:recognize_exit"
                                 if local_image_ocr_available() else ct.OCR_UNAVAILABLE)
                with fresh_memos():
                    row = self.store.resolve(self.handle(ids[0]), QUALITY,
                                             tsp.reader_audience())
                    self.assertIn(f"A1=L0-1 |", row["row"]["content_text"])
                    self.assertIsNone(self.store.resolve(self.handle(ids[0]), OUTSIDER,
                                                         tsp.reader_audience()))
                # Offered once per generation: the next walk reads nothing again.
                downloads = self.drive.downloads()
                again = self.root(self.capture().run(tsp.scope()))["counts"]
                self.assertEqual((again["texts_reread"], self.drive.downloads()),
                                 (0, downloads))

    def test_a_source_outage_stops_the_pass_marks_nothing_and_a_healthy_run_reads_all(self):
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import SourceUnavailable
        # A throttle or no connection says the source is out at once; a 5xx only when
        # REREAD_OUTAGE_STREAK downloads fail in a row with none succeeding.
        for message, calls in (("graph GET returned HTTP 503", cs.REREAD_OUTAGE_STREAK),
                               ("download returned HTTP 500", cs.REREAD_OUTAGE_STREAK),
                               ("graph GET returned HTTP 429", 1),
                               ("graph GET failed: TimeoutError", 1)):
            with self.subTest(message):
                self.tearDown()
                self.setUp()
                ids = self.seed(3)
                self.old_reader_capture()
                with mock.patch.object(SharePointReader, "get_content",
                                       side_effect=SourceUnavailable(message)) as get:
                    counts = self.root(self.capture().run(tsp.scope()))["counts"]
                # The source is down: the pass stops, no file counts a try.
                self.assertEqual((get.call_count, counts["texts_reread"],
                                  counts["texts_reread_refetch_failed"],
                                  counts["texts_reread_gaps"]), (calls, 0, calls, 0))
                self.assertEqual(self.store.db.execute(
                    "SELECT COUNT(*) FROM text_reread_attempts WHERE attempts > 0"
                ).fetchone()[0], 0)
                self.assertEqual(self.store.current(tsp.DRIVE, "01W000")["text_status"],
                                 "failed")
                # A healthy run (no clock advance: no backoff) reads every one of them.
                counts = self.root(self.capture().run(tsp.scope()))["counts"]
                self.assertEqual((counts["texts_reread"],
                                  counts["texts_reread_refetch_failed"]), (len(ids), 0))
                for i in ids:
                    self.assertEqual(self.store.current(tsp.DRIVE, i)["text_status"],
                                     ct.TEXT_EXTRACTED, i)

    def test_refused_credentials_stop_the_pass_as_auth_and_count_no_file(self):
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import SourceUnavailable
        for message in ("graph GET returned HTTP 401", "graph token request refused"):
            with self.subTest(message):
                self.tearDown()
                self.setUp()
                ids = self.seed(3)
                self.old_reader_capture()
                with mock.patch.object(SharePointReader, "get_content",
                                       side_effect=SourceUnavailable(message)) as get:
                    counts = self.root(self.capture().run(tsp.scope()))["counts"]
                    # one refusal: no further refetch this run, marked as auth
                    self.assertEqual((get.call_count, counts["texts_reread_auth_refused"],
                                      counts["texts_reread_gaps"]), (1, 1, 0))
                    # however many runs it lasts, no file is counted a try or made a gap
                    for _ in range(cs.REREAD_MAX_ATTEMPTS + 1):
                        self.store._clock.now += timedelta(days=4)
                        counts = self.root(self.capture().run(tsp.scope()))["counts"]
                        self.assertEqual(counts["texts_reread_auth_refused"], 1)
                self.assertEqual(self.store.db.execute(
                    "SELECT COUNT(*) FROM text_reread_attempts WHERE attempts > 0 OR "
                    "gave_up_at IS NOT NULL").fetchone()[0], 0)
                self.assertEqual(self.store.reread_gaps(tsp.DRIVE, self.root_path()), [])
                counts = self.root(self.capture().run(tsp.scope()))["counts"]
                self.assertEqual(counts["texts_reread"], len(ids))

    def test_a_file_that_5xxs_on_every_download_ends_a_named_gap_and_blocks_none(self):
        """One file's download fails with a 500 every time while the source serves the
        others: it is that file's own failure, backs off and ends a
        named gap; a file that failed once on its own behind it is retried and read."""
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import SourceUnavailable
        ids = self.seed(3)
        self.old_reader_capture()
        real = SharePointReader.get_content
        tried: list[str] = []

        def flaky(reader, actor, item_id, **kwargs):
            tried.append(item_id)
            if item_id == "01W000":   # the oldest stale version: first in line
                raise SourceUnavailable("download returned HTTP 500")
            if item_id == "01W001" and tried.count(item_id) == 1:
                raise SourceUnavailable("graph GET returned HTTP 403")
            return real(reader, actor, item_id, **kwargs)

        def attempts(iid):
            row = self.store.db.execute(
                "SELECT attempts FROM text_reread_attempts WHERE l1_handle=?",
                (self.handle(iid),)).fetchone()
            return None if row is None else row[0]

        clock = self.store._clock
        with mock.patch.object(SharePointReader, "get_content", flaky):
            counts = self.root(self.capture().run(tsp.scope()))["counts"]
            # the 500 did not stop the pass: the files behind it were read
            self.assertEqual((counts["texts_reread"], counts["texts_reread_refetch_failed"]),
                             (len(ids) - 2, 2))
            self.assertEqual((attempts("01W000"), attempts("01W001")), (1, 1))
            runs = 1
            while not self.store.reread_gaps(tsp.DRIVE, self.root_path()):
                self.assertLess(runs, 2 * cs.REREAD_MAX_ATTEMPTS)   # bounded
                clock.now += timedelta(hours=cs.REREAD_BACKOFF_HOURS * 2 ** attempts(
                    "01W000"))
                # a new file each run: the source serves downloads in this run
                self.drive.file(f"01N{runs:03d}", f"note-{runs}.txt", "01AOS",
                                b"plain text %d" % runs, mime="text/plain")
                self.capture().run(tsp.scope())
                runs += 1
            self.assertEqual(self.store.current(tsp.DRIVE, "01W001")["text_status"],
                             ct.TEXT_EXTRACTED)
            gaps = self.store.reread_gaps(tsp.DRIVE, self.root_path())
            self.assertEqual([(g["l1_handle"], g["attempts"], g["last_error"]) for g in gaps],
                             [(self.handle("01W000"), cs.REREAD_MAX_ATTEMPTS, "http_500")])
            self.assertEqual(tried.count("01W000"), cs.REREAD_MAX_ATTEMPTS)
            tried.clear()
            clock.now += timedelta(days=365)
            self.capture().run(tsp.scope())
            self.assertNotIn("01W000", tried)   # never retried after the gap
        # In a quiet run (no download succeeds, none did since it last failed) a lone 5xx
        # is not told from an outage: no try is counted, nothing waits behind it.
        self.tearDown()
        self.setUp()
        self.seed(1)
        self.old_reader_capture()
        with mock.patch.object(SharePointReader, "get_content",
                               side_effect=SourceUnavailable("download returned HTTP 502")):
            clock = self.store._clock
            for _ in range(2):
                clock.now += timedelta(hours=1)
                counts = self.root(self.capture().run(tsp.scope()))["counts"]
                # both stale files tried once each, under the outage streak
                self.assertEqual(counts["texts_reread_refetch_failed"], 2)
        self.assertEqual((attempts("01W000"), attempts("01DOC")), (0, 0))

    def test_a_file_that_keeps_failing_alone_never_blocks_the_others_and_ends_a_named_gap(
            self):
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import SourceUnavailable
        ids = self.seed(3)
        self.old_reader_capture()
        real = SharePointReader.get_content
        tried: list[str] = []

        def poisoned(reader, actor, item_id, **kwargs):
            tried.append(item_id)
            if item_id == "01W000":   # the oldest stale version: first in line
                raise SourceUnavailable("drive item lacks a download URL")
            return real(reader, actor, item_id, **kwargs)

        clock = self.store._clock
        with mock.patch.object(SharePointReader, "get_content", poisoned):
            counts = self.root(self.capture().run(tsp.scope()))["counts"]
            # Every other file is read in the same run.
            self.assertEqual((counts["texts_reread"], counts["texts_reread_refetch_failed"]),
                             (len(ids) - 1, 1))
            for i in ids[1:]:
                self.assertEqual(self.store.current(tsp.DRIVE, i)["text_status"],
                                 ct.TEXT_EXTRACTED, i)
            # Backing off: the next run does not fetch it again.
            tried.clear()
            self.capture().run(tsp.scope())
            self.assertNotIn("01W000", tried)
            # Past each backoff it is tried again, until it is given up as a named gap.
            for attempt in range(2, cs.REREAD_MAX_ATTEMPTS + 1):
                clock.now += timedelta(hours=cs.REREAD_BACKOFF_HOURS * 2 ** (attempt - 1))
                tried.clear()
                counts = self.root(self.capture().run(tsp.scope()))["counts"]
                self.assertEqual(tried.count("01W000"), 1, attempt)
                self.assertEqual(counts["texts_reread_gaps"],
                                 int(attempt == cs.REREAD_MAX_ATTEMPTS))
            gaps = self.store.reread_gaps(tsp.DRIVE, self.root_path())
            self.assertEqual([(g["l1_handle"], g["attempts"], g["last_error"]) for g in gaps],
                             [(self.handle("01W000"), cs.REREAD_MAX_ATTEMPTS, "unclassified")])
            clock.now += timedelta(days=365)
            tried.clear()
            self.capture().run(tsp.scope())
            self.assertNotIn("01W000", tried)   # bounded: never retried after the gap
        self.assertEqual(self.store.current(tsp.DRIVE, "01W000")["text_status"], "failed")

    def root_path(self) -> str:
        return self.store.db.execute("SELECT root_path FROM items WHERE item_id='01W000'"
                                     ).fetchone()[0]

    def test_a_refetch_that_does_not_hash_to_the_pin_is_not_read(self):
        self.seed(1)
        self.old_reader_capture()
        self.drive.items[f"{tsp.DRIVE}:01W000"]["content"] = workbook([("X", rows(2))])
        run = self.capture().run(tsp.scope())
        counts = self.root(run)["counts"]
        self.assertGreaterEqual(counts["texts_reread_refetch_failed"], 1)
        self.assertEqual(self.store.current(tsp.DRIVE, "01W000")["text_status"], "failed")
        # Bytes other than the pin are permanent: never fetched again this generation.
        downloads = self.drive.downloads()
        self.capture().run(tsp.scope())
        self.assertEqual(self.drive.downloads(), downloads)


if __name__ == "__main__":
    unittest.main()
