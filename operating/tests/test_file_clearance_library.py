"""``operating.file_clearance_check``'s library-wide DocumentCategory read: one reader
path (``SharePointReader.library_item_fields``) behind the drive gate, and a typed reason
for every file it leaves to its own read. Injected transport only (the capture fake)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from verify import live_verify as lv
from integrations import graph_read
from integrations.graph_read import SharePointReader
from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience, NotVisible,
                                            SourceUnavailable)
from integrations import capture_sharepoint as cs
from integrations.tests.test_capture_sharepoint import DRIVE, ENV, RICARDO, SP_HOST, j
from ontology.core import Actor
from operating import file_clearance_check
from operating.tests.test_employee_permissions import (column_reads, columns_drive,
                                                       run_clearance_check)

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "verify/live-checks/sharepoint.restricted_class.json")
                  .read_text(encoding="utf-8"))


def customers_drive(n: int = 3, **setup):
    drive = columns_drive()
    drive.folder("01CUST", "Customers")
    for i in range(n):
        drive.file(f"01F{i:03d}", f"f{i}.pdf", "01CUST", acl=(RICARDO,))
    drive.categories = {"01F000": "TAX_FORM"}
    for name, value in setup.items():
        setattr(drive, name, value)
    return drive


class LibraryReadTests(unittest.TestCase):
    def test_the_library_read_passes_the_reader_gate(self):
        # The drive gate refuses once the capture is stored (a grant revoked between the
        # capture and the column read): the check reports the refusal and reads no list.
        real_capture = file_clearance_check.cs.run_cli

        def capture_then_revoke(*args, **kwargs):
            out = real_capture(*args, **kwargs)
            revoke = mock.patch.object(SharePointReader, "_gate", side_effect=NotVisible)
            revoke.start()
            self.addCleanup(revoke.stop)
            return out

        drive = customers_drive()
        with mock.patch.object(file_clearance_check.cs, "run_cli", capture_then_revoke):
            report = run_clearance_check(drive)
        self.assertEqual(column_reads(drive), {"list": 0, "pages": 0, "per_file": 0})
        self.assertFalse(report["ok"])
        self.assertEqual(report["listitem_failures"], {"NotVisible": 3, "pages.NotVisible": 1})
        self.assertEqual(report["listitem_fallbacks"], {"NotVisible": 3})
        self.assertEqual((report["files_classified"], report["listitem_pages_read"]), (0, 0))
        self.assertIn("listitem_failures not empty", lv.check_expectations(SPEC, 1, report))

    def test_each_file_left_to_its_own_read_is_counted_by_why(self):
        # The page read refused (400) and every file read on its own: the check passes,
        # and the refusal is recorded rather than masked by the fallback's success.
        drive = customers_drive(pages_status=400)
        report = run_clearance_check(drive)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["listitem_fallbacks"], {"SourceUnavailable.http_400": 3})
        self.assertEqual(column_reads(drive), {"list": 1, "pages": 1, "per_file": 3})
        self.assertEqual(lv.check_expectations(SPEC, 0, report), [])
        self.assertEqual(lv._recorded(SPEC, report)["listitem_fallbacks"],
                         {"SourceUnavailable.http_400": 3})
        # A library past the page cap: what the pages bound stays bound, the rest read
        # on its own under page_cap (the folder is a row: 24 files on the first page).
        drive = customers_drive(30)
        with mock.patch.object(file_clearance_check, "_MAX_PAGES", 1):
            report = run_clearance_check(drive)
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["listitem_fallbacks"], report["listitem_pages_read"],
                          report["files_classified"], report["class_counts"]["FINANCE"]),
                         ({"page_cap": 6}, 1, 30, 1))
        self.assertEqual(column_reads(drive), {"list": 1, "pages": 1, "per_file": 6})
        self.assertTrue(all(method == "GET" for method, _ in drive.calls))
        # All bound by the pages: nothing to name.
        report = run_clearance_check(customers_drive())
        self.assertEqual((report["ok"], report["listitem_fallbacks"]), (True, {}))


def content_gets(drive) -> int:
    """The content GETs a run sent: download-URL reads and downloads."""
    return sum(1 for _m, url in drive.calls
               if urlsplit(url).hostname == SP_HOST or "content.downloadUrl" in url)


class MetadataOnlyCaptureTests(unittest.TestCase):
    """The check reads metadata, permissions and eTags only: its capture sends no content
    GET, so a scope whose downloads would outrun the capture budget completes."""

    def run_check(self, drive, *, capture_budget, download_seconds, state_dir=None):
        clock = [0.0]

        def sender(method, url, headers, body, timeout, max_bytes):
            if urlsplit(url).hostname == SP_HOST:
                clock[0] += download_seconds   # each download spends this much time
            return drive(method, url, headers, body, timeout, max_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            scope, audience = Path(tmp) / "scope.json", Path(tmp) / "audience.json"
            scope.write_text(json.dumps({
                "schema": "plx-sharepoint-capture-scope/v1", "targets": [
                    {"id": "customers", "drive_id": DRIVE, "folders": ["/Customers"],
                     "max_depth": 2, "max_folders": 10}]}), encoding="utf-8")
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": ["svc:sharepoint-capture"]}}), encoding="utf-8")
            return file_clearance_check.run(argparse.Namespace(
                state_dir=str(state_dir or Path(tmp) / "state"), scope=str(scope),
                live_audience=str(audience), principal="svc:sharepoint-capture",
                capture_budget_seconds=capture_budget, budget_seconds=None),
                environ=ENV, sender=sender, sleep=lambda s: None,
                monotonic=lambda: clock[0])

    def test_content_gets_cannot_exhaust_the_capture_budget(self):
        # Three downloads of 600 s each would outrun a 1,000 s capture budget (the live
        # failure: the walk stopped at ~1,222 of 1,309 files): none is sent.
        drive = customers_drive()
        report = self.run_check(drive, capture_budget=1000, download_seconds=600)
        self.assertEqual(content_gets(drive), 0)
        self.assertTrue(all(method == "GET" for method, _ in drive.calls))
        self.assertTrue(report["capture_complete"], report)
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["files_current"], report["files_classified"],
                          report["files_over_bound"]), (3, 3, 0))
        self.assertEqual(lv.check_expectations(SPEC, 0, report), [])
        recorded = lv._recorded(SPEC, report)
        self.assertEqual((recorded["capture_complete"], recorded["files_over_bound"]),
                         (True, 0))
        # The spec names why a run fails: an incomplete capture fails it.
        self.assertIn("capture_complete", SPEC["record"])
        self.assertIn("files_over_bound", SPEC["record"])
        failed = lv.check_expectations(SPEC, 1, {**report, "ok": False,
                                                  "capture_complete": False})
        self.assertTrue(any("capture_complete" in reason for reason in failed), failed)

    def test_the_capture_never_writes_a_store_another_reader_opens(self):
        # --state-dir may be the Workbench state directory a host reads its SharePoint
        # capture from: the check's metadata-only capture goes to a private directory it
        # removes, and a store already there is left as it was.
        from integrations.tests.test_capture_sharepoint import CAPTURE, Clock, capture_audience
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "workbench"
            host = customers_drive()
            store = cs.SharePointCaptureStore(state, clock=Clock())
            try:
                api, transport = graph_read.build_graph(ENV, sender=host)
                reader = SharePointReader(api, capture_audience(), DRIVE, transport=transport,
                                          read_permissions=True)
                cs.SharePointCapture(store, lambda d: reader, CAPTURE).run(
                    cs.CaptureScope.from_json({
                        "schema": "plx-sharepoint-capture-scope/v1", "targets": [
                            {"id": "customers", "drive_id": DRIVE, "folders": ["/Customers"],
                             "max_depth": 2, "max_folders": 10}]}))
                before = store.db.execute(
                    "SELECT l1_handle, content_status FROM items ORDER BY 1").fetchall()
            finally:
                store.close()
            entries = sorted(p.name for p in state.iterdir())
            drive = customers_drive(4)
            report = self.run_check(drive, capture_budget=1000, download_seconds=600,
                                    state_dir=state)
            self.assertEqual((report["ok"], report["files_current"]), (True, 4))
            self.assertEqual(sorted(p.name for p in state.iterdir()), entries)
            store = cs.SharePointCaptureStore.open_read_only(state)
            try:
                after = store.db.execute(
                    "SELECT l1_handle, content_status FROM items ORDER BY 1").fetchall()
                self.assertEqual([tuple(r) for r in after], [tuple(r) for r in before])
                self.assertNotIn(cs.CONTENT_SKIPPED, {r[1] for r in after})
            finally:
                store.close()
        with tempfile.TemporaryDirectory() as tmp:   # a fresh one: nothing is left in it
            state = Path(tmp) / "state"
            self.assertTrue(self.run_check(customers_drive(), capture_budget=1000,
                                           download_seconds=600, state_dir=state)["ok"])
            self.assertEqual(list(state.iterdir()), [])
            self.assertFalse(cs.store_exists(state))

    def test_no_file_lost_or_duplicated_at_any_scope_size(self):
        for n in (1, 10, 60):
            with self.subTest(files=n):
                drive = customers_drive(n)
                report = self.run_check(drive, capture_budget=1000, download_seconds=600)
                self.assertEqual(content_gets(drive), 0)
                self.assertEqual((report["ok"], report["capture_complete"],
                                  report["files_current"], report["files_classified"]),
                                 (True, True, n, n))

    def test_a_content_run_reads_the_bytes_a_metadata_run_skipped(self):
        # A store a metadata-only run filled is never taken for captured content: a later
        # content run on it reads every skipped file's bytes and is complete then.
        from integrations.tests.test_capture_sharepoint import CAPTURE, Clock, capture_audience
        drive = customers_drive()
        with tempfile.TemporaryDirectory() as tmp:
            store = cs.SharePointCaptureStore(Path(tmp) / "state", clock=Clock())
            try:
                api, transport = graph_read.build_graph(ENV, sender=drive)
                reader = SharePointReader(api, capture_audience(), DRIVE, transport=transport,
                                          read_permissions=True)
                scope = cs.CaptureScope.from_json({
                    "schema": "plx-sharepoint-capture-scope/v1", "targets": [
                        {"id": "customers", "drive_id": DRIVE, "folders": ["/Customers"],
                         "max_depth": 2, "max_folders": 10}]})
                run = cs.SharePointCapture(store, lambda d: reader, CAPTURE,
                                           read_content=False).run(scope)
                self.assertEqual(content_gets(drive), 0)
                rows = store.db.execute("SELECT content_status FROM items").fetchall()
                self.assertEqual({r[0] for r in rows}, {cs.CONTENT_SKIPPED})
                # Listed to the end, but never content-complete for any reader.
                self.assertFalse(run["complete"], run)
                completeness = store.completeness(CAPTURE, capture_audience())
                (root,) = completeness["roots"]
                self.assertFalse(completeness["complete"])
                self.assertEqual((root["complete"], root["metadata_complete"], root["status"],
                                  root["reason"], root["content_skipped"],
                                  root["last_complete_at"]),
                                 (False, True, "incomplete", cs.CONTENT_NOT_READ, 3, None))
                self.assertIsNone(store.walked_at(DRIVE, "/Customers"))
                counts = store.counts(CAPTURE, capture_audience())
                self.assertEqual((counts["contents_captured"], counts["contents_skipped"]),
                                 (0, 3))
                run = cs.SharePointCapture(store, lambda d: reader, CAPTURE).run(scope)
                self.assertEqual(drive.downloads(), 3)
                self.assertTrue(run["complete"], run)
                self.assertEqual(store.counts(CAPTURE, capture_audience())
                                 ["contents_captured"], 3)
                self.assertTrue(store.completeness(CAPTURE, capture_audience())["complete"])
            finally:
                store.close()

    def test_an_unknown_content_mode_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = cs.SharePointCaptureStore(Path(tmp) / "state")
            try:
                for value in (None, 0, "false"):
                    with self.subTest(value=value), self.assertRaises(cs.CaptureError):
                        cs.SharePointCapture(store, lambda d: None, None, read_content=value)
            finally:
                store.close()


class ReaderTests(unittest.TestCase):
    """``SharePointReader.library_item_fields`` itself, over the same fake library."""

    def reader(self, drive, audiences=None):
        api, transport = graph_read.build_graph(ENV, sender=drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        audience = LiveSourceAudience.from_json({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": audiences or {"sharepoint:*": ["svc:reader"]}})
        return SharePointReader(api, audience, DRIVE, transport=transport)

    def read(self, reader, files, **kwargs):
        return reader.library_item_fields(Actor("plx", frozenset({"svc:reader"}), "restricted"),
                                          files, field_names=["DocumentCategory"],
                                          max_pages=kwargs.pop("max_pages", 5), **kwargs)

    def test_binds_by_id_and_etag_and_keeps_what_it_bound(self):
        drive = customers_drive(30)   # the folder is a row: 24 files on the first page
        etags = {item["id"]: item["etag"] for item in drive.items.values()
                 if item["id"].startswith("01F")}
        out = self.read(self.reader(drive),
                        {**etags, "01F001": '"{01F001},9"', "01F002": None, "01ZZZ": '"z"'})
        self.assertEqual((sorted(out["snapshots"]), out["pages_read"], out["stopped"]),
                         (sorted(set(etags) - {"01F001", "01F002"}), 2, None))
        bound = out["snapshots"]["01F000"]
        self.assertEqual((bound["source"], bound["drive_etag"], bound["list_item_id"],
                          bound["fields"]), ("sharepoint-listitem-fields/v1", etags["01F000"],
                                             "01F000", {"DocumentCategory": "TAX_FORM"}))
        self.assertTrue(all(method == "GET" for method, _ in drive.calls))
        # A failure on the second page keeps the 24 files the first one bound.
        first_page = drive.list_page
        drive.list_page = lambda url, d: (j(400, {"error": {"code": "invalidRequest"}})
                                          if "skiptoken" in url else first_page(url, d))
        with self.assertRaises(SourceUnavailable) as caught:
            self.read(self.reader(drive), etags)
        self.assertEqual((len(caught.exception.snapshots), caught.exception.pages_read), (24, 1))
        for failure in (NotVisible(), ValueError("page")):
            with self.subTest(failure=type(failure).__name__):
                reader = self.reader(customers_drive(30))
                real = reader._api.get_page
                pages = iter([real, mock.Mock(side_effect=failure)])
                with mock.patch.object(reader._api, "get_page",
                                       side_effect=lambda url: next(pages)(url)):
                    with self.assertRaises(type(failure)) as caught:
                        self.read(reader, etags)
                self.assertEqual((len(caught.exception.snapshots),
                                  caught.exception.pages_read), (24, 1))

    def test_a_refusing_gate_reads_nothing(self):
        drive = customers_drive()
        for audiences in ({"mail:*": ["svc:reader"]},   # no grant on the drive: the gate
                          {"sharepoint:*": ["svc:reader"],   # narrower than the drive
                           f"sharepoint:{DRIVE}:/Payroll/*": ["staff:hr"]}):
            with self.subTest(audiences=audiences):
                calls = len(drive.calls)
                with self.assertRaises(NotVisible):
                    self.read(self.reader(drive, audiences), {"01F000": '"x"'})
                self.assertEqual(len(drive.calls), calls)


if __name__ == "__main__":
    unittest.main()
