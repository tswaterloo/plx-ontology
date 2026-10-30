"""Versioned SharePoint capture (integrations/capture_sharepoint.py) over an injected
Graph transport: a tiny document library with eTags, permissions and download bytes.
No network, no credentials. Evidence: injected-transport."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
import logging
import os
import re
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

from integrations import capture_sharepoint as cs
from integrations import capture_store, graph_read
from integrations.captured_text import OCR_PENDING
from integrations.capture_mail import CaptureError
from integrations.capture_sharepoint import (CaptureScope, SharePointCapture,
                                             SharePointCaptureStore, version_handle)
from integrations.graph_read import SharePointReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience, NotVisible
from ontology.types import Actor

TENANT_ID = "11111111-2222-3333-4444-555555555555"
SP_HOST = "contoso-plx.sharepoint.com"
SITE_ID = (f"{SP_HOST},11111111-0000-0000-0000-000000000001,"
           "11111111-0000-0000-0000-000000000002")
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": TENANT_ID,
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak",
       "SHAREPOINT_SITE_ID": SITE_ID}
DRIVE = "b!quality-Drive_01"
SITE_DRIVE = "b!agents-Drive_02"
ROOT_ID = "01ROOT"
QUALITY_FOLDER = "/Quality/AOs"
RICARDO = "ricardo@example-plx.com"
SALES = "sales@example-plx.com"
NAME_SECRET = "Secret supplier pricing.pdf"
TEXT_SECRET = "Lot release for PO100234, 1,502 units filled."

# The fixture PDFs are not real PDFs; the optional parser's warnings are noise here.
logging.getLogger("pypdf").setLevel(logging.ERROR)

CAPTURE = Actor("plx", frozenset({"svc:sharepoint-capture"}), "restricted")
QUALITY = Actor("plx", frozenset({"staff:quality"}), "restricted")
EMPLOYEE = Actor("plx", frozenset({"staff:sales"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")


def j(status, value):
    return status, {}, json.dumps(value).encode()


def perms(*emails):
    return [{"roles": ["read"], "grantedToV2": {"user": {"email": e, "id": e}}}
            for e in emails]


class FakeDrive:
    """A tiny Graph document library (two drives, one site); GET only after the token."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.drive_of: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.download_count = 0
        self.fail_download: dict[str, int] = {}   # item id -> HTTP status
        self.download_errors: dict[str, int] = {}  # item id -> downloads cut in transit
        self.extra_children: dict[str, list[dict]] = {}
        self.library_names: dict[str, str] = {}
        for drive in (DRIVE, SITE_DRIVE):
            self.items[f"{drive}:{ROOT_ID}"] = {"id": ROOT_ID, "root": True, "name": "root",
                                                "parent": None, "folder": True}
            self.drive_of[f"{drive}:{ROOT_ID}"] = drive

    # -- mutations
    def folder(self, iid, name, parent=ROOT_ID, drive=DRIVE):
        self.items[f"{drive}:{iid}"] = {"id": iid, "name": name, "parent": parent,
                                        "folder": True, "etag": f'"{{{iid}}},1"'}
        self.drive_of[f"{drive}:{iid}"] = drive

    def file(self, iid, name, parent, content=b"%PDF-1.7 scan", *, version=1,
             mime="application/pdf", acl=(RICARDO,), drive=DRIVE):
        self.items[f"{drive}:{iid}"] = {"id": iid, "name": name, "parent": parent,
                                        "folder": False, "content": content, "mime": mime,
                                        "etag": f'"{{{iid}}},{version}"', "acl": list(acl)}
        self.drive_of[f"{drive}:{iid}"] = drive

    def change(self, iid, content, drive=DRIVE):
        item = self.items[f"{drive}:{iid}"]
        version = int(item["etag"].rsplit(",", 1)[1].rstrip('"')) + 1
        item.update(content=content, etag=f'"{{{iid}}},{version}"')

    def move(self, iid, parent, *, new_etag=False, drive=DRIVE):
        item = self.items[f"{drive}:{iid}"]
        item["parent"] = parent
        if new_etag:
            version = int(item["etag"].rsplit(",", 1)[1].rstrip('"')) + 1
            item["etag"] = f'"{{{iid}}},{version}"'

    def delete(self, iid, drive=DRIVE):
        del self.items[f"{drive}:{iid}"]

    # -- shape
    def path(self, drive, iid) -> str:
        parts = []
        while iid != ROOT_ID:
            item = self.items[f"{drive}:{iid}"]
            parts.append(item["name"])
            iid = item["parent"]
        return "/" + "/".join(reversed(parts))

    def raw(self, drive, iid) -> dict:
        item = self.items[f"{drive}:{iid}"]
        if item.get("root"):
            return {"id": ROOT_ID, "name": "root", "eTag": '"{ROOT},1"', "root": {},
                    "folder": {"childCount": 1}, "parentReference": {"driveId": drive}}
        parent = self.path(drive, item["parent"])
        body = {"id": iid, "name": item["name"], "eTag": item["etag"],
                "cTag": item["etag"].replace('"{', '"c:{'),
                "webUrl": f"https://{SP_HOST}/sites/Quality/Shared%20Documents"
                          + quote(self.path(drive, iid)),
                "createdDateTime": "2026-01-05T10:00:00Z",
                "lastModifiedDateTime": "2026-01-07T22:30:00Z",
                "parentReference": {"driveId": drive, "id": item["parent"],
                                    "path": f"/drives/{drive}/root:"
                                            + ("" if parent == "/" else quote(parent))},
                "lastModifiedBy": {"user": {"email": RICARDO}}}
        if item["folder"]:
            body["folder"] = {"childCount": 1}
        else:
            body["size"] = len(item["content"])
            body["file"] = {"mimeType": item["mime"], "hashes": {
                "sha256Hash": sha256(item["content"]).hexdigest().upper()}}
        return body

    def children(self, drive, iid, start=0, stop=None) -> tuple[list[dict], int]:
        """Children ``start:stop`` (only those are built) and how many there are."""
        ids = [item["id"] for key, item in sorted(self.items.items())
               if self.drive_of[key] == drive and item.get("parent") == iid]
        extra = self.extra_children.get(iid, [])
        rows = [self.raw(drive, child) for child in ids[start:stop]]
        rows += extra[max(0, start - len(ids)):None if stop is None else max(0, stop - len(ids))]
        return rows, len(ids) + len(extra)

    def by_path(self, drive, path) -> str | None:   # SharePoint paths ignore case
        for key, item in self.items.items():
            if self.drive_of[key] == drive and not item.get("root") \
                    and self.path(drive, item["id"]).casefold() == path.casefold():
                return item["id"]
        return None

    # -- transport
    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.TOKEN-do-not-leak.sig", "expires_in": 3599})
        self.calls.append((method, url))
        assert method == "GET", method
        if parts.hostname == SP_HOST:
            self.download_count += 1
            iid = parts.path.rsplit("/", 1)[1]
            drive = parse_qs(parts.query)["drive"][0]
            if iid in self.fail_download:
                return self.fail_download[iid], {}, b""
            if self.download_errors.get(iid):
                self.download_errors[iid] -= 1
                raise URLError("connection reset by peer")
            return 200, {}, self.items[f"{drive}:{iid}"]["content"]
        path = unquote(parts.path)
        select = parse_qs(parts.query).get("$select", [""])[0].split(",")
        if path == f"/v1.0/sites/{SP_HOST}:/sites/AGENTS":
            return j(200, {"id": SITE_ID})
        if path == f"/v1.0/sites/{SITE_ID}/drive":
            return j(200, {"id": SITE_DRIVE})
        for drive in (DRIVE, SITE_DRIVE):
            base = f"/v1.0/drives/{drive}"
            if path == base:   # the library a named target is classified by
                return j(200, {"id": drive, "name": self.library_names.get(drive, "Documents"),
                               "driveType": "documentLibrary"})
            if not path.startswith(base + "/"):
                continue
            rest = path[len(base):]
            if rest == "/root":
                return j(200, self.raw(drive, ROOT_ID))
            if rest.startswith("/root:") and rest.endswith(":"):
                found = self.by_path(drive, rest[len("/root:"):-1])
                return (j(200, self.raw(drive, found)) if found
                        else j(404, {"error": {"code": "itemNotFound"}}))
            segments = rest.split("/")   # "", "items", id, [children|permissions]
            iid = segments[2]
            if f"{drive}:{iid}" not in self.items:
                return j(404, {"error": {"code": "itemNotFound"}})
            if len(segments) == 4 and segments[3] == "children":
                # Pages of $top children, continued by a $skiptoken nextLink as Graph's.
                query = [(k, v) for k, v in parse_qsl(parts.query) if k != "$skiptoken"]
                top, at = int(dict(query)["$top"]), int(parse_qs(parts.query).get(
                    "$skiptoken", ["0"])[0])
                kids, total = self.children(drive, iid, at, at + top)
                page = {"value": kids}
                if at + top < total:
                    page["@odata.nextLink"] = (f"https://graph.microsoft.com{parts.path}?"
                                               + urlencode(query + [("$skiptoken", at + top)]))
                return j(200, page)
            if len(segments) == 4 and segments[3] == "permissions":
                return j(200, {"value": perms(*self.items[f"{drive}:{iid}"].get("acl", []))})
            body = self.raw(drive, iid)
            if "content.downloadUrl" in select:
                body["@microsoft.graph.downloadUrl"] = (
                    f"https://{SP_HOST}/_layouts/15/download/{iid}?drive={drive}&tempauth=x")
            return j(200, body)
        return j(404, {"error": {"code": "itemNotFound"}})

    def downloads(self) -> int:
        return self.download_count


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def capture_audience() -> LiveSourceAudience:
    return LiveSourceAudience({"sharepoint:*": ["svc:sharepoint-capture"]}, tenant="plx")


def reader_audience() -> LiveSourceAudience:
    """View time: quality and sales hold the Quality folder; the item permissions decide
    which files each may see (sales maps to its own source identity, not Ricardo's)."""
    return LiveSourceAudience(
        {f"sharepoint:{DRIVE}:{QUALITY_FOLDER}": ["staff:quality", "staff:sales"],
         f"sharepoint:{DRIVE}:{QUALITY_FOLDER}/*": ["staff:quality", "staff:sales"],
         f"sharepoint:{DRIVE}:/Restricted": ["staff:quality"],
         f"sharepoint:{DRIVE}:/Restricted/*": ["staff:quality"]},
        tenant="plx", identities={"staff:quality": [RICARDO], "staff:sales": [SALES]},
        source_acl_required=[f"sharepoint:{DRIVE}:*"])


def scope(**target) -> CaptureScope:
    entry = {"id": "quality", "drive_id": DRIVE, "folders": [QUALITY_FOLDER],
             "max_depth": 3, "max_folders": 50, **target}
    return CaptureScope([entry])


def seeded_drive(drive: FakeDrive | None = None) -> FakeDrive:
    drive = FakeDrive() if drive is None else drive
    drive.folder("01QUALITY", "Quality")
    drive.folder("01AOS", "AOs", parent="01QUALITY")
    drive.folder("01RESTRICTED", "Restricted")
    drive.folder("01LOTS", "Lots", parent="01AOS")
    drive.file("01FILEA", "AO packet PO100234.pdf", "01AOS")
    drive.file("01FILEB", "release.txt", "01LOTS", TEXT_SECRET.encode(), mime="text/plain",
               acl=(RICARDO, SALES))
    drive.file("01FILEC", NAME_SECRET, "01AOS", b"%PDF-1.7 pricing", acl=(RICARDO,))
    return drive


