"""The capture.sharepoint_containers live check (gap-e10f050d25a3, ING-17).

``integrations/sharepoint_container_check.py`` runs over the shipped spec's own scope
and audience files with the Graph transport injected (a fake AGENTS site library, GET
only): it counts every container's members (N=1/10/60 probe: none lost), the parts not
read by reason, the typed members and the members resolved offline, proves a no-grant
and a folder-only actor see 0 members, and fails with a named reason otherwise. The
printed report is checked against the spec's expectations.

Evidence label: injected-transport. No network I/O, no credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit
import zipfile

from verify import live_verify as lv
from integrations import capture_sharepoint as cs
from integrations import sharepoint_container_check as check
from integrations.tests import test_capture_sharepoint as tsp
from ontology import project
from ontology.tests import test_container_members as tcm

ROOT = Path(__file__).resolve().parents[2]
SPEC_ID = "capture.sharepoint_containers"
SPEC = json.loads((ROOT / lv.SPECS_DIR_NAME / f"{SPEC_ID}.json").read_text("utf-8"))
SCOPE = str(ROOT / SPEC["command"][SPEC["command"].index("--scope") + 1])
AUDIENCE = str(ROOT / SPEC["command"][SPEC["command"].index("--live-audience") + 1])
FOLDER = "01USECASES"


def library(*files):
    """The AGENTS site library with its use-case folder holding ``files``
    ((id, name, content, mime, acl))."""
    drive = tsp.FakeDrive()
    drive.folder(FOLDER, "Ontology UseCases", drive=tsp.SITE_DRIVE)
    for iid, name, content, mime, acl in files:
        drive.file(iid, name, FOLDER, content, mime=mime, acl=acl, drive=tsp.SITE_DRIVE)
    return drive


def notes(count):
    return tcm.zipped([(f"n{i:02d}.txt", f"Re PO100236 batch {i}".encode())
                       for i in range(count)])


def large_packet():
    """A use-case packet over the 20 MB document bound, made at test time: two stored
    (uncompressed) scan members, each under the 20 MB member bound, and one note."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as bundle:
        for n in range(2):
            bundle.writestr(f"scan-{n}.bin", bytes(range(256)) * 41_000)   # 10.5 MB each
        bundle.writestr("note.txt", b"Re PO100236 batch 1")
    return buffer.getvalue()


ZIP = "application/zip"
PDF = ("01PLAIN", "plain.pdf", b"%PDF-1.7 not a container", "application/pdf",
       (tsp.RICARDO,))


class SharePointContainerCheckTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def run_check(self, drive, audience=AUDIENCE, scope=SCOPE, state="state"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check.main(["--state-dir", str(self.tmp / state), "--scope", scope,
                               "--live-audience", audience], environ=tsp.ENV, sender=drive,
                              sleep=lambda s: None)
        return code, out.getvalue(), json.loads(out.getvalue())

    def test_counts_every_member_and_a_no_grant_actor_sees_none(self):
        lv.validate_spec(SPEC, stem=SPEC_ID)
        self.assertIn(SPEC["command"][2], lv.ALLOWED_MODULES)
        drive = library(("01PACKET", "po-packet.zip", tcm.PACKET, ZIP, (tsp.RICARDO,)),
                        *[(f"01N{n}", f"notes-{n}.zip", notes(n), ZIP,
                           (tsp.RICARDO, tsp.SALES)) for n in (1, 10, 60)],
                        ("01BROKEN", "broken.zip", tcm.BROKEN, ZIP, (tsp.RICARDO,)), PDF)
        code, text, report = self.run_check(drive)
        self.assertEqual((code, report["reasons"], report["ok"]), (0, [], True), report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual({m for m, _ in drive.calls}, {"GET"})
        # Containers: the packet, three note zips and the unopenable zip; the PDF is not.
        self.assertEqual(report["containers"], 5)
        # 7 packet parts (nested zip and .eml included) + 1 + 10 + 60 notes + 1 gap.
        self.assertEqual((report["container_members"], report["member_records"]), (79, 79))
        self.assertEqual(report["members_by_state"],
                         {"read": 75, "container": 1, "unread": 3})
        self.assertEqual(report["members_unread"], {"text_unsupported": 1, "encrypted": 1,
                                                    "container_unreadable": 1})
        self.assertEqual(report["members_typed"], 1)   # the supplier confirmation member
        self.assertEqual(report["members_resolved"], 79)
        self.assertEqual(report["containers_unopened"], 0)
        self.assertIs(report["containers_unopened_truncated"], False)
        self.assertEqual(report["capture_resolve_check"]["resolved_offline"], 6)
        self.assertEqual(report["leak_check"], {"no_grant_members_visible": 0,
                                                "folder_only_members_visible": 0})
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])
        recorded = json.dumps(lv._recorded(SPEC, report))
        for marker in ("po-packet", "confirmation", "PO100235", "PO100236", "n07",
                       "Ontology", tsp.SITE_DRIVE, "01PACKET", "s3cr3t", "TOKEN"):
            self.assertNotIn(marker, text)
            self.assertNotIn(marker, recorded)

    def host_run(self, drive, budget=None, clock=None):
        """One run on the host state (no --state-dir, kept across runs) with a clock that
        advances 100 s per download (or ``clock``), so --budget-seconds 250 cuts the third;
        the report and the item ids it downloaded."""
        env = {**tsp.ENV, "PLX_WORKBENCH_STATE_DIR": str(self.tmp / "host")}
        before = len(drive.calls)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check.main(["--scope", SCOPE, "--live-audience", AUDIENCE,
                               *([] if budget is None else ["--budget-seconds", budget])],
                              environ=env, sender=drive, sleep=lambda s: None,
                              monotonic=clock or (lambda: drive.downloads() * 100))
        downloaded = [urlsplit(u).path.rsplit("/", 1)[1] for _m, u in drive.calls[before:]
                      if urlsplit(u).hostname == tsp.SP_HOST]
        return code, json.loads(out.getvalue()), downloaded

    def test_budget_cut_runs_resume_from_a_cursor_containers_first_until_complete(self):
        # Live 2026-10-17 (gap-ea28540d0766): every run began from an empty store, so a
        # folder larger than one budget never completed (roots_complete 0 of 1).
        sizes = {"01C1": 1, "01C3": 3, "01C5": 5, "01C7": 7}
        drive = library(("01A", "a.zip", notes(2), ZIP, (tsp.RICARDO,)),
                        *[(iid, f"c{n}.zip", notes(n), ZIP, (tsp.RICARDO,))
                          for iid, n in sizes.items()], PDF)
        drive.fail_download["01A"] = 503
        code, first, got = self.host_run(drive, "250")
        self.assertEqual((code, first["reasons"]), (1, ["capture_incomplete"]))
        self.assertEqual(got, ["01A", "01C1", "01C3"])
        self.assertEqual(first["storage"], {"host_state": True, "carried": False,
                                            "state_base": "workbench_state_dir"})
        self.assertTrue((self.tmp / "host" / check.CHECK_STATE / "injected-transport"
                         / "sharepoint-capture").is_dir())
        # The cursor: the walk position is kept, one container is still to be retried.
        self.assertEqual((first["cursor"]["held"], first["cursor"]["containers_pending"]),
                         (True, 1))
        self.assertEqual((first["containers_opened"], first["members_read"]), (2, 4))
        # The cut root is named, resumable, and the host state keeps its walk: no ask
        # (the check's own store is the host's; run_cli is told so by state_kept).
        self.assertEqual([(e["target"], e["reasons"], e["resumable"], e["persisted"],
                           e["later_ask"]) for e in first["incomplete_scope"]],
                         [(first["incomplete_scope"][0]["target"], ["time_budget"], True,
                           True, None)])
        self.assertEqual(lv._recorded(SPEC, first)["cursor"]["held"], True)
        self.assertTrue(lv.check_expectations(SPEC, code, first))
        self.assertEqual(first["blocked_upstream"], [])   # the kept store resumes it
        drive.fail_download.clear()
        # The next run retries the unopened container first, then resumes the walk on the
        # containers not reached; nothing captured before is downloaded again.
        code, second, got = self.host_run(drive, "250")
        self.assertEqual((code, second["reasons"]), (1, ["capture_incomplete"]))
        self.assertEqual(got, ["01A", "01C5", "01C7"])
        self.assertEqual(second["cursor"]["containers_retried_first"], 1)
        self.assertEqual(second["progress"], {
            "carried": True, "previous": True, "runs": 2, "containers_opened_before": 2,
            "members_read_before": 4, "contents_captured_before": 2,
            "walk_folders_listed": 1, "walk_folders_listed_before": 1, "walk_moved": False,
            "forward": True})
        self.assertEqual((second["containers_opened"], second["members_read"]), (5, 18))
        code, third, got = self.host_run(drive, "250")
        self.assertEqual(got, ["01PLAIN"])
        # A fully read root: complete, every member once, nothing left in the cursor.
        self.assertEqual((code, third["reasons"], third["capture"]["roots_complete"]),
                         (0, [], 1), third)
        self.assertEqual((third["containers"], third["containers_unopened"],
                          third["container_members"], third["members_read"]),
                         (5, 0, 2 + sum(sizes.values()), 2 + sum(sizes.values())))
        self.assertEqual((third["cursor"]["held"], third["cursor"]["containers_pending"]),
                         (False, 0))
        self.assertEqual(lv.check_expectations(SPEC, code, third), [])
        # Complete and unchanged: the next run follows the change feed, downloads nothing.
        code, fourth, got = self.host_run(drive, "250")
        self.assertEqual((code, fourth["reasons"], got), (0, [], []))

    def test_a_run_that_moves_nothing_forward_fails_as_stalled(self):
        drive = library(("01A", "a.zip", notes(2), ZIP, (tsp.RICARDO,)),
                        ("01C3", "c3.zip", notes(3), ZIP, (tsp.RICARDO,)))
        drive.fail_download["01A"] = 503   # a container whose bytes never come
        code, first, _ = self.host_run(drive)
        self.assertEqual((code, first["reasons"]), (1, ["capture_incomplete"]))
        code, second, got = self.host_run(drive)
        self.assertEqual(got[0], "01A")   # retried first, again unavailable
        self.assertEqual((code, second["reasons"], second["progress"]["forward"]),
                         (1, ["capture_incomplete", "capture_stalled"], False))
        self.assertTrue(lv.check_expectations(SPEC, code, second))
        # A progress record this check did not write is never trusted as progress.
        progress = (self.tmp / "host" / check.CHECK_STATE / "injected-transport"
                    / check.PROGRESS_FILE)
        progress.write_text(json.dumps({"schema": check.PROGRESS_SCHEMA, "runs": 2,
                                        "containers_opened": "9"}), encoding="utf-8")
        code, third, _ = self.host_run(drive)
        self.assertEqual((third["reasons"], third["progress"]["previous"]),
                         (["capture_incomplete"], False))

    def test_a_failed_container_behind_100_failed_files_is_retried_first_once_a_run(self):
        # Review: the first pass looked only at the first 100 failed files and a container
        # failing again was retried a second time by the root's own repair.
        plain = [(f"01P{n:03d}", f"p{n:03d}.pdf", b"%PDF-1.7 p", "application/pdf",
                  (tsp.RICARDO,)) for n in range(101)]
        drive = library(*plain, ("01Z", "z.zip", notes(2), ZIP, (tsp.RICARDO,)))
        for iid, *_ in plain:
            drive.fail_download[iid] = 503
        drive.fail_download["01Z"] = 503
        code, first, got = self.host_run(drive)
        self.assertEqual((code, got.count("01Z"), first["cursor"]["containers_pending"]),
                         (1, 1, 1))
        code, second, got = self.host_run(drive)
        self.assertEqual((got[0], got.count("01Z")), ("01Z", 1), got)
        self.assertEqual(len(got), 1 + cs.REPAIRS_PER_ROOT)   # the rest retried once too
        self.assertEqual(second["cursor"]["containers_retried_first"], 0)
        drive.fail_download.pop("01Z")
        code, third, got = self.host_run(drive)
        self.assertEqual((got[0], got.count("01Z"), third["cursor"]["containers_retried_first"],
                          third["containers_opened"]), ("01Z", 1, 1, 1))

    def test_the_first_pass_spends_at_most_half_the_budget_so_the_walk_advances(self):
        drive = library(*[(f"01A{n}", f"a{n}.zip", notes(2), ZIP, (tsp.RICARDO,))
                          for n in (1, 2, 3)], ("01B", "b.zip", notes(1), ZIP, (tsp.RICARDO,)))
        for n in (1, 2, 3):
            drive.fail_download[f"01A{n}"] = 503
        code, first, got = self.host_run(drive, "300")
        self.assertEqual((got, first["cursor"]["containers_pending"]),
                         (["01A1", "01A2", "01A3"], 3))
        drive.fail_download.clear()
        # 300 s: the first pass retries until 150 s are spent (two containers at 100 s
        # each), then the walk resumes and opens the container it never reached.
        code, second, got = self.host_run(drive, "300")
        self.assertEqual(got, ["01A1", "01A2", "01B"])
        self.assertEqual((second["cursor"]["containers_retried_first"],
                          second["containers_opened"]), (2, 3))

    def test_a_run_that_only_moves_the_walk_cursor_is_not_stalled(self):
        # Review: a run that listed folders but captured nothing new read as stalled.
        drive = library(("01A", "a.zip", notes(2), ZIP, (tsp.RICARDO,)))
        for n in range(12):
            drive.folder(f"01S{n:02d}", f"s{n:02d}", parent=FOLDER, drive=tsp.SITE_DRIVE)
        drive.fail_download["01A"] = 503   # never readable: the root never completes
        clock = lambda: len(drive.calls) * 10.0   # every Graph GET takes 10 s
        runs = []
        for _ in range(6):
            code, report, _got = self.host_run(drive, "200", clock=clock)
            runs.append(report)
            if not report["cursor"]["held"]:
                break
        self.assertTrue(runs[0]["cursor"]["held"], runs[0])
        moving = [r for r in runs[1:] if r["progress"]["walk_moved"]]
        self.assertTrue(moving, [r["progress"] for r in runs])
        for report in moving:
            self.assertEqual((report["containers_opened"], report["progress"]["forward"]),
                             (0, True))
            self.assertNotIn("capture_stalled", report["reasons"])
        # The walk finished listing; the next run lists the same folders again (no held
        # walk to resume) and captures nothing: stalled.
        self.assertFalse(runs[-1]["cursor"]["held"], [r["cursor"] for r in runs])
        code, after, _got = self.host_run(drive, "1000", clock=clock)
        self.assertEqual((after["progress"]["walk_moved"], after["reasons"]),
                         (False, ["capture_incomplete", "capture_stalled",
                                 "containers_unopened"]), after["progress"])

    def test_verification_spends_the_budget_the_capture_left_unused_and_no_more(self):
        # Live 2026-10-19: a capture cut at the budget left 62 of 76 versions deferred by
        # the fixed VERIFY_SECONDS. A capture that ends early now verifies in what is left,
        # never past run_start + budget + VERIFY_SECONDS.
        files = [(f"01N{n}", f"notes-{n}.zip", notes(n), ZIP, (tsp.RICARDO, tsp.SALES))
                 for n in range(1, 21)]
        spent, verified_at = [0.0], []
        real = check._verify_item

        def verify(*a, **k):
            spent[0] += 10.0
            verified_at.append(drive.downloads() * 2 + spent[0])
            return real(*a, **k)
        drive = library(*files)
        with mock.patch.object(check, "_verify_item", verify):
            code, report, _dl = self.host_run(
                drive, budget="200", clock=lambda: drive.downloads() * 2 + spent[0])
        self.assertLess(drive.downloads() * 2, 100)     # the capture ended early
        self.assertGreater(len(verified_at), int(check.VERIFY_SECONDS // 10) + 1)
        self.assertLessEqual(max(verified_at) - 10, 200 + check.VERIFY_SECONDS)
        self.assertEqual(report["projection"]["items_verified"], len(verified_at))

    def test_post_capture_verification_is_bounded_and_carries_verified_versions(self):
        # Live 2026-10-17: with a kept store, re-projecting every captured version each run
        # grew past the spec's 300 s timeout. Each version is verified once (ledger) and a
        # run verifies new versions only until VERIFY_SECONDS, deferring the rest.
        files = [(f"01N{n}", f"notes-{n}.zip", notes(n), ZIP, (tsp.RICARDO, tsp.SALES))
                 for n in (1, 10, 60)] + [PDF]
        code, _text, whole = self.run_check(library(*files), state="whole")
        self.assertEqual((code, whole["projection"]),
                         (0, {"items_verified": 4, "items_carried": 0, "items_deferred": 0}))
        drive = library(*files)
        stale = self.tmp / "state" / f"{check.WORK_PREFIX}killed"
        stale.mkdir(parents=True)
        (stale / "item-1.sqlite").write_bytes(b"left by a killed run")
        totals = ("containers", "container_members", "member_records", "members_by_state",
                  "members_resolved", "members_typed", "leak_check")
        runs = []
        with mock.patch.object(check, "VERIFY_SECONDS", 0.0):
            for _ in range(4):
                code, _text, report = self.run_check(drive)
                runs.append((code, report))
        self.assertFalse(stale.exists())
        self.assertEqual([r["projection"] for _c, r in runs], [
            {"items_verified": 1, "items_carried": n, "items_deferred": 3 - n}
            for n in range(4)])
        for code, report in runs[:3]:
            self.assertEqual((code, report["reasons"]), (1, ["projection_deferred"]))
            self.assertTrue(lv.check_expectations(SPEC, code, report))
        code, last = runs[-1]
        self.assertEqual((code, last["reasons"]), (0, []))
        self.assertEqual({k: last[k] for k in totals}, {k: whole[k] for k in totals})
        # Nothing new: every version carried, none projected, the same counts.
        with mock.patch.object(check, "_verify_item", side_effect=AssertionError):
            code, _text, again = self.run_check(drive)
        self.assertEqual((code, again["projection"]["items_carried"]), (0, 4))
        self.assertEqual({k: again[k] for k in totals}, {k: whole[k] for k in totals})
        self.assertEqual(lv.check_expectations(SPEC, code, again), [])
        ledger = self.tmp / "state" / check.LEDGER_FILE
        self.assertNotIn("notes-", ledger.read_text("utf-8"))   # counts and digests only
        # A ledger this check would not write is never trusted: everything re-verified.
        doc = json.loads(ledger.read_text("utf-8"))
        next(iter(doc["items"].values()))["container_members"] = "9"
        ledger.write_text(json.dumps(doc), encoding="utf-8")
        code, _text, rebuilt = self.run_check(drive)
        self.assertEqual((code, rebuilt["projection"]["items_verified"]), (0, 4))
        # New bytes: the new version and the interval it supersedes (still projected, now
        # closed) get new keys and only they are verified again.
        drive.change("01N1", notes(2), drive=tsp.SITE_DRIVE)
        code, _text, changed = self.run_check(drive)
        self.assertEqual((code, changed["projection"]["items_verified"],
                          changed["projection"]["items_carried"],
                          changed["container_members"], changed["member_records"]),
                         (0, 2, 3, whole["container_members"] + 2,
                          whole["container_members"] + 2))

    def test_a_folder_without_containers_fails_with_a_named_reason(self):
        code, _, report = self.run_check(library(PDF))
        self.assertEqual((code, report["ok"]), (1, False))
        self.assertEqual(report["reasons"], ["no_container_members"])
        self.assertEqual(report["container_members"], 0)
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_a_cut_over_a_store_that_is_not_kept_is_blocked_by_its_later_ask(self):
        drive = library(("01A", "a.zip", notes(2), ZIP, (tsp.RICARDO,)),
                        ("01C1", "c1.zip", notes(1), ZIP, (tsp.RICARDO,)),
                        ("01C3", "c3.zip", notes(3), ZIP, (tsp.RICARDO,)), PDF)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):   # --state-dir: gone when the check ends
            code = check.main(["--state-dir", str(self.tmp / "state"), "--scope", SCOPE,
                               "--live-audience", AUDIENCE, "--budget-seconds", "150"],
                              environ=tsp.ENV, sender=drive, sleep=lambda s: None,
                              monotonic=lambda: drive.downloads() * 100)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)   # blocked on its later_ask, never ok
        self.assertFalse(report["ok"])
        self.assertIn("capture_incomplete", report["reasons"])
        self.assertEqual([e["later_ask"] for e in report["incomplete_scope"]],
                         ["live-host-persistent-state-dir"])
        self.assertEqual([p["cause"] for p in lv.blocked_parts(report)], ["later_ask"])
        self.assertEqual(lv.check_expectations(SPEC, code, report, SPEC["expect_blocked"]), [])
        self.assertTrue(lv.check_expectations(SPEC, code, report))
        leaking = {**report, "leak_check": {**report["leak_check"],
                                            "no_grant_members_visible": 1}}
        self.assertTrue(lv.check_expectations(SPEC, code, leaking, SPEC["expect_blocked"]))

    def test_a_folder_the_capture_cannot_read_fails_as_capture_incomplete(self):
        drive = tsp.FakeDrive()   # the AGENTS library holds no use-case folder
        code, _, report = self.run_check(drive)
        self.assertEqual(code, 1)
        self.assertEqual(report["reasons"], ["capture_incomplete", "no_container_members"])
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_a_grant_to_the_no_grant_actor_fails_as_a_leak(self):
        leaky = self.tmp / "audience.json"
        audience = json.loads(Path(AUDIENCE).read_text("utf-8"))
        audience["audiences"]["sharepoint:*"].append(check.WITHHELD)
        leaky.write_text(json.dumps(audience), encoding="utf-8")
        code, _, report = self.run_check(
            library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,))), str(leaky))
        self.assertEqual(code, 1)
        self.assertEqual(report["reasons"], ["capture_leak", "no_grant_sees_members"])
        self.assertEqual(report["leak_check"]["no_grant_members_visible"], 10)
        self.assertEqual(report["leak_check"]["folder_only_members_visible"], 0)
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_a_plain_file_that_does_not_resolve_offline_fails_the_check(self):
        resolve = cs.SharePointCaptureStore.resolve

        def lost_pdf(store, handle, *args, **kwargs):
            found = resolve(store, handle, *args, **kwargs)
            return None if found and found["row"].get("name") == "plain.pdf" else found

        with mock.patch.object(cs.SharePointCaptureStore, "resolve", lost_pdf):
            code, _, report = self.run_check(
                library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,)), PDF))
        self.assertEqual((code, report["reasons"]), (1, ["capture_unresolved"]))
        self.assertEqual(report["capture_resolve_check"]["resolved_offline"], 1)
        self.assertEqual(report["container_members"], 10)   # the members themselves pass
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_containers_over_the_content_cap_fail_as_unopened(self):
        scope = json.loads(Path(SCOPE).read_text("utf-8"))
        scope["max_content_bytes"] = len(PDF[2])   # every zip is over the cap
        capped = self.tmp / "scope.json"
        capped.write_text(json.dumps(scope), encoding="utf-8")
        code, _, report = self.run_check(
            library(*[(f"01N{n}", f"notes-{n}.zip", notes(n), ZIP, (tsp.RICARDO,))
                      for n in (1, 10)], PDF), scope=str(capped))
        self.assertEqual((code, report["reasons"]), (1, ["containers_unopened"]))
        self.assertEqual((report["containers_unopened"], report["container_members"]), (2, 0))
        self.assertEqual(report["capture"]["content_exceeds_cap"], 2)
        # Live 2026-10-08 (gap-47c7b50314bd): still containers, never 0, their members
        # one unread part each with the typed reason.
        self.assertEqual((report["containers"], report["members_by_state"],
                          report["members_unread"]),
                         (2, {"unread": 2}, {"container_exceeds_cap": 2}))
        failures = lv.check_expectations(SPEC, code, report)
        self.assertTrue(failures)
        self.assertFalse([f for f in failures if f.startswith("containers ")], failures)

    def test_a_container_over_20_mb_is_captured_pinned_and_read_member_by_member(self):
        # Live 2026-10-08 (gap-e10f050d25a3): the folder's one packet (~31 MB) was over a
        # 20 MB capture cap, so no member was read. The shipped scope declares the
        # guard's one-download bound: the packet's bytes are captured and pinned, and it
        # is opened as a container, each member read within its own bound.
        packet = large_packet()
        self.assertGreater(len(packet), 20_000_000)
        drive = library(("01LARGE", "packet.zip", packet, ZIP, (tsp.RICARDO,)))
        code, _, report = self.run_check(drive)
        self.assertEqual((code, report["reasons"], report["ok"]), (0, [], True), report)
        self.assertEqual((report["capture"]["contents_captured"],
                          report["capture"]["content_exceeds_cap"]), (1, 0))
        resolve_check = report["capture_resolve_check"]
        self.assertEqual((resolve_check["contents_checked"],
                          resolve_check["content_bytes_verified"]), (1, 1))
        self.assertEqual((report["containers"], report["containers_unopened"],
                          report["container_members"], report["member_records"],
                          report["members_resolved"]), (1, 0, 3, 3, 3))
        self.assertEqual(report["members_by_state"], {"read": 1, "unread": 2})
        self.assertEqual(report["leak_check"], {"no_grant_members_visible": 0,
                                                "folder_only_members_visible": 0})
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])
        store = cs.SharePointCaptureStore.open_read_only(self.tmp / "state")
        try:
            (row,) = store.db.execute("SELECT content_sha256, text_status FROM items")
        finally:
            store.close()
        self.assertEqual(tuple(row), (hashlib.sha256(packet).hexdigest(), "exceeds_bound"))
        # A scope cap still bounds it: one byte under the packet, it stays exceeds_cap
        # with its metadata, never read.
        scope = json.loads(Path(SCOPE).read_text("utf-8"))
        scope["max_content_bytes"] = len(packet) - 1
        capped = self.tmp / "scope.json"
        capped.write_text(json.dumps(scope), encoding="utf-8")
        downloads = drive.downloads()
        code, _, report = self.run_check(drive, scope=str(capped), state="capped")
        self.assertEqual((code, report["reasons"]), (1, ["containers_unopened"]))
        self.assertEqual((report["capture"]["files_current"],
                          report["capture"]["content_exceeds_cap"],
                          report["members_unread"]), (1, 1, {"container_exceeds_cap": 1}))
        self.assertEqual(drive.downloads(), downloads)

    def test_a_truncated_index_is_never_a_complete_unopened_count(self):
        scope = json.loads(Path(SCOPE).read_text("utf-8"))
        scope["max_content_bytes"] = len(PDF[2])   # every zip is over the cap
        capped = self.tmp / "scope.json"
        capped.write_text(json.dumps(scope), encoding="utf-8")
        with mock.patch.object(check, "INDEX_LIMIT", 1):
            code, _, report = self.run_check(
                library(*[(f"01N{n}", f"notes-{n}.zip", notes(n), ZIP, (tsp.RICARDO,))
                          for n in (1, 10)], PDF), scope=str(capped))
        # One of the three files is indexed: 0 or 1 unopened, a lower bound, flagged, and
        # never read as a folder without containers.
        self.assertEqual((code, report["reasons"][0]), (1, "index_truncated"))
        self.assertNotIn("no_container_members", report["reasons"])
        self.assertIs(report["containers_unopened_truncated"], True)
        self.assertLess(report["containers_unopened"], 2)
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_a_capture_not_ok_for_another_cause_fails_as_capture_failed(self):
        run_cli = cs.run_cli

        def not_ok(*args, **kwargs):
            return {**run_cli(*args, **kwargs), "ok": False}

        with mock.patch.object(cs, "run_cli", not_ok):
            code, _, report = self.run_check(
                library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,))))
        self.assertEqual((code, report["reasons"]), (1, ["capture_failed"]))
        self.assertEqual(report["container_members"], 10)
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_pinned_bytes_that_fail_the_rehash_are_a_typed_count_not_a_crash(self):
        run = cs.SharePointCapture.run

        def tampers(capture, scope):
            result = run(capture, scope)
            digest = hashlib.sha256(PDF[2]).hexdigest()
            (capture.store.blobs / digest[:2] / digest).write_bytes(b"tampered")
            return result

        with mock.patch.object(cs.SharePointCapture, "run", tampers):
            code, text, report = self.run_check(
                library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,)), PDF))
        self.assertEqual((code, report["reasons"]), (1, ["capture_unresolved"]))
        resolve_check = report["capture_resolve_check"]
        self.assertEqual((resolve_check["contents_checked"],
                          resolve_check["content_bytes_verified"],
                          resolve_check["content_bytes_failed"]),
                         (2, 1, {"blob_digest_mismatch": 1}))
        self.assertNotIn("Traceback", text)
        self.assertTrue(lv.check_expectations(SPEC, code, report))
        self.assertEqual(lv._recorded(SPEC, report)["capture_resolve_check"]
                         ["content_bytes_failed"], {"blob_digest_mismatch": 1})

    def test_a_container_whose_content_is_unavailable_is_unopened(self):
        drive = library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,)))
        drive.fail_download["01N10"] = 503
        code, _, report = self.run_check(drive)
        self.assertEqual((code, report["reasons"]),
                         (1, ["capture_incomplete", "containers_unopened"]))
        self.assertEqual(report["containers_unopened"], 1)
        self.assertEqual((report["containers"], report["members_unread"]),
                         (1, {"container_unavailable": 1}))

    def test_a_member_lost_by_the_projection_fails_as_not_projected(self):
        build = project.build_store

        def loses_one(path, *args, **kwargs):
            result = build(path, *args, **kwargs)
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("DELETE FROM objects WHERE object_id = (SELECT object_id FROM "
                             "objects WHERE object_type = ? LIMIT 1)", (check.MEMBER_TYPE,))
            return result

        with mock.patch.object(project, "build_store", loses_one):
            code, _, report = self.run_check(
                library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,))))
        self.assertEqual((code, report["reasons"]), (1, ["members_not_projected"]))
        self.assertEqual((report["member_records"], report["container_members"]), (10, 9))
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_a_member_handle_that_does_not_resolve_fails_as_unresolved(self):
        resolve = cs.SharePointCaptureStore.resolve

        def no_members(store, handle, *args, **kwargs):
            return None if check.split_member(handle) else resolve(store, handle, *args,
                                                                    **kwargs)

        with mock.patch.object(cs.SharePointCaptureStore, "resolve", no_members):
            code, _, report = self.run_check(
                library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,))))
        self.assertEqual((code, report["reasons"]), (1, ["members_unresolved"]))
        self.assertEqual((report["container_members"], report["members_resolved"]), (10, 0))
        self.assertTrue(lv.check_expectations(SPEC, code, report))

    def test_a_projection_error_fails_with_its_code(self):
        def fails(*args, **kwargs):
            raise project.ProjectionError("sharepoint_capture_layout_unsupported")

        with mock.patch.object(project, "build_store", fails):
            code, _, report = self.run_check(
                library(("01N10", "notes.zip", notes(10), ZIP, (tsp.RICARDO,))))
        self.assertEqual((code, report["reasons"]), (1, ["projection_failed"]))
        self.assertEqual(report["projection_error"], "sharepoint_capture_layout_unsupported")
        self.assertEqual(report["container_members"], 0)
        self.assertTrue(lv.check_expectations(SPEC, code, report))


if __name__ == "__main__":
    unittest.main()
