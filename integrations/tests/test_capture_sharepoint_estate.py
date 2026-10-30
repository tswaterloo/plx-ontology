"""SharePoint estate capture (integrations/capture_sharepoint.py ``discover``) over an
injected Graph transport: paged site and library enumeration, the data-driven exclusion
list, a forbidden library and an unlistable site as gaps, change-feed resume per
discovered library, per-reader trimming through ``plx.find`` / ``plx.documents``, the
tenant's OneDrive host in scope (item permissions re-gated per reader) and estate progress
kept across CLI invocations in the host state directory, a cut refresh resumed before new
roots, each root's full-walk age, the default store bound and pruning of older captures.
No network, no credentials.
Evidence: injected-transport."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, quote, unquote, urlsplit

from integrations import capture_sharepoint as cs
from integrations import graph_read
from integrations import live_source_grammar as grammar
from integrations.capture_sharepoint import CaptureScope, SharePointCapture, version_handle
from integrations.graph_read import SharePointReader
from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience,
                                            ReadOnlyGuardError, check_request)
from integrations.tests import test_capture_sharepoint as ts
from integrations.tests.test_capture_breadth import DeltaDrive, seeded_delta_drive
from ontology.types import Actor

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
Q_SITE = (f"{ts.SP_HOST},22222222-0000-0000-0000-000000000001,"
          "22222222-0000-0000-0000-000000000002")
HR_SITE = (f"{ts.SP_HOST},33333333-0000-0000-0000-000000000001,"
           "33333333-0000-0000-0000-000000000002")
LEGACY_SITE = (f"{ts.SP_HOST},44444444-0000-0000-0000-000000000001,"
               "44444444-0000-0000-0000-000000000002")
# The tenant's OneDrive host (personal sites) and a third host the guard never downloads from.
MY_HOST = "contoso-plx-my.sharepoint.com"
PERSONAL_SITE = (f"{MY_HOST},55555555-0000-0000-0000-000000000001,"
                 "55555555-0000-0000-0000-000000000002")
OTHER_SITE = ("contoso-other.sharepoint.com,66666666-0000-0000-0000-000000000001,"
              "66666666-0000-0000-0000-000000000002")
PERSONAL_DRIVE = "b!personal-Drive_07"
OTHER_DRIVE = "b!partner-Drive_08"
OWNER = "owner@example-plx.com"
# The excluded person of ``ts.ENV`` (synthetic ID): their OneDrive is never listed.
EXCLUDED_OID = ts.ENV["PLX_EXCLUDED_PEOPLE_OIDS"]
EXCLUDED_UPN = "excluded.person@example-plx.com"
EXCLUDED_PATH = "excluded_person_example-plx_com"
COLLEAGUE = "colleague@example-plx.com"
MY_FILE = "travel notes.txt"
FORBIDDEN = "b!board-Drive_03"
ASSETS = "b!assets-Drive_04"
HR_DRIVE = "b!hr-Drive_05"
AGENTS_USER = "agents@example-plx.com"
USE_CASE = "use case PO100234.txt"
SITES = {Q_SITE: f"https://{ts.SP_HOST}/sites/Quality",
         ts.SITE_ID: f"https://{ts.SP_HOST}/sites/AGENTS",
         HR_SITE: f"https://{ts.SP_HOST}/sites/HR",
         LEGACY_SITE: f"https://{ts.SP_HOST}/sites/Legacy",
         PERSONAL_SITE: f"https://{MY_HOST}/personal/someone",
         OTHER_SITE: "https://contoso-other.sharepoint.com/sites/Partner"}
LIBRARIES = {Q_SITE: [(ts.DRIVE, "Documents"), (ASSETS, "Site Assets")],
             ts.SITE_ID: [(ts.SITE_DRIVE, "Documents"), (FORBIDDEN, "Board")],
             HR_SITE: [(HR_DRIVE, "Documents")],
             PERSONAL_SITE: [(PERSONAL_DRIVE, "Documents")],
             OTHER_SITE: [(OTHER_DRIVE, "Documents")]}
NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
QUALITY = Actor("plx", frozenset({"staff:quality"}), "restricted")
AGENTS = Actor("plx", frozenset({"staff:agents"}), "restricted")
OWNER_ACTOR = Actor("plx", frozenset({"staff:owner"}), "restricted")
COLLEAGUE_ACTOR = Actor("plx", frozenset({"staff:colleague"}), "restricted")


class EstateDrive(DeltaDrive):
    """The delta drive fake plus ``/sites/getAllSites`` and ``/sites/{id}/drives``, both
    in two pages; ``forbidden`` drives and ``unlistable`` sites answer HTTP 403.
    ``personal``: OneDrive libraries (drive id -> a delta fake of its own whose quality
    drive answers under that id), their downloads served on the OneDrive host only."""

    def __init__(self, sites=tuple(SITES), libraries=None, forbidden=(FORBIDDEN,),
                 unlistable=(LEGACY_SITE,), personal=None):
        super().__init__()
        self.sites, self.forbidden, self.unlistable = list(sites), set(forbidden), set(unlistable)
        self.libraries = LIBRARIES if libraries is None else libraries
        self.personal = dict(personal or {})
        self.drive_types: dict[str, str] = {}   # a drive's driveType, by id, overriding
        self.locked: set[str] = set()   # sites whose drives listing answers HTTP 423
        # The tenant directory (``GET /users``, one page) the people guard binds from;
        # ``users_status`` other than 200 answers that error.
        self.users = [{"id": EXCLUDED_OID, "userPrincipalName": EXCLUDED_UPN,
                       "mail": EXCLUDED_UPN, "proxyAddresses": [f"SMTP:{EXCLUDED_UPN}"]},
                      {"id": "0e0e0e0e-0000-4000-8000-0000000000a1",
                       "userPrincipalName": OWNER, "mail": OWNER, "proxyAddresses": []}]
        self.users_status = 200

    def _onedrive(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == MY_HOST:
            drive = parse_qs(parts.query)["drive"][0]
        else:
            drive = re.match(r"/v1\.0/drives/([^/]+)", unquote(parts.path))[1]
        if drive not in self.personal:
            return None
        self.calls.append((method, url))
        assert method == "GET", method
        inner = (url.replace(quote(drive, safe=""), ts.DRIVE).replace(drive, ts.DRIVE)
                 .replace(f"//{MY_HOST}/", f"//{ts.SP_HOST}/"))
        status, answer_headers, answer = self.personal[drive](method, inner, headers, body,
                                                              timeout, max_bytes)
        if parts.hostname != MY_HOST:   # Graph JSON: its ids, links and download host
            answer = (answer.replace(ts.DRIVE.encode(), drive.encode())
                      .replace(ts.SP_HOST.encode(), MY_HOST.encode()))
        return status, answer_headers, answer

    @staticmethod
    def _paged(values, url, token):
        if token is None and len(values) > 1:
            return ts.j(200, {"value": values[:len(values) // 2 + 1],
                              "@odata.nextLink": f"{url}?$skiptoken=p2"})
        return ts.j(200, {"value": values[len(values) // 2 + 1:] if token else values})

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        path = unquote(parts.path)
        token = parse_qs(parts.query).get("$skiptoken", [None])[0]
        if parts.hostname == MY_HOST or (parts.hostname == "graph.microsoft.com"
                                         and path.startswith("/v1.0/drives/")):
            answer = self._onedrive(method, url, headers, body, timeout, max_bytes)
            if answer is not None:
                return answer
        if parts.hostname == "graph.microsoft.com" and path == "/v1.0/users":
            self.calls.append((method, url))
            assert method == "GET", method
            if self.users_status != 200:
                return ts.j(self.users_status, {"error": {"code": "serviceUnavailable"}})
            return ts.j(200, {"value": self.users})
        if parts.hostname == "graph.microsoft.com":
            site = re.fullmatch(r"/v1\.0/sites/([^/]+)/drives", path)
            drive = re.match(r"/v1\.0/drives/([^/]+)/", path)
            if path == "/v1.0/sites/getAllSites" or site or (drive and drive[1] in
                                                              self.forbidden):
                self.calls.append((method, url))
                assert method == "GET", method
            if path == "/v1.0/sites/getAllSites":
                return self._paged([{"id": s, "name": "x", "webUrl": SITES[s]}
                                    for s in self.sites], f"{GRAPH_BASE}/sites/getAllSites",
                                   token)
            if site and site[1] in self.unlistable:
                return ts.j(403, {"error": {"code": "accessDenied"}})
            if site and site[1] in self.locked:
                return ts.j(423, {"error": {"code": "resourceLocked"}})
            if site:
                # Graph types a OneDrive for Business drive "business", a site library
                # "documentLibrary".
                default = "business" if site[1].startswith(MY_HOST + ",") else "documentLibrary"
                return self._paged([{"id": d, "name": n,
                                     "driveType": self.drive_types.get(d, default)}
                                    for d, n in self.libraries.get(site[1], [])],
                                   f"{GRAPH_BASE}/sites/{site[1]}/drives", token)
            if drive and drive[1] in self.forbidden:
                return ts.j(403, {"error": {"code": "accessDenied"}})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


def personal_drive() -> DeltaDrive:
    """One user's OneDrive library: a file shared with its owner only."""
    mine = DeltaDrive()
    mine.folder("01NOTES", "Notes")
    mine.file("01MINE", MY_FILE, "01NOTES", b"travel notes, the owner's only",
              mime="text/plain", acl=(OWNER,))
    return mine