def scanned_pdf(pages: list[str]) -> bytes:
    """A PDF whose pages are images only (each page's text rendered to a PNG by
    pdftoppm and placed as a picture): the shape of a scanned AO packet. Without reportlab
    the test needing it is skipped, never errored."""
    import io
    import subprocess

    if importlib.util.find_spec("reportlab") is None:
        raise unittest.SkipTest("needs reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    with tempfile.TemporaryDirectory() as tmp:
        out = io.BytesIO()
        scan = canvas.Canvas(out, pagesize=letter)
        for index, text in enumerate(pages):
            typed = Path(tmp) / f"typed-{index}.pdf"
            page = canvas.Canvas(str(typed), pagesize=letter)
            page.setFont("Helvetica", 28)
            page.drawString(72, 600, text)
            page.save()
            subprocess.run(["pdftoppm", "-r", "100", "-gray", "-png", "-singlefile", str(typed),
                            str(Path(tmp) / f"image-{index}")], check=True, timeout=60)
            scan.drawImage(str(Path(tmp) / f"image-{index}.png"), 0, 0,
                           width=letter[0], height=letter[1])
            scan.showPage()
        scan.save()
        return out.getvalue()


class CaptureCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.drive = seeded_drive()
        self.store = SharePointCaptureStore(self.state, clock=Clock())

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def capture(self, **kwargs) -> SharePointCapture:
        api, self.transport = graph_read.build_graph(
            ENV, sender=self.drive, retry=BoundedBackoff(sleep=lambda s: None,
                                                         jitter=lambda: 0.0))

        def reader(drive_id):
            return SharePointReader(api, capture_audience(), drive_id, transport=self.transport,
                                    read_permissions=True, max_acl_reads=cs.MAX_ACL_READS)

        return SharePointCapture(self.store, reader, CAPTURE,
                                 site_drive=lambda name: graph_read.resolve_site_drive(
                                     api, SP_HOST, name), **kwargs)

    def handle(self, iid, version=1):
        return version_handle(DRIVE, iid, f'"{{{iid}}},{version}"')

    def root(self, run, index=0):
        return run["targets"][0]["roots"][index]


class CaptureTests(CaptureCase):
    def test_capture_keeps_versions_permissions_and_pinned_bytes_through_get_only(self):
        run = self.capture().run(scope())
        self.assertTrue(run["complete"], run)
        root = self.root(run)
        self.assertEqual((root["status"], root["reason"]), ("complete", None))
        self.assertEqual(root["counts"]["versions_new"], 3)
        self.assertEqual(root["counts"]["folders_listed"], 2)
        self.assertEqual(root["counts"]["texts_extracted"], 1)
        self.assertTrue(all(m == "GET" for m, _u in self.drive.calls))
        found = self.store.resolve(self.handle("01FILEB"), CAPTURE, capture_audience())
        self.assertEqual(found["row"]["path"], QUALITY_FOLDER + "/Lots/release.txt")
        self.assertEqual(found["row"]["content_text"], TEXT_SECRET)
        self.assertEqual(found["row"]["content_status"], "captured")
        self.assertEqual(found["lineage"], "sharepoint")
        digest = sha256(TEXT_SECRET.encode()).hexdigest()
        self.assertEqual(found["row"]["content_sha256"], digest)
        pinned = f"{self.handle('01FILEB')}#sha256={digest}"
        self.assertEqual(self.store.content(pinned, CAPTURE, capture_audience()),
                         TEXT_SECRET.encode())
        self.assertIsNone(self.store.resolve(self.handle("01FILEB") + "#sha256=" + "0" * 64,
                                             CAPTURE, capture_audience()))
        acl = json.loads(self.store.db.execute(
            "SELECT source_acl FROM items WHERE item_id='01FILEB'").fetchone()[0])
        self.assertEqual(acl["status"], "read")
        self.assertEqual(sorted(acl["identities"]), [RICARDO, SALES])
        # The scope root is the walk's unit; nothing outside it was listed.
        self.assertFalse(any("01RESTRICTED" in u for _m, u in self.drive.calls))

    def test_recapture_on_etag_change_keeps_both_versions(self):
        capture = self.capture()
        capture.run(scope())
        downloads = self.drive.downloads()
        self.drive.change("01FILEB", b"Revised release for PO100235.")
        run = capture.run(scope())
        counts = self.root(run)["counts"]
        self.assertEqual((counts["versions_new"], counts["versions_superseded"],
                          counts["unchanged"]), (1, 1, 2))
        self.assertEqual(self.drive.downloads(), downloads + 1)   # unchanged: no re-read
        old, new = self.handle("01FILEB"), self.handle("01FILEB", 2)
        history = self.store.history(CAPTURE, capture_audience(), new)
        self.assertEqual([h["l1_handle"] for h in history], [old, new])
        self.assertIsNotNone(history[0]["superseded_at"])
        self.assertIsNone(history[1]["superseded_at"])
        # The superseded version still resolves to exactly what it cited.
        self.assertEqual(self.store.resolve(old, CAPTURE, capture_audience())["row"]
                         ["content_text"], TEXT_SECRET)
        self.assertEqual(self.store.counts(CAPTURE, capture_audience())["versions"], 4)

    def test_moved_item_keeps_its_identity_and_is_regated_at_the_new_path(self):
        capture = self.capture()
        capture.run(scope(folders=[QUALITY_FOLDER, "/Restricted"]))
        handle = self.handle("01FILEB")
        self.assertIsNotNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        self.drive.move("01FILEB", "01RESTRICTED")           # same eTag, new path
        run = capture.run(scope(folders=[QUALITY_FOLDER, "/Restricted"]))
        self.assertEqual(self.root(run, 1)["counts"]["moved"], 1)
        current = self.store.current(DRIVE, "01FILEB")
        self.assertEqual((current["path"], current["l1_handle"]),
                         ("/Restricted/release.txt", handle))
        rows = self.store.db.execute("SELECT path, superseded_at FROM items WHERE "
                                     "item_id='01FILEB' ORDER BY version_id").fetchall()
        self.assertEqual([r[0] for r in rows], [QUALITY_FOLDER + "/Lots/release.txt",
                                                "/Restricted/release.txt"])
        self.assertIsNotNone(rows[0][1])
        # Re-gated on where it is now: sales holds no grant on /Restricted, which hides
        # the earlier interval at the old path too.
        self.assertIsNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        self.assertEqual(self.store.history(EMPLOYEE, reader_audience(), handle), [])
        self.assertEqual(len(self.store.history(QUALITY, reader_audience(), handle)), 2)
        self.assertIsNotNone(self.store.resolve(handle, QUALITY, reader_audience()))
        # A move that also changes the eTag is a new version of the same item.
        self.drive.move("01FILEB", "01AOS", new_etag=True)
        capture.run(scope(folders=[QUALITY_FOLDER, "/Restricted"]))
        self.assertEqual(self.store.current(DRIVE, "01FILEB")["path"],
                         QUALITY_FOLDER + "/release.txt")
        self.assertEqual(self.store.counts(CAPTURE, capture_audience())["files_current"], 3)

    def test_a_deleted_item_is_superseded_only_after_a_walk_that_listed_everything(self):
        capture = self.capture()
        capture.run(scope())
        self.drive.delete("01FILEC")
        run = capture.run(scope(max_folders=1))
        self.assertEqual(self.root(run)["reason"], "folder_bound")
        self.assertIsNotNone(self.store.current(DRIVE, "01FILEC"))   # never "absent"
        run = capture.run(scope())
        self.assertEqual(self.root(run)["counts"]["absent_after_resync"], 1)
        self.assertIsNone(self.store.current(DRIVE, "01FILEC"))
        rows = self.store.db.execute("SELECT removed_reason FROM items WHERE "
                                     "item_id='01FILEC'").fetchall()
        self.assertEqual([r[0] for r in rows], ["absent_after_resync"])

    def test_a_spent_time_budget_stops_the_run_incomplete_and_reconciles_nothing(self):
        # Live 2026-10-05: capture.sharepoint ran into its 1800 s timeout, reporting
        # nothing. A spent budget is a reported partial state, never complete. The root
        # it reached before reading anything is deferred: still complete as its last walk
        # left it (nothing forgotten, so it is not walked again for that), the run not.
        self.capture().run(scope())
        self.drive.delete("01FILEC")
        self.drive.file("01FILED", "AO packet 2.pdf", "01AOS")
        ticks = iter(range(10_000))
        run = self.capture(budget_seconds=2, monotonic=lambda: next(ticks)).run(scope())
        self.assertEqual((run["complete"], self.root(run)["complete"],
                          self.root(run)["deferred"], self.root(run)["reason"]),
                         (False, True, True, None))
        self.assertEqual(self.store.root_state(DRIVE, QUALITY_FOLDER)["status"], "complete")
        self.assertTrue(self.store.walked_folders(DRIVE, QUALITY_FOLDER))
        self.assertIsNotNone(self.store.current(DRIVE, "01FILEC"))   # never "absent"
        self.assertIsNone(self.store.current(DRIVE, "01FILED"))
        run = self.capture().run(scope())
        self.assertTrue(run["complete"])
        self.assertIsNone(self.store.current(DRIVE, "01FILEC"))
        self.assertIsNotNone(self.store.current(DRIVE, "01FILED"))

    def test_permission_reads_that_outrun_the_budget_stop_inside_the_listing(self):
        # A listing reads up to 5000 items' permissions while the
        # budget was asked only per folder, so one large folder overran the check's
        # timeout. Each permission read costs one second of this clock; the budget runs
        # out halfway through a folder of N items (N=10/60): exactly that many reads,
        # a partial root with its typed reason, nothing reconciled, and a later run
        # captures every item once.
        with self.assertRaises(CaptureError):
            self.capture(budget_seconds=-1)
        for size in (10, 60):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp:
                self.drive = seeded_drive()
                self.store.close()
                self.store = SharePointCaptureStore(Path(tmp) / "state", clock=Clock())
                self.capture().run(scope())
                self.drive.delete("01FILEC")
                self.drive.folder("01BIG", "Big", parent="01AOS")
                for n in range(size):
                    self.drive.file(f"01BIG{n:03d}", f"b{n}.txt", "01BIG", b"x",
                                    mime="text/plain")
                start = len(self.drive.calls)

                def acl_reads():
                    return sum(1 for _m, u in self.drive.calls[start:]
                               if u.split("?")[0].endswith("/permissions"))

                # AOs lists four children (Big, A, C gone, Lots: 3 reads), then Big; each
                # new file is captured as soon as its permissions are read (its byte read
                # keeps the listing's: no second read).
                budget = 3 + size // 2
                run = self.capture(budget_seconds=budget, monotonic=acl_reads).run(scope())
                root = self.root(run)
                self.assertEqual((run["complete"], root["status"], root["reason"]),
                                 (False, "incomplete", cs.TIME_BUDGET))
                counts = root["counts"]
                self.assertEqual((acl_reads(), counts["acl_reads"]), (budget, budget))
                self.assertIsNotNone(self.store.current(DRIVE, "01FILEC"))   # not absent
                self.assertEqual(self.store.root_state(DRIVE, QUALITY_FOLDER)["reason"],
                                 cs.TIME_BUDGET)
                run = self.capture().run(scope())
                self.assertTrue(run["complete"], run)
                self.assertIsNone(self.store.current(DRIVE, "01FILEC"))
                current = [r[0] for r in self.store.db.execute(
                    "SELECT item_id FROM items WHERE superseded_at IS NULL AND item_id "
                    "LIKE '01BIG%'")]
                self.assertEqual(sorted(current), [f"01BIG{n:03d}" for n in range(size)])

    def test_a_root_larger_than_one_budget_resumes_its_walk_and_completes_on_run_three(self):
        # A root the time budget cut was walked from the top again
        # by every run, so a root larger than one budget never completed. Each download
        # costs one second of this clock and a run has four: the walk resumes from its
        # stored position (the cut folder with the children it holds, then the folders
        # left) and completes on the third run. Every child is read once and stored with
        # the permissions read for it; nothing is reconciled before the walk ends.
        self.capture().run(scope())
        self.drive.delete("01FILEC")
        self.drive.folder("01BIG", "Big", parent="01AOS")
        big = [f"01BIG{n:03d}" for n in range(9)]
        for n, iid in enumerate(big):
            self.drive.file(iid, f"b{n}.txt", "01BIG", b"x%d" % n, mime="text/plain")
        start, roots = len(self.drive.calls), []
        for number in range(3):
            run = self.capture(budget_seconds=4, monotonic=self.drive.downloads).run(scope())
            roots.append(self.root(run))
            if number < 2:   # the walk has not ended: nothing is called absent
                self.assertIsNotNone(self.store.current(DRIVE, "01FILEC"))
        self.assertEqual([(r["complete"], r["reason"], r["cursor"], r["full_walk_reason"])
                          for r in roots],
                         [(False, cs.TIME_BUDGET, "walk_position", "feed_unavailable"),
                          (False, cs.TIME_BUDGET, "walk_position", "resumed"),
                          (True, None, None, "resumed")])
        self.assertEqual([r["walk_position"] for r in roots],
                         [{"folders_listed": 2, "folders_left": 2, "items_held": 4},
                          {"folders_listed": 2, "folders_left": 2, "items_held": 8}, None])
        self.assertEqual([r["counts"]["versions_new"] for r in roots], [4, 4, 1])
        self.assertEqual(self.store.root_state(DRIVE, QUALITY_FOLDER)["status"], "complete")
        self.assertIsNone(self.store.current(DRIVE, "01FILEC"))    # reconciled at the end
        current = self.store.db.execute("SELECT item_id, source_acl FROM items WHERE "
                                        "superseded_at IS NULL ORDER BY item_id").fetchall()
        self.assertEqual([r[0] for r in current], sorted(big + ["01FILEA", "01FILEB"]))
        self.assertEqual({json.loads(r[1])["status"] for r in current}, {"read"})
        permission_reads = [u for _m, u in self.drive.calls[start:]
                            if u.split("?")[0].endswith("/permissions")]
        for iid in big:   # read once by its listing: not by its bytes, nor a resumed run
            self.assertEqual(sum(f"/items/{iid}/" in u for u in permission_reads), 1, iid)
        self.assertIsNone(self.store.walk_position(DRIVE, QUALITY_FOLDER))

    def test_a_folder_larger_than_one_listing_is_walked_whole_across_slices(self):
        # gap-375b698206de: a listing stopped at LIST_MAX_ITEMS (5000) and the walk
        # flagged listing_truncated on every run. Scaled down (12,000 captured files cost
        # a minute; the census test lists 12,000 at the real bounds): pages of 7, a
        # listing of at most 50 and a budget of 45 downloads a run, so caps and budget
        # cuts fall inside pages. Each listing resumes at its stored page and position;
        # the walk ends complete with every child read and captured exactly once.
        for size in (1, 60, 120):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp:
                self.drive = seeded_drive()
                self.store.close()
                self.store = SharePointCaptureStore(Path(tmp) / "state", clock=Clock())
                self.drive.folder("01BIG", "Big", parent="01AOS")
                big = [f"01BIG{n:05d}" for n in range(size)]
                for n, iid in enumerate(big):
                    self.drive.file(iid, f"b{n}.txt", "01BIG", b"x%d" % n, mime="text/plain")
                roots = []
                with mock.patch.object(cs, "LIST_PAGE_SIZE", 7), \
                        mock.patch.object(cs, "LIST_MAX_ITEMS", 50):
                    while not (roots and roots[-1]["complete"]) and len(roots) < 5:
                        run = self.capture(budget_seconds=45,
                                           monotonic=self.drive.downloads).run(scope())
                        roots.append(self.root(run))
                self.assertEqual([r["reason"] for r in roots],
                                 [cs.TIME_BUDGET] * (len(roots) - 1) + [None])
                self.assertEqual(len(roots), 1 + (size + 3 - 1) // 45)
                self.assertEqual(sum(r["counts"]["versions_new"] for r in roots), size + 3)
                current = [r[0] for r in self.store.db.execute(
                    "SELECT item_id FROM items WHERE superseded_at IS NULL AND item_id "
                    "LIKE '01BIG0%'")]
                self.assertEqual(sorted(current), big)
                permission_reads = [u for _m, u in self.drive.calls
                                    if u.split("?")[0].endswith("/permissions")
                                    and "/items/01BIG0" in u]
                self.assertEqual(len(permission_reads), size)   # none read twice
                self.assertEqual(self.drive.downloads(), size + 3)
                self.assertIsNone(self.store.walk_position(DRIVE, QUALITY_FOLDER))

    def test_a_child_deleted_before_the_resume_point_shifts_no_child_past_it(self):
        # A walk cut inside a page resumed at its stored offset: a held child deleted on
        # that page between runs shifted the page, and the child after the cut was never
        # listed while the walk ended complete. It resumes at the page's start, skipping
        # the children it holds: every remaining child is captured exactly once.
        self.drive.folder("01BIG", "Big", parent="01AOS")
        big = [f"01BIG{n:05d}" for n in range(30)]
        for n, iid in enumerate(big):
            self.drive.file(iid, f"b{n}.txt", "01BIG", b"x%d" % n, mime="text/plain")
        with mock.patch.object(cs, "LIST_PAGE_SIZE", 7), \
                mock.patch.object(cs, "LIST_MAX_ITEMS", 50):
            first = self.root(self.capture(budget_seconds=13,
                                           monotonic=self.drive.downloads).run(scope()))
            self.assertEqual(first["reason"], cs.TIME_BUDGET)
            entry = self.store.walk_position(DRIVE, QUALITY_FOLDER)["queue"][0]
            page = int(parse_qs(urlsplit(entry["next"]["url"]).query)["$skiptoken"][0])
            gone = [iid for iid in big[page:page + 7] if iid in entry["held"]]
            self.assertTrue(gone and big[page + 6] not in entry["held"])   # cut mid-page
            self.drive.delete(gone[0])
            done = self.root(self.capture().run(scope()))
        self.assertEqual((done["complete"], done["reason"]), (True, None))
        current = [r[0] for r in self.store.db.execute(
            "SELECT item_id FROM items WHERE superseded_at IS NULL AND item_id LIKE "
            "'01BIG0%'")]
        self.assertEqual(sorted(set(current) - {gone[0]}), [i for i in big if i != gone[0]])
        reads = [u.split("/items/")[1].split("/")[0] for _m, u in self.drive.calls
                 if u.split("?")[0].endswith("/permissions") and "/items/01BIG0" in u]
        self.assertEqual(sorted(reads), big)   # none read twice

    def cut_walk(self) -> None:
        """A full capture, then a walk the budget (two downloads) cuts inside AOs/Big with
        two of its three new files read: Lots, holding release.txt, is still queued."""
        self.capture().run(scope())
        self.drive.folder("01BIG", "Big", parent="01AOS")
        for n in range(3):
            self.drive.file(f"01BIG{n}", f"b{n}.txt", "01BIG", b"x%d" % n, mime="text/plain")
        root = self.root(self.capture(budget_seconds=2, monotonic=self.drive.downloads)
                         .run(scope()))
        self.assertEqual((root["reason"], root["walk_position"]),
                         (cs.TIME_BUDGET, {"folders_listed": 2, "folders_left": 2,
                                           "items_held": 2}))

    def test_a_folder_renamed_while_queued_is_resumed_where_it_is_now(self):
        # The resume queue kept folders by path, so a folder
        # renamed between runs was a not_visible gap and the walk started over. It is
        # kept by item id: the renamed folder is listed where it is now and its file
        # re-keyed there; the walk completes.
        self.cut_walk()
        self.drive.items[f"{DRIVE}:01LOTS"]["name"] = "Lots 2026"
        done = self.root(self.capture().run(scope()))
        self.assertEqual((done["complete"], done["reason"], done["full_walk_reason"],
                          done["counts"]["moved"]), (True, None, "resumed", 1))
        self.assertEqual(self.store.current(DRIVE, "01FILEB")["path"],
                         QUALITY_FOLDER + "/Lots 2026/release.txt")
        self.assertTrue(all(m == "GET" for m, _u in self.drive.calls))
        # A queued folder moved out of the root is not this root's any more: nothing in
        # it is captured under the root, and its file is moved out of scope.
        self.store.close()
        self.store = SharePointCaptureStore(Path(self.tmp.name) / "moved-out", clock=Clock())
        self.drive = seeded_drive()
        self.cut_walk()
        self.drive.move("01LOTS", "01RESTRICTED")
        done = self.root(self.capture().run(scope()))
        self.assertEqual((done["complete"], done["counts"]["moved_out_of_scope"]), (True, 1))
        self.assertIsNone(self.store.current(DRIVE, "01FILEB"))
        self.assertFalse([r for r in self.store.db.execute("SELECT path FROM items")
                          if r[0].startswith("/Restricted")])
        # A folder id that is malformed or names a file is refused before any listing.
        api, transport = graph_read.build_graph(ENV, sender=self.drive)
        reader = SharePointReader(api, capture_audience(), DRIVE, transport=transport)
        for bad in ("../01LOTS", "01FILEA"):
            with self.assertRaises(NotVisible):
                reader.list_folder(CAPTURE, "/", folder_id=bad)

    def test_a_file_moved_into_a_walked_folder_mid_walk_stays_current(self):
        # A file moved from a folder the walk had not reached
        # into one it had listed was called moved_out_of_scope when the walk ended (gone
        # from every reader's index for a run). It is captured where it is now.
        self.cut_walk()
        self.drive.move("01FILEB", "01AOS")
        done = self.root(self.capture().run(scope()))
        self.assertEqual((done["complete"], done["counts"]["moved"],
                          done["counts"]["moved_out_of_scope"],
                          done["counts"]["absent_after_resync"]), (True, 1, 0, 0))
        current = self.store.current(DRIVE, "01FILEB")
        self.assertEqual((current["path"], current["removed_reason"]),
                         (QUALITY_FOLDER + "/release.txt", None))
        self.assertIn(self.handle("01FILEB"), {i["l1_handle"] for i in self.store.index(
            EMPLOYEE, reader_audience())["items"]})

    def test_a_file_moved_within_the_root_is_regated_when_the_budget_ends_first(self):
        # The budget ran out between the reconcile and the
        # capture at the new path, so the file kept its old permissions for a run while the
        # root read complete. The move dropped sales from its permissions: hidden from it
        # at once, and the root stays incomplete until a run captures it where it is.
        self.cut_walk()
        self.drive.move("01FILEB", "01AOS")
        self.drive.items[f"{DRIVE}:01FILEB"]["acl"] = [RICARDO]
        handle = self.handle("01FILEB")
        self.assertIsNotNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        reconciled = []
        reconcile = self.store.reconcile_root

        def then_spent(*args, **kwargs):
            out = reconcile(*args, **kwargs)
            reconciled.append(out)
            return out

        capture = self.capture(budget_seconds=1, monotonic=lambda: 9 if reconciled else 0)
        with mock.patch.object(self.store, "reconcile_root", side_effect=then_spent):
            done = self.root(capture.run(scope()))
        self.assertEqual(len(reconciled[0][1]), 1)   # found still under the root
        self.assertEqual((done["complete"], done["reason"], done["counts"]["moved"]),
                         (False, cs.TIME_BUDGET, 0))
        self.assertIsNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        self.assertNotIn(handle, {i["l1_handle"] for i in self.store.index(
            EMPLOYEE, reader_audience())["items"]})
        self.assertIsNotNone(self.store.resolve(handle, QUALITY, reader_audience()))
        done = self.root(self.capture().run(scope()))
        self.assertEqual((done["complete"], done["counts"]["moved"]), (True, 1))
        self.assertEqual(self.store.current(DRIVE, "01FILEB")["path"],
                         QUALITY_FOLDER + "/release.txt")
        self.assertIsNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))

    def test_the_reconcile_matches_a_root_spelled_in_another_case(self):
        # The walk matched the scope root ignoring case, the
        # reconcile did not, so a file deleted under a root spelled in another case stayed
        # current (and served) forever.
        lower = scope(folders=[QUALITY_FOLDER.lower()])
        capture = self.capture()
        self.assertTrue(capture.run(lower)["complete"])
        self.assertEqual(self.store.current(DRIVE, "01FILEC")["root_path"],
                         QUALITY_FOLDER.lower())
        self.drive.delete("01FILEC")
        run = capture.run(lower)
        self.assertEqual((run["complete"], self.root(run)["counts"]["absent_after_resync"]),
                         (True, 1))
        self.assertIsNone(self.store.current(DRIVE, "01FILEC"))
        self.assertIsNone(self.store.resolve(self.handle("01FILEC"), QUALITY,
                                             reader_audience()))

    def test_permission_trimming_for_an_employee_without_access(self):
        self.capture().run(scope())
        audience = reader_audience()
        secret = self.handle("01FILEC")      # item permissions: Ricardo only
        self.assertIsNotNone(self.store.resolve(secret, QUALITY, audience))
        self.assertIsNone(self.store.resolve(secret, EMPLOYEE, audience))
        self.assertIsNone(self.store.content(secret, EMPLOYEE, audience))
        self.assertEqual(self.store.history(EMPLOYEE, audience, secret), [])
        handles = {i["l1_handle"] for i in self.store.index(EMPLOYEE, audience)["items"]}
        self.assertEqual(handles, {self.handle("01FILEB")})
        self.assertEqual(self.store.counts(EMPLOYEE, audience)["files_current"], 1)
        self.assertEqual(self.store.counts(QUALITY, audience)["files_current"], 3)
        (root,) = self.store.completeness(EMPLOYEE, audience)["roots"]
        self.assertEqual(root["current_files"], 1)
        self.assertFalse(root["complete"])        # something here is withheld from it
        self.assertTrue(self.store.completeness(QUALITY, audience)["complete"])
        self.assertNotIn(NAME_SECRET, json.dumps(self.store.completeness(EMPLOYEE, audience)))
        # A reader with no grant at all sees nothing: no root, no row, no count.
        self.assertEqual(self.store.completeness(OUTSIDER, audience)["roots"], [])
        self.assertEqual(sum(self.store.counts(OUTSIDER, audience).values()), 0)
        self.assertFalse(self.store.item_gate(audience)(OUTSIDER, self.handle("01FILEB")))
        self.assertFalse(self.store.audience_check(audience)(OUTSIDER, DRIVE))
        self.assertTrue(self.store.audience_check(audience)(EMPLOYEE, DRIVE))

    def test_a_revoked_permission_hides_the_earlier_versions_too(self):
        capture = self.capture()
        capture.run(scope())
        old = self.handle("01FILEB")
        self.assertIsNotNone(self.store.resolve(old, EMPLOYEE, reader_audience()))
        self.drive.items[f"{DRIVE}:01FILEB"]["acl"] = [RICARDO]   # sales removed
        capture.run(scope())                  # permissions change without a new eTag
        self.assertIsNone(self.store.resolve(old, EMPLOYEE, reader_audience()))
        self.drive.change("01FILEB", b"Revised release.")
        capture.run(scope())
        for handle in (old, self.handle("01FILEB", 2)):
            self.assertIsNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
            self.assertIsNotNone(self.store.resolve(handle, QUALITY, reader_audience()))
        self.assertEqual(self.store.counts(EMPLOYEE, reader_audience())["versions"], 0)

    def test_a_grant_revoked_without_the_sharing_annotation_is_dropped_after_the_bound(self):
        # The feed lists a permission-only change (same eTag)
        # without the sharing annotation. Within permission_reread_hours the eTag shortcut
        # still skips get_item (the permission re-read); the first run after it reads the
        # feed from the root's permission position and re-reads the entry, although the
        # root's own position had already passed it and no full walk is due.
        from integrations.tests.test_capture_breadth import DeltaDrive   # imports this module
        self.drive = DeltaDrive()
        self.drive.folder("01QUALITY", "Quality")
        self.drive.folder("01AOS", "AOs", parent="01QUALITY")
        self.drive.file("01FILEB", "release.txt", "01AOS", b"release", mime="text/plain",
                        acl=(RICARDO, SALES))
        bounded = CaptureScope([{"id": "quality", "drive_id": DRIVE,
                                 "folders": [QUALITY_FOLDER]}], full_walk_hours=72)
        handle = self.handle("01FILEB")

        def run(hours):
            self.store._clock.now += timedelta(hours=hours)
            mark = len(self.drive.calls)
            root = self.root(self.capture().run(bounded))
            return root, [u for _m, u in self.drive.calls[mark:] if "/items/01FILEB" in u]

        self.assertEqual(run(0)[0]["round"], "full")
        self.assertIsNotNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        self.drive.items[f"{DRIVE}:01FILEB"]["acl"] = [RICARDO]   # sales revoked
        self.drive._logged(DRIVE, "01FILEB")                      # listed, no annotation
        root, reads = run(1)
        self.assertEqual((root["round"], root["complete"], reads), ("incremental", True, []))
        root, reads = run(24)
        self.assertIsNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        self.assertIsNotNone(self.store.resolve(handle, QUALITY, reader_audience()))
        self.assertEqual((root["round"], root["complete"], root["counts"]["permissions_reread"]),
                         ("incremental", True, 1))
        self.assertTrue(reads)
        # The re-read moved the permission position: within the bound again, an entry
        # listed unannotated is passed over.
        self.drive._logged(DRIVE, "01FILEB")
        root, reads = run(1)
        self.assertEqual((root["round"], root["counts"]["permissions_reread"], reads),
                         ("incremental", 0, []))
        # A store that holds no permission position (written before it) walks.
        with self.store.db:
            self.store.db.execute("DELETE FROM root_permissions")
        root, _reads = run(1)
        self.assertEqual((root["round"], root["full_walk_reason"], root["complete"]),
                         ("full", "permissions_unknown", True))

    def test_a_permission_reread_the_budget_cuts_resumes_and_converges(self):
        # With the shortcut off, a re-read the budget cut read the same
        # leading entries every run and never ended. Five entries due, two permission reads
        # per run (3 budgets): the changed file first, then each entry once, done in run 3
        # (within k + 1 runs).
        from integrations.tests.test_capture_breadth import DeltaDrive   # imports this module
        self.drive = DeltaDrive()
        self.drive.folder("01QUALITY", "Quality")
        self.drive.folder("01AOS", "AOs", parent="01QUALITY")
        ids = [f"01F{n}" for n in range(5)]
        for iid in ids:
            self.drive.file(iid, f"{iid}.txt", "01AOS", iid.encode(), mime="text/plain",
                            acl=(RICARDO, SALES))
        bounded = CaptureScope([{"id": "quality", "drive_id": DRIVE,
                                 "folders": [QUALITY_FOLDER]}], full_walk_hours=720)
        self.capture().run(bounded)
        for iid in ids[:3]:
            self.drive._logged(DRIVE, iid)                          # listed, unchanged
        self.drive.items[f"{DRIVE}:01F3"]["acl"] = [RICARDO]       # revoked, no annotation
        self.drive._logged(DRIVE, "01F3")
        self.drive.change("01F4", b"new version")                  # a real change
        self.store._clock.now += timedelta(hours=25)

        def acl_reads():
            return [u.split("/items/")[1].split("/")[0] for _m, u in self.drive.calls
                    if u.split("?")[0].endswith("/permissions")]

        rounds, read = [], []
        while len(rounds) < 6:
            self.store._clock.now += timedelta(hours=1)
            start = len(acl_reads())
            root = self.root(self.capture(budget_seconds=2,
                                          monotonic=lambda: len(acl_reads()) - start
                                          ).run(bounded))
            rounds.append((root["round"], root["complete"]))
            read.append(set(acl_reads()[start:]))
            if not root.get("deferred"):
                break
        self.assertEqual(rounds, [("incremental", True)] * 3)
        self.assertEqual(sorted(set().union(*read)), ids)           # each entry in one run
        self.assertEqual(sum(map(len, read)), len(ids))
        self.assertIsNone(self.store.resolve(self.handle("01F3"), EMPLOYEE, reader_audience()))
        self.assertIsNotNone(self.store.resolve(self.handle("01F4", 2), QUALITY,
                                                reader_audience()))
        self.assertFalse(self.capture()._reread_due(DRIVE, QUALITY_FOLDER))

    def test_truncation_bounds_and_withheld_children_are_reported_incomplete(self):
        capture = self.capture()
        # A listing that stops incomplete without a resume point is a truncated folder.
        list_folder = SharePointReader.list_folder

        def no_resume(reader, *args, **kwargs):
            listing = list_folder(reader, *args, **kwargs)
            listing["completeness"]["next"] = None
            return listing

        with mock.patch.object(cs, "LIST_MAX_ITEMS", 2), \
                mock.patch.object(SharePointReader, "list_folder", no_resume):
            run = capture.run(scope())
        self.assertFalse(run["complete"])
        self.assertIn("listing_truncated", self.root(run)["reason"])
        run = capture.run(scope(max_depth=0))
        self.assertEqual(self.root(run)["reason"], "depth_bound")
        run = capture.run(scope(max_folders=1))
        self.assertEqual(self.root(run)["reason"], "folder_bound")
        narrowed = LiveSourceAudience({
            f"sharepoint:{DRIVE}:{QUALITY_FOLDER}": ["svc:sharepoint-capture"],
            f"sharepoint:{DRIVE}:{QUALITY_FOLDER}/*": ["svc:sharepoint-capture"],
            f"sharepoint:{DRIVE}:{QUALITY_FOLDER}/{NAME_SECRET}": []}, tenant="plx")
        api, transport = graph_read.build_graph(ENV, sender=self.drive)
        run = SharePointCapture(self.store, lambda d: SharePointReader(
            api, narrowed, d, transport=transport, read_permissions=True), CAPTURE).run(scope())
        self.assertEqual(self.root(run)["reason"], "withheld_from_capture")
        # Nothing was reconciled away by any of these partial walks.
        self.assertEqual(self.store.counts(CAPTURE, capture_audience())["files_current"], 3)
        state = self.store.completeness(CAPTURE, capture_audience())
        self.assertFalse(state["complete"])

    def test_an_over_cap_file_is_a_recorded_terminal_outcome_and_the_root_completes(self):
        # Live defect 2026-10-04: one file over the cap left the root incomplete.
        capture = self.capture(max_content_bytes=20)
        run = capture.run(scope())
        root = self.root(run)
        self.assertEqual((root["status"], root["complete"], root["reason"]),
                         ("complete", True, None))
        self.assertTrue(run["complete"])
        self.assertEqual(root["counts"]["contents_exceeds_cap"], 1)
        over = self.store.current(DRIVE, "01FILEB")      # 47 bytes > 20
        self.assertEqual((over["content_status"], over["content_sha256"]), ("exceeds_cap", None))
        self.assertEqual(json.loads(over["record"])["size"], len(TEXT_SECRET.encode()))
        # Metadata, permissions and version kept; it resolves as captured, without text.
        found = self.store.resolve(self.handle("01FILEB"), CAPTURE, capture_audience())
        self.assertEqual((found["row"]["content_status"], found["row"]["content_text"],
                          found["row"]["name"]), ("exceeds_cap", None, "release.txt"))
        self.assertEqual(found["source_acl"]["status"], "read")
        (state,) = self.store.completeness(CAPTURE, capture_audience())["roots"]
        self.assertEqual((state["content_exceeds_cap"], state["content_unavailable"],
                          state["complete"]), (1, 0, True))   # counted apart, not hidden
        self.assertEqual(self.store.counts(CAPTURE, capture_audience())["files_current"], 3)
        # A bounded walk still leaves the root incomplete.
        cut = self.root(capture.run(scope(max_folders=1)))
        self.assertFalse(cut["complete"])
        self.assertIn("folder_bound", cut["reason"])

    def test_a_fetch_failure_leaves_the_root_incomplete_until_a_later_run_reads_it(self):
        self.drive.fail_download["01FILEA"] = 403
        capture = self.capture(max_content_bytes=20)
        run = capture.run(scope())
        root = self.root(run)
        self.assertEqual((root["status"], root["reason"]), ("incomplete", "content_incomplete"))
        self.assertEqual(self.store.current(DRIVE, "01FILEA")["content_status"],
                         "unavailable:http_403")
        self.assertEqual(self.store.counts(CAPTURE, capture_audience())["files_current"], 3)
        (state,) = self.store.completeness(CAPTURE, capture_audience())["roots"]
        self.assertEqual((state["content_exceeds_cap"], state["content_unavailable"],
                          state["complete"]), (1, 1, False))
        # A later run retries the unreadable bytes of the same version.
        del self.drive.fail_download["01FILEA"]
        run = self.capture().run(scope())
        self.assertEqual(self.root(run)["counts"]["contents_recovered"], 1)
        self.assertTrue(self.root(run)["complete"])
        self.assertEqual(self.store.current(DRIVE, "01FILEA")["content_status"], "captured")
        events = [r[0] for r in self.store.db.execute(
            "SELECT event FROM item_events WHERE l1_handle=?", (self.handle("01FILEA"),))]
        self.assertIn("content_recovered", events)

    def test_a_transient_download_error_is_retried_once_then_typed(self):
        # Live 2026-10-11 (capture.sharepoint_estate): contents_unavailable urlerror 14.
        # A download that failed in transit waited for the run's byte repair, which a
        # spent budget skips. It is read again at once (a fresh download URL, same eTag)
        # while the budget lasts; a second failure leaves the file typed and counted,
        # and a later run retries it. Each download costs one second of a two-second run.
        def downloads(iid):
            return sum(1 for _m, u in self.drive.calls
                       if urlsplit(u).hostname == SP_HOST and f"/{iid}?" in u)

        self.drive.download_errors["01FILEA"] = 1
        self.capture(budget_seconds=2, monotonic=self.drive.downloads).run(scope())
        self.assertEqual(self.store.current(DRIVE, "01FILEA")["content_status"], "captured")
        self.assertEqual(downloads("01FILEA"), 2)
        self.assertTrue(self.root(self.capture().run(scope()))["complete"])
        self.drive.file("01FILED", "d.txt", "01AOS", b"d", mime="text/plain")
        self.drive.download_errors["01FILED"] = 10
        run = self.capture().run(scope())
        self.assertEqual((self.root(run)["reason"], self.root(run)["counts"]
                          ["contents_unavailable"]), ("content_incomplete", 1))
        self.assertEqual(self.store.current(DRIVE, "01FILED")["content_status"],
                         "unavailable:urlerror")
        # The walk's read and the run's byte repair, each retried once, never more.
        self.assertEqual(downloads("01FILED"), 4)
        # With the budget spent by the first attempt there is no retry.
        self.drive.file("01FILEE", "e.txt", "01AOS", b"e", mime="text/plain")
        self.drive.download_errors["01FILEE"] = 10
        base = self.drive.downloads()
        self.capture(budget_seconds=1, monotonic=lambda: self.drive.downloads() - base
                     ).run(scope())
        self.assertEqual((downloads("01FILEE"), self.drive.downloads() - base), (1, 1))
        self.assertEqual(self.store.current(DRIVE, "01FILEE")["content_status"],
                         "unavailable:urlerror")

    def test_a_site_target_resolves_its_default_library_at_run_time(self):
        self.drive.folder("02CASES", "Ontology UseCases", drive=SITE_DRIVE)
        self.drive.file("02FILE", "case notes.txt", "02CASES", b"PO100236 notes",
                        mime="text/plain", drive=SITE_DRIVE)
        run = self.capture().run(CaptureScope([{"id": "agents", "site": "AGENTS",
                                                "folders": ["/Ontology UseCases"]}]))
        self.assertTrue(run["complete"], run)
        self.assertEqual(run["targets"][0]["drive_id"], SITE_DRIVE)
        self.assertIsNotNone(self.store.current(SITE_DRIVE, "02FILE"))
        missing = self.capture().run(CaptureScope([{"id": "x", "site": "Nowhere"}]))
        self.assertEqual(missing["targets"][0]["status"], "site_not_found")


