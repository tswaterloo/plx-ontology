"""Named items first (integrations/capture_sharepoint.py, ``named_items``): the items a
caller names (the case originals' source items) are captured before any root is walked,
read by id with their item permissions, recorded exactly as the walk records them, only
within the scope and the people exclusion, without moving any root's state; typed per
item, idempotent and resumable. Over the estate fake (injected Graph transport).
No network, no credentials. Evidence: injected-transport."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from urllib.parse import unquote

from integrations import capture_sharepoint as cs
from integrations import graph_read
from integrations.capture_sharepoint import (CaptureScope, SharePointCapture,
                                             SharePointCaptureStore)
from integrations.graph_read import SharePointReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests import test_capture_sharepoint as ts
from integrations.tests import test_capture_sharepoint_estate as te

EXCLUDED_SITE = (f"{te.MY_HOST},99999999-0000-0000-0000-000000000001,"
                 "99999999-0000-0000-0000-000000000002")
EXCLUDED_DRIVE = "b!excluded-Drive_77"
NAMED = [(ts.DRIVE, "01FILEA"), (ts.DRIVE, "01FILEB"), (te.PERSONAL_DRIVE, "01MINE")]
ROOT_TABLES = ("walk_positions", "root_cursors", "root_permissions", "walked_folders",
               "root_walks")


class Cut:
    """A monotonic clock the test spends at once after the ``after``-th item read by id,
    so a run does its named pass and then defers every root (nothing walked)."""

    def __init__(self, after: int) -> None:
        self.now, self.after, self.reads = 0.0, after, 0

    def __call__(self) -> float:
        return self.now

    def read(self) -> None:
        self.reads += 1
        if self.reads >= self.after:
            self.now = 1e9


class _Reader:
    """A SharePointReader whose item reads by id tick ``cut`` (everything else as is)."""

    def __init__(self, inner, cut):
        self._inner, self._cut = inner, cut

    def get_item(self, actor, item_id):
        try:
            return self._inner.get_item(actor, item_id)
        finally:
            if self._cut is not None:
                self._cut.read()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class NamedCase(te.EstateCase):
    def setUp(self) -> None:
        super().setUp()
        te.SITES[EXCLUDED_SITE] = f"https://{te.MY_HOST}/personal/{te.EXCLUDED_PATH}"
        self.addCleanup(te.SITES.pop, EXCLUDED_SITE)
        self.drive.sites.append(EXCLUDED_SITE)
        self.drive.libraries = {**te.LIBRARIES, EXCLUDED_SITE: [(EXCLUDED_DRIVE, "Documents")]}

    def named_capture(self, named, *, store=None, cut=None, **kwargs) -> SharePointCapture:
        api, transport = graph_read.build_graph(
            ts.ENV, sender=self.drive,
            retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

        def reader(drive_id):
            return _Reader(SharePointReader(api, ts.capture_audience(), drive_id,
                                            transport=transport, read_permissions=True,
                                            max_acl_reads=cs.MAX_ACL_READS), cut)

        if cut is not None:
            # Room for an OCR read (``OCR_RESERVE_SECONDS``), as an unbudgeted walk has.
            kwargs.update(budget_seconds=10_000, monotonic=cut)
        capture = SharePointCapture(self.store if store is None else store, reader,
                                    ts.CAPTURE, estate=graph_read.SharePointSites(api, ts.SP_HOST),
                                    **kwargs)
        capture.named_items = list(named)
        return capture

    def table(self, name, store=None):
        return (store or self.store).db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]

    def walked_store(self):
        """A second store the estate was walked into whole, without named items."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SharePointCaptureStore(Path(tmp.name) / "state", clock=ts.Clock())
        self.addCleanup(store.close)
        run = self.named_capture((), store=store).run(te.estate_scope())
        self.assertTrue(all(r["status"] == "complete" for r in self.roots(run).values()
                            if r["reason"] is None), run)
        return store