def estate_drive(**kwargs) -> EstateDrive:
    drive = EstateDrive(**{"personal": {PERSONAL_DRIVE: personal_drive()}, **kwargs})
    seeded = seeded_delta_drive()
    drive.items, drive.drive_of = seeded.items, seeded.drive_of
    drive.log, drive.seq = seeded.log, seeded.seq
    drive.folder("01CASES", "UseCases", drive=ts.SITE_DRIVE)
    drive.file("01USE", USE_CASE, "01CASES", b"Customer PO100234 use case", mime="text/plain",
               acl=(AGENTS_USER,), drive=ts.SITE_DRIVE)
    return drive


def estate_scope(*targets, exclude=({"site": "HR"}, {"library": "Site Assets"}), **kwargs):
    return CaptureScope(list(targets), discover={"max_depth": 4, "max_folders": 50,
                                                 "exclude": list(exclude)}, **kwargs)


def reader_audience() -> LiveSourceAudience:
    """Quality holds the quality library, agents the AGENTS library, the owner and a
    colleague the personal drive; item permissions are required everywhere."""
    return LiveSourceAudience(
        {f"sharepoint:{ts.DRIVE}:*": ["staff:quality"],
         f"sharepoint:{ts.SITE_DRIVE}:*": ["staff:agents"],
         f"sharepoint:{PERSONAL_DRIVE}:*": ["staff:owner", "staff:colleague"]},
        tenant="plx", identities={"staff:quality": [ts.RICARDO], "staff:agents": [AGENTS_USER],
                                  "staff:owner": [OWNER], "staff:colleague": [COLLEAGUE]},
        source_acl_required=["sharepoint:*"])