class ScopeRemovalAndTextTests(CaptureCase):
    def test_nested_roots_are_refused_in_one_target_and_across_targets(self):
        with self.assertRaises(CaptureError) as raised:
            scope(folders=["/Quality", QUALITY_FOLDER])
        self.assertEqual(raised.exception.code, "scope_folders_nested")
        run = self.capture().run(CaptureScope([
            {"id": "aos", "drive_id": DRIVE, "folders": [QUALITY_FOLDER]},
            {"id": "lots", "drive_id": DRIVE, "folders": [QUALITY_FOLDER + "/Lots"]}]))
        self.assertEqual(run["targets"][1]["status"], "scope_root_nested")
        self.assertFalse(run["complete"])
        # Nothing under the nested root was attributed to a second root.
        roots = {r[0] for r in self.store.db.execute("SELECT DISTINCT root_path FROM items")}
        self.assertEqual(roots, {QUALITY_FOLDER})

    def test_a_file_moved_out_of_scope_is_regated_where_it_is_now(self):
        capture = self.capture()
        capture.run(scope())
        handle = self.handle("01FILEB")
        self.drive.move("01FILEB", "01RESTRICTED")      # outside the captured folder
        run = capture.run(scope())
        counts = self.root(run)["counts"]
        self.assertEqual((counts["moved_out_of_scope"], counts["absent_after_resync"]), (1, 0))
        row = self.store.db.execute("SELECT removed_reason, path, resources FROM items "
                                    "WHERE item_id='01FILEB'").fetchone()
        self.assertEqual(row[0], cs.MOVED_OUT)
        self.assertEqual(row[1], QUALITY_FOLDER + "/Lots/release.txt")   # its history
        self.assertEqual(json.loads(row[2]), [f"sharepoint:{DRIVE}:/Restricted/release.txt"])
        # Sales may not see /Restricted: the whole history is hidden from it now.
        self.assertIsNone(self.store.resolve(handle, EMPLOYEE, reader_audience()))
        self.assertEqual(self.store.history(EMPLOYEE, reader_audience(), handle), [])
        found = self.store.resolve(handle, QUALITY, reader_audience())
        self.assertEqual(found["removed_reason"], cs.MOVED_OUT)
        self.assertTrue(all(m == "GET" for m, _u in self.drive.calls))

    def test_a_deleted_file_is_hidden_from_every_reader(self):
        capture = self.capture()
        capture.run(scope())
        handle = self.handle("01FILEC")
        self.drive.delete("01FILEC")
        run = capture.run(scope())
        self.assertEqual(self.root(run)["counts"]["absent_after_resync"], 1)
        for actor, audience in ((QUALITY, reader_audience()), (CAPTURE, capture_audience())):
            self.assertIsNone(self.store.resolve(handle, actor, audience))
            self.assertEqual(self.store.history(actor, audience, handle), [])

    def test_a_pdf_page_citation_resolves_to_that_page(self):
        self.drive.file("01PAGED", "two pages.pdf", "01AOS", b"%PDF-1.7 paged")
        self.capture().run(scope())
        handle = self.handle("01PAGED")
        with self.store.db:   # as a capture of a scanned two-page PDF stores its OCR text
            text = "first page\fPurchase order PO100235"
            digest = self.store.put_blob(text.encode(), kind="text")
            self.store.db.execute("UPDATE items SET text_status='extracted_ocr', "
                                  "text_sha256=? WHERE item_id='01PAGED'", (digest,))
        page = self.store.resolve(handle + "#page=2", QUALITY, reader_audience())
        self.assertEqual((page["page"], page["row"]["content_text"]),
                         (2, "Purchase order PO100235"))
        self.assertIsNone(self.store.resolve(handle + "#page=3", QUALITY, reader_audience()))
        self.assertIsNone(self.store.resolve(self.handle("01FILEB") + "#page=1", QUALITY,
                                             reader_audience()))   # not a paged file
        self.assertEqual(mentions_fields(handle, text),
                         [(handle + "#page=1", "first page"),
                          (handle + "#page=2", "Purchase order PO100235")])

    def test_without_a_local_ocr_engine_a_scan_is_ocr_unavailable_then_read_later(self):
        scan = _scanned_or_skip(self)
        self.drive.file("01SCAN", "scan.pdf", "01AOS", scan)
        with mock.patch("integrations.document_understanding.local_ocr_available",
                        return_value=False):
            run = self.capture().run(scope())
        self.assertEqual(self.root(run)["counts"]["texts_ocr_unavailable"], 1)
        self.assertEqual(self.store.current(DRIVE, "01SCAN")["text_status"],
                         cs.OCR_UNAVAILABLE)
        downloads = self.drive.downloads()
        run = self.capture().run(scope())    # this host has OCR: the captured bytes
        counts = self.root(run)["counts"]
        self.assertEqual((counts["texts_reextracted"], counts["texts_ocr"]), (1, 1))
        self.assertEqual(self.drive.downloads(), downloads)     # no source read for it
        current = self.store.current(DRIVE, "01SCAN")
        self.assertEqual(current["text_status"], cs.TEXT_OCR)
        page = self.store.resolve(self.handle("01SCAN") + "#page=2", QUALITY,
                                  reader_audience())
        self.assertIn("PO100235", page["row"]["content_text"])
        self.assertEqual(self.store.counts(CAPTURE, capture_audience())["texts_ocr"], 1)

    def test_ocr_starts_only_with_its_reserve_of_the_budget_left_then_reads_later(self):
        """Live 2026-10-27: a run cut while scans were being OCR-read waited for them past
        the host's grace and was killed, reporting nothing. With less than
        OCR_RESERVE_SECONDS of the budget left no OCR starts: the scan's text layer is
        recorded at once (``ocr_pending``, bytes pinned), and a later walk with the
        reserve left OCR-reads the captured bytes (no source read)."""
        scan = _scanned_or_skip(self)
        self.drive.file("01SCAN", "scan.pdf", "01AOS", scan)
        short = cs.OCR_RESERVE_SECONDS - 1
        run = self.capture(budget_seconds=short, monotonic=lambda: 0.0).run(scope())
        self.assertEqual(self.root(run)["counts"]["texts_ocr"], 0)
        self.assertEqual(self.store.current(DRIVE, "01SCAN")["text_status"], OCR_PENDING)
        downloads = self.drive.downloads()
        run = self.capture(budget_seconds=short, monotonic=lambda: 0.0).run(scope())
        self.assertEqual(self.root(run)["counts"]["texts_reextracted"], 0)   # still short
        self.assertEqual(self.store.current(DRIVE, "01SCAN")["text_status"], OCR_PENDING)
        run = self.capture(budget_seconds=cs.OCR_RESERVE_SECONDS,
                           monotonic=lambda: 0.0).run(scope())
        counts = self.root(run)["counts"]
        self.assertEqual((counts["texts_reextracted"], counts["texts_ocr"]), (1, 1))
        self.assertEqual(self.drive.downloads(), downloads)     # no source read for it
        self.assertEqual(self.store.current(DRIVE, "01SCAN")["text_status"], cs.TEXT_OCR)
        page = self.store.resolve(self.handle("01SCAN") + "#page=2", QUALITY,
                                  reader_audience())
        self.assertIn("PO100235", page["row"]["content_text"])