class NamedItemsTests(NamedCase):
    def test_named_items_are_recorded_as_the_walk_records_them_before_any_root(self):
        cut = Cut(after=len(NAMED))
        start = len(self.drive.calls)
        run = self.named_capture(NAMED, cut=cut).run(te.estate_scope())
        named = run["named_items"]
        self.assertEqual(named["outcomes"]["captured"], 3, named)
        self.assertEqual(sum(named["outcomes"].values()), 3, named)
        # By id only: no folder listing, no change feed, no walk.
        urls = [unquote(u) for _m, u in self.drive.calls[start:]]
        self.assertFalse([u for u in urls if "/children" in u or "/delta" in u], urls)
        # No root moved: no walk position, cursor, permission position or walked folder,
        # and every root the run reached was deferred, never started or completed.
        for table in ROOT_TABLES:
            self.assertEqual(self.table(table), 0, table)
        self.assertEqual({(r["status"], r["reason"]) for r in self.store.db.execute(
            "SELECT status, reason FROM roots")}, {("incomplete", cs.TIME_BUDGET)})
        # The same version row a full walk writes: record, keys, item permissions, bytes
        # and text.
        walked = self.walked_store()
        columns = ("l1_handle", "root_path", "path", "record", "resources", "source_acl",
                   "content_status", "content_sha256", "text_status", "text_sha256")
        for drive_id, item_id in NAMED:
            mine, theirs = self.store.current(drive_id, item_id), walked.current(drive_id,
                                                                                 item_id)
            self.assertEqual({c: mine[c] for c in columns}, {c: theirs[c] for c in columns})
            acl = json.loads(mine["source_acl"])
            self.assertEqual(acl["status"], "read")
            self.assertTrue(acl["identities"])
        # The walk that follows finds them captured: one version each, nothing re-read.
        again = self.named_capture(()).run(te.estate_scope())
        self.assertTrue(self.roots(again)[ts.DRIVE]["complete"], again)
        for drive_id, item_id in NAMED:
            self.assertEqual(self.store.db.execute(
                "SELECT COUNT(*) FROM items WHERE drive_id=? AND item_id=?",
                (drive_id, item_id)).fetchone()[0], 1)

    def test_an_item_outside_the_scope_or_in_an_excluded_onedrive_is_never_read(self):
        outside = [(te.HR_DRIVE, "01HRFILE"),            # its site the scope excludes
                   (EXCLUDED_DRIVE, "01EXCLUDEDFILE"),   # an excluded person's OneDrive
                   ("b!unplanned-Drive_99", "01UNKNOWN"),  # a drive no scope plans
                   (te.ASSETS, "01ASSETFILE")]           # a library the scope excludes
        capture = self.named_capture([*outside, (ts.DRIVE, "01FILEA"), ("", None)])
        run = capture.run(te.estate_scope())
        outcomes = run["named_items"]["outcomes"]
        # Each refusal is named by item on the capture (never in the printed report).
        self.assertEqual(sorted((d, i, o) for d, i, o in capture.named_failures),
                         sorted((d, i, "out_of_scope") for d, i in outside))
        self.assertEqual((outcomes["out_of_scope"], outcomes["captured"],
                          outcomes["invalid"]), (4, 1, 1), outcomes)
        urls = " ".join(unquote(u) for _m, u in self.drive.calls)
        for drive_id, item_id in outside:
            self.assertNotIn(item_id, urls)
            self.assertNotIn(drive_id, urls)
            self.assertIsNone(self.store.current(drive_id, item_id))
        # The report a CLI prints names no item, drive or path.
        printed = json.dumps(cs._report_run(1, run))
        for drive_id, item_id in outside + [(ts.DRIVE, "01FILEA")]:
            self.assertNotIn(item_id, printed)
        self.assertIn("named_items", printed)

    def test_failures_are_typed_per_item_and_an_item_outside_the_roots_is_not_recorded(self):
        # A named target holding only the Lots folder: a file elsewhere in the drive is read
        # (its path is known only then) and never recorded; a missing item is typed.
        scope = CaptureScope([{"id": "lots", "drive_id": ts.DRIVE,
                               "folders": ["/Quality/AOs/Lots"], "max_depth": 3,
                               "max_folders": 50}])
        capture = self.named_capture([(ts.DRIVE, "01FILEA"), (ts.DRIVE, "01MISSING"),
                                      (ts.DRIVE, "01FILEB")])
        start = len(self.drive.calls)
        run = capture.run(scope)
        outcomes = run["named_items"]["outcomes"]
        self.assertEqual(outcomes["outside_root"], 1, outcomes)
        self.assertEqual(outcomes["captured"], 1, outcomes)
        failures = {item: outcome for _d, item, outcome in capture.named_failures}
        self.assertEqual(failures["01FILEA"], "outside_root")
        self.assertIn(failures["01MISSING"], ("not_found", "not_visible"))
        self.assertNotIn("01FILEB", failures)
        self.assertIsNone(self.store.current(ts.DRIVE, "01FILEA"))
        content = [u for _m, u in self.drive.calls[start:] if "01FILEA" in unquote(u)
                   and "/content" in unquote(u)]
        self.assertEqual(content, [])

    def test_the_pass_is_idempotent_and_a_cut_pass_resumes_with_the_items_not_held(self):
        first = self.named_capture(NAMED, cut=Cut(after=1)).run(te.estate_scope())
        outcomes = first["named_items"]["outcomes"]
        self.assertEqual((outcomes["captured"], outcomes[cs.TIME_BUDGET]), (1, 2), outcomes)
        second = self.named_capture(NAMED, cut=Cut(after=2)).run(te.estate_scope())
        outcomes = second["named_items"]["outcomes"]
        # The one held is not read again; the two left are read first.
        self.assertEqual((outcomes["held_fresh"], outcomes["captured"]), (1, 2), outcomes)
        idle = Cut(after=1)
        third = self.named_capture(NAMED, cut=idle).run(te.estate_scope())
        self.assertEqual(third["named_items"]["outcomes"]["held_fresh"], 3)
        self.assertEqual(idle.reads, 0)   # none read by id again
        for drive_id, item_id in NAMED:
            self.assertEqual(self.store.db.execute(
                "SELECT COUNT(*) FROM items WHERE drive_id=? AND item_id=?",
                (drive_id, item_id)).fetchone()[0], 1)