class EstateCase(ts.CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.drive = estate_drive()

    def capture(self, **kwargs) -> SharePointCapture:
        api, transport = graph_read.build_graph(
            ts.ENV, sender=self.drive,
            retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

        def reader(drive_id):
            return SharePointReader(api, ts.capture_audience(), drive_id, transport=transport,
                                    read_permissions=True, max_acl_reads=cs.MAX_ACL_READS)

        return SharePointCapture(self.store, reader, ts.CAPTURE,
                                 estate=graph_read.SharePointSites(api, ts.SP_HOST), **kwargs)

    @staticmethod
    def roots(run):
        return {t["drive_id"]: t["roots"][0] for t in run["targets"] if t.get("discovered")}

    def urls(self, start=0):
        return [unquote(u) for _m, u in self.drive.calls[start:]]


class EstateDiscoveryTests(EstateCase):
    def test_a_business_drive_is_planned_on_the_onedrive_host_only(self):
        # Graph types a user's OneDrive "business": planned on the tenant OneDrive host;
        # on the configured host only a documentLibrary is walked, as before.
        self.drive.drive_types = {ts.SITE_DRIVE: "business"}
        run = self.capture().run(estate_scope())
        counts = run["discovery"]["counts"]
        self.assertEqual((counts["libraries_not_document_library"], counts["libraries_planned"],
                          counts["libraries_personal_planned"]), (1, 3, 1))
        self.assertIn(PERSONAL_DRIVE, self.roots(run))
        self.assertNotIn(ts.SITE_DRIVE, self.roots(run))
        self.assertIsNone(self.store.current(ts.SITE_DRIVE, "01USE"))

    def test_every_readable_library_is_discovered_paged_and_gaps_are_never_dropped(self):
        run = self.capture().run(estate_scope())
        discovery = run["discovery"]
        # The personal site (tenant OneDrive host) is listed and planned like any site;
        # the site on a third host is counted, never listed.
        self.assertEqual(discovery["counts"], {
            "sites_seen": 6, "sites_other_host": 1, "sites_personal": 1,
            "sites_locked": 0, "sites_excluded": 1, "sites_people_excluded": 0,
            "sites_people_withheld": 0, "sites_listed": 3,
            "libraries_seen": 5, "libraries_not_document_library": 0,
            "libraries_excluded": 1, "libraries_covered_by_target": 0,
            "libraries_planned": 4, "libraries_personal_planned": 1,
            "libraries_restricted_unclassified": 0})
        # An unlistable site and a forbidden library are named gaps, never dropped.
        self.assertEqual(discovery["gaps"], ["library_listing:http_403"])
        roots = {d: (r["status"], r["reason"]) for d, r in self.roots(run).items()}
        self.assertEqual(roots, {ts.DRIVE: ("complete", None),
                                 ts.SITE_DRIVE: ("complete", None),
                                 PERSONAL_DRIVE: ("complete", None),
                                 FORBIDDEN: ("incomplete", "source_error:http_403")})
        self.assertFalse(run["complete"])
        self.assertEqual((run["complete_bound"], run["bound_total"]), (3, 5))
        self.assertEqual(run["incomplete_reasons"],
                         ["library_listing:http_403", "source_error:http_403"])
        self.assertIsNotNone(self.store.current(ts.SITE_DRIVE, "01USE"))
        self.assertIsNotNone(self.store.current(ts.DRIVE, "01FILEB"))
        # The personal file with its item permissions, its bytes from the OneDrive host
        # (the real guard admits that host and no other).
        mine = self.store.current(PERSONAL_DRIVE, "01MINE")
        self.assertIsNotNone(mine)
        self.assertEqual(self.store.resolve(mine["l1_handle"], OWNER_ACTOR, reader_audience())
                         ["source_acl"]["status"], "read")
        self.assertEqual(self.store.content(f"{mine['l1_handle']}#sha256="
                                            f"{mine['content_sha256']}", OWNER_ACTOR,
                                            reader_audience()),
                         b"travel notes, the owner's only")
        self.assertEqual(len([u for _m, u in self.drive.calls
                              if urlsplit(u).hostname == MY_HOST]), 1)
        self.assertEqual(run["discovery"]["sites"][None], "listed")
        self.assertEqual(cs.registry_site_outcomes(run, ["Partner"])["reasons"],
                         {"Partner": "other_host"})
        urls = self.urls()
        self.assertEqual(len([u for u in urls if "/sites/getAllSites" in u]), 2)   # paged
        self.assertEqual(len([u for u in urls if f"/sites/{ts.SITE_ID}/drives" in u]), 2)
        # Excluded by data or out of host: never listed, never read.
        for never in (HR_SITE, HR_DRIVE, ASSETS, OTHER_SITE, OTHER_DRIVE):
            self.assertFalse([u for u in urls if never in u], never)
        # GET only: the fake asserts every method; the guard refuses anything else.
        self.assertTrue(all(m == "GET" for m, _u in self.drive.calls))
        for op, params in (("graph.sites_all", {}), ("graph.site_drives", {"site": Q_SITE})):
            check_request("GET", grammar.url(op, **params))
            with self.assertRaises(ReadOnlyGuardError):
                check_request("POST", grammar.url(op, **params))

    def test_discovered_libraries_resume_from_their_change_feed(self):
        capture = self.capture()
        capture.run(estate_scope())
        mark = len(self.drive.calls)
        self.drive.file("01USE2", "second use case.txt", "01CASES", b"second", mime="text/plain",
                        acl=(AGENTS_USER,), drive=ts.SITE_DRIVE)
        self.drive.change("01FILEB", b"release two")
        run = capture.run(estate_scope())
        rounds = {d: (r["round"], r["status"], r["cursor"]) for d, r in self.roots(run).items()}
        self.assertEqual(rounds[ts.DRIVE], ("incremental", "complete", "drive_delta"))
        self.assertEqual(rounds[ts.SITE_DRIVE], ("incremental", "complete", "drive_delta"))
        self.assertEqual(rounds[FORBIDDEN][:2], ("full", "incomplete"))
        self.assertIsNotNone(self.store.current(ts.SITE_DRIVE, "01USE2"))
        self.assertEqual(self.store.current(ts.DRIVE, "01FILEB")["etag"], '"{01FILEB},2"')
        urls = self.urls(mark)
        for drive in (ts.DRIVE, ts.SITE_DRIVE):
            self.assertFalse([u for u in urls if f"/drives/{drive}/" in u and "/children" in u])
            feed = [u for u in urls if f"/drives/{drive}/root/delta" in u]
            self.assertEqual(len(feed), 1)
            self.assertNotIn("token=latest", feed[0])

    def test_a_folder_larger_than_the_old_permission_bound_is_captured_complete(self):
        # The live estate's budget-cut root also carried acl_read_bound: a listing read
        # item permissions for 500 items only. Every listed item's are read now.
        self.drive.folder("01BIG", "Big", drive=ts.SITE_DRIVE)
        for n in range(501):
            self.drive.file(f"01B{n:04d}", f"f{n}.txt", "01BIG", b"x", mime="text/plain",
                            acl=(AGENTS_USER,), drive=ts.SITE_DRIVE)
        run = self.capture(max_content_bytes=1).run(estate_scope())
        root = self.roots(run)[ts.SITE_DRIVE]
        self.assertEqual((root["status"], root["reason"]), ("complete", None))
        last = self.store.resolve(self.store.current(ts.SITE_DRIVE, "01B0500")["l1_handle"],
                                  AGENTS, reader_audience())
        self.assertEqual(last["source_acl"]["status"], "read")   # served by its own grant

    def test_a_site_is_captured_only_when_every_library_of_it_is_walked(self):
        # A site was credited captured when one library was
        # walked although another was cut by the time budget.
        self.drive = estate_drive(sites=(Q_SITE, ts.SITE_ID, PERSONAL_SITE), libraries={
            Q_SITE: [(ts.DRIVE, "Documents"), (ts.SITE_DRIVE, "Records")], ts.SITE_ID: [],
            PERSONAL_SITE: [("b!personal-Drive_07", "Documents")]})

        def clock():   # the budget is spent once the second library's walk lists it
            return sum(1 for _m, u in self.drive.calls
                       if ts.SITE_DRIVE in u and u.split("?")[0].endswith("/children"))

        run = self.capture(budget_seconds=1, monotonic=clock).run(estate_scope())
        roots = {d: (r["status"], r["reason"]) for d, r in self.roots(run).items()}
        self.assertEqual(roots, {ts.DRIVE: ("complete", None),
                                 ts.SITE_DRIVE: ("incomplete", cs.TIME_BUDGET),
                                 PERSONAL_DRIVE: ("incomplete", cs.TIME_BUDGET)})
        self.assertEqual(cs.registry_site_outcomes(run, ["Quality", "AGENTS", "Elsewhere"]), {
            "captured": [], "partial": ["Quality"], "unexercised": ["AGENTS"],
            "failed": ["Elsewhere"],
            "reasons": {"Quality": cs.TIME_BUDGET, "AGENTS": "no_library_planned",
                        "Elsewhere": "not_listed"}})
        run = self.capture().run(estate_scope())
        self.assertEqual(cs.registry_site_outcomes(run, ["Quality"])["captured"], ["Quality"])
        # A discovery that could not list the sites names every registry site as failed.
        run = SharePointCapture(self.store, lambda d: None, ts.CAPTURE).run(estate_scope())
        self.assertEqual(cs.registry_site_outcomes(run, ["Quality"])["reasons"],
                         {"Quality": "discovery_incomplete"})

    def test_a_due_refresh_the_budget_cuts_keeps_the_root_complete_and_resumes(self):
        # A complete root's daily full walk (walk_due) cut by the time budget must not
        # make the estate's complete roots fall: it keeps its state and resumes next run.
        self.capture().run(estate_scope())
        complete = {r["drive_id"] for r in self.store.completeness(ts.CAPTURE,
                                                                   ts.capture_audience())
                    ["roots"] if r["complete"]}
        self.assertEqual(complete, {ts.DRIVE, ts.SITE_DRIVE, PERSONAL_DRIVE})
        self.store._clock.now += dt.timedelta(hours=25)
        mark = len(self.drive.calls)

        def clock():   # spent once the first refresh walk lists its first folder
            return sum(1 for _m, u in self.drive.calls[mark:]
                       if u.split("?")[0].endswith("/children"))

        run = self.capture(budget_seconds=1, monotonic=clock).run(estate_scope())
        roots = self.roots(run)
        cut = [d for d, r in roots.items() if r.get("refresh_cut")]
        self.assertEqual(len(cut), 1)
        self.assertEqual((roots[cut[0]]["status"], roots[cut[0]]["full_walk_reason"]),
                         ("complete", "walk_due"))
        self.assertIsNotNone(roots[cut[0]]["walk_position"])
        self.assertEqual({r["drive_id"] for r in self.store.completeness(
            ts.CAPTURE, ts.capture_audience())["roots"] if r["complete"]}, complete)
        run = self.capture().run(estate_scope())
        root = self.roots(run)[cut[0]]
        self.assertEqual((root["status"], root["full_walk_reason"], root["walk_position"]),
                         ("complete", "resumed", None))
        self.assertIsNone(self.store.walk_position(cut[0], "/"))

    def test_a_cut_refresh_resumes_before_a_pending_root_and_applies_its_revocation(self):
        # Decision 28e628682928 (ING-16 gap-d5365c514b2a): a budget-cut refresh of a
        # complete root stood after every root no walk had reached, so on a growing
        # estate a permission revoked or a file deleted under it waited behind new roots.
        self.capture().run(estate_scope())
        self.store._clock.now += dt.timedelta(hours=25)
        mark = len(self.drive.calls)

        def quality_listed():   # spent once the quality library's refresh lists "/"
            return sum(1 for _m, u in self.drive.calls[mark:]
                       if f"/drives/{ts.DRIVE}/" in unquote(u)
                       and u.split("?")[0].endswith("/children"))

        run = self.capture(budget_seconds=1, monotonic=quality_listed).run(estate_scope())
        cut = self.roots(run)[ts.DRIVE]
        self.assertEqual((cut["status"], cut.get("refresh_cut"), cut["full_walk_reason"]),
                         ("complete", True, "walk_due"))
        # The root is complete but its captured state is 25 hours old, a refresh open.
        age = {r["drive_id"]: r for r in self.store.completeness(
            ts.CAPTURE, ts.capture_audience())["roots"]}[ts.DRIVE]
        self.assertEqual((age["complete"], age["age_hours"], age["refresh_open"]),
                         (True, 25, True))
        self.assertIsNotNone(age["last_full_walk_at"])
        handle_b = self.store.current(ts.DRIVE, "01FILEB")["l1_handle"]
        cited = self.store.resolve(handle_b, QUALITY, reader_audience())
        self.assertEqual((cited["root_full_walk"]["age_hours"],
                          cited["root_full_walk"]["refresh_open"]), (25, True))
        # Meanwhile the file's grant is revoked and another file deleted; the estate grows
        # a library no walk has reached.
        self.drive.items[f"{ts.DRIVE}:01FILEB"]["acl"] = [COLLEAGUE]
        self.drive.delete("01FILEA")
        new_drive = "b!personal-Drive_09"
        self.drive.personal[new_drive] = personal_drive()
        self.drive.libraries = {**LIBRARIES, PERSONAL_SITE: [*LIBRARIES[PERSONAL_SITE],
                                                             (new_drive, "Documents")]}
        mark = len(self.drive.calls)

        def listed():   # the refresh's four listings fit; a fifth spends the budget
            return sum(1 for _m, u in self.drive.calls[mark:]
                       if u.split("?")[0].endswith("/children"))

        run = self.capture(budget_seconds=5, monotonic=listed).run(estate_scope())
        first = next(unquote(u) for _m, u in self.drive.calls[mark:]
                     if u.split("?")[0].endswith("/children"))
        self.assertIn(f"/drives/{ts.DRIVE}/", first)   # the cut refresh before the new root
        roots = self.roots(run)
        self.assertEqual((roots[ts.DRIVE]["status"], roots[ts.DRIVE]["full_walk_reason"],
                          roots[ts.DRIVE]["walk_position"]), ("complete", "resumed", None))
        self.assertEqual(roots[ts.DRIVE]["counts"]["absent_after_resync"], 1)
        self.assertNotEqual(roots[new_drive]["status"], "complete")   # waits its turn
        # The revocation is applied once the refresh ends: the reader loses the file
        # through every read path; the deleted file is gone.
        audience = reader_audience()
        self.assertIsNone(self.store.resolve(handle_b, QUALITY, audience))
        self.assertEqual([i for i in self.store.index(QUALITY, audience)["items"]
                          if i["drive_id"] == ts.DRIVE], [])
        self.assertEqual(self.store.history(QUALITY, audience, handle_b), [])
        self.assertIsNone(self.store.current(ts.DRIVE, "01FILEA"))
        age = {r["drive_id"]: r for r in self.store.completeness(
            ts.CAPTURE, ts.capture_audience())["roots"]}
        self.assertEqual((age[ts.DRIVE]["age_hours"], age[ts.DRIVE]["refresh_open"]),
                         (0, False))
        self.assertEqual(age[new_drive]["last_full_walk_at"], None)
        self.assertIsNone(age[new_drive]["age_hours"])

    def test_a_large_root_gets_its_slice_and_never_starves_the_roots_after_it(self):
        # Live 2026-10-16: the estate's second root ate the whole 1500 s budget and 222
        # of 224 roots were never reached; carried over, it would starve them every day.
        for n in range(12):   # the quality library, first in the schedule, grows large
            self.drive.folder(f"01BULK{n}", f"Bulk {n}")
            self.drive.file(f"01BULKF{n}", f"bulk {n}.txt", f"01BULK{n}", b"bulk %d" % n,
                            mime="text/plain")
        quality_files = sum(1 for key, item in self.drive.items.items()
                            if key.startswith(ts.DRIVE + ":") and not item["folder"])

        def listings(drive=None, since=0):
            return [unquote(u) for _m, u in self.drive.calls[since:]
                    if u.split("?")[0].endswith("/children")
                    and (drive is None or f"/drives/{drive}/" in unquote(u))]

        def tick():   # ten seconds per folder listing
            return 10 * len(listings())

        # 120 s over four roots: the first pass spends at most half (a 15 s slice cuts the
        # quality walk); the small roots after it are each walked within theirs; the
        # budget left resumes it on the later passes (at least 20 s each).
        run = self.capture(budget_seconds=120, monotonic=tick).run(estate_scope())
        roots = self.roots(run)
        self.assertEqual({d: (r["status"], r["reason"]) for d, r in roots.items()
                          if d != ts.DRIVE},
                         {ts.SITE_DRIVE: ("complete", None), PERSONAL_DRIVE: ("complete", None),
                          FORBIDDEN: ("incomplete", "source_error:http_403")})
        self.assertFalse([d for d, r in roots.items() if r.get("deferred")])   # all visited
        big = roots[ts.DRIVE]
        self.assertEqual((big["status"], big["reason"], big["cursor"], big["walk_slices"],
                          big["full_walk_reason"]),
                         ("incomplete", "time_budget", "walk_position", 2, "first_walk"))
        # One result per root: its counts are every slice's, never restarted. A slice cut
        # inside a listing keeps that folder first with the children it holds, so only
        # it is listed again (its held children skipped).
        day_one = listings(ts.DRIVE)
        self.assertEqual(big["counts"]["folders_listed"], len(day_one))
        self.assertLessEqual(len(day_one) - len(set(day_one)), big["walk_slices"] - 1)
        self.assertEqual(self.store.walk_position(ts.DRIVE, "/")["folders_listed"],
                         len(set(day_one)))
        # The next run (the store carried) resumes the cut walk from its stored position:
        # no folder listed whole is listed again, no file read twice, the root completes.
        mark = len(self.drive.calls)
        run = self.capture(budget_seconds=1000, monotonic=tick).run(estate_scope())
        big = self.roots(run)[ts.DRIVE]
        self.assertEqual((big["status"], big["full_walk_reason"], big["cursor"]),
                         ("complete", "resumed", "drive_delta"))
        day_two = listings(ts.DRIVE, mark)
        self.assertLessEqual(len(set(day_one) & set(day_two)), 1)   # the cut folder only
        self.assertEqual(len(set(day_one) | set(day_two)), 12 + 4)   # every folder
        quality = [key.split(":", 1)[1] for key, item in self.drive.items.items()
                   if key.startswith(ts.DRIVE + ":") and not item["folder"]]
        self.assertEqual(len(quality), quality_files)
        for iid in quality:
            self.assertIsNotNone(self.store.current(ts.DRIVE, iid), iid)
            self.assertEqual(sum(1 for _m, u in self.drive.calls
                                 if urlsplit(u).hostname == ts.SP_HOST
                                 and f"/download/{iid}?" in u
                                 and f"drive={ts.DRIVE}&" in unquote(u)), 1, iid)

    def test_a_slice_cuts_inside_a_long_listing_and_never_before_its_first_child(self):
        # Live at b38701bd1 (2026-10-18T10:41Z) the slice cut only between folders and one
        # listing's 152 files ran 224 s past it. A slice cuts inside the listing once it
        # read a child; it never cuts before (a slice shorter than one listing would cut
        # the walk where the last one did, every pass and every run).
        self.drive.folder("01WIDE", "Wide")
        for n in range(40):
            self.drive.file(f"01WIDEF{n}", f"wide {n}.txt", "01WIDE", b"wide %d" % n,
                            mime="text/plain")

        def tick():   # ten seconds per listing, one per file read
            calls = [u.split("?")[0] for _m, u in self.drive.calls]
            return (10 * sum(1 for u in calls if u.endswith("/children"))
                    + sum(1 for u in calls if "/download/" in u))

        run = self.capture(budget_seconds=120, monotonic=tick).run(estate_scope())
        roots = self.roots(run)
        self.assertFalse([d for d, r in roots.items() if r.get("deferred")])
        for drive in (ts.SITE_DRIVE, PERSONAL_DRIVE):
            self.assertEqual(roots[drive]["status"], "complete", drive)
        big = roots[ts.DRIVE]
        self.assertEqual((big["reason"], big["cursor"]), ("time_budget", "walk_position"))
        position = self.store.walk_position(ts.DRIVE, "/")
        head = position["queue"][0]
        # Cut inside the wide folder's listing, with some of its children held, not
        # after it ran all forty files.
        self.assertEqual(head["path"], "/Wide")
        self.assertTrue(0 < len(head["held"]) < 40, len(head["held"]))
        # The same clock, a slice far shorter than one listing: each run still reads a
        # child of the cut listing or ends a folder, so the walk completes.
        for _day in range(40):
            began = tick()
            run = self.capture(budget_seconds=12, monotonic=lambda: tick() - began
                               ).run(estate_scope())
            if self.roots(run)[ts.DRIVE]["status"] == "complete":
                break
        self.assertEqual(self.roots(run)[ts.DRIVE]["status"], "complete")
        wide = [iid for key in self.drive.items
                if key.startswith(ts.DRIVE + ":01WIDEF") for iid in [key.split(":", 1)[1]]]
        self.assertEqual(len(wide), 40)
        for iid in wide:
            self.assertIsNotNone(self.store.current(ts.DRIVE, iid), iid)

    def test_a_slice_shares_the_budget_over_the_walks_after_it_not_the_followed_roots(self):
        # Dividing by every root left, complete ones included, gave
        # each walk of a 224-root estate the 20 s floor though most roots only follow
        # their change feed. A complete root's follow is no walk and takes no share.
        self.capture().run(estate_scope())
        new_drive = "b!personal-Drive_09"
        self.drive.personal[new_drive] = personal_drive()
        self.drive.libraries = {**LIBRARIES, PERSONAL_SITE: [*LIBRARIES[PERSONAL_SITE],
                                                             (new_drive, "Documents")]}
        capture = self.capture(budget_seconds=600, monotonic=lambda: 0.0)
        shares, sliced = [], capture._sliced_walk

        def recording(roots_left, reader, drive_id, root, **walk):
            shares.append((drive_id, roots_left))
            return sliced(roots_left, reader, drive_id, root, **walk)

        capture._sliced_walk = recording
        run = capture.run(estate_scope())
        roots = self.roots(run)
        walked = {d for d, r in roots.items() if r["full_walk_reason"] is not None}
        followed = set(roots) - walked
        self.assertTrue(followed)   # the complete roots only follow their feed
        self.assertEqual({d for d, _left in shares}, walked)
        # Each walk's share is over the walks from it on, never over the followed roots.
        self.assertEqual([left for _d, left in shares],
                         list(range(len(walked), 0, -1)))
        self.assertEqual(roots[new_drive]["status"], "complete")
        # A complete root its change feed forces to walk (a new folder under a walked
        # one) counts itself and the planned walks after it.
        self.drive.folder("01LATE", "Late")
        shares.clear()
        run = capture.run(estate_scope())
        roots = self.roots(run)
        forced = [d for d, r in roots.items() if r["full_walk_reason"] == "structural_change"]
        self.assertEqual(forced, [ts.DRIVE])
        order = [d for d, _left in shares]
        self.assertIn(ts.DRIVE, order)
        self.assertEqual(dict(shares)[ts.DRIVE], len(order) - order.index(ts.DRIVE))

    def test_a_slice_floor_never_leaves_small_roots_after_large_ones_unreached(self):
        # Live 2026-10-18T09:46Z at dabe69ce3 (224 roots, 1500 s): a 20 s least slice gave
        # each of 52 large roots more than its fair share (about 6 s); none completed and
        # 78 roots after them were never reached. The slice is the fair share alone: what
        # a small root leaves rolls to the walks after it, every root is visited and the
        # large ones resume on the run's later passes.
        big = [f"b!personal-Drive_1{n}" for n in range(4)]
        small = [f"b!personal-Drive_2{n}" for n in range(6)]
        for drive_id in big + small:
            mine = personal_drive()
            if drive_id in big:
                for n in range(10):
                    mine.folder(f"01BIG{n}", f"Big {n}")
                    mine.file(f"01BIGF{n}", f"big {n}.txt", f"01BIG{n}", b"big %d" % n,
                              mime="text/plain", acl=(OWNER,))
            self.drive.personal[drive_id] = mine
        # The large OneDrives come first in discovery order, the small ones after them.
        self.drive.libraries = {**LIBRARIES, PERSONAL_SITE: [
            *[(d, "Documents") for d in big], *LIBRARIES[PERSONAL_SITE],
            *[(d, "Documents") for d in small]]}

        def tick():   # four seconds per folder listing
            return 4 * sum(1 for _m, u in self.drive.calls
                           if u.split("?")[0].endswith("/children"))

        # 150 s, 14 roots: a first-pass share of about 5 s each, under the old 20 s
        # least slice (which left a small root unreached here).
        capture = self.capture(budget_seconds=150, monotonic=tick)
        run = capture.run(estate_scope())
        roots = self.roots(run)
        self.assertEqual(len(roots), 4 + len(big) + len(small))
        self.assertFalse([d for d, r in roots.items() if r.get("deferred")])   # all visited
        for drive_id in [PERSONAL_DRIVE, ts.SITE_DRIVE, *small]:
            self.assertEqual((roots[drive_id]["status"], roots[drive_id]["reason"]),
                             ("complete", None), drive_id)
        cut = {d for d, r in roots.items() if r["reason"] == cs.TIME_BUDGET}
        self.assertTrue(cut)
        self.assertLessEqual(cut, {*big, ts.DRIVE})   # only the large roots
        for drive_id in cut:   # its position kept: the next run resumes it
            self.assertEqual(roots[drive_id]["cursor"], "walk_position", drive_id)
        # The budget the small roots left resumed the large ones on later passes.
        self.assertTrue([d for d in big if roots[d].get("walk_slices", 1) >= 2])
        # Every cut root has its typed cause by root class; the causes sum to the cut.
        personal = len(cut - {ts.DRIVE})
        self.assertEqual(cs.budget_cut_causes(run), {
            "personal": {"unreached": 0, "start_cut": 0, "walk_cut": personal,
                         "finish_cut": 0},
            **({"site": {"unreached": 0, "start_cut": 0, "walk_cut": 1,
                       "finish_cut": 0}}
               if ts.DRIVE in cut else {})})
        # The next run resumes the cut walks; every root completes, no file read twice.
        run = self.capture(budget_seconds=1000, monotonic=tick).run(estate_scope())
        roots = self.roots(run)
        self.assertEqual({d for d, r in roots.items() if r["status"] != "complete"},
                         {FORBIDDEN})
        self.assertEqual(cs.budget_cut_causes(run), {})
        for drive_id in big:
            for n in range(10):
                self.assertIsNotNone(self.store.current(drive_id, f"01BIGF{n}"))

    def test_roots_each_over_their_share_are_visited_and_still_completed(self):
        # Probe of the fair share alone (no reserve): like roots each a little over
        # their share were all cut and none completed (45 here: one site lists at most 50
        # libraries). The first pass keeps half the
        # budget: every root is visited once, no root lost or doubled, and the passes
        # after it complete roots instead of spreading the rest thin.
        extra = [f"b!personal-Drive_{100 + n}" for n in range(45)]
        for drive_id in extra:
            mine = personal_drive()
            for n in range(7):
                mine.folder(f"01SUB{n}", f"Sub {n}")
            self.drive.personal[drive_id] = mine
        self.drive.libraries = {**LIBRARIES, PERSONAL_SITE: [
            *LIBRARIES[PERSONAL_SITE], *[(d, "Documents") for d in extra]]}

        def tick():   # a quarter second per folder listing; a personal root lists nine
            return 0.25 * sum(1 for _m, u in self.drive.calls
                              if u.split("?")[0].endswith("/children"))

        # 75 s for about 100 s of walks: the fair share alone (1.5 s each, under the 2.25
        # s a root takes) completed none; the old 20 s least slice left 12 unreached.
        run = self.capture(budget_seconds=75, monotonic=tick).run(estate_scope())
        planned = [t["drive_id"] for t in run["targets"] if t.get("discovered")
                   for _r in t["roots"]]
        self.assertEqual(len(planned), len(set(planned)))
        roots = self.roots(run)
        self.assertLessEqual(set(extra), set(roots))
        self.assertFalse([d for d, r in roots.items() if r.get("deferred")])
        done = [d for d in extra if roots[d]["status"] == "complete"]
        self.assertGreaterEqual(len(done), 20)
        cut = [d for d, r in roots.items() if r["reason"] == cs.TIME_BUDGET]
        self.assertEqual(sum(sum(c.values()) for c in cs.budget_cut_causes(run).values()),
                         len(cut))

    def test_a_refresh_is_never_sliced_and_a_budget_without_room_defers(self):
        # Decision 28e628682928: a due refresh of a complete root runs on the run's
        # budget, not a slice; roots the budget does not reach are deferred, unvisited.
        self.capture().run(estate_scope())
        self.store._clock.now += dt.timedelta(hours=25)
        mark = len(self.drive.calls)

        def tick():   # thirty seconds per folder listing: past any 20 s slice at once
            return 30 * sum(1 for _m, u in self.drive.calls[mark:]
                            if u.split("?")[0].endswith("/children"))

        run = self.capture(budget_seconds=110, monotonic=tick).run(estate_scope())
        roots = self.roots(run)
        refreshed = [d for d, r in roots.items() if r["full_walk_reason"] == "walk_due"]
        # The first refresh lists four folders (120 s), more than any slice would allow.
        first = refreshed[0]
        self.assertEqual(roots[first]["counts"]["folders_listed"], 4)
        self.assertNotIn("walk_slices", roots[first])
        self.assertTrue(all(r.get("deferred") for d, r in roots.items() if d != first))

    def test_a_locked_personal_site_is_counted_never_a_gap(self):
        # Live 2026-10-16: one OneDrive answered its library listing HTTP 423 (Locked: a
        # OneDrive held for a removed user, readable by nobody).
        locked = (f"{MY_HOST},77777777-0000-0000-0000-000000000001,"
                  "77777777-0000-0000-0000-000000000002")
        SITES[locked] = f"https://{MY_HOST}/personal/gone"
        self.addCleanup(SITES.pop, locked)
        locked_site = (f"{ts.SP_HOST},88888888-0000-0000-0000-000000000001,"
                       "88888888-0000-0000-0000-000000000002")
        SITES[locked_site] = f"https://{ts.SP_HOST}/sites/Archive"
        self.addCleanup(SITES.pop, locked_site)
        self.drive.locked = {locked, locked_site}
        self.drive.sites.append(locked)
        run = self.capture().run(estate_scope())
        discovery = run["discovery"]
        self.assertEqual(discovery["counts"]["sites_locked"], 1)
        self.assertEqual(discovery["gaps"], ["library_listing:http_403"])   # Legacy only
        self.assertNotIn("library_listing:http_423", run["incomplete_reasons"])
        # A locked site on the configured host stays a named gap (business content).
        self.drive.sites.append(locked_site)
        run = self.capture().run(estate_scope())
        self.assertIn("library_listing:http_423", run["discovery"]["gaps"])
        self.assertEqual(run["discovery"]["counts"]["sites_locked"], 1)
        self.assertEqual(cs.registry_site_outcomes(run, ["Archive"])["reasons"],
                         {"Archive": "library_listing_failed"})

    def test_a_library_wide_root_takes_over_a_folder_root_captured_before(self):
        self.capture().run(CaptureScope([{"id": "quality", "drive_id": ts.DRIVE,
                                          "folders": [ts.QUALITY_FOLDER]}]))
        self.drive.delete("01FILEA")
        run = self.capture().run(estate_scope())
        self.assertEqual(self.roots(run)[ts.DRIVE]["counts"]["absent_after_resync"], 1)
        self.assertIsNone(self.store.current(ts.DRIVE, "01FILEA"))
        self.assertEqual(self.store.current(ts.DRIVE, "01FILEB")["root_path"], "/")
        self.assertEqual([r[0] for r in self.store.db.execute(
            "SELECT root_path FROM roots WHERE drive_id=?", (ts.DRIVE,))], ["/"])
        # A drive an explicit target names is left to that target.
        run = self.capture().run(estate_scope({"id": "quality", "drive_id": ts.DRIVE,
                                               "folders": ["/"]}))
        self.assertEqual(run["discovery"]["counts"]["libraries_covered_by_target"], 1)


class EstateStoreTests(EstateCase):
    def test_the_store_is_bounded_by_default_and_keeps_only_the_last_complete_capture(self):
        # Decision 28e628682928: the estate store had no bound unless one was passed, and
        # kept the bytes of every version ever captured.
        with tempfile.TemporaryDirectory() as tmp:
            store = cs.SharePointCaptureStore(Path(tmp) / "state")
            self.addCleanup(store.db.close)
            self.assertEqual(store.max_store_bytes, cs.MAX_STORE_BYTES)
            parser = argparse.ArgumentParser()
            cs.add_cli_arguments(parser)
            self.assertEqual(parser.get_default("max_store_bytes"), cs.MAX_STORE_BYTES)
            store.store_bytes = lambda: cs.MAX_STORE_BYTES - 3   # the default bound holds
            store.put_blob(b"abc", kind="content")
            with self.assertRaises(cs.CaptureError) as refused:
                store.put_blob(b"four", kind="content")
            self.assertEqual(str(refused.exception), "store_full")
        capture = self.capture()
        capture.run(estate_scope())
        old = self.store.current(ts.DRIVE, "01FILEB")
        self.drive.change("01FILEB", b"release two")
        run = capture.run(estate_scope())   # the feed: v2 captured, v1 kept until a walk
        self.assertEqual(self.roots(run)[ts.DRIVE]["round"], "incremental")
        old_blob = self.store.blobs / old["content_sha256"][:2] / old["content_sha256"]
        self.assertTrue(old_blob.exists())
        self.store._clock.now += dt.timedelta(hours=25)
        packet = self.store.current(ts.DRIVE, "01FILEA")
        self.drive.change("01FILEA", b"%PDF-1.7 packet two")   # superseded by the walk
        run = capture.run(estate_scope())   # the due full walk lists everything
        root = self.roots(run)[ts.DRIVE]
        self.assertEqual((root["round"], root["status"], root["counts"]["contents_pruned"]),
                         ("full", "complete", 1))
        audience = reader_audience()
        pruned = self.store.resolve(old["l1_handle"], QUALITY, audience)
        # The older capture's bytes and text are gone; its metadata, permissions and
        # interval stay, so its citation resolves without bytes and its digest pin fails.
        self.assertEqual((pruned["row"]["content_status"], pruned["row"]["content_sha256"],
                          pruned["source_acl"]["status"]), (cs.PRUNED, None, "read"))
        self.assertIsNotNone(pruned["superseded_at"])
        self.assertIsNone(self.store.content(f"{old['l1_handle']}#sha256="
                                             f"{old['content_sha256']}", QUALITY, audience))
        self.assertFalse(old_blob.exists())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM blobs WHERE sha256=?",
                                                (old["content_sha256"],)).fetchone())
        current = self.store.current(ts.DRIVE, "01FILEB")
        self.assertEqual(self.store.content(f"{current['l1_handle']}#sha256="
                                            f"{current['content_sha256']}", QUALITY, audience),
                         b"release two")
        self.assertEqual([v["content_status"] for v in self.store.history(
            QUALITY, audience, current["l1_handle"])], [cs.PRUNED, cs.CAPTURED])
        # A version the walk itself superseded keeps its bytes until the next complete
        # walk (what a reader cited of the previous capture still reads); a pruned
        # version that comes back is read from the source again, never served byteless.
        self.assertEqual(self.store.content(f"{packet['l1_handle']}#sha256="
                                            f"{packet['content_sha256']}", QUALITY, audience),
                         b"%PDF-1.7 scan")
        self.assertEqual(self.store.current(ts.DRIVE, "01FILEA")["content_status"],
                         cs.CAPTURED)
        self.assertEqual(self.store.observe(json.loads(old["record"]), "/", [], {}), "new")

    def test_estate_hosts_and_the_guard_derive_hosts_from_one_function(self):
        from integrations import live_source_guard as guard
        self.assertIs(cs.estate_hosts, guard.tenant_download_hosts)
        # A configured OneDrive host never widens to the tenant's SharePoint host.
        for host, hosts in (("Contoso-PLX.sharepoint.com", {ts.SP_HOST, MY_HOST}),
                            (ts.SP_HOST, {ts.SP_HOST, MY_HOST}), (MY_HOST, {MY_HOST})):
            with self.subTest(host=host):
                self.assertEqual(cs.estate_hosts(host),
                                 guard.GuardedTransport(sharepoint_host=host).download_hosts)
                self.assertEqual(cs.estate_hosts(host), hosts)
        # No configured host: no download host, never a guess.
        self.assertEqual(guard.GuardedTransport().download_hosts, frozenset())
        self.assertEqual(cs.estate_hosts(None), frozenset())