def mentions_fields(handle, text):
    from ontology.mentions import file_text_fields
    return [(f.citation, f.text) for f in file_text_fields(handle, "row.content_text", text,
                                                           paged=True)]


def _scanned_or_skip(case):
    from integrations.document_understanding import local_ocr_available
    if not local_ocr_available():
        case.skipTest("poppler and tesseract are not installed (CI has them)")
    if not shutil.which("pdftoppm"):   # scanned_pdf rasterizes with it (macOS too)
        case.skipTest("pdftoppm is not installed (needed to generate the scanned fixture)")
    return scanned_pdf(["Assembly packet scan", "Purchase order PO100235"])


def cli_args(tmp) -> argparse.Namespace:
    """The CLI over the quality folder, state and files under ``tmp``, two runs."""
    scope_file = Path(tmp) / "scope.json"
    scope_file.write_text(json.dumps({
        "schema": cs.SCOPE_SCHEMA, "targets": [
            {"id": "quality", "drive_id": DRIVE, "folders": [QUALITY_FOLDER]}]}))
    audience_file = Path(tmp) / "audience.json"
    audience_file.write_text(json.dumps({
        "schema": "plx-live-source-audience/v1", "tenant": "plx",
        "audiences": {"sharepoint:*": ["svc:sharepoint-capture"]}}))
    return argparse.Namespace(state_dir=str(Path(tmp) / "state"), scope=str(scope_file),
                              live_audience=str(audience_file),
                              principal=cs.CAPTURE_PRINCIPAL,
                              withheld_principal="staff:nobody", runs=2, max_store_bytes=None)