class NamedOriginalsCliTests(NamedCase):
    def test_the_cli_reads_the_case_originals_items_first_and_prints_counts_only(self):
        import argparse
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            receipts = state / "case-originals" / "receipts" / ("f" * 64)
            receipts.mkdir(parents=True)
            for number, (drive_id, item_id) in enumerate(NAMED):
                content = f"brief {number}".encode()
                digest = hashlib.sha256(content).hexdigest()
                (state / "case-originals" / digest).write_bytes(content)
                entry = f"co-{number:012x}"
                (receipts / f"{entry}.json").write_text(json.dumps({
                    "schema": "plx-case-original-receipt/v1", "entry_id": entry,
                    "body_status": "read", "match": True, "sha256": digest,
                    "stored": f"case-originals/{digest}", "parent": None,
                    "canonical_source": {"drive_id": drive_id, "item_id": item_id}}))
            audience = Path(tmp) / "audience.json"
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": ["svc:sharepoint-capture"]}}))
            args = argparse.Namespace(
                state_dir=str(state), live_audience=str(audience),
                scope=str(Path(cs.REPO_ROOT) / "integrations/sharepoint_estate_scope.json"),
                principal=cs.CAPTURE_PRINCIPAL, withheld_principal="staff:nobody", runs=1,
                max_store_bytes=None, registry=None, budget_seconds=None,
                named_originals=True)
            report = cs.run_cli(args, environ=ts.ENV, sender=self.drive, sleep=lambda s: None)
        self.assertEqual(report["named_items_held"], len(NAMED))
        self.assertEqual(report["runs"][0]["named_items"]["outcomes"], {"captured": 3})
        printed = json.dumps(report)
        for drive_id, item_id in NAMED:
            self.assertNotIn(item_id, printed)
        # The flag is opt-in: a parser without it reads no case originals.
        parser = argparse.ArgumentParser()
        cs.add_cli_arguments(parser)
        self.assertFalse(parser.parse_args(["--live-audience", "a.json"]).named_originals)
