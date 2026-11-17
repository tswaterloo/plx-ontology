"""The host's SharePoint QC/QA library capture: budget-cut walks, the census and its carried
verdicts (split from test_host so each module stays under the suite's per-module limit)."""

from __future__ import annotations

import itertools
import json

from operating.tests.test_host import (CaptureScope, DeltaDrive, ENV, HostCase, Path, ROOT, cs,
    host_module, lv, patch, sp_t, unquote, urlsplit)


class QcqaLibraryTests(HostCase):
    """The QCQA library as a served, incremental capture (``capture.qcqa_library``): the
    host config names a library-root scope, later cycles read only new and changed files,
    and ``sharepoint_coverage`` lists every folder under the root against the store."""

    def setUp(self) -> None:
        super().setUp()
        drive = DeltaDrive()   # nested folders under the library root, with a change feed
        drive.folder("01QUALITY", "Quality")
        drive.folder("01AOS", "AOs", parent="01QUALITY")
        drive.folder("01AOS2026", "2026 - AOs", parent="01AOS")
        drive.folder("01POLICY", "Policies", parent="01QUALITY")
        drive.file("01TOP", "index.pdf", sp_t.ROOT_ID)
        drive.file("01AO1", "AO-1.pdf", "01AOS2026")
        drive.file("01AO2", "AO-2.pdf", "01AOS2026", b"%PDF-1.7 second")
        drive.file("01RISK", "risk.txt", "01POLICY", b"risk review", mime="text/plain")
        self.router.drive = self.drive = drive
        self.write("library-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 5000}]})
        self.config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                    "budget_seconds": 600})

    def check(self, *extra):
        code, out, err = self.main("check", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint", *extra)
        return code, json.loads(out) if out else None, err

    def test_a_file_added_between_cycles_is_the_only_one_read_and_coverage_is_whole(self):
        shipped = CaptureScope.from_file(
            ROOT / "examples/sharepoint_capture_scope.qcqa_library.json")
        self.assertEqual([t.folders for t in shipped.targets], [("/",)])   # the listing's
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        lv.validate_spec(spec, stem="capture.qcqa_library")
        # Three cycles, each bounded by the shipped config's capture budget, and the
        # coverage census, by its own, leave the timeout room for the projections and the
        # trim: a verdict.
        shipped_config = host_module.HostConfig.from_file(
            ROOT / spec["command"][spec["command"].index("--config") + 1])
        shipped = shipped_config.sources["sharepoint"]
        self.assertEqual(Path(shipped["scope"]).resolve(),
                         (ROOT / "examples/sharepoint_capture_scope.qcqa_library.json").resolve())
        self.assertLessEqual(3 * shipped["budget_seconds"] + shipped["census_budget_seconds"],
                             spec["timeout_seconds"] - 600)
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint")
        first = json.loads(out)
        self.assertEqual({n: s["state"] for n, s in first["sources"].items()},
                         {"portal": "not_configured", "bc": "not_configured",
                          "mail": "not_configured", "sharepoint": "captured",
                          "teams": "not_configured"}, err)
        self.assertEqual(first["sources"]["sharepoint"]["rounds"], {"full": 1})
        self.assertEqual(first["sources"]["sharepoint"]["counts"]["versions_new"], 4)
        downloads = self.drive.downloads()

        self.drive.file("01AO3", "AO-3.pdf", "01AOS2026", b"%PDF-1.7 third")
        mark = self.router.mark()
        code, document, err = self.check("--cycles", "3")
        sharepoint = [c["sources"]["sharepoint"] for c in document["cycles"]]
        # Only the new file is read: every cycle follows the change feed, never re-walks.
        self.assertEqual([s["rounds"] for s in sharepoint], [{"incremental": 1}] * 3)
        self.assertEqual([(s["counts"]["files_seen"], s["counts"]["versions_new"])
                          for s in sharepoint], [(1, 1), (0, 0), (0, 0)])
        self.assertEqual(self.drive.downloads(), downloads + 1)
        coverage = document["sharepoint_coverage"]
        listings = [u for _m, u in self.router.since(mark) if "/children" in u]
        self.assertEqual(len(listings), coverage["folders_listed"])   # the listing's only
        self.assertEqual((coverage["roots"], coverage["folders_listed"],
                          coverage["files_seen"], coverage["files_current"],
                          coverage["complete"]), (1, 5, 5, 5, True), coverage)
        self.assertEqual(lv.check_expectations(spec, code, document), [], (err, document))
        self.assertEqual(lv.completeness(spec, document), {"complete": True})

        # A file the change feed never reports and one deleted without a feed entry: the
        # listing finds both, so the check fails rather than serving a whole root.
        sp_t.FakeDrive.file(self.drive, "01LOST", "AO-4.pdf", "01AOS")
        sp_t.FakeDrive.delete(self.drive, "01RISK")
        code, document, err = self.check("--cycles", "3")
        coverage = document["sharepoint_coverage"]
        self.assertEqual((coverage["files_seen"], coverage["files_current"],
                          coverage["files_missed"], coverage["files_current_not_seen"],
                          coverage["complete"]), (5, 5, 1, 1, False), coverage)
        failures = lv.check_expectations(spec, code, document)
        self.assertEqual(sorted(f.split()[0] for f in failures),
                         ["sharepoint_coverage.files_current_not_seen",
                          "sharepoint_coverage.files_missed"], failures)
        # A source the config does not name, or a budget that is not a positive number of
        # seconds, is refused, never silently skipped, run unbounded or never run.
        code, document, err = self.check("--sources", "sharepoint,nope")
        self.assertEqual((code, document), (2, None))
        self.assertIn("host_sources_invalid", err)
        # 0 would never read a file; a census without a finite bound would never end.
        for budget in ({"budget_seconds": "600"}, {"budget_seconds": 0},
                       {"census_budget_seconds": None}, {"census_budget_seconds": 0},
                       {"census_budget_seconds": float("inf")}):
            self.config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                        **budget})
            code, document, err = self.check()
            self.assertEqual((code, document), (2, None))
            self.assertIn("host_config_invalid:sharepoint", err)

    def test_a_walk_cut_by_the_budget_resumes_in_the_next_cycle_never_restarts(self):
        # A library larger than one cycle's budget (QA-01 live: ~63 files per 1,200 s
        # against 32,644): each cycle resumes the stored walk position, never starts over.
        for n in range(8):
            self.drive.folder(f"01BULK{n}", f"Bulk {n}", parent="01QUALITY")
            for m in range(3):
                self.drive.file(f"01BULK{n}F{m}", f"bulk-{n}-{m}.txt", f"01BULK{n}",
                                b"bulk %d %d" % (n, m), mime="text/plain")
        self.config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                    "budget_seconds": 40})
        config = host_module.HostConfig.from_file(self.config).only(["sharepoint"])
        ticks = itertools.count()   # one second per budget check

        def make_host():
            return host_module.Host(self.state, config, environ=ENV, sender=self.router,
                                    clock=self.clock, monotonic=lambda: next(ticks))

        # 40 checks a cycle read 2 files in the first cycle (it lists the folders and reads
        # their permissions) and 8 in each later one (11-14): the 28 files take five cycles.
        document = host_module.cycles_check(make_host, cycles=6)
        sharepoint = [c["sources"]["sharepoint"] for c in document["cycles"]]
        resumed = [s["resumed_from"] for s in sharepoint]
        self.assertEqual(resumed[0], {"new_walk": 1}, sharepoint)
        self.assertEqual(sharepoint[0]["walk_positions_held"], 1, sharepoint)
        # Every later cycle until the walk finishes resumes it; none starts a new walk.
        finished = next(i for i, s in enumerate(sharepoint) if s["complete"])
        self.assertGreater(finished, 1, sharepoint)
        self.assertEqual(resumed[1:finished + 1], [{"walk_position": 1}] * finished,
                         sharepoint)
        self.assertEqual(document["checks"]["sharepoint_roots_restarted"], 0)
        # Every file is read exactly once across the cycles: progress is kept, not redone.
        self.assertEqual(sum(s["counts"]["versions_new"] for s in sharepoint), 4 + 24)
        self.assertEqual(self.drive.downloads(), 4 + 24)

        # A cycle that drops the held walk position and starts over is counted.
        later = {"sources": {"sharepoint": {"resumed_from": {"new_walk": 1}}}}
        earlier = {"sources": {"sharepoint": {"walk_positions_held": 1}}}
        self.assertEqual(host_module._sharepoint_restarted(earlier, later), 1)
        self.assertEqual(host_module._sharepoint_restarted({"sources": {}}, later), 0)

    def test_a_census_cut_by_a_scope_bound_fails_the_check_never_blocked(self):
        # QA-01 live 2026-10-21: a library over max_folders. The bound is the repo's own
        # scope (agent-fixable), not an upstream value: the check fails on census_complete
        # and the document carries no blocked part to hide it behind.
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                         "max_depth": 8, "max_folders": 2}]})
        config = host_module.HostConfig.from_file(self.write_config(
            sharepoint={"scope": "bound-scope.json", "budget_seconds": 600})).only(["sharepoint"])
        document = host_module.cycles_check(lambda: host_module.Host(
            self.state, config, environ=ENV, sender=self.router, clock=self.clock), cycles=2)
        coverage = document["sharepoint_coverage"]
        self.assertEqual((coverage["census_complete"], coverage["reasons"]),
                         (False, ["folder_bound"]))
        self.assertEqual(lv.blocked_parts(document), [])
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        failures = lv.check_expectations(spec, 0, document)
        self.assertTrue([f for f in failures if "census_complete" in f], failures)
        from tests.spec_verdict import verdict
        self.assertEqual(verdict(spec, 0, document)[0], "fail")

    def test_a_census_claims_a_file_gone_only_where_it_listed_the_folder_whole(self):
        # A library of more files than one capture budget lists (QA-01: the census the
        # capture's 400 s cut reported 12,220 files): the census's own budget bounds it.
        self.drive.folder("01BULK", "Bulk")
        for n in range(60):
            self.drive.file(f"01BULK{n}", f"bulk-{n}.txt", "01BULK", b"bulk %d" % n,
                            mime="text/plain")
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint")
        self.assertEqual(json.loads(out)["sources"]["sharepoint"]["counts"]["versions_new"],
                         64, err)
        keys = ("census_complete", "complete", "reasons", "roots_listed", "folders_listed",
                "files_seen", "files_current", "files_not_captured", "files_current_not_seen")

        def census(ticks=None, **sharepoint) -> dict:
            config = self.write_config(sharepoint={"scope": "library-scope.json",
                                                   **sharepoint})
            host = host_module.Host(self.state, host_module.HostConfig.from_file(config),
                                    environ=ENV, sender=self.router, clock=self.clock)
            ticks = iter(range(1000) if ticks is None else ticks)
            coverage = host_module.sharepoint_coverage(host, monotonic=lambda: next(ticks))
            return {key: coverage[key] for key in keys}

        # Six folders take seven steps (one second each): a 2 s capture budget no longer
        # cuts the census.
        self.assertEqual(census(budget_seconds=2, census_budget_seconds=60),
                         {"census_complete": True, "complete": True, "reasons": [],
                          "roots_listed": 1, "folders_listed": 6, "files_seen": 64,
                          "files_current": 64, "files_not_captured": 0,
                          "files_current_not_seen": 0})
        # The census's budget spent after the root: complete false with the one file it
        # listed, and the 63 in the folders it never reached are not claimed gone (no
        # earlier census's verdict to keep: QcqaCensusVerdictTests).
        spent = [0, 0] + [host_module.CENSUS_BUDGET_SECONDS] * 10
        (self.state / host_module.HOST_DIR / host_module.CENSUS_FILE).unlink()
        self.assertEqual(census(spent),
                         {"census_complete": False, "complete": False,
                          "reasons": ["time_budget"], "roots_listed": 0, "folders_listed": 1,
                          "files_seen": 1, "files_current": 64, "files_not_captured": 0,
                          "files_current_not_seen": 0})
        # A root with a permanent gap (folders below max_depth 1 are never listed): a file
        # gone from a folder it listed whole (the root, Bulk) is reported, one gone from a
        # folder it never reached is not.
        self.write("library-depth1.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                         "max_depth": 1, "max_folders": 5000}]})
        for item_id in ("01TOP", "01BULK0", "01AO1"):
            sp_t.FakeDrive.delete(self.drive, item_id)
        self.assertEqual(census(scope="library-depth1.json"),
                         {"census_complete": False, "complete": False,
                          "reasons": ["depth_bound"], "roots_listed": 0, "folders_listed": 3,
                          "files_seen": 59, "files_current": 64, "files_not_captured": 0,
                          "files_current_not_seen": 2})


    def test_a_census_lists_a_folder_larger_than_one_listing_whole(self):
        # gap-375b698206de: the census listed a folder only up to LIST_MAX_ITEMS (5000)
        # and reported listing_truncated on every run. A folder of 12,000 children is
        # listed in three listings (5,000 + 5,000 + 2,000), each resuming at the page the
        # one before it stopped: every page read once, every file seen once, no gap.
        self.drive.folder("01BIG", "Big")
        for n in range(12_000):
            self.drive.file(f"01BIG{n:05d}", f"b{n}.txt", "01BIG", b"x", mime="text/plain")
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV, sender=self.router, clock=self.clock)
        ticks = itertools.count()
        coverage = host_module.sharepoint_coverage(host, monotonic=lambda: next(ticks))
        self.assertEqual({key: coverage[key] for key in (
            "census_complete", "reasons", "folders_listed", "files_seen",
            "files_not_captured")},
                         {"census_complete": True, "reasons": [], "folders_listed": 6,
                          "files_seen": 12_004, "files_not_captured": 12_004})
        pages = [u for _m, u in self.drive.calls
                 if urlsplit(u).path.endswith("/items/01BIG/children")]
        self.assertEqual((len(pages), len(set(pages))), (60, 60))


    def census_library(self, folders: int, files: int = 3) -> int:
        """``folders`` more folders under the root, ``files`` files in each; the number
        of files the library then holds."""
        for n in range(folders):
            self.drive.folder(f"01CF{n:03d}", f"Census {n}", parent="01QUALITY")
            for m in range(files):
                self.drive.file(f"01CF{n:03d}F{m}", f"c-{n}-{m}.txt", f"01CF{n:03d}",
                                b"census %d %d" % (n, m), mime="text/plain")
        return 4 + folders * files

    def capture_all(self) -> None:
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(self.config), "--sources", "sharepoint")
        self.assertEqual(code, 0, err)

    def census_host(self, **sharepoint):
        config = self.write_config(sharepoint={"scope": sharepoint.pop("scope",
                                                                       "library-scope.json"),
                                               **sharepoint})
        return host_module.Host(self.state, host_module.HostConfig.from_file(config),
                                environ=ENV, sender=self.router, clock=self.clock)

    def listings(self, since: int = 0) -> list[str]:
        return [unquote(u) for _m, u in self.drive.calls[since:] if "/children" in u]

    def test_a_census_cut_by_the_folder_bound_resumes_and_lists_each_folder_once(self):
        total = self.census_library(10)
        self.capture_all()
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 4}]})
        mark = len(self.drive.calls)
        runs = []
        for _call in range(10):
            coverage = host_module.sharepoint_coverage(
                self.census_host(scope="bound-scope.json", census_budget_seconds=60))
            runs.append(coverage)
            if coverage["census_complete"]:
                break
        # 15 folders at 4 a call: four calls; every cut one names its bound and carries.
        self.assertEqual([r["reasons"] for r in runs], [["folder_bound"]] * 3 + [[]], runs)
        self.assertEqual([r["folders_listed"] for r in runs], [4, 8, 12, 15])
        self.assertEqual([r["census"]["carried"] for r in runs], [False, True, True, True])
        self.assertEqual([r["census"]["calls"] for r in runs], [1, 2, 3, 4])
        self.assertEqual([r["census"]["folders_listed_before"] for r in runs], [0, 4, 8, 12])
        final = runs[-1]
        self.assertEqual((final["files_seen"], final["files_current"], final["complete"],
                          final["census"]["folders_pending"]), (total, total, True, 0))
        # No folder listed twice and none skipped: 15 listings, 15 distinct folders.
        firsts = [u for u in self.listings(mark) if "skiptoken" not in u]
        self.assertEqual((len(firsts), len(set(firsts))), (15, 15))
        # A finished census is consumed: the next call lists afresh from the root, and
        # while that listing is cut by its own bound the finished census's verdict is
        # kept inside its age window, with the cut named.
        again = host_module.sharepoint_coverage(
            self.census_host(scope="bound-scope.json", census_budget_seconds=60))
        self.assertEqual((again["census"]["carried"], again["census"]["calls"],
                          again["reasons"], again["census"]["verdict"],
                          again["census"]["refresh_reasons"], again["folders_listed"]),
                         (False, 1, [], "ended_kept", ["folder_bound"], 4))
        # Past the window the verdict is gone: the cut fails census_complete again.
        self.clock.advance(25 * 3600)
        later = host_module.sharepoint_coverage(
            self.census_host(scope="bound-scope.json", census_budget_seconds=60))
        self.assertEqual((later["census_complete"], later["reasons"],
                          later["census"]["verdict"]), (False, ["folder_bound"], "this_census"))

    def test_a_census_cut_by_its_time_budget_resumes_inside_the_folder_it_was_in(self):
        # Pages of 5 children: a folder of 12 files is three listings; the budget (three
        # listings a call) cuts inside folders, and the next call carries on at the page
        # it stopped at, never from the folder's start.
        self.census_library(6, files=12)
        self.capture_all()
        mark = len(self.drive.calls)
        runs = []
        with patch.object(cs, "LIST_PAGE_SIZE", 5):
            for _call in range(40):
                start = len(self.listings())
                coverage = host_module.sharepoint_coverage(
                    self.census_host(census_budget_seconds=3),
                    monotonic=lambda: float(len(self.listings()) - start))
                runs.append(coverage)
                if coverage["census_complete"]:
                    break
            end = len(self.drive.calls)
            # The same library listed in one go (the finished census was consumed).
            whole = host_module.sharepoint_coverage(self.census_host(
                census_budget_seconds=10_000), monotonic=lambda: 0.0)
        self.assertGreater(len(runs), 3)
        self.assertTrue(all(r["reasons"] == ["time_budget"] for r in runs[:-1]), runs)
        self.assertTrue(runs[-1]["census_complete"], runs[-1])
        keys = ("folders_listed", "files_seen", "files_current", "files_not_captured",
                "files_current_not_seen", "complete")
        self.assertEqual({k: runs[-1][k] for k in keys}, {k: whole[k] for k in keys})
        self.assertEqual(runs[-1]["files_seen"], 4 + 72)
        rising = [r["folders_listed"] for r in runs]
        self.assertEqual(rising, sorted(rising))
        # Every listing page was read once across the carried calls, and the carried
        # census read exactly the pages the uninterrupted one did.
        carried = [unquote(u) for _m, u in self.drive.calls[mark:end] if "/children" in u]
        self.assertEqual(len(carried), len(set(carried)), carried)
        uninterrupted = [unquote(u) for _m, u in self.drive.calls[end:] if "/children" in u]
        self.assertEqual(sorted(carried), sorted(uninterrupted))

    def test_a_stale_foreign_or_unreadable_census_restarts_and_never_resumes(self):
        self.census_library(6)
        self.capture_all()
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 3}]})

        def call(scope="bound-scope.json"):
            return host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))

        kept = self.state / host_module.HOST_DIR / host_module.CENSUS_FILE
        first, second = call(), call()
        self.assertEqual((first["census"]["carried"], second["census"]["carried"],
                          second["folders_listed"]), (False, True, 6))
        # Older than the capture's full_walk_hours: the listing starts over.
        self.clock.advance(25 * 3600)
        third = call()
        self.assertEqual((third["census"]["carried"], third["census"]["restarted_stale"],
                          third["folders_listed"], third["census"]["calls"]), (False, True, 3, 1))
        # Another scope's bounds: not the same census.
        self.write("other-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 7,
                                                "max_folders": 3}]})
        other = call("other-scope.json")
        self.assertEqual((other["census"]["carried"], other["folders_listed"]), (False, 3))
        # A kept file that is not a census (or has lost a field) is never trusted.
        for damaged in ("{not json", json.dumps({"schema": "x"}),
                        json.dumps({**json.loads(kept.read_text()), "roots": {"0:/": {}}})):
            call("other-scope.json")
            kept.write_text(damaged, "utf-8")
            fresh = call("other-scope.json")
            self.assertEqual((fresh["census"]["carried"], fresh["folders_listed"]), (False, 3))
        self.assertEqual(kept.stat().st_mode & 0o777, 0o600)

    def bounded_scope(self, max_folders: int, **extra) -> str:
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, **extra,
            "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                         "max_depth": 8, "max_folders": max_folders}]})
        return "bound-scope.json"

    def test_a_census_carries_at_the_checks_weekly_cadence_within_its_own_age_window(self):
        self.census_library(6)
        self.capture_all()
        scope = self.bounded_scope(4, census_max_age_hours=720)
        outs = []
        for _ in range(4):
            cov = host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))
            outs.append((cov["census_complete"], cov["census"]["carried"],
                         cov["census"]["restarted_stale"]))
            self.clock.advance(7 * 24 * 3600)
            if cov["census_complete"]:
                break
        self.assertEqual(outs, [(False, False, False), (False, True, False),
                                (True, True, False)])
        # Without its own window the census keeps the capture's full_walk_hours.
        host_module.sharepoint_coverage(self.census_host(
            scope=self.bounded_scope(4), census_budget_seconds=60))
        self.clock.advance(7 * 24 * 3600)
        again = host_module.sharepoint_coverage(self.census_host(
            scope=self.bounded_scope(4), census_budget_seconds=60))
        self.assertEqual((again["census"]["carried"], again["census"]["restarted_stale"]),
                         (False, True))

    def test_a_capture_stopped_by_its_own_folder_bound_is_named_never_a_pass(self):
        self.census_library(6)          # 11 folders; the capture's walk bound is 8
        scope = self.bounded_scope(8)
        config = self.write_config(sharepoint={"scope": scope})
        code, _out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                    str(config), "--sources", "sharepoint")
        self.assertEqual(code, 0, err)
        cov = {}
        for _ in range(4):
            cov = host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))
            if cov["census_complete"]:
                break
        self.assertEqual((cov["census_complete"], cov["complete"], cov["folders_listed"],
                          cov["capture_incomplete"]), (True, False, 11,
                                                       ["capture_folder_bound"]))
        self.assertGreater(cov["files_not_captured"], 0)

    # ------------- one rule for a budget cut

    def census_cut_check(self, census_listings: int = 3) -> dict:
        """``check`` of three cycles over this test's state, its coverage census cut by
        its own time budget after ``census_listings`` folder listings (a fake clock: one
        second a listing), the capture itself complete."""
        self.census_library(10)
        config = host_module.HostConfig.from_file(self.write_config(sharepoint={
            "scope": "library-scope.json", "budget_seconds": 600,
            "census_budget_seconds": census_listings})).only(["sharepoint"])
        coverage = host_module.sharepoint_coverage
        start = [0]

        def census(host, **_kw):
            start[0] = len(self.listings())
            return coverage(host, monotonic=lambda: float(len(self.listings()) - start[0]))

        with patch.object(host_module, "sharepoint_coverage", census):
            return host_module.cycles_check(lambda: host_module.Host(
                self.state, config, environ=ENV, sender=self.router, clock=self.clock),
                cycles=3)

    def test_cold_census_cut_is_blocked_later_ask(self):
        from tests.spec_verdict import verdict
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        lv.validate_spec(spec, stem="capture.qcqa_library")
        self.assertFalse(cs.store_exists(self.state))
        cold = self.census_cut_check()
        coverage = cold["sharepoint_coverage"]
        self.assertEqual((coverage["census_complete"], coverage["reasons"],
                          coverage["folders_listed"], coverage["census"]["carried"]),
                         (False, ["time_budget"], 3, False), coverage)
        # The capture is whole; only the census, cut on a state no run left, falls short:
        # blocked on the human step that keeps the state, its progress shown.
        self.assertEqual(cold["blocked_upstream"], [
            {"part": "sharepoint.census", "source": "microsoft-graph", "cause": "later_ask",
             "later_ask": "live-host-persistent-state-dir"}])
        self.assertEqual(verdict(spec, 0, cold), ("blocked", []))
        # A leak beside the cut is never hidden by it.
        leaked = json.loads(json.dumps(cold))
        leaked["trim"]["withheld_leaks"] = 1
        self.assertEqual(verdict(spec, 0, leaked)[0], "fail")
        # The same cut on the next run, which found the census and the store kept: the
        # cut resumes by itself, so it is a typed fail until it finishes, never blocked.
        carried = self.census_cut_check()
        self.assertTrue(carried["sharepoint_coverage"]["census"]["carried"])
        self.assertGreater(carried["sharepoint_coverage"]["census"]["folders_listed_before"], 0)
        self.assertNotIn("blocked_upstream", carried)
        result, failures = verdict(spec, 0, carried)
        self.assertEqual(result, "fail")
        self.assertTrue([f for f in failures if "census_complete" in f], failures)
        # A cold cut beside a scope bound or a gap is a typed fail too (the one rule).
        self.assertEqual(host_module.coverage_cut_block(
            {"cycles": cold["cycles"], "sharepoint_coverage": {
                **coverage, "reasons": ["folder_bound", "time_budget"]}}, carried=False), [])
        self.assertEqual(host_module.coverage_cut_block(
            {"cycles": cold["cycles"], "sharepoint_coverage": {
                **coverage, "files_missed": 1}}, carried=False), [])
        self.assertEqual(host_module.coverage_cut_block(
            {"cycles": cold["cycles"], "sharepoint_coverage": {
                "census_complete": False, "complete": False, "reasons": ["time_budget"]}},
            carried=False), [])   # coverage not measured: never blocked

    def test_ended_census_verdict_kept_within_window(self):
        # A library larger than one call's census: once a census ended complete, a later
        # call whose fresh census its own bound cuts keeps that verdict inside the age
        # window, and judges what it re-listed; it never keeps it past a change or the
        # window.
        self.census_library(10)
        self.capture_all()
        scope = self.bounded_scope(4, census_max_age_hours=720)

        def call():
            return host_module.sharepoint_coverage(
                self.census_host(scope=scope, census_budget_seconds=60))

        runs = [call() for _ in range(4)]
        self.assertEqual([r["census_complete"] for r in runs], [False] * 3 + [True])
        self.assertEqual([r["census"]["verdict"] for r in runs], ["this_census"] * 4)
        self.clock.advance(7 * 24 * 3600)
        mark = len(self.listings())
        kept = call()
        self.assertEqual((kept["census_complete"], kept["complete"], kept["reasons"],
                          kept["census"]["verdict"], kept["census"]["refresh_reasons"],
                          kept["census"]["carried"], kept["folders_listed"]),
                         (True, True, [], "ended_kept", ["folder_bound"], False, 4))
        self.assertEqual(len(self.listings()) - mark, 4)   # it re-listed, never skipped it
        # A file the change feed never reported, in a folder the fresh census lists on
        # its next call: found, and the kept verdict is dropped (a missed file, the
        # census cut): never a pass on the earlier listing.
        sp_t.FakeDrive.file(self.drive, "01SILENT", "AO-9.pdf", "01CF001")
        self.clock.advance(7 * 24 * 3600)
        changed = call()
        self.assertEqual((changed["census_complete"], changed["census"]["verdict"],
                          changed["reasons"], changed["files_missed"]),
                         (False, "this_census", ["folder_bound"], 1), changed)
        # Once the file reaches the capture (here: the feed reports it), the fresh census
        # ends complete, and its own verdict is kept no longer than the window.
        self.drive._logged(sp_t.DRIVE, "01SILENT")
        self.capture_all()
        for _ in range(4):
            ended = call()
            if ended["census"]["verdict"] == "this_census" and ended["census_complete"]:
                break
        self.assertEqual((ended["census_complete"], ended["complete"],
                          ended["census"]["verdict"]), (True, True, "this_census"), ended)
        self.assertEqual(call()["census"]["verdict"], "ended_kept")
        self.clock.advance(721 * 3600)
        stale = call()
        self.assertEqual((stale["census_complete"], stale["census"]["verdict"],
                          stale["reasons"]), (False, "this_census", ["folder_bound"]))

    def test_an_invalid_census_window_is_refused(self):
        for bad in (0, -1, 721, True, "24", 1.5):
            with self.subTest(bad=bad), self.assertRaises(cs.CaptureError):
                CaptureScope([{"id": "a", "drive_id": "d", "folders": ["/"]}],
                             census_max_age_hours=bad)

    def test_an_exception_in_a_folders_listing_never_skips_the_folder(self):
        from integrations import graph_read
        total = self.census_library(6)
        self.capture_all()
        scope = self.bounded_scope(50)
        orig = graph_read.SharePointReader.list_folder
        for failing in (4, 1):
            with self.subTest(failing=failing):
                kept = self.state / host_module.HOST_DIR / host_module.CENSUS_FILE
                kept.unlink(missing_ok=True)
                calls = {"n": 0}

                def boom(reader, *a, **k):
                    calls["n"] += 1
                    if calls["n"] == failing:
                        raise RuntimeError("interrupted")
                    return orig(reader, *a, **k)
                with patch.object(graph_read.SharePointReader, "list_folder", boom):
                    with self.assertRaises(RuntimeError):
                        host_module.sharepoint_coverage(
                            self.census_host(scope=scope, census_budget_seconds=60))
                cov = host_module.sharepoint_coverage(
                    self.census_host(scope=scope, census_budget_seconds=60))
                self.assertEqual((cov["census_complete"], cov["folders_listed"],
                                  cov["files_seen"], cov["files_current_not_seen"]),
                                 (True, 5 + 6, total, 0))

    def test_a_carried_census_loses_and_repeats_no_folder_at_any_size(self):
        for folders in (1, 10, 60):
            with self.subTest(folders=folders):
                self.setUp()
                total = self.census_library(folders, files=2)
                self.capture_all()
                self.write("bound-scope.json", {
                    "schema": "plx-sharepoint-capture-scope/v1",
                    "max_content_bytes": 1_000_000, "targets": [{
                        "id": "qcqa.library", "drive_id": sp_t.DRIVE, "folders": ["/"],
                        "max_depth": 8, "max_folders": 7}]})
                mark = len(self.drive.calls)
                for _call in range(30):
                    coverage = host_module.sharepoint_coverage(self.census_host(
                        scope="bound-scope.json", census_budget_seconds=60))
                    if coverage["census_complete"]:
                        break
                self.assertEqual((coverage["census_complete"], coverage["folders_listed"],
                                  coverage["files_seen"], coverage["files_current"],
                                  coverage["complete"]),
                                 (True, 5 + folders, total, total, True), coverage)
                firsts = [u for u in self.listings(mark) if "skiptoken" not in u]
                self.assertEqual((len(firsts), len(set(firsts))), (5 + folders,) * 2)

    def test_the_check_keeps_its_state_by_name_and_passes_once_the_census_completes(self):
        self.census_library(8)
        self.write("bound-scope.json", {
            "schema": "plx-sharepoint-capture-scope/v1", "max_content_bytes": 1_000_000,
            "full_walk_hours": 24, "targets": [{"id": "qcqa.library", "drive_id": sp_t.DRIVE,
                                                "folders": ["/"], "max_depth": 8,
                                                "max_folders": 8}]})
        self.config = self.write_config(sharepoint={
            "scope": "bound-scope.json", "budget_seconds": 600, "census_budget_seconds": 60})
        spec = json.loads((ROOT / "devloop/live-checks/capture.qcqa_library.json").read_text())
        name = spec["command"][spec["command"].index("--state-name") + 1]
        workbench = self.root / "workbench"
        env = {**ENV, "PLX_WORKBENCH_STATE_DIR": str(workbench)}

        def run():
            code, out, err = self.main("check", "--state-name", name, "--config",
                                       str(self.config), "--sources", "sharepoint",
                                       "--cycles", "2", environ=env)
            return code, json.loads(out or "null"), err

        code, first, err = run()
        self.assertEqual(code, 0, err)
        self.assertTrue((workbench / f"host-check-{name}" / "live" / "host").is_dir())
        coverage = first["sharepoint_coverage"]
        self.assertEqual((coverage["census_complete"], coverage["reasons"],
                          coverage["census"]["carried"]), (False, ["folder_bound"], False))
        failures = lv.check_expectations(spec, 0, first)
        self.assertTrue([f for f in failures if "census_complete" in f], failures)
        code, second, err = run()
        coverage2 = second["sharepoint_coverage"]
        self.assertTrue(coverage2["census"]["carried"], coverage2)
        self.assertGreater(coverage2["folders_listed"], coverage["folders_listed"])
        self.assertTrue(coverage2["census_complete"], coverage2)
        self.assertEqual([f for f in lv.check_expectations(spec, code, second)
                          if "census_complete" in f or "reasons" in f], [])
        # --state-name and --state-dir together, or a name that is no name: refused.
        for argv in (("--state-name", name, "--state-dir", str(self.state)),
                     ("--state-name", "../x")):
            code, out, err = self.main("check", *argv, "--config", str(self.config),
                                       environ=env)
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn("state_name_invalid", err)



if __name__ == "__main__":
    import unittest
    unittest.main()