class ScopeStoreAndCliTests(unittest.TestCase):
    def test_scope_validation_and_the_shipped_scope(self):
        shipped = CaptureScope.from_file(cs.DEFAULT_SCOPE)
        # QA/QC library, Customer Documents, Operations CM Operations Files, AGENTS
        self.assertEqual(len(shipped.targets), 4)
        self.assertEqual({bool(t.site) for t in shipped.targets}, {True, False})
        for bad in ([], [{"id": "a"}], [{"id": "a", "drive_id": DRIVE, "site": "X"}],
                    [{"id": "a", "drive_id": DRIVE, "folders": ["no-slash"]}],
                    [{"id": "a", "drive_id": DRIVE, "max_depth": 99}],
                    [{"id": "a", "drive_id": DRIVE}, {"id": "a", "drive_id": DRIVE}]):
            with self.assertRaises(CaptureError):
                CaptureScope(bad)
        with self.assertRaises(CaptureError):
            CaptureScope.from_json({"schema": "other", "targets": []})
        # A scope may declare a content cap up to the guard's one-download bound, never
        # beyond it (gap-e10f050d25a3).
        target = [{"id": "a", "drive_id": DRIVE}]
        self.assertEqual(CaptureScope(target, max_content_bytes=100_000_000)
                         .max_content_bytes, 100_000_000)
        for bad in (0, 100_000_001, True, "20000000"):
            with self.assertRaises(CaptureError) as raised:
                CaptureScope(target, max_content_bytes=bad)
            self.assertEqual(raised.exception.code, "scope_max_content_bytes_invalid")
        # The permission re-read bound defaults to 24 h, is
        # declared in the shipped scope, and an unknown value fails closed.
        self.assertEqual((CaptureScope(target).permission_reread_hours,
                          shipped.permission_reread_hours), (24, 24))
        for bad in (0, 721, True, "24"):
            with self.assertRaises(CaptureError):
                CaptureScope.from_json({"schema": cs.SCOPE_SCHEMA, "targets": target,
                                        "permission_reread_hours": bad})
        with self.assertRaises(CaptureError):
            SharePointCapture(None, None, CAPTURE, max_content_bytes=100_000_001)

    def test_store_is_outside_git_and_registered_as_a_source_kind(self):
        with self.assertRaises(CaptureError) as raised:
            SharePointCaptureStore(Path(__file__).parent / "state")
        self.assertEqual(raised.exception.code, "state_dir_inside_git_tree")
        kind = capture_store.source_kind("sharepoint")
        self.assertEqual((kind.scheme, kind.unit((DRIVE, "01X"))),
                         ("sharepoint", f"sharepoint:{DRIVE}"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(cs.store_exists(tmp))
            store = SharePointCaptureStore(Path(tmp) / "s")
            store.close()
            self.assertTrue(cs.store_exists(Path(tmp) / "s"))
            self.assertEqual(os.stat(Path(tmp) / "s" / cs.STORE_DIR / cs.DB_FILE).st_mode
                             & 0o777, 0o600)

    def test_cli_prints_counts_only_and_proves_no_leak(self):
        drive = seeded_drive()
        with tempfile.TemporaryDirectory() as tmp:
            report = cs.run_cli(cli_args(tmp), environ=ENV, sender=drive, sleep=lambda s: None)
        self.assertTrue(report["ok"], report)
        self.assertIs(report["resolve_check"]["index_truncated"], False)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["summary"]["files_current"], 3)
        self.assertEqual(report["summary"]["rewalk_versions_new"], 0)
        self.assertEqual(report["resolve_check"]["content_bytes_verified"], 3)
        self.assertEqual(set(report["leak_check"].values()), {0})
        text = json.dumps(report)
        for secret in (NAME_SECRET, TEXT_SECRET, "PO100234", DRIVE, "01FILE", QUALITY_FOLDER,
                       "s3cr3t", "TOKEN"):
            self.assertNotIn(secret, text)
        from verify import live_verify as lv
        root = Path(__file__).resolve().parents[2]
        spec = json.loads((root / "devloop/live-checks/capture.sharepoint.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="capture.sharepoint")
        self.assertEqual({k: v for k, v in report["leak_check"].items()
                          if k.startswith("folder_only")},
                         {"folder_only_actor_rows": 0, "folder_only_actor_counts": 0,
                          "folder_only_actor_resolved": 0})
        report["summary"]["roots_complete"] = 2   # the live scope has two roots
        report["summary"]["texts_ocr"] = 1        # its AO packets are scans
        self.assertEqual(lv.check_expectations(spec, 0, report), [])
        self.assertEqual(lv._recorded(spec, report)["relations"], {"driveItems": 3})
        # The Graph reads the run was answered for: the registry credits their endpoints.
        self.assertEqual(lv._recorded(spec, report)["operations_read"], [
            "graph.drive", "graph.drive_children", "graph.drive_item", "graph.drive_path",
            "graph.drive_permissions"])

    def test_the_cli_is_never_ok_over_a_truncated_index_or_a_failed_byte_read(self):
        # The index stops at INDEX_LIMIT files and any failure of
        # a pinned-bytes read is a typed count, never a crash or an ok over a partial read.
        # The CLI checks each pinned original with ``original_verified`` (held bytes, or
        # a dropped original's whole ``.dropped`` record).
        verified = SharePointCaptureStore.original_verified

        def fails_once(store, handle, *args, **kwargs):
            if "01FILEA" in handle:
                raise RuntimeError("unexpected")
            return verified(store, handle, *args, **kwargs)

        for patch, expected in (
                (mock.patch.object(cs, "INDEX_LIMIT", 2), (True, 2, 2, {})),
                (mock.patch.object(SharePointCaptureStore, "original_verified", fails_once),
                 (False, 3, 2, {"unexpected:RuntimeError": 1}))):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp, patch:
                report = cs.run_cli(cli_args(tmp), environ=ENV, sender=seeded_drive(),
                                    sleep=lambda s: None)
                check = report["resolve_check"]
                self.assertEqual((check["index_truncated"], check["handles_checked"],
                                  check["content_bytes_verified"],
                                  check["content_bytes_failed"]), expected)
                self.assertFalse(report["ok"])

    def test_a_run_cut_by_budget_names_its_incomplete_scope_and_resumes_in_named_host_state(self):
        # Failing first (daily 2026-10-21 capture.sharepoint: the large root was incomplete
        # in both runs of a fresh {tmp}/state and nothing said what was left or why): a root
        # the budget cuts is never complete; the report names the target, the root, its
        # typed reason and whether the cut resumes; a named host state carries the walk to
        # the next invocation, which completes it.
        drive = seeded_drive()
        with tempfile.TemporaryDirectory() as tmp:
            args = cli_args(tmp)
            args.state_dir, args.state_name, args.runs = None, "sp-live-check", 1
            env = {**ENV, "PLX_WORKBENCH_STATE_DIR": str(Path(tmp) / "host")}
            args.budget_seconds = 2
            first = cs.run_cli(args, environ=env, sender=drive, sleep=lambda s: None,
                               monotonic=drive.downloads)
            self.assertFalse(first["ok"])
            self.assertEqual(first["storage"]["state_base"], "workbench_state_dir")
            self.assertTrue(first["storage"]["host_state"])
            # A first run in the host state: the store was not carried into it, so its
            # persistence is unproven (observed, never inferred from where the store lies).
            self.assertFalse(first["storage"]["carried"])
            self.assertEqual(first["incomplete_scope"], [{
                "target": "quality", "root": 0, "reasons": [cs.TIME_BUDGET],
                "resumable": True, "persisted": False,
                "later_ask": "live-host-persistent-state-dir"}])
            self.assertIn("sp-live-check", str(next(Path(tmp, "host").iterdir())))
            # The verifier records the named scope from this real report, and the spec
            # still fails the cut run (never a pass on a partial root).
            from verify import live_verify as lv
            spec = json.loads((Path(__file__).resolve().parents[2]
                               / "devloop/live-checks/capture.sharepoint.json")
                              .read_text(encoding="utf-8"))
            recorded = lv._recorded(spec, first)
            self.assertEqual(recorded["incomplete_scope[*].target"], ["quality"])
            self.assertEqual(recorded["incomplete_scope[*].reasons"], [[cs.TIME_BUDGET]])
            self.assertTrue(lv.check_expectations(spec, 1, first))
            # The budget alone cut it over a store not carried: blocked later_ask, never
            # a pass and never an untyped fail.
            self.assertEqual(lv.blocked_parts(first), [
                {"part": "quality.root0", "source": "microsoft-graph", "cause": "later_ask"}])
            args.budget_seconds = 1000
            second = cs.run_cli(args, environ=env, sender=drive, sleep=lambda s: None,
                                monotonic=drive.downloads)
            self.assertTrue(second["storage"]["carried"])
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["incomplete_scope"], [])
            self.assertEqual(second["summary"]["files_current"], 3)

    def test_an_incomplete_scope_without_kept_state_names_the_later_ask(self):
        drive = seeded_drive()
        with tempfile.TemporaryDirectory() as tmp:
            args = cli_args(tmp)   # --state-dir under tmp: gone when the check ends
            args.runs, args.budget_seconds = 1, 2
            report = cs.run_cli(args, environ=ENV, sender=drive, sleep=lambda s: None,
                                monotonic=drive.downloads)
        self.assertFalse(report["ok"])
        scope = report["incomplete_scope"]
        self.assertEqual([(e["target"], e["reasons"], e["resumable"], e["persisted"],
                           e["later_ask"]) for e in scope],
                         [("quality", [cs.TIME_BUDGET], True, False,
                           "live-host-persistent-state-dir")])
        # Typed by a later_ask, the run is blocked (never a pass) and the rest still checks.
        from verify import live_verify as lv
        spec = json.loads((Path(__file__).resolve().parents[2]
                           / "devloop/live-checks/capture.sharepoint.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(lv.blocked_parts(report), [
            {"part": "quality.root0", "source": "microsoft-graph", "cause": "later_ask"}])
        self.assertTrue(lv.check_expectations(spec, 1, report))
        self.assertEqual(lv.check_expectations(spec, 0, report, spec["expect_blocked"]), [])
        # A leak or an untyped incomplete reason keeps failing under the blocked expectation.
        self.assertTrue(lv.check_expectations(
            spec, 0, {**report, "leak_check": {**report["leak_check"],
                                               "folder_only_actor_rows": 1}},
            spec["expect_blocked"]))
        untyped = [{**scope[0], "reasons": [cs.TIME_BUDGET, "listing_truncated"]}]
        self.assertEqual(cs.blocked_scope(untyped), [])

    def test_state_name_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**ENV, "PLX_WORKBENCH_STATE_DIR": str(Path(tmp) / "host")}
            for name, state_dir in (("../escape", None), ("a/b", None), ("", None),
                                    ("Upper", None), ("x" * 80, None),
                                    ("ok-name", str(Path(tmp) / "state"))):
                with self.subTest(name=name):
                    args = cli_args(tmp)
                    args.state_dir, args.state_name = state_dir, name
                    with self.assertRaises(ValueError):
                        cs.run_cli(args, environ=env, sender=seeded_drive(),
                                   sleep=lambda s: None)
            self.assertFalse(Path(tmp, "host").exists())

    def test_graph_read_cli_exposes_the_capture(self):
        with mock.patch.object(cs, "run_cli", return_value={"ok": True}) as run, \
                mock.patch("sys.stdout"):
            code = graph_read.main(["sharepoint-capture", "--state-dir", "/tmp/x",
                                    "--live-audience", "a.json"], environ=ENV)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0].scope, str(cs.DEFAULT_SCOPE))