class EstateClassificationTests(EstateCase):
    def test_a_restricted_library_is_walked_only_once_the_scope_classifies_it(self):
        from operating import doc_metadata_taxonomy as taxonomy
        from operating.file_clearance_check import synthetic_entitlement
        self.drive.libraries = {**LIBRARIES, Q_SITE: [(ts.DRIVE, "PLX Finance")]}
        uncleared = synthetic_entitlement("plx", "00000000-0000-4000-8000-0000000000a1",
                                          "SUPER_ADMIN", "Operations")
        cleared = synthetic_entitlement("plx", "00000000-0000-4000-8000-0000000000f1",
                                        "STAFF", "Finance")
        capture = self.capture()

        def served():
            view = self.store.resolve(self.store.current(ts.DRIVE, "01FILEB")["l1_handle"],
                                      QUALITY, reader_audience(), text=False)
            return view["row"]

        # Unclassified: refused before any read of it, and a gap of the run.
        mark = len(self.drive.calls)
        run = capture.run(estate_scope())
        self.assertNotIn(ts.DRIVE, self.roots(run))
        self.assertIn("restricted_library_unclassified", run["discovery"]["gaps"])
        self.assertEqual(run["discovery"]["counts"]["libraries_restricted_unclassified"], 1)
        self.assertFalse([u for u in self.urls(mark) if f"/drives/{ts.DRIVE}/" in u])
        self.assertIsNone(self.store.current(ts.DRIVE, "01FILEB"))
        # Classified beside the targets: walked, and every version carries the class.
        classes = [{"site": "Quality", "library": "PLX Finance", "class": "FINANCE"}]
        run = capture.run(estate_scope(library_security_classes=classes))
        self.assertEqual(self.roots(run)[ts.DRIVE]["status"], "complete")
        self.assertEqual(taxonomy.file_security_class(served())[0], "FINANCE")
        self.assertFalse(uncleared.file_class_allows(served()))   # role never widens
        self.assertTrue(cleared.file_class_allows(served()))
        # The class removed again: what was captured is served to no employee.
        capture.run(estate_scope())
        self.assertEqual(taxonomy.file_security_class(served())[0], "UNRESOLVED")
        self.assertFalse(cleared.file_class_allows(served()))


