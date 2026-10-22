"""The SharePoint read budget: the shared walker's rules
that let a large scope fit a live check's time budget, over the injected Graph fakes of
integrations/tests/test_capture_sharepoint.py and tests/test_case_originals.py. No
network, no credentials. Evidence: injected-transport.

An unchanged version is not read again (capture store and case-originals receipts), a
cut root is resumed before any other, a throttled read waits the source's Retry-After
once and only within the budget, and text extraction on worker threads loses or
duplicates no version."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations import capture_sharepoint as cs
from integrations.capture_sharepoint import CaptureScope
from integrations.tests import test_capture_sharepoint as fake
from integrations.tests.test_capture_sharepoint import DRIVE, QUALITY_FOLDER, CaptureCase
from tests import test_case_originals as originals

RESTRICTED = "/Restricted"


class ThrottlingDrive(fake.FakeDrive):
    """Answers an item's metadata read with 429 and a Retry-After ``throttle[id]`` times,
    and the next ``throttle_listings`` folder listings likewise."""

    def __init__(self) -> None:
        super().__init__()
        self.throttle: dict[str, int] = {}
        self.throttle_listings = 0
        self.throttle_feeds = False   # every change-feed read refused
        self.retry_after = "30"

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        path = url.split("?")[0]
        if ((path.endswith("/children") and self.throttle_listings)
                or (path.endswith("/delta") and self.throttle_feeds)):
            if not path.endswith("/delta"):
                self.throttle_listings -= 1
            self.calls.append((method, url))
            return 429, {"Retry-After": self.retry_after}, json.dumps(
                {"error": {"code": "TooManyRequests"}}).encode()
        segments = url.split("?")[0].split("/")
        if "items" in segments and segments.index("items") == len(segments) - 2:
            iid = segments[-1]
            if self.throttle.get(iid):
                self.throttle[iid] -= 1
                self.calls.append((method, url))
                return 429, {"Retry-After": self.retry_after}, json.dumps(
                    {"error": {"code": "TooManyRequests"}}).encode()
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class WalkerBudgetTests(CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.drive = fake.seeded_drive(ThrottlingDrive())
        self.slept: list[float] = []

    def versions(self) -> list[tuple[str, str]]:
        return [tuple(r) for r in self.store.db.execute(
            "SELECT item_id, etag FROM items ORDER BY item_id, etag").fetchall()]

    def test_an_unchanged_version_is_not_read_again_and_a_changed_one_is(self):
        first = self.root(self.capture().run(fake.scope()))
        self.assertEqual((first["complete"], first["counts"]["versions_new"]), (True, 3))
        downloads, kept = self.drive.downloads(), self.versions()
        # A new capture over the persisted store: every listed eTag is known, so no
        # content, text or version is read or written again.
        again = self.root(self.capture().run(fake.scope()))
        self.assertEqual(self.drive.downloads(), downloads)
        self.assertEqual((again["counts"]["versions_new"], again["counts"]["unchanged"]),
                         (0, 3))
        self.assertEqual(self.versions(), kept)
        self.drive.change("01FILEB", b"lot release revised")
        start = len(self.drive.calls)
        changed = self.root(self.capture().run(fake.scope()))
        self.assertEqual(self.drive.downloads(), downloads + 1)
        # The changed file's reads: its permissions once (the listing's), its metadata
        # twice (the download URL and the eTag re-read after the bytes), its bytes once.
        reads = [u.split("?")[0] for _m, u in self.drive.calls[start:] if "01FILEB" in u]
        self.assertEqual((sum(u.endswith("/01FILEB/permissions") for u in reads),
                          sum(u.endswith("/items/01FILEB") for u in reads), len(reads)),
                         (1, 2, 4))
        self.assertEqual((changed["counts"]["versions_new"], changed["counts"]["unchanged"],
                          changed["counts"]["versions_superseded"]), (1, 2, 1))
        # Read back: a capture actor sees the new version, an actor without a grant none.
        current = self.store.current(DRIVE, "01FILEB")
        self.assertEqual(current["etag"], '"{01FILEB},2"')
        withheld = self.store.index(fake.OUTSIDER, fake.reader_audience(), limit=100)
        self.assertEqual(withheld["items"], [])

    def test_the_walk_resumes_the_cut_root_before_any_other(self):
        self.drive.file("01RFILE", "r.txt", "01RESTRICTED", b"restricted", mime="text/plain")
        two = CaptureScope([{"id": "two", "drive_id": DRIVE,
                             "folders": [RESTRICTED, QUALITY_FOLDER], "max_depth": 3,
                             "max_folders": 50}])
        # One second per download, three a run: the first root (one file) completes, the
        # second is cut inside its walk with two of its three files read.
        run = self.capture(budget_seconds=3, monotonic=self.drive.downloads).run(two)
        self.assertEqual([(r["complete"], r["reason"]) for r in run["targets"][0]["roots"]],
                         [(True, None), (False, cs.TIME_BUDGET)])
        start = len(self.drive.calls)
        run = self.capture(budget_seconds=60, monotonic=self.drive.downloads).run(two)
        listings = [u for _m, u in self.drive.calls[start:]
                    if u.split("?")[0].endswith("/children")]
        self.assertNotIn("/items/01RESTRICTED/", listings[0])   # the cut root first
        restricted, quality = run["targets"][0]["roots"]
        self.assertEqual((quality["complete"], quality["full_walk_reason"]), (True, "resumed"))
        self.assertEqual(quality["counts"]["versions_new"], 1)
        self.assertTrue(restricted["complete"])
        self.assertEqual(self.drive.downloads(), 4)   # every file read once

    def test_a_throttled_read_waits_its_retry_after_once_and_only_within_the_budget(self):
        # Refused on the first attempt and both client retries: the capture waits the
        # source's 30 s once (the run has 100 s left) and reads the file.
        self.drive.throttle["01FILEA"] = 3
        capture = self.capture(budget_seconds=100, monotonic=lambda: 0.0,
                               sleep=self.slept.append)
        root = self.root(capture.run(fake.scope()))
        self.assertEqual(self.slept, [30.0])
        self.assertEqual((capture.retry_after_waits, capture.retry_after_seconds), (1, 30.0))
        self.assertEqual(self.store.current(DRIVE, "01FILEA")["content_status"], cs.CAPTURED)
        self.assertTrue(root["complete"])
        # Refused on every attempt: each read of it (the walk's and the repair's) waits
        # once, never twice, and the file is left degraded for a later run.
        self.drive.change("01FILEA", b"%PDF-1.7 scan v2")
        self.drive.throttle["01FILEA"] = 100
        capture = self.capture(budget_seconds=100, monotonic=lambda: 0.0,
                               sleep=self.slept.append)
        root = self.root(capture.run(fake.scope()))
        refused = 100 - self.drive.throttle["01FILEA"]
        self.assertEqual(refused, 2 * 3 * capture.retry_after_waits)   # 3 attempts a call
        self.assertEqual(self.slept, [30.0] * (1 + capture.retry_after_waits))
        self.assertTrue(self.store.current(DRIVE, "01FILEA")["content_status"]
                        .startswith("unavailable:"))
        self.assertEqual((root["complete"], root["reason"]), (False, "content_incomplete"))
        # A wait longer than the budget left is never taken.
        self.drive.change("01FILEA", b"%PDF-1.7 scan v3")
        self.drive.throttle["01FILEA"] = 100
        waited = len(self.slept)
        capture = self.capture(budget_seconds=10, monotonic=lambda: 0.0,
                               sleep=self.slept.append)
        capture.run(fake.scope())
        self.assertEqual((len(self.slept), capture.retry_after_waits), (waited, 0))
        self.assertTrue(self.store.current(DRIVE, "01FILEA")["content_status"]
                        .startswith("unavailable:"))

    def test_a_wait_before_the_walk_moved_is_held_to_its_slice_not_only_the_budget(self):
        # The root listing is refused (first attempt and both client retries) before the
        # walk read a child, so its slice cannot cut it yet; the run has 100 s, the one
        # root's first-pass slice is half of it (capture_sharepoint.FIRST_WALK_PASS_SHARE).
        # A Retry-After within that slice is waited once; one past it is never waited,
        # though the run's budget could hold it: one throttled root never spends more
        # than its share on a wait.
        share = 100 * cs.FIRST_WALK_PASS_SHARE
        for retry_after, waited in ((share - 10, True), (share + 10, False)):
            self.drive.throttle_listings = 3
            self.drive.retry_after, slept = str(int(retry_after)), []
            capture = self.capture(budget_seconds=100, monotonic=lambda: 0.0,
                                   sleep=slept.append)
            root = self.root(capture.run(fake.scope()))
            self.assertEqual(self.drive.throttle_listings, 0)   # all three refused
            self.assertEqual((slept, capture.retry_after_waits),
                             (([float(int(retry_after))], 1) if waited else ([], 0)),
                             retry_after)
            self.assertEqual(root["complete"], waited, retry_after)

    def test_a_walk_the_budget_ends_before_its_first_listing_is_typed_start_cut(self):
        # The drive's change feed refuses every read with a wait no budget holds, so the
        # walk reads its own cursor as it starts; the clock (one second a feed read past
        # the run's own six: two reads, three attempts each) ends the budget there,
        # before the walk listed any folder. It keeps no position (the next run walks it
        # from the start), and its cause is start_cut, never finish_cut (a walk that
        # listed everything and was cut while reconciling).
        self.drive.throttle_feeds, self.drive.retry_after = True, "9999"
        feeds = lambda: sum(1 for _m, u in self.drive.calls
                            if u.split("?")[0].endswith("/delta"))
        before = feeds()
        run = self.capture(budget_seconds=1, monotonic=lambda: max(0, feeds() - before - 6),
                           sleep=self.slept.append).run(fake.scope())
        root = self.root(run)
        self.assertEqual(feeds() - before, 9)   # the run's two reads, the walk's own
        self.assertEqual((root["round"], root["reason"], root.get("walk_position"),
                          root["counts"]["folders_listed"]), ("full", cs.TIME_BUDGET, None, 0))
        self.assertEqual(cs.budget_cut_causes(run), {"named": {
            "unreached": 0, "start_cut": 1, "walk_cut": 0, "finish_cut": 0}})
        self.assertEqual(self.slept, [])
        # The next run, the feed readable again, walks the root from its start whole.
        self.drive.throttle_feeds = False
        again = self.root(self.capture().run(fake.scope()))
        self.assertEqual((again["complete"], again["full_walk_reason"]), (True, "first_walk"))

    def test_extraction_on_worker_threads_records_every_version_once(self):
        """N = 1, 10, 60 new files a walk: each version recorded once with its text,
        whatever the number of extraction workers; a budget cut mid-walk resumes."""
        self.drive.folder("01BULK", "Bulk", parent="01AOS")
        total = 3
        for n in (1, 10, 60):
            for i in range(n):
                self.drive.file(f"01B{n:02d}{i:02d}", f"b{n}-{i}.txt", "01BULK",
                                f"bulk {n} {i}".encode(), mime="text/plain")
            total += n
            start = self.drive.downloads()
            for workers in (3, 1):
                self.capture(extract_workers=workers).run(fake.scope())
            self.assertEqual(self.drive.downloads() - start, n if total > 3 + n else total)
            rows = self.store.db.execute(
                "SELECT item_id, text_status, text_sha256 FROM items "
                "WHERE superseded_at IS NULL").fetchall()
            self.assertEqual(len(rows), total)
            self.assertEqual(len({r[0] for r in rows}), total)
            self.assertTrue(all(r[1] == "extracted" and r[2] for r in rows
                                if r[0].startswith("01B")))
        # Cut by the budget every few downloads: the resumed walks still record each once.
        for i in range(12):
            self.drive.change(f"01B60{i:02d}", f"bulk revised {i}".encode())
        for _ in range(6):
            self.capture(budget_seconds=3, monotonic=self.drive.downloads,
                         extract_workers=3).run(fake.scope())
        changed = self.store.db.execute(
            "SELECT item_id, COUNT(*) FROM items WHERE item_id LIKE '01B60%' "
            "GROUP BY item_id").fetchall()
        self.assertEqual(sorted(c for _i, c in changed), [1] * 48 + [2] * 12)
        self.assertIsNone(self.store.walk_position(DRIVE, QUALITY_FOLDER))


class CaseOriginalsReuseTests(unittest.TestCase):
    """A discovered watch-folder file read in full by an earlier run is not read again
    while its eTag, size and stored bytes are unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        registry = originals.co.build_registry(
            json.dumps(originals.record()).encode(), advisory_commit=originals.COMMIT,
            advisory_path="context/x.json", delta_bytes=json.dumps(originals.delta()).encode(),
            delta_commit="d" * 40, delta_path="context/y.json",
            watch_links=[originals.PROCESS],
            watch_drives=[(originals.DRIVE, "/Examples/F", originals.ART_URL)])
        self.path = self.root / "case_originals.json"
        self.path.write_text(json.dumps(registry), encoding="utf-8")

    def run_read(self, tenant, run_id, **kwargs):
        return originals.co.read(
            originals.ENV, registry_path=self.path, state_dir=self.root / "state",
            run_id=run_id, role="verifier", environment="PLX development", sender=tenant,
            ocr=lambda image, timeout=None: originals.OCR_TEXT, **kwargs)

    def receipt(self, row_id):
        return json.loads((self.root / "state" / "case-originals" / "receipts" / "discovered"
                           / f"{row_id}.json").read_text(encoding="utf-8"))

    def test_an_unchanged_discovered_file_is_reused_and_a_changed_one_read(self):
        def read(tenant, run_id):
            report = self.run_read(tenant, run_id=run_id, reuse_unchanged=True)
            downloads = {i: sum(f"i={i}&" in u for _m, u in tenant.calls)
                         for i in ("01NEW", "01FNEW")}
            return report, downloads

        first, downloads = read(originals.Tenant(), "run-1")
        self.assertEqual((first["ok"], first["discovered_reused"], downloads),
                         (True, 0, {"01NEW": 1, "01FNEW": 1}))
        second, downloads = read(originals.Tenant(), "run-2")
        self.assertEqual((second["ok"], second["discovered_reused"], downloads),
                         (True, 2, {"01NEW": 0, "01FNEW": 0}))
        self.assertEqual(second["by_status"], first["by_status"])
        receipt = self.receipt(co_id("01NEW"))
        self.assertEqual((receipt["body_status"], receipt["reused"]["basis"],
                          receipt["reused"]["reader"]["run_id"], receipt["reader"]["run_id"]),
                         ("read", "etag_unchanged", "run-2", "run-1"))
        # 01NEW changed at the source (new eTag); 01FNEW's stored bytes were altered: both
        # are read again.
        tenant = ChangedTenant({"01NEW"})
        tenant.files[(originals.DRIVE, "01NEW")] = (
            *tenant.files[(originals.DRIVE, "01NEW")][:3], b"<p>new, revised</p>")
        stored = Path(self.root / "state" / receipt["stored"].replace(
            "case-originals/", "case-originals/", 1))
        fnew = self.receipt(co_id("01FNEW"))
        (self.root / "state" / fnew["stored"]).write_bytes(b"altered")
        third, downloads = read(tenant, "run-3")
        self.assertEqual((third["ok"], third["discovered_reused"], downloads),
                         (True, 0, {"01NEW": 1, "01FNEW": 1}))
        self.assertTrue(stored.exists())
        self.assertNotIn("reused", self.receipt(co_id("01NEW")))


def co_id(item: str) -> str:
    return originals.co._id("nr", f"{originals.DRIVE}/{item}")


class ChangedTenant(originals.Tenant):
    def __init__(self, bumped: set[str]) -> None:
        super().__init__()
        self.bumped = bumped

    def item(self, drive, item):
        body = super().item(drive, item)
        if item in self.bumped:
            body["eTag"] = f"\"{{{item}}},2\""
        return body


if __name__ == "__main__":
    unittest.main()