class ColumnsDrive(FakeDrive):
    """The fake library plus each file's listItem (``fields``: item id -> listItem fields,
    as Graph returns them: an empty column is omitted)."""

    def __init__(self, fields: dict[str, dict]) -> None:
        super().__init__()
        self.fields = fields

    def raw(self, drive, iid) -> dict:
        body = super().raw(drive, iid)
        body["sharepointIds"] = {"siteId": "s", "listId": "l", "listItemId": iid}
        return body

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if "/listItem" in url:
            self.calls.append((method, url))
            assert method == "GET", method
            iid = url.split("/items/", 1)[1].split("/", 1)[0]
            return j(200, {"id": iid, "fields": self.fields.get(iid, {}), "eTag": '"li"',
                           "sharepointIds": {"siteId": "s", "listId": "l",
                                             "listItemId": iid}})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class PortalColumnsTests(CaptureCase):
    """QA-06: the capture consumes the Portal-taxonomy columns the Portal states (one column
    list, operating/doc_metadata_taxonomy.py COLUMNS), eTag-bound per version, projected as
    SharePointFile.portal_metadata cited to the listItem; Ref values are mentions."""

    STATED = {"PartyType": "Supplier", "PartyRef": "PO100235", "ItemRef": " PO100235 ",
              "DocStatus": "Approved", "ReviewStatus": "  ", "FileLeafRef": "release.txt",
              "ContentType": "Document"}

    def setUp(self) -> None:
        super().setUp()
        # release.txt (Ricardo and sales) states its columns; the pricing PDF (Ricardo only)
        # names the same PO; the AO packet's columns are all empty.
        self.drive = seeded_drive(ColumnsDrive({"01FILEB": dict(self.STATED),
                                                "01FILEC": {"ItemRef": "PO100235"}}))

    def test_a_listitem_with_party_and_item_refs_is_captured_and_projected_cited(self):
        import sqlite3
        from integrations import live_source_grammar as grammar
        from ontology import mentions, schema as schema_loader, store_ddl
        from ontology.project import SharePointCaptures, build_store
        from ontology.tests.test_mail_documents import StaticPortal, po
        from operating import doc_metadata_taxonomy as taxonomy
        from operating import ontology_tools as ot
        from operating import ontology_tools_traverse as tt

        names = [column["name"] for column in taxonomy.COLUMNS]
        run = self.capture().run(CaptureScope(
            [{"id": "quality", "drive_id": DRIVE, "folders": [QUALITY_FOLDER],
              "max_depth": 3, "max_folders": 50}], list_item_columns="portal_taxonomy"))
        self.assertTrue(run["complete"], run)
        self.assertEqual(self.root(run)["counts"]["list_items_read"], 3)
        self.assertTrue(all(method == "GET" for method, _ in self.drive.calls))
        row = self.store.resolve(self.handle("01FILEB"), CAPTURE, capture_audience(),
                                 text=False)["row"]
        self.assertEqual(row["list_item_fields"]["field_names"], names)
        stated = row["portal_metadata"]
        self.assertEqual((stated["stated_by"], stated["written_to_sources"]),
                         ("stated-by-Portal", False))
        self.assertEqual(stated["columns"], {"PartyType": "Supplier", "PartyRef": "PO100235",
                                             "ItemRef": "PO100235", "DocStatus": "Approved"})
        # Every taxonomy column once: stated, or a typed gap; nothing outside the taxonomy.
        self.assertEqual(sorted([*stated["columns"], *stated["gaps"]]), sorted(names))
        self.assertEqual((stated["gaps"]["ReviewStatus"], stated["gaps"]["CustomerVisible"]),
                         ("empty", "empty_or_not_provisioned"))
        self.assertNotIn("FileLeafRef", json.dumps(stated))
        self.assertEqual(stated["citation"],
                         {"source_api": grammar.drive_list_item_fields_url(DRIVE, "01FILEB"),
                          "list_item_id": "01FILEB", "drive_etag": row["etag"]})

        compiled = schema_loader.load_schema()
        portal = StaticPortal([
            po("public.app_purchase_order", {"id": "p1"}, {"id": "p1", "po_number": "PO100234"}),
            po("public.app_purchase_order", {"id": "p2"}, {"id": "p2", "po_number": "PO100235"})])
        path = Path(self.tmp.name) / "ontology.sqlite"
        with SharePointCaptures(self.state) as files:
            build_store(path, compiled, {"portal_api": portal, "sharepoint": files},
                        clock=lambda: datetime(2026, 10, 3, 12, tzinfo=timezone.utc),
                        post_project={"mentions": mentions.store_mention_links})
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)

        def file_object(iid):
            return conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                "'SharePointFile' AND json_extract(native_key, '$.id') = ?",
                                (iid,)).fetchone()[0]

        value = conn.execute("SELECT value, l1_handle, field_path FROM object_values WHERE "
                             "object_id = ? AND property = 'portal_metadata'",
                             (file_object("01FILEB"),)).fetchone()
        self.assertEqual((json.loads(value[0]), value[1], value[2]),
                         (stated, self.handle("01FILEB"), "row.portal_metadata"))
        # The stated PO is a mention of each file stating it, cited to the captured column.
        p2 = conn.execute("SELECT object_id FROM objects WHERE object_id LIKE ?",
                          ('PurchaseOrder:portal.app_purchase_order:{"id":"p2"}',)).fetchone()[0]
        links = conn.execute("SELECT from_object, reason FROM links WHERE link_type = "
                             "'SharePointFile.mentions_purchase_order' AND to_object = ?",
                             (p2,)).fetchall()
        self.assertEqual({src for src, _ in links},
                         {file_object("01FILEB"), file_object("01FILEC")})
        paths = {c["field_path"] for _, reason in links for f in json.loads(reason)
                 for c in f.get("citations", [])}
        self.assertTrue({"row.list_item_fields.fields.ItemRef",
                         "row.list_item_fields.fields.PartyRef"} <= paths, paths)

        # A reader the item permissions withhold sees neither the file nor its mention.
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:quality", "staff:sales", "staff:outsider"},
            "sharepoint:*": {"staff:quality", "staff:sales", "staff:outsider"}},
            markings={m: {"staff:quality", "staff:sales"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy, item_gate=self.store.item_gate(reader_audience()),
                                schema=ot.SchemaView.from_compiled(compiled), grammar=None,
                                contracts=json.loads((Path(__file__).resolve().parents[2] /
                                                      "operating/ontology_tool_contracts.json")
                                                     .read_text("utf-8")))
        shown = base.call("plx.object", {"object_id": file_object("01FILEB")}, QUALITY)
        (served,) = [v for v in shown["values"] if v["property"] == "portal_metadata"]
        self.assertEqual((served["citation"]["l1_handle"], served["citation"]["field_path"]),
                         (self.handle("01FILEB"), "row.portal_metadata"))
        self.assertIn("PO100235", json.dumps(served))
        withheld = file_object("01FILEC")
        self.assertEqual(base.call("plx.object", {"object_id": withheld}, EMPLOYEE)
                         ["error"]["code"], "object_not_found")
        around = tt.TraverseTools(base).call("plx.around", {
            "from": p2, "path": [{"link_type": "SharePointFile.mentions_purchase_order",
                                  "direction": "reverse"}], "tier_floor": "lead"}, EMPLOYEE)
        self.assertEqual({o["object_id"] for o in around["objects"]}, {file_object("01FILEB")})
        for actor in (EMPLOYEE, OUTSIDER):
            self.assertIsNone(self.store.resolve(self.handle("01FILEC"), actor,
                                                 reader_audience()))

    def test_a_column_set_version_change_rereads_an_unchanged_item_once(self):
        # DECIDED (portal-columns-followups): a version read before a reader change is read
        # again once, although its listItem and eTag are unchanged and the root only
        # follows its feed; the earlier interval keeps what was read then.
        from integrations.tests.test_capture_breadth import DeltaDrive   # imports this module

        class Drive(ColumnsDrive, DeltaDrive):
            pass

        self.drive = Drive({"01FILEB": {"PartyType": "Supplier", "ItemRef": "PO100235"}})
        self.drive.folder("01QUALITY", "Quality")
        self.drive.folder("01AOS", "AOs", parent="01QUALITY")
        self.drive.file("01FILEB", "release.txt", "01AOS", b"release", mime="text/plain",
                        acl=(RICARDO, SALES))
        self.drive.file("01FILED", "notes.txt", "01AOS", b"notes", mime="text/plain",
                        acl=(RICARDO, SALES))
        columns = CaptureScope([{"id": "quality", "drive_id": DRIVE,
                                 "folders": [QUALITY_FOLDER]}], full_walk_hours=72,
                               list_item_columns="portal_taxonomy")
        handle = self.handle("01FILEB")

        def run(hours=1):
            self.store._clock.now += timedelta(hours=hours)
            mark = len(self.drive.calls)
            root = self.root(self.capture().run(columns))
            return root, [u for _m, u in self.drive.calls[mark:] if "/listItem" in u]

        def stated():
            row = self.store.resolve(handle, CAPTURE, capture_audience(), text=False)["row"]
            return row["portal_metadata"]["columns"]["ItemRef"]

        root, reads = run(0)
        self.assertEqual((root["round"], root["counts"]["list_items_read"],
                          root["counts"]["list_items_reread"], len(reads)), ("full", 2, 0, 2))
        version = self.store.current(DRIVE, "01FILEB")
        self.assertEqual(json.loads(version["record"])["list_item_columns_version"],
                         cs.column_set_version(columns.list_item_columns))
        # The Portal restates a column; nothing lists a change, and the same reader reads
        # nothing again.
        self.drive.fields["01FILEB"]["ItemRef"] = "PO100236"
        root, reads = run()
        self.assertEqual((root["round"], root["counts"]["list_items_reread"], reads),
                         ("incremental", 0, []))
        self.assertEqual(stated(), "PO100235")
        # A changed reader re-reads every current version under the root once, in follow
        # mode, with no new eTag: the new interval serves the column read now.
        with mock.patch.object(cs, "LIST_ITEM_READER_VERSION", 2):
            root, reads = run()
            self.assertEqual((root["round"], root["complete"], root["counts"]["list_items_reread"],
                              root["counts"]["list_items_read"], root["counts"]["versions_new"],
                              len(reads)), ("incremental", True, 2, 2, 0, 2))
            self.assertEqual(stated(), "PO100236")
            root, reads = run()
            self.assertEqual((root["counts"]["list_items_reread"], reads), (0, []))
            # A full walk due later lists the item unchanged and reads nothing again.
            root, reads = run(72)
            self.assertEqual((root["round"], root["counts"]["list_items_reread"], reads),
                             ("full", 0, []))
        conn = self.store.db
        intervals = conn.execute(
            "SELECT json_extract(record, '$.list_item_fields.fields.ItemRef'), "
            "superseded_at IS NULL FROM items WHERE l1_handle = ? ORDER BY version_id",
            (handle,)).fetchall()
        self.assertEqual([tuple(r) for r in intervals], [("PO100235", 0), ("PO100236", 1)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_events WHERE l1_handle = ? "
                                      "AND event = 'columns_reread'", (handle,)).fetchone()[0],
                         1)

    def test_a_person_confirmed_column_that_differs_from_the_portal_is_a_conflict(self):
        # QA-06: the projection puts beside each confirmation the confirmed columns against
        # SharePointFile.portal_metadata of the same version; a differing one is a conflict
        # with both values kept, and a reader the file withholds sees neither.
        import sqlite3
        from ontology import schema as schema_loader, store_ddl
        from ontology.project import SharePointCaptures, build_store
        from ontology.tests.test_mail_documents import StaticPortal
        from operating import kind_confirmation
        from operating import ontology_tools as ot

        run = self.capture().run(CaptureScope(
            [{"id": "quality", "drive_id": DRIVE, "folders": [QUALITY_FOLDER],
              "max_depth": 3, "max_folders": 50}], list_item_columns="portal_taxonomy"))
        self.assertTrue(run["complete"], run)
        path = Path(self.tmp.name) / "kind-confirmations.sqlite"
        store = kind_confirmation.KindConfirmations(path)
        db = store._db()
        try:
            for n, (iid, fields) in enumerate((
                    ("01FILEB", {"PartyType": "Supplier", "PartyRef": "PO100234",
                                 "DocStatus": "Approved", "DocumentCategory": "COA"}),
                    ("01FILEC", {"ItemRef": "PO100236"}))):
                current = self.store.current(DRIVE, iid)
                original = f"orig-{n}" + "0" * 31
                origin = {"source": "sharepoint-drive", "drive_id": DRIVE, "item_id": iid,
                          "etag": current["etag"]}
                record = {"schema": kind_confirmation.SCHEMA, "id": f"kc-rec-{n:024d}",
                          "tenant": "plx", "original_id": original,
                          "citation": f"original://{original}",
                          "original_sha256": current["content_sha256"], "provenance": origin,
                          "confirmed_fields": fields, "document_kind": None,
                          "amended_columns": sorted(fields),
                          "confirmed_by": "employee:00000000-0000-0000-0000-00000000000a",
                          "confirmed_at": current["recorded_at"], "written_to_sources": False,
                          "supersedes": None}
                text = kind_confirmation._canon(record)
                db.execute("INSERT INTO confirmation (id, tenant, original_id, drive_id, "
                           "item_id, record_json, record_sha256, supersedes) VALUES "
                           "(?,?,?,?,?,?,?,?)", (record["id"], "plx", original, DRIVE, iid,
                                                 text, kind_confirmation._sha(text), None))
        finally:
            db.close()
        compiled = schema_loader.load_schema()
        built = Path(self.tmp.name) / "ontology.sqlite"
        with SharePointCaptures(self.state) as files:
            report = build_store(built, compiled, {"portal_api": StaticPortal([]),
                                                   "sharepoint": files},
                                 clock=lambda: datetime(2026, 10, 3, 12, tzinfo=timezone.utc),
                                 post_project={"confirmations": kind_confirmation
                                               .projection_step(path, "plx")})
        counts = report["post_project"]["confirmations"]["counts"]
        self.assertEqual((counts["projected"], counts["portal_conflicts"]), (2, 2))
        conn = sqlite3.connect(built)
        self.addCleanup(conn.close)

        def against(iid):
            (value,), = conn.execute(
                "SELECT v.value FROM object_values v JOIN objects o ON o.object_id = "
                "v.object_id WHERE o.object_type = 'SharePointFile' AND json_extract("
                "o.native_key, '$.id') = ? AND v.property = 'proposed_portal_metadata'",
                (iid,)).fetchall()
            return json.loads(value)["against_portal"]

        b = against("01FILEB")
        self.assertEqual((b["status"], b["conflicts"],
                          {n: r["comparison"] for n, r in b["columns"].items()}),
                         ("conflict", ["PartyRef"],
                          {"PartyType": "agree", "PartyRef": "conflict", "DocStatus": "agree",
                           "DocumentCategory": "portal_empty"}))
        self.assertEqual([(c["value"], c["basis"]) for c in b["columns"]["PartyRef"]
                          ["candidates"]], [("PO100234", "person_confirmed"),
                                            ("PO100235", "stated_by_portal")])
        self.assertEqual(b["stated_citation"]["drive_etag"],
                         self.store.current(DRIVE, "01FILEB")["etag"])
        self.assertEqual((against("01FILEC")["status"], against("01FILEC")["conflicts"]),
                         ("conflict", ["ItemRef"]))
        # Permission-first: the file the item permissions withhold from sales is served to
        # no such reader, conflict and all.
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:quality", "staff:sales"},
            "sharepoint:*": {"staff:quality", "staff:sales"}},
            markings={m: {"staff:quality", "staff:sales"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(built)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(reader, policy, item_gate=self.store.item_gate(reader_audience()),
                                 schema=ot.SchemaView.from_compiled(compiled), grammar=None,
                                 contracts=json.loads((Path(__file__).resolve().parents[2] /
                                                       "operating/ontology_tool_contracts.json")
                                                      .read_text("utf-8")))
        (withheld,), = conn.execute("SELECT object_id FROM objects WHERE object_type = "
                                    "'SharePointFile' AND json_extract(native_key, '$.id') = "
                                    "'01FILEC'").fetchall()
        self.assertEqual(tools.call("plx.object", {"object_id": withheld}, EMPLOYEE)
                         ["error"]["code"], "object_not_found")
        shown = tools.call("plx.object", {"object_id": withheld}, QUALITY)
        (served,) = [v for v in shown["values"] if v["property"] == "proposed_portal_metadata"]
        self.assertIn("PO100236", json.dumps(served))
        found = tools.call("plx.find", {"object_type": "SharePointFile", "filters": [
            {"property": "document_category", "op": "eq", "value": "COA"}]}, EMPLOYEE)
        self.assertNotIn(withheld, json.dumps(found))

    def test_the_scope_reads_the_taxonomy_columns_and_refuses_any_other(self):
        from operating import doc_metadata_taxonomy as taxonomy

        names = tuple(column["name"] for column in taxonomy.COLUMNS)
        target = [{"id": "a", "drive_id": DRIVE}]
        self.assertEqual(CaptureScope(target, list_item_columns="portal_taxonomy")
                         .list_item_columns, names)
        for bad, code in ((["ContentType"], "scope_list_item_column_not_in_taxonomy"),
                          (["DocumentCategory", "FileLeafRef"],
                           "scope_list_item_column_not_in_taxonomy"),
                          ("DocumentCategory", "scope_list_item_columns_invalid")):
            with self.subTest(bad=bad), self.assertRaises(CaptureError) as raised:
                CaptureScope.from_json({"schema": cs.SCOPE_SCHEMA, "targets": target,
                                        "list_item_columns": bad})
            self.assertEqual(raised.exception.code, code)
        root = Path(__file__).resolve().parents[2]
        for shipped in ("integrations/sharepoint_estate_scope.json",
                        "examples/sharepoint_capture_scope.customer_documents.live-check.json",
                        "examples/issued_sends_sharepoint_capture_scope.live-check.json"):
            self.assertEqual(CaptureScope.from_file(root / shipped).list_item_columns, names,
                             shipped)


class SiteReadbackTests(unittest.TestCase):
    """The
    estate check reads each registry site back by counts and a typed reason. The
    estate fixtures live in the estate test module (imported here to avoid a cycle)."""

    def test_each_registry_site_is_read_back_by_counts_and_a_reason_code(self):
        from integrations.tests import test_capture_sharepoint_estate as te

        sites = ("Quality", "AGENTS", "HR", "Legacy", "Partner", "Elsewhere")
        registry = [{"id": f"sharepoint.site:{n}", "system": "sharepoint", "kind": "site",
                     "name": n, "evidence": ["portal:x@000000000000"]} for n in sites]
        drive = te.estate_drive(
            sites=(te.Q_SITE, SITE_ID, te.HR_SITE, te.LEGACY_SITE, te.OTHER_SITE),
            libraries={te.Q_SITE: [(DRIVE, "Documents")], SITE_ID: [(SITE_DRIVE, "Documents")],
                       te.HR_SITE: [(te.HR_DRIVE, "Documents")],
                       te.OTHER_SITE: [(te.OTHER_DRIVE, "Documents")]},
            forbidden=(te.HR_DRIVE,), unlistable=(te.LEGACY_SITE,))
        del drive.items[f"{SITE_DRIVE}:01CASES"], drive.items[f"{SITE_DRIVE}:01USE"]   # AGENTS: empty
        report = te.EstateCliTests.run_cli(self, drive, registry, home=None)
        readback = report["registry_sites"]["readback"]
        quality = readback.pop("Quality")
        self.assertGreater(quality["items_read"], 0)
        self.assertEqual((quality["reason"], quality["withheld"]), (None, 0))
        self.assertEqual(quality["items_listed"], quality["items_read"])
        zero = {"libraries": 1, "items_listed": 0, "items_read": 0, "withheld": 0}
        self.assertEqual(readback, {
            "AGENTS": {**zero, "reason": "empty_site"},
            "HR": {**zero, "reason": "no_access"},            # 403 on its library
            "Legacy": {**zero, "libraries": 0, "reason": "no_access"},   # listing 403
            "Partner": {**zero, "libraries": 0, "reason": "not_in_scope"},   # other host
            "Elsewhere": {**zero, "libraries": 0, "reason": "not_in_scope"}})
        # A site walked whole that holds no file is never credited captured.
        outcomes = report["registry_sites"]
        self.assertEqual(outcomes["captured"], ["Quality"])
        self.assertEqual(outcomes["reasons"]["AGENTS"], "empty_site")
        self.assertIn("AGENTS", outcomes["unexercised"])
        # Counts and codes only: no name, path, id or body in the record.
        text = json.dumps(readback)
        for secret in (te.USE_CASE, "Board", DRIVE, SITE_DRIVE, te.HR_DRIVE, te.Q_SITE):
            self.assertNotIn(secret, text)
        for row in readback.values():
            self.assertTrue(all(isinstance(v, (int, str, type(None))) for v in row.values()))

    def test_a_root_cut_in_one_run_is_walked_before_the_roots_cut_again_in_the_next(self):
        # ING-16: with a budget for one root's walk a day, each run resumes first the
        # root the last run cut, every run moves each root it reaches on (each slice
        # ends at least one folder), the first place rotates to another root once the
        # root holding it completes, and every root completes within four runs.
        #
        # The original check here ("the first root differs on more than two days") is
        # false by design since the oldest resumed walk keeps a least slice
        # (capture_sharepoint.ROOT_SLICE_MIN_SECONDS) and goes first until it completes:
        # the first place moves only when that root is done. So the rotation asserted is
        # explicit: day one reaches every readable root, the first root of each day is
        # the one the last run cut, a root that held the first place and completed never
        # holds it again, and the first place holds at least two roots in four days.
        from integrations.tests import test_capture_sharepoint_estate as te

        class Rotating(te.EstateCase):
            def runTest(self):
                pass

        def drive_of(url):
            return re.search(r"/drives/([^/]+)/", unquote(url))[1]

        case = Rotating()
        case.setUp()
        try:
            cut, first, orders, done = None, [], [], []
            for _day in range(4):
                mark = len(case.drive.calls)
                run = case.capture(budget_seconds=3, monotonic=lambda: sum(
                    1 for _m, u in case.drive.calls[mark:]
                    if u.split("?")[0].endswith("/children"))).run(te.estate_scope())
                order = []
                for _m, u in case.drive.calls[mark:]:
                    if (u.split("?")[0].endswith("/children")
                            and drive_of(u) not in order):
                        order.append(drive_of(u))
                orders.append(order)
                first.append(order[0])
                if cut is not None:
                    self.assertEqual(first[-1], cut)   # the root cut last run, first now
                self.assertNotIn(first[-1], done)      # a completed first never returns
                roots = case.roots(run)
                if roots[first[-1]]["reason"] is None:
                    done.append(first[-1])
                cut = next((d for d, r in roots.items() if r["reason"] == cs.TIME_BUDGET
                            and not r.get("deferred")), None)
            readable = {d for d, r in roots.items() if r["reason"] is None}
            # Day one's first pass reaches every root whose listing is readable (all but
            # the 403 library), not only the first one.
            self.assertEqual(len(readable), 3)
            self.assertEqual(set(orders[0]), readable)
            # The first place rotated: more than one root held it across the four days.
            self.assertGreaterEqual(len(set(first)), 2)
            self.assertNotIn(cs.TIME_BUDGET, [r["reason"] for r in roots.values()])
        finally:
            case.tearDown()

if __name__ == "__main__":
    unittest.main()