class EstateReaderTests(EstateCase):
    def test_a_personal_drive_file_is_served_only_to_its_item_grantees(self):
        # Everyone granted the OneDrive library by folder sees only the files its item
        # permissions share with them: the owner the file, a colleague on the same
        # library grant, a withheld actor and a folder-only actor nothing at all.
        self.capture().run(estate_scope())
        audience = reader_audience()
        handle = self.store.current(PERSONAL_DRIVE, "01MINE")["l1_handle"]
        folder_principal = "staff:folder-only"
        folder_audience = cs.folder_only_audience(audience, folder_principal,
                                                  [(PERSONAL_DRIVE, "/")])
        withheld = Actor("plx", frozenset({"staff:nobody"}), "restricted")
        folder_actor = Actor("plx", frozenset({folder_principal}), "restricted")
        self.assertIsNotNone(self.store.resolve(handle, OWNER_ACTOR, audience))
        self.assertEqual([i["drive_id"] for i in self.store.index(OWNER_ACTOR,
                                                                  audience)["items"]],
                         [PERSONAL_DRIVE])
        for actor, seen_by in ((COLLEAGUE_ACTOR, audience), (withheld, audience),
                               (folder_actor, folder_audience), (QUALITY, audience)):
            with self.subTest(actor=sorted(actor.principals)):
                self.assertIsNone(self.store.resolve(handle, actor, seen_by))
                self.assertEqual([i for i in self.store.index(actor, seen_by)["items"]
                                  if i["drive_id"] == PERSONAL_DRIVE], [])
                self.assertFalse([r for r in self.store.completeness(actor, seen_by)["roots"]
                                  if r["drive_id"] == PERSONAL_DRIVE and r["current_files"]])
        for actor, seen_by in ((withheld, audience), (folder_actor, folder_audience)):
            self.assertEqual(sum(self.store.counts(actor, seen_by).values()), 0)

    def test_a_reader_without_access_to_a_library_sees_none_of_its_items_or_counts(self):
        from ontology import mentions
        from ontology import schema as schema_loader
        from ontology import store_ddl
        from ontology.project import SharePointCaptures, build_store
        from ontology.tests.test_mail_documents import StaticPortal, po
        from operating import ontology_tools as ot
        from operating import ontology_tools_evidence as oe

        self.capture().run(estate_scope())
        audience = reader_audience()
        use_case = version_handle(ts.SITE_DRIVE, "01USE", '"{01USE},1"')
        self.assertIsNotNone(self.store.resolve(use_case, AGENTS, audience))
        self.assertIsNone(self.store.resolve(use_case, QUALITY, audience))
        self.assertEqual({i["drive_id"] for i in self.store.index(QUALITY, audience)["items"]},
                         {ts.DRIVE})
        self.assertEqual(self.store.counts(QUALITY, audience)["drives"], 1)
        self.assertEqual({r["drive_id"] for r in self.store.completeness(QUALITY,
                                                                         audience)["roots"]},
                         {ts.DRIVE})
        # The same through the ontology tools: find, object counts and documents.
        compiled = schema_loader.load_schema()
        path = Path(self.tmp.name) / "ontology.sqlite"
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        with SharePointCaptures(self.state) as files:
            build_store(path, compiled, {"portal_api": portal, "sharepoint": files},
                        clock=lambda: NOW, post_project={"mentions": mentions.store_mention_links})
        staff = {"staff:quality", "staff:agents", "staff:owner", "staff:colleague"}
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": staff, "sharepoint:*": staff},
                               markings={m: staff for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        contracts = json.loads((Path(__file__).resolve().parents[2]
                                / "operating/ontology_tool_contracts.json").read_text("utf-8"))
        base = ot.OntologyTools(reader, policy, item_gate=self.store.item_gate(audience),
                                schema=ot.SchemaView.from_compiled(compiled), grammar=None,
                                contracts=contracts, clock=lambda: NOW)
        evidence = oe.EvidenceTools(base)
        p1 = reader.conn.execute("SELECT object_id FROM objects WHERE object_id LIKE "
                                 "'PurchaseOrder:portal.app_purchase_order:%'").fetchone()[0]
        link = "SharePointFile.mentions_purchase_order"
        for actor, seen in ((AGENTS, 1), (QUALITY, 0)):
            found = base.call("plx.find", {"query": "use case",
                                           "object_type": "SharePointFile"}, actor)
            self.assertEqual(len(found["candidates"]), seen, json.dumps(found)[:600])
            counts = {(c["link_type"], c["direction"]): c["count"] for c in base.call(
                "plx.object", {"object_id": p1}, actor)["linked_counts"]}
            # Name and text mentions are separate links; hidden ones are not counted.
            self.assertEqual(counts.get((link, "reverse"), 0) > 0, bool(seen))
            docs = evidence.call("plx.documents", {"object_id": p1, "sources": ["sharepoint"]},
                                 actor)
            self.assertNotIn("error", docs)
            self.assertEqual(USE_CASE in json.dumps(docs), bool(seen))
        # A personal-drive file through the tools: its owner only, never a colleague
        # holding the same library grant or a reader of another library.
        for actor, seen in ((OWNER_ACTOR, 1), (COLLEAGUE_ACTOR, 0), (AGENTS, 0), (QUALITY, 0)):
            found = base.call("plx.find", {"query": "travel notes",
                                           "object_type": "SharePointFile"}, actor)
            self.assertEqual(len(found["candidates"]), seen, json.dumps(found)[:600])


# Source registry entries naming two of the fake estate's sites and one it does not hold.
REGISTRY_SITES = [{"id": f"sharepoint.site:{name}", "system": "sharepoint", "kind": "site",
                   "name": name, "evidence": ["portal:x@000000000000"]}
                  for name in ("Quality", "AGENTS", "Elsewhere")]


# The estate the shipped scope's CLI tests read: one walkable library per site (the
# shipped scope excludes no library, business rule everything, 2026-10-27).
SHIPPED_LIBRARIES = {**LIBRARIES, Q_SITE: [(ts.DRIVE, "Documents")],
                     ts.SITE_ID: [(ts.SITE_DRIVE, "Documents")]}


class EstateCliTests(unittest.TestCase):
    def run_cli(self, drive, registry_sites=REGISTRY_SITES, *, home=None, budget_seconds=None,
                monotonic=None, environ=None):
        """One CLI invocation; ``home``: the host state base (XDG_STATE_HOME, no
        --state-dir), kept by the caller across invocations."""
        with tempfile.TemporaryDirectory() as tmp:
            audience = Path(tmp) / "audience.json"
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": ["svc:sharepoint-capture"]}}))
            registry = Path(tmp) / "registry.json"
            registry.write_text(json.dumps({"entries": registry_sites}))
            args = argparse.Namespace(
                state_dir=None if home or environ else str(Path(tmp) / "state"),
                scope=str(Path(cs.REPO_ROOT) / "integrations/sharepoint_estate_scope.json"),
                live_audience=str(audience), principal=cs.CAPTURE_PRINCIPAL,
                withheld_principal="staff:nobody", runs=1, max_store_bytes=None,
                registry=str(registry), budget_seconds=budget_seconds)
            return cs.run_cli(args, environ=environ or ({**ts.ENV, "XDG_STATE_HOME": home}
                                                        if home else ts.ENV),
                              sender=drive, sleep=lambda s: None,
                              **({"monotonic": monotonic} if monotonic else {}))

    def test_cli_reports_the_estate_by_counts_and_a_gap_fails_the_live_check(self):
        from verify import live_verify as lv
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        spec = json.loads((Path(cs.REPO_ROOT) / "devloop/live-checks/"
                           "capture.sharepoint_estate.json").read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="capture.sharepoint_estate")
        drive = estate_drive(
            sites=(Q_SITE, ts.SITE_ID, PERSONAL_SITE, OTHER_SITE),
            libraries=SHIPPED_LIBRARIES)
        # The source refuses one file's bytes (live 2026-10-07: content_incomplete): its
        # root is walked to the end, a named incomplete-source gap, never a failure.
        drive.fail_download["01USE"] = 403
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        report = self.run_cli(drive, home=home.name)   # the spec's command: host state
        self.assertTrue(report["ok"], json.dumps(report)[:1500])
        self.assertEqual(report["evidence_label"], "injected-transport")
        estate = report["estate"]
        # The shipped scope leaves no library out (business rule everything, 2026-10-27).
        self.assertEqual((estate["libraries_planned"], estate["libraries_excluded"],
                          estate["sites_other_host"], estate["sites_personal"],
                          estate["libraries_personal_planned"],
                          estate["roots_other_gaps"]), (3, 0, 1, 1, 1, 0))
        self.assertEqual((estate["roots_incomplete_source"], estate["root_gaps"],
                          estate["contents_unavailable"]),
                         (1, {"content_incomplete": 1}, {"http_403": 1}))
        self.assertFalse(report["runs"][0]["complete"])   # partial, retried next run
        # Every planned root by the age of its last full walk that listed everything (the
        # incomplete-source root's walk listed everything too).
        self.assertEqual((estate["full_walk_age"], estate["full_walk_age_max_hours"]),
                         ({"fresh": 3, "due": 0, "stale": 0, "never": 0}, 0))
        # Only sites the registry already names are named: every one, by outcome, with
        # the typed reason of each one not captured (Elsewhere: discovery never listed it).
        self.assertEqual({k: v for k, v in report["registry_sites"].items() if k != "readback"},
                         {"captured": ["AGENTS", "Quality"], "partial": [], "unexercised": [],
                          "failed": ["Elsewhere"], "reasons": {"Elsewhere": "not_listed"}})
        text = json.dumps({k: v for k, v in report.items() if k != "registry_sites"})
        for secret in ("Quality", "AGENTS", "Board", "Site Assets", "Partner", "someone",
                       ts.DRIVE, ts.SITE_DRIVE, PERSONAL_DRIVE, FORBIDDEN, Q_SITE, OTHER_SITE,
                       USE_CASE, MY_FILE, OWNER, "PO100234", "s3cr3t", "TOKEN"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(report["leak_check"].values()), {0})
        self.assertEqual(lv.check_expectations(spec, 0 if report["ok"] else 1, report), [])
        # No personal drive planned (one Graph types otherwise): the live check fails on it.
        drive = estate_drive(
            sites=(Q_SITE, ts.SITE_ID, PERSONAL_SITE, OTHER_SITE),
            libraries=SHIPPED_LIBRARIES)
        drive.drive_types = {PERSONAL_DRIVE: "personal"}
        unplanned = self.run_cli(drive)
        self.assertEqual((unplanned["estate"]["sites_personal"],
                          unplanned["estate"]["libraries_personal_planned"]), (1, 0))
        self.assertTrue([f for f in lv.check_expectations(spec, 0, unplanned)
                         if "estate.libraries_personal_planned" in f])
        # The recorded result takes a registry site to evaluated (the registry's reading).
        from scripts import source_registry as sr
        registry = {"entries": REGISTRY_SITES,
                    "entries_digest": sr.entries_digest(REGISTRY_SITES), "upstream": {}}
        facts = sr.RepoFacts()

        def status(result, fields):
            live = [{"_from": "daily", "generated_at": "2026-10-07T00:00:00Z", "checks": [
                {"id": spec["id"], "source": spec["source"], "result": result,
                 "detail": {"fields": fields}}]}]
            return {row["name"]: (row["status"], row.get("reason"))
                    for row in sr.coverage(registry, live, facts)}
        recorded = lv._recorded(spec, report)
        self.assertEqual(recorded["registry_sites.reasons"], {"Elsewhere": "not_listed"})
        # Every registry site is named by the check (gap-91998eed9724): none is left
        # unnamed (no_live_check).
        self.assertEqual(status("pass", recorded), {
            "Quality": ("evaluated", None), "AGENTS": ("evaluated", None),
            "Elsewhere": ("fixture_queryable", "live_not_passing")})
        failing = self.run_cli(estate_drive())
        self.assertFalse(failing["ok"])
        self.assertEqual(failing["runs"][0]["discovery"]["gaps"], ["library_listing:http_403"])
        # The forbidden library and the unreadable ones (HR and Site Assets: the shipped
        # list excludes nothing), each named.
        self.assertEqual((failing["estate"]["roots_other_gaps"], failing["estate"]["root_gaps"]),
                         (3, {"not_visible": 2, "source_error:http_403": 1}))
        self.assertEqual({k: v for k, v in failing["registry_sites"].items() if k != "readback"},
                         {"captured": [], "partial": [], "unexercised": [],
                          "failed": ["AGENTS", "Elsewhere", "Quality"],
                          "reasons": {"AGENTS": "source_error:http_403",
                                      "Elsewhere": "not_listed", "Quality": "not_visible"}})
        self.assertTrue(lv.check_expectations(spec, 1, failing))
        self.assertEqual(status("fail", lv._recorded(spec, failing))["AGENTS"],
                         ("fixture_queryable", "live_not_passing"))

    def test_estate_progress_carries_across_invocations_in_the_host_state(self):
        # Two daily invocations with no --state-dir: the second resumes the first's walk
        # from its stored position, so roots no walk reached fall and complete roots
        # never do (gap-e70bce853a2f).
        from verify import live_verify as lv
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        spec = json.loads((Path(cs.REPO_ROOT) / "devloop/live-checks/"
                           "capture.sharepoint_estate.json").read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="capture.sharepoint_estate")
        drive = estate_drive(sites=(Q_SITE, ts.SITE_ID, PERSONAL_SITE, OTHER_SITE),
                             libraries=SHIPPED_LIBRARIES)

        def listed():
            return [u.split("?")[0] for _m, u in drive.calls
                    if u.split("?")[0].endswith("/children")]

        with tempfile.TemporaryDirectory() as home:
            # Day one: the budget (two listings) is spent by the first two walks, one
            # listing each: the first is cut by its slice once its root's listing ended,
            # the second by the run's budget inside it, and the third root is never
            # reached.
            first = self.run_cli(drive, home=home, budget_seconds=2,
                                 monotonic=lambda: len(listed()))
            state = Path(home, *cs.CHECK_STATE, "injected-transport")
            self.assertTrue(cs.store_exists(state))
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((first["storage"]["host_state"], first["storage"]["carried"]),
                             (True, False))
            progress = first["estate"]["progress"]
            self.assertEqual((progress["roots_unvisited_before"], progress["roots_unvisited"],
                              progress["roots_complete"]), (3, 1, 0))
            self.assertEqual((first["estate"]["roots_budget_cut"],
                              first["estate"]["roots_budget_cut_by"]),
                             (3, {"personal": {"unreached": 1, "start_cut": 0,
                                               "walk_cut": 0, "finish_cut": 0},
                                  "site": {"unreached": 0, "start_cut": 0,
                                           "walk_cut": 2, "finish_cut": 0}}))
            self.assertEqual(first["estate"]["full_walk_age"],
                             {"fresh": 0, "due": 0, "stale": 0, "never": 3})
            self.assertIsNone(first["estate"]["full_walk_age_max_hours"])
            self.assertFalse(first["ok"])
            self.assertTrue(lv.check_expectations(spec, 1, first))
            day_one = listed()
            # Day two: a new invocation on the same host state.
            second = self.run_cli(drive, home=home)
            self.assertEqual((second["storage"]["host_state"], second["storage"]["carried"]),
                             (True, True))
            reasons = [r["full_walk_reason"] for t in second["runs"][0]["targets"]
                       for r in t["roots"]]
            self.assertEqual(sorted(reasons), ["first_walk", "resumed", "resumed"])
            # Day one's first walk finished its root's listing before its slice cut it
            # (the slice cuts between children once the walk has moved; here it ended
            # with the listing, the queue of subfolders kept), and the second was cut by
            # the run's budget inside its root's listing: on day two only that listing
            # is read again (skipping the children it held), once, and the first walk's
            # root listing, finished on day one, is never read again.
            self.assertEqual(len(set(day_one)), 2)
            self.assertEqual([u for u in listed()[2:] if u in day_one], day_one[1:])
            self.assertEqual(second["estate"]["progress"], {
                "carried": True, "roots_complete_before": 0, "roots_complete": 3,
                "roots_complete_not_falling": True, "roots_unvisited_before": 1,
                "roots_unvisited": 0, "roots_unvisited_falling": True})
            self.assertTrue(second["ok"], json.dumps(second)[:1500])
            self.assertEqual(second["estate"]["full_walk_age"],
                             {"fresh": 3, "due": 0, "stale": 0, "never": 0})
            self.assertEqual(lv.check_expectations(spec, 0, second), [])
            # The estate judged across the runs, not by one: day one reached two roots, the
            # carried estate is incomplete; day two settles every planned root.
            self.assertEqual(first["estate"]["carried"],
                             {"roots_planned": 3, "roots_complete": 0, "roots_unvisited": 1,
                              "roots_incomplete": 2, "roots_stale": 0, "complete": False})
            self.assertEqual(second["estate"]["carried"],
                             {"roots_planned": 3, "roots_complete": 3, "roots_unvisited": 0,
                              "roots_incomplete": 0, "roots_stale": 0, "complete": True})
            self.assertEqual(lv.completeness(spec, first)["complete"], False)
            self.assertEqual(lv.completeness(spec, second), {"complete": True})
            # The spec fails a day whose complete roots fell or whose unvisited roots
            # did not fall.
            for field in ("roots_complete_not_falling", "roots_unvisited_falling"):
                fell = json.loads(json.dumps(second))
                fell["estate"]["progress"][field] = False
                self.assertTrue(lv.check_expectations(spec, 0, fell), field)

    def test_the_carried_estate_fails_on_one_unvisited_stale_or_incomplete_root(self):
        # gap-92884c654a7a: the estate is complete when every planned root is complete
        # within the full-walk freshness window; one root short of that fails it.
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        drive = estate_drive(sites=(Q_SITE, ts.SITE_ID, PERSONAL_SITE, OTHER_SITE),
                             libraries=SHIPPED_LIBRARIES)
        with tempfile.TemporaryDirectory() as home:
            report = self.run_cli(drive, home=home)
            self.assertTrue(report["estate"]["carried"]["complete"])
            state = Path(home, *cs.CHECK_STATE, "injected-transport")
            store = cs.SharePointCaptureStore(state)
            self.addCleanup(store.close)
            audience = LiveSourceAudience({"sharepoint:*": [cs.CAPTURE_PRINCIPAL]},
                                          tenant="plx")
            actor = ts.CAPTURE
            roots = [(r["drive_id"], r["root_path"]) for r in store.db.execute(
                "SELECT drive_id, root_path FROM roots ORDER BY drive_id")]
            self.assertEqual(len(roots), 3)

            def carried(planned=roots):
                return cs.carried_estate(store, actor, audience, planned)

            self.assertTrue(carried()["complete"])
            self.assertEqual(carried(roots + [("b!never", "/")])["roots_unvisited"], 1)
            self.assertFalse(carried(roots + [("b!never", "/")])["complete"])
            self.assertFalse(carried([])["complete"])   # nothing planned is never complete
            # One root's last full walk older than the stale window.
            old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                hours=cs.STALE_HOURS + 5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            with store.db:
                store.db.execute("UPDATE root_walks SET walked_at=? WHERE drive_id=? AND "
                                 "root_path=?", (old, *roots[0]))
            stale = carried()
            self.assertEqual((stale["roots_stale"], stale["roots_complete"], stale["complete"]),
                             (1, 2, False))
            # A due refresh inside the window keeps its root complete (the store serves it).
            due = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")
            with store.db:
                store.db.execute("UPDATE root_walks SET walked_at=? WHERE drive_id=? AND "
                                 "root_path=?", (due, *roots[0]))
            self.assertTrue(carried()["complete"])
            # One root whose walk is not complete.
            with store.db:
                store.db.execute("UPDATE roots SET status='in_progress' WHERE drive_id=? AND "
                                 "root_path=?", roots[1])
            part = carried()
            self.assertEqual((part["roots_incomplete"], part["complete"]), (1, False))

    def test_the_host_state_follows_a_set_workbench_state_dir(self):
        # The verifier's dailies keep only PLX_WORKBENCH_STATE_DIR (XDG state is not
        # carried there): when set, the check's store lives under it and carries across.
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        with tempfile.TemporaryDirectory() as workbench, \
                tempfile.TemporaryDirectory() as xdg:
            env = {**ts.ENV, "PLX_WORKBENCH_STATE_DIR": workbench, "XDG_STATE_HOME": xdg}
            first = self.run_cli(estate_drive(), environ=env)
            state = Path(workbench, "sharepoint-capture-check", "injected-transport")
            self.assertTrue(cs.store_exists(state))
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(Path(xdg).iterdir()), [])   # never the XDG base
            self.assertEqual((first["storage"]["state_base"], first["storage"]["carried"]),
                             ("workbench_state_dir", False))
            second = self.run_cli(estate_drive(), environ=env)
            self.assertEqual((second["storage"]["state_base"], second["storage"]["carried"],
                              second["storage"]["host_state"]),
                             ("workbench_state_dir", True, True))
            # Without it, the XDG base, named as such.
            third = self.run_cli(estate_drive(), environ={**ts.ENV, "XDG_STATE_HOME": xdg})
            self.assertEqual(third["storage"]["state_base"], "xdg_state_home")
        # A relative PLX_WORKBENCH_STATE_DIR is refused before any read, never a guess.
        drive = estate_drive()
        with self.assertRaisesRegex(ValueError, "state_dir_invalid"):
            self.run_cli(drive, environ={**ts.ENV, "PLX_WORKBENCH_STATE_DIR": "rel/state"})
        self.assertEqual(drive.calls, [])

    def test_a_malformed_registry_is_refused_before_any_read(self):
        drive = estate_drive()
        for entry in ({"system": "sharepoint", "kind": "site", "name": "no such/site"},
                      {"system": "sharepoint", "kind": "site"}, "PLXCUSTOMERS"):
            with self.subTest(entry=entry), self.assertRaises(cs.CaptureError):
                self.run_cli(drive, [*REGISTRY_SITES, entry])
        self.assertEqual(drive.calls, [])


if __name__ == "__main__":
    unittest.main()


class EstateKeptStateTests(unittest.TestCase):
    """A run cut by the time budget alone is blocked (later_ask
    live-host-persistent-state-dir, the progress shown) only over a store this
    invocation did not find kept: observed, never inferred from the state base."""

    run_cli = EstateCliTests.run_cli

    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.drive = estate_drive(
            sites=(Q_SITE, ts.SITE_ID, PERSONAL_SITE, OTHER_SITE),
            libraries=SHIPPED_LIBRARIES)

    def listed(self) -> int:
        return sum(1 for _m, u in self.drive.calls if u.split("?")[0].endswith("/children"))

    def cut(self, **kwargs) -> dict:
        return self.run_cli(self.drive, budget_seconds=1, monotonic=self.listed, **kwargs)

    def assert_blocked(self, report) -> None:
        from tests.spec_verdict import spec_of, verdict
        self.assertEqual(report["storage"]["carried"], False)
        self.assertFalse(report["ok"])
        named = report["incomplete_scope"]
        self.assertTrue(named)
        self.assertEqual({(e["persisted"], e["later_ask"]) for e in named},
                         {(False, "live-host-persistent-state-dir")})
        self.assertEqual(sorted(p["part"] for p in report["blocked_upstream"]),
                         sorted(f"{e['target']}.root{e['root']}" for e in named))
        self.assertEqual({(p["cause"], p["later_ask"]) for p in report["blocked_upstream"]},
                         {("later_ask", "live-host-persistent-state-dir")})
        self.assertEqual(verdict(spec_of("capture.sharepoint_estate"), 0, report),
                         ("blocked", []))

    def test_cold_estate_budget_cut_is_blocked(self):
        # The spec's own command (host state, no --state-dir) on its first day: the
        # discovered libraries are cut by the budget over a store nothing carried.
        with tempfile.TemporaryDirectory() as home:
            cold = self.cut(home=home)
            self.assertGreater(cold["estate"]["roots_budget_cut"], 0)
            self.assert_blocked(cold)
            self.assertTrue(all(e["target"].startswith("library")
                                for e in cold["incomplete_scope"]))
            # The next day carries it: the same cut is a typed fail, never blocked.
            again = self.cut(home=home)
            self.assertEqual((again["storage"]["carried"], again["blocked_upstream"]),
                             (True, []))
            self.assertTrue(again["incomplete_scope"])
            self.assertTrue(all(e["persisted"] and e["later_ask"] is None
                                for e in again["incomplete_scope"]))

    def test_state_dir_set_but_store_new_is_not_persisted(self):
        # PLX_WORKBENCH_STATE_DIR set (the kept base) is no proof the store is kept: a
        # store this run created is not persisted, its cut blocked; only a store found
        # there is.
        with tempfile.TemporaryDirectory() as workbench:
            env = {**ts.ENV, "PLX_WORKBENCH_STATE_DIR": workbench}
            first = self.cut(environ=env)
            self.assertEqual(first["storage"]["state_base"], "workbench_state_dir")
            self.assert_blocked(first)
            second = self.cut(environ=env)
            self.assertEqual((second["storage"]["carried"], second["blocked_upstream"]),
                             (True, []))
            self.assertTrue(second["incomplete_scope"])
            self.assertTrue(all(e["persisted"] for e in second["incomplete_scope"]))

