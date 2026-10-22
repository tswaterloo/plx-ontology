"""Capture breadth over injected Graph transports: every allowlisted mailbox,
inbox and sent items, plus SharePoint roots that follow their drive's change feed.

No network, no credentials. Evidence: injected-transport. Covers the rules at their
boundary: a mailbox outside the one allowlist is never requested, cycle 2 follows only
stored cursors, attachment bytes that fail verification are a recorded failure, every
scope folder is in the completeness denominator with a named reason, GET only, and
exposed-case sources resolve from the captures by their source ids.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

from integrations import capture_breadth, capture_mail, capture_sharepoint, graph_read
from integrations.capture_mail import CaptureScope, MailCaptureStore
from integrations.capture_sharepoint import SharePointCaptureStore
from integrations.graph_read import GraphMailReader, SharePointReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint as ts
from ontology.types import Actor

ROOT = Path(__file__).resolve().parents[2]
EXPOSED = ROOT / "evals" / "exposed"
# Allowlisted (operating/case_mail_scope.json) and not allowlisted addresses.
UPN_R, UPN_S, UPN_X = ("receiving@petrasoap.com", "ship@petrasoap.com",
                       "outsider@petrasoap.com")
BOX_R, BOX_S, BOX_X = ("12345678-aaaa-bbbb-cccc-0000000000a1",
                       "12345678-aaaa-bbbb-cccc-0000000000a2",
                       "12345678-aaaa-bbbb-cccc-0000000000a3")
PRINCIPAL = "svc:capture"
CAPTURE = Actor("plx", frozenset({PRINCIPAL}), "restricted")
WITHHELD = Actor("plx", frozenset({"staff:capture-withheld"}), "restricted")
ENV = ts.ENV


def audience(*mailboxes: str) -> LiveSourceAudience:
    grants = {f"mail:{box}": [PRINCIPAL] for box in mailboxes or (UPN_R, UPN_S)}
    grants["sharepoint:*"] = [PRINCIPAL]
    return LiveSourceAudience(grants, tenant="plx", classifications={"mail:*": "restricted"})


class MailGraph(tm.FakeGraph):
    """The mail fake with allowlisted mailboxes, an inbox and sent items each, a delta
    feed per folder and one directory user who is not on the allowlist."""

    def __init__(self) -> None:
        super().__init__()
        self.users = {box: {"id": box, "userPrincipalName": upn, "mail": upn,
                            "proxyAddresses": [f"SMTP:{upn}"]}
                      for box, upn in ((BOX_R, UPN_R), (BOX_S, UPN_S), (BOX_X, UPN_X))}
        self.folders, self.messages = {}, {}
        for box in self.users:
            inbox, sent = f"AAMkIN-{box[-2:]}=", f"AAMkSENT-{box[-2:]}="
            self.folders[box] = {"inbox": inbox, inbox.lower(): inbox,
                                 "sentitems": sent, sent.lower(): sent}
            self.messages[box] = {}

    def put_in(self, user, mid, change_key, folder="inbox", **kwargs) -> None:
        self.put(user, mid, change_key, **kwargs)
        self.messages[user][mid]["parentFolderId"] = self.folders[user][folder]

    def _delta(self, user, folder, query, headers):
        everything = self.messages[user]   # the base fake's feed covers one folder each
        self.messages[user] = {k: m for k, m in everything.items()
                               if m["parentFolderId"] == folder}
        try:
            return super()._delta(user, folder, query, headers)
        finally:
            self.messages[user] = everything

    def mailbox_calls(self, *keys: str) -> list[str]:
        """Graph URLs naming any of these mailbox keys (directory listing excluded)."""
        return [u for _m, u, _h in self.calls
                if any(k.lower() in unquote(u).lower() for k in keys)]


class DeltaDrive(ts.FakeDrive):
    """The SharePoint fake plus a drive change feed (``root/delta``): ``token=latest``
    answers a deltaLink, ``token=<n>`` lists every item changed since (with its
    ancestors, as Graph does), deleted items with a ``deleted`` facet, never a path."""

    def __init__(self) -> None:
        super().__init__()
        self.seq = 0
        self.log: list[tuple[int, str, str, dict | None]] = []
        self.function_links = False
        self.delta_failures = 0   # the next N feed reads answer a malformed page

    def _logged(self, drive, iid, gone=None):
        self.seq += 1
        self.log.append((self.seq, drive, iid, gone))

    def folder(self, iid, name, parent=ts.ROOT_ID, drive=ts.DRIVE):
        super().folder(iid, name, parent, drive)
        self._logged(drive, iid)

    def file(self, iid, name, parent, content=b"%PDF-1.7 scan", **kwargs):
        super().file(iid, name, parent, content, **kwargs)
        self._logged(kwargs.get("drive", ts.DRIVE), iid)

    def change(self, iid, content, drive=ts.DRIVE):
        super().change(iid, content, drive)
        self._logged(drive, iid)

    def move(self, iid, parent, *, new_etag=False, drive=ts.DRIVE):
        super().move(iid, parent, new_etag=new_etag, drive=drive)
        self._logged(drive, iid)

    def delete(self, iid, drive=ts.DRIVE):
        item = self.items[f"{drive}:{iid}"]
        gone = {"id": iid, "deleted": {"state": "deleted"},
                "parentReference": {"driveId": drive, "id": item["parent"]},
                ("folder" if item["folder"] else "file"): {}}
        super().delete(iid, drive)
        self._logged(drive, iid, gone)

    def _link(self, drive, seq):
        base = f"https://graph.microsoft.com/v1.0/drives/{drive}/root/delta"
        return f"{base}(token='{seq}')" if self.function_links else f"{base}?token={seq}"

    def _entry(self, drive, iid):
        raw = self.raw(drive, iid)
        raw.get("parentReference", {}).pop("path", None)
        return raw

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        path = unquote(parts.path)
        for drive in (ts.DRIVE, ts.SITE_DRIVE):
            if path == f"/v1.0/drives/{drive}/root/delta":
                self.calls.append((method, url))
                assert method == "GET", method
                if self.delta_failures:
                    self.delta_failures -= 1
                    return ts.j(200, {"error": "not a delta page"})
                token = parse_qs(parts.query)["token"][0]
                if token == "latest":
                    return ts.j(200, {"value": [], "@odata.deltaLink": self._link(drive,
                                                                                   self.seq)})
                latest: dict[str, dict] = {}
                for seq, where, iid, gone in self.log:
                    if seq <= int(token) or where != drive:
                        continue
                    chain = [iid]
                    while f"{drive}:{chain[-1]}" in self.items \
                            and self.items[f"{drive}:{chain[-1]}"].get("parent"):
                        chain.append(self.items[f"{drive}:{chain[-1]}"]["parent"])
                    for node in chain:
                        latest[node] = (self._entry(drive, node)
                                        if f"{drive}:{node}" in self.items else gone)
                return ts.j(200, {"value": list(latest.values()),
                                  "@odata.deltaLink": self._link(drive, self.seq)})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


def seeded_delta_drive() -> DeltaDrive:
    drive = DeltaDrive()
    drive.folder("01QUALITY", "Quality")
    drive.folder("01AOS", "AOs", parent="01QUALITY")
    drive.folder("01LOTS", "Lots", parent="01AOS")
    drive.file("01FILEA", "packet.pdf", "01AOS")
    drive.file("01FILEB", "release.txt", "01LOTS", b"release one", mime="text/plain")
    return drive


class BreadthCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.graph = MailGraph()
        self.drive = seeded_delta_drive()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def mail_api(self):
        return graph_read.build_graph(ENV, sender=self.graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))[0]

    def cycle(self, mail_scope=None, sharepoint_scope=None, grants=(UPN_R, UPN_S), **kw):
        rights = audience(*grants)
        api = self.mail_api()
        sp_api, transport = graph_read.build_graph(ENV, sender=self.drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        return capture_breadth.capture_cycle(
            self.state, CAPTURE, mail_scope=mail_scope, mail_reader=GraphMailReader(api, rights),
            sharepoint_scope=sharepoint_scope, page_size=2, sleep=lambda s: None,
            sharepoint_readers=lambda d: SharePointReader(
                sp_api, rights, d, transport=transport, read_permissions=True),
            directory=lambda: capture_mail.directory_mailboxes(
                api, mail_scope.directory_allows) if mail_scope else None, **kw)


class MailBreadthTests(BreadthCase):
    def test_cycle_two_follows_only_stored_delta_links_for_inbox_and_sent_items(self):
        for box in (BOX_R, BOX_S):
            self.graph.put_in(box, f"IN-{box[-2:]}", "CK-1")
            self.graph.put_in(box, f"SENT-{box[-2:]}", "CK-1", folder="sentitems")
        scope = CaptureScope([{"address": UPN_R, "folders": ["inbox", "sentitems"]},
                              {"address": UPN_S, "folders": ["inbox", "sentitems"]}])
        first = self.cycle(scope)
        self.assertTrue(first["complete"], first)
        self.assertEqual((first["complete_bound"], first["bound_total"]), (4, 4))
        self.assertEqual({(f["folder"], f["round"]) for f in first["folders"]},
                         {("inbox", "initial"), ("sentitems", "initial")})
        calls_before = len(self.graph.delta_calls())
        self.graph.put_in(BOX_R, "SENT-NEW", "CK-1", folder="sentitems")
        second = self.cycle(scope)
        self.assertEqual({f["round"] for f in second["folders"]}, {"incremental"})
        later = self.graph.delta_calls()[calls_before:]
        self.assertEqual(len(later), 4)
        for _m, url, _h in later:
            query = parse_qs(urlsplit(url).query)
            self.assertIn("$deltatoken", query)          # a stored delta link, never a
            self.assertNotIn("$filter", query)           # fresh windowed round
        store = MailCaptureStore(self.state)
        try:
            sent = store.db.execute("SELECT COUNT(*) FROM messages WHERE folder_id=? AND "
                                    "superseded_at IS NULL",
                                    (self.graph.folders[BOX_R]["sentitems"],)).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(sent, 2)
        self.assertEqual({m for m, _u, _h in self.graph.calls
                          if urlsplit(_u).hostname == "graph.microsoft.com"}, {"GET"})

    def test_a_mailbox_not_on_the_allowlist_is_never_requested(self):
        # Not listed, pending confirmation, an object id: set aside when read, never run.
        named = [UPN_X, "accounting1@petrasoap.com", BOX_R, UPN_S]
        scope = CaptureScope.from_json({"schema": capture_mail.SCOPE_SCHEMA,
                                        "mailboxes": [{"address": a} for a in named]})
        self.assertEqual(scope.outside_allowlist, tuple(named[:3]))
        self.assertEqual(scope.mailboxes, ((UPN_S, ("inbox",)),))
        self.graph.put_in(BOX_X, "X-0", "CK-1")
        run = self.cycle(scope, grants=(UPN_R, UPN_S, UPN_X))
        self.assertEqual((run["complete_bound"], run["bound_total"]), (1, 1))
        self.assertEqual(self.graph.mailbox_calls(UPN_X, BOX_X, "accounting1", BOX_R), [])

    def test_the_shipped_scope_is_every_tenant_mailbox_inbox_and_sent_items(self):
        from operating.case_mail_scope import MailScope

        shipped = CaptureScope.from_file(capture_mail.DEFAULT_SCOPE)
        self.assertTrue(shipped.all_mailboxes)
        self.assertEqual(shipped.all_mailbox_folders, ("inbox", "sentitems"))
        self.assertEqual(shipped.internal_domains, MailScope.builtin().internal_domains)
        document = json.loads(capture_mail.DEFAULT_SCOPE.read_text(encoding="utf-8"))
        self.assertIsInstance(document["all_mailboxes"]["exclude"], list)   # explicit
        for example in ("mail_capture_scope", "case_mail_capture_scope",
                        "case_mail_purchasing_capture_scope"):
            CaptureScope.from_file(ROOT / "examples" / f"{example}.live-check.json")
        # The breadth check's capture principal holds a grant for any tenant mailbox.
        spec = json.loads((ROOT / "verify/live-checks/capture.breadth.json").read_text())
        command = spec["command"]
        self.assertEqual(command[command.index("--mail-scope") + 1],
                         "integrations/mail_capture_scope.json")
        rights = LiveSourceAudience.from_file(
            ROOT / command[command.index("--live-audience") + 1])
        self.assertTrue(rights.allows(CAPTURE, f"mail:{BOX_X}"))
        self.assertTrue(rights.allows(CAPTURE, "mail:someone-new@petrasoap.com"))
        # The capture's own time budget ends it partial before the check's timeout.
        self.assertLess(float(command[command.index("--budget-seconds") + 1]),
                        spec["timeout_seconds"])


class TenantScopeTests(BreadthCase):
    """Every mailbox of the tenant directory, paged, minus the exclusion list,
    each with its own cursor; a refused mailbox is a bound gap (injected-transport)."""

    def scope(self, *excluded):
        return CaptureScope.from_json({
            "schema": capture_mail.SCOPE_SCHEMA, "mailboxes": [],
            "all_mailboxes": {"enabled": True, "folders": ["inbox", "sentitems"],
                              "exclude": [{"address": a, "reason": "test"} for a in excluded]}})

    def test_the_paged_directory_is_captured_except_the_excluded_mailbox(self):
        for box in (BOX_R, BOX_S, BOX_X):
            self.graph.put_in(box, f"IN-{box[-2:]}", "CK-1")
        guest = "12345678-aaaa-bbbb-cccc-0000000000a9"
        self.graph.users[guest] = {"id": guest, "userPrincipalName": "g#EXT#@petrasoap.com",
                                   "mail": "guest@partner.example", "userType": "Guest"}
        self.graph.users_page = 1                  # four /users pages
        run = self.cycle(self.scope(UPN_S), grants=(UPN_R, UPN_S, UPN_X,
                                                    "guest@partner.example"))
        self.assertEqual(run["mail"]["directory"], "complete")
        self.assertEqual(sorted(m["mailbox_id"] for m in run["mail"]["mailboxes"]),
                         [BOX_R, BOX_X])            # X is not on the allowlist: captured
        self.assertEqual((run["mail"]["mailboxes_listed"], run["mail"]["mailboxes_excluded"]),
                         (2, 1))                    # the guest is skipped, S excluded
        self.assertEqual(self.graph.mailbox_calls(guest), [])
        self.assertTrue(run["complete"], run)
        self.assertEqual((run["complete_bound"], run["bound_total"]), (4, 4))
        self.assertEqual(self.graph.mailbox_calls(UPN_S, BOX_S), [])   # never requested
        listing = [u for _m, u, _h in self.graph.calls if urlsplit(u).path == "/v1.0/users"]
        # Four pages for the mailbox directory and four for the people guard's bind.
        guard = [u for u in listing if "proxyAddresses" in unquote(u)]
        self.assertEqual((len(listing) - len(guard), len(guard)), (4, 4))
        self.assertEqual({m for m, u, _h in self.graph.calls
                          if urlsplit(u).hostname == "graph.microsoft.com"}, {"GET"})
        # An exclusion by object id holds too.
        self.graph.calls.clear()
        run = self.cycle(self.scope(BOX_X), grants=(UPN_R, UPN_S, UPN_X))
        self.assertNotIn(BOX_X, [m["mailbox_id"] for m in run["mail"]["mailboxes"]])
        self.assertEqual(run["mail"]["mailboxes_excluded"], 1)
        self.assertEqual(self.graph.mailbox_calls(BOX_X, UPN_X), [])

    def test_a_mailbox_new_in_the_directory_starts_while_the_others_resume(self):
        scope = self.scope()
        later = {BOX_S: self.graph.users.pop(BOX_S), BOX_X: self.graph.users.pop(BOX_X)}
        self.graph.put_in(BOX_R, "IN-R", "CK-1")
        first = self.cycle(scope)
        self.assertEqual([m["mailbox_id"] for m in first["mail"]["mailboxes"]], [BOX_R])
        self.graph.users[BOX_S] = later[BOX_S]
        self.graph.put_in(BOX_S, "IN-S", "CK-1")
        second = self.cycle(scope)
        rounds = {m["mailbox_id"]: {f["round"] for f in m["folders"]}
                  for m in second["mail"]["mailboxes"]}
        self.assertEqual(rounds, {BOX_R: {"incremental"}, BOX_S: {"initial"}})
        self.assertTrue(second["complete"], second)

    def test_a_forbidden_mailbox_is_a_bound_gap_and_the_run_goes_on(self):
        for box in (BOX_R, BOX_S):
            self.graph.put_in(box, f"IN-{box[-2:]}", "CK-1")
        self.graph.forbidden = {BOX_S}
        self.graph.users.pop(BOX_X)
        run = self.cycle(self.scope())
        self.assertFalse(run["complete"])
        self.assertEqual((run["complete_bound"], run["bound_total"]), (2, 4))
        self.assertIn("forbidden", run["incomplete_reasons"])
        gaps = {(f["mailbox"], f["reason"], f["state"]) for f in run["folders"]
                if not f["complete"]}
        self.assertEqual(len(gaps), 1)
        self.assertEqual(next(iter(gaps))[1:], ("forbidden", "failed"))
        # The next run asks again (no cursor), and completes once Graph allows it.
        self.graph.forbidden = set()
        again = self.cycle(self.scope())
        self.assertTrue(again["complete"], again)

    def test_attachment_bytes_that_fail_verification_are_a_recorded_failure(self):
        short = tm.file_attachment("ATT-0")
        short["size"] = len(tm.ATTACHMENT_BYTES) - 10        # bytes exceed Graph's size
        self.graph.put_in(BOX_S, "S-0", "CK-1", attachments=[short])
        self.graph.put_in(BOX_R, "R-1", "CK-1", attachments=[tm.file_attachment("ATT-1")])
        self.graph.put_in(BOX_R, "R-2", "CK-1", attachments=[tm.file_attachment("ATT-2")])
        scope = CaptureScope([{"address": UPN_R}])
        from integrations import graph_evidence
        decode = graph_evidence.decode_file_attachment

        def forged(raw, attachment_id, **kwargs):
            """One read answers other bytes than listed: another size, another digest."""
            metadata, content = decode(raw, attachment_id, **kwargs)
            if attachment_id == "ATT-1":
                metadata = {**metadata, "size": metadata["size"] + 7}
            if attachment_id == "ATT-2":
                metadata = {**metadata, "content_sha256": "0" * 64}
            return metadata, content

        with mock.patch.object(graph_evidence, "decode_file_attachment", forged):
            run = self.cycle(scope)
        folder = run["folders"][0]
        self.assertEqual((folder["complete"], folder["reason"]),
                         (False, "attachment_bytes_mismatch"))
        counts = run["mail"]["mailboxes"][0]["folders"][0]["counts"]
        self.assertEqual(counts["versions_new"], 2)         # kept as versions, not dropped
        self.assertGreaterEqual(counts["attachments_mismatched"], 2)
        store = MailCaptureStore(self.state)
        try:
            states = dict(store.db.execute("SELECT attachment_id, content_status FROM "
                                           "attachments").fetchall())
            self.assertEqual(states, {"ATT-1": "mismatch:size", "ATT-2": "mismatch:sha256"})
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM blobs WHERE kind="
                                              "'attachment'").fetchone()[0], 0)
            report = store.completeness(CAPTURE, audience())
            self.assertEqual(report["mailboxes"][0]["mismatched_attachments"], 2)
        finally:
            store.close()
        # The source answers consistently again: a later cycle repairs both.
        again = self.cycle(scope)
        self.assertTrue(again["complete"], again)
        store = MailCaptureStore(self.state)
        try:
            self.assertEqual({r[0] for r in store.db.execute(
                "SELECT content_status FROM attachments")}, {"captured"})
        finally:
            store.close()
        # Decoded bytes larger than Graph's reported size are the same recorded failure.
        run = self.cycle(CaptureScope([{"address": UPN_S}]))
        self.assertEqual(run["folders"][0]["reason"], "attachment_bytes_mismatch")
        store = MailCaptureStore(self.state)
        try:
            self.assertEqual(store.db.execute(
                "SELECT content_status FROM attachments WHERE attachment_id='ATT-0'"
            ).fetchone()[0], "mismatch:size")
        finally:
            store.close()

    def test_completeness_counts_every_scope_folder_with_a_named_reason(self):
        for n in range(1, 6):
            self.graph.put_in(BOX_R, f"R-{n}", "CK-1")
        self.graph.put_in(BOX_R, "R-SENT", "CK-1", folder="sentitems")
        scope = CaptureScope([{"address": UPN_R, "folders": ["inbox", "sentitems"]},
                              {"address": UPN_S, "folders": ["inbox", "sentitems"]}])
        run = self.cycle(scope, grants=(UPN_R,), page_budget=1)   # ship@ withheld
        self.assertEqual((run["complete_bound"], run["bound_total"]), (1, 4))
        self.assertEqual(run["incomplete_reasons"], ["not_visible", "page_budget"])
        reasons = {(f["mailbox"], f["folder"]): f["reason"] for f in run["folders"]}
        self.assertEqual(reasons, {(0, "inbox"): "page_budget", (0, "sentitems"): None,
                                   (1, "inbox"): "not_visible",
                                   (1, "sentitems"): "not_visible"})
        self.assertFalse(run["complete"])
        # A delta stopped mid-round is in progress from its stored page cursor, never
        # complete; a folder it cannot read is failed with its reason.
        states = {(f["mailbox"], f["folder"]): (f["state"], f["cursor"]) for f in run["folders"]}
        self.assertEqual(states, {(0, "inbox"): ("in_progress", "next_url"),
                                  (0, "sentitems"): ("complete", "delta_url"),
                                  (1, "inbox"): ("failed", None),
                                  (1, "sentitems"): ("failed", None)})

    def test_a_spent_time_budget_cuts_folders_and_roots_partial_and_they_resume(self):
        # Live 2026-10-08: capture.breadth ran into its 6900 s timeout.
        # Each delta page costs one second of this clock and the cycle has one: the inbox
        # stops after its first page, every other folder and the SharePoint root are cut
        # before any read, each a partial unit with its typed reason, never a timeout.
        for n in range(1, 6):
            self.graph.put_in(BOX_R, f"R-{n}", "CK-1")
        scope = CaptureScope([{"address": UPN_R, "folders": ["inbox", "sentitems"]},
                              {"address": UPN_S, "folders": ["inbox"]}])
        drive_calls = len(self.drive.calls)
        run = self.cycle(scope, sharepoint_scope=sp_scope(), budget_seconds=1,
                         monotonic=lambda: len(self.graph.delta_calls()))
        self.assertEqual(len(self.graph.delta_calls()), 1)
        # The target resolved and classified, its root read nothing.
        self.assertEqual([u for _m, u in self.drive.calls[drive_calls:]
                          if "/root" in u or "/items/" in u], [])
        rows = {(f["source"], f.get("mailbox"), f.get("folder")): (f["reason"], f["state"],
                                                                  f["cursor"])
                for f in run["folders"]}
        self.assertEqual(rows, {
            ("mail", 0, "inbox"): ("time_budget", "in_progress", "next_url"),
            ("mail", 0, "sentitems"): ("time_budget", "failed", None),
            ("mail", 1, "inbox"): ("time_budget", "failed", None),
            ("sharepoint", None, None): ("time_budget", "failed", None)})
        self.assertEqual((run["complete"], run["complete_bound"], run["bound_total"],
                          run["incomplete_reasons"]), (False, 0, 4, ["time_budget"]))
        with self.assertRaises(capture_mail.CaptureError):
            self.cycle(scope, budget_seconds=-1)
        again = self.cycle(scope, sharepoint_scope=sp_scope())
        self.assertTrue(again["complete"], again["incomplete_reasons"])
        inbox = again["mail"]["mailboxes"][0]["folders"][0]
        self.assertEqual(inbox.get("resumed_from"), "next_url")
        store = MailCaptureStore(self.state)
        try:
            self.assertEqual(store.db.execute(
                "SELECT COUNT(*) FROM messages WHERE superseded_at IS NULL").fetchone()[0], 5)
        finally:
            store.close()

    def test_mail_cannot_spend_the_sharepoint_share_of_a_cycle(self):
        # Mail ran first on the whole cycle budget, so a large
        # mailbox starved SharePoint. Each mail delta page costs one second of a cycle of
        # four: the inbox stops at its half (time_budget), the SharePoint root walks to
        # the end on its own share, and a mailbox done early leaves the rest to it.
        for n in range(1, 10):
            self.graph.put_in(BOX_R, f"R-{n}", "CK-1")
        scope = CaptureScope([{"address": UPN_R, "folders": ["inbox"]}])
        run = self.cycle(scope, sharepoint_scope=sp_scope(), budget_seconds=4,
                         monotonic=lambda: len(self.graph.delta_calls()))
        self.assertEqual(len(self.graph.delta_calls()), 2)
        self.assertEqual({f["source"]: (f["complete"], f["reason"]) for f in run["folders"]},
                         {"mail": (False, "time_budget"), "sharepoint": (True, None)})
        self.assertEqual(run["budget"], {"mail_granted": 2, "sharepoint_granted": 2})
        self.graph.put_in(BOX_S, "S-1", "CK-1")
        base = len(self.graph.delta_calls())
        run = self.cycle(CaptureScope([{"address": UPN_S}]), sharepoint_scope=sp_scope(),
                         budget_seconds=4,
                         monotonic=lambda: len(self.graph.delta_calls()) - base)
        self.assertEqual(run["budget"], {"mail_granted": 2, "sharepoint_granted": 3})
        with self.assertRaises(capture_mail.CaptureError):
            self.cycle(scope, sharepoint_scope=sp_scope(), sharepoint_share=1.5)


def sp_scope(**target):
    entry = {"id": "quality", "drive_id": ts.DRIVE, "folders": [ts.QUALITY_FOLDER],
             "max_depth": 3, "max_folders": 50, **target}
    return capture_sharepoint.CaptureScope([entry])


def flat(value) -> str:
    """Tool output as text with object ids unescaped (they hold quotes)."""
    return json.dumps(value).replace('\\"', '"')


class TenantTrimmingTests(unittest.TestCase):
    """Broader capture never broadens visibility: mailbox B, captured with the whole
    directory, stays invisible to a reader granted mailbox A only -- no B item and no
    B count through plx.find, plx.around or plx.documents (injected-transport)."""

    @classmethod
    def setUpClass(cls):
        import datetime as dt
        import sqlite3

        from ontology import mentions, store_ddl
        from ontology import schema as schema_loader
        from ontology.project import MailCaptures, build_store
        from ontology.tests.test_mail_documents import CONTRACTS, StaticPortal, po
        from operating import ontology_tools as ot
        from operating import ontology_tools_aggregate as oa
        from operating import ontology_tools_evidence as ev
        from operating import ontology_tools_traverse as tt

        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        state = Path(cls.tmp.name) / "state"
        graph = tm.FakeGraph()
        graph.put(tm.BOX_A, "MSG-A", "CK-A", body="Please confirm PO100234 today.",
                  subject="Order PO100234")
        graph.put(tm.BOX_B, "MSG-B", "CK-B", body="Finance hold on PO100234.",
                  subject="Finance PO100234 hold")
        cls.store = MailCaptureStore(state, clock=tm.Clock())
        api, _t = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        scope = CaptureScope([], all_mailboxes=True, allowlist=tm.SYNTHETIC,
                             internal_domains=["example-plx.com"])
        run = capture_mail.MailCapture(cls.store, GraphMailReader(api, tm.capture_audience()),
                                       tm.CAPTURE, sleep=lambda s: None).run(
            scope, directory=lambda: capture_mail.directory_mailboxes(
                api, scope.directory_allows))
        assert run["complete"] and len(run["mailboxes"]) == 2, run
        compiled = schema_loader.load_schema()
        path = Path(cls.tmp.name) / "ontology.sqlite"
        now = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        with MailCaptures(state, internal_domains=scope.internal_domains) as mail:
            build_store(path, compiled, {"portal_api": portal, "graph_mail": mail},
                        clock=lambda: now,
                        post_project={"mentions": mentions.store_mention_links})
        policy = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"staff:orders", "staff:finance"},
            "mail:*": {"staff:orders", "staff:finance"}},
            markings={m: {"staff:orders", "staff:finance"} for m in store_ddl.MARKINGS})
        cls.reader = ot.StoreReader.open(path)
        base = ot.OntologyTools(cls.reader, policy,
                                item_gate=cls.store.item_gate(tm.reader_audience()),
                                schema=ot.SchemaView.from_compiled(compiled), grammar=None,
                                contracts=CONTRACTS, clock=lambda: now)
        cls.base, cls.traverse = base, tt.TraverseTools(base)
        cls.evidence = ev.EvidenceTools(base, reader=None)
        cls.aggregate = oa.AggregateTools(base)
        conn = sqlite3.connect(path)
        cls.emails = dict(conn.execute(
            "SELECT json_extract(native_key, '$.id'), object_id FROM objects "
            "WHERE object_type = 'Email'").fetchall())
        cls.po = conn.execute("SELECT object_id FROM objects WHERE object_type = "
                              "'PurchaseOrder'").fetchone()[0]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.reader.conn.close()
        cls.store.close()
        cls.tmp.cleanup()

    def calls(self, actor):
        emails = self.base.call("plx.find", {"query": "PO100234", "object_type": "Email"},
                                actor)
        return {
            "find": self.base.call("plx.find", {"query": "PO100234"}, actor),
            "find_email": emails,
            "around": self.traverse.call("plx.around", {
                "from": self.po, "path": [{"link_type": "Email.mentions_purchase_order",
                                           "direction": "reverse"}],
                "tier_floor": "lead"}, actor),
            "documents": self.evidence.call("plx.documents", {"query": "PO100234"}, actor),
            "documents_po": self.evidence.call("plx.documents", {"object_id": self.po},
                                               actor),
            "aggregate": self.aggregate.call("plx.aggregate", {
                "set": emails["set"], "measure": {"op": "count"}}, actor)}

    def test_a_reader_without_mailbox_b_sees_no_b_item_or_count(self):
        self.assertEqual(set(self.emails), {"MSG-A", "MSG-B"})   # both captured
        seen = self.calls(tm.READER_A)
        for name, out in seen.items():
            text = flat(out)
            self.assertNotIn("error", out, name + text[:800])
            for marker in (self.emails["MSG-B"], "MSG-B", tm.BOX_B, tm.UPN_B, "Finance"):
                self.assertNotIn(marker, text, name)
        self.assertIn(self.emails["MSG-A"], flat(seen["around"]))
        self.assertIn(self.emails["MSG-A"], flat(seen["documents"]))
        self.assertEqual(seen["aggregate"]["groups"][0]["value"], "1")   # B never counts
        # The reader granted mailbox B sees it through the same calls.
        finance = Actor("plx", frozenset({"staff:finance"}), "restricted")
        self.assertIn(self.emails["MSG-B"], flat(self.calls(finance)["documents"]))
        both = Actor("plx", frozenset({"staff:finance", "staff:orders"}), "restricted")
        self.assertEqual(self.calls(both)["aggregate"]["groups"][0]["value"], "2")


class SharePointFeedTests(BreadthCase):
    def roots(self, run):
        return [f for f in run["folders"] if f["source"] == "sharepoint"]

    def test_runs_after_a_full_walk_follow_the_drive_change_feed(self):
        first = self.cycle(sharepoint_scope=sp_scope())
        self.assertEqual([(r["round"], r["state"], r["cursor"]) for r in self.roots(first)],
                         [("full", "complete", "drive_delta")])
        mark = len(self.drive.calls)
        self.drive.file("01FILEN", "new packet.pdf", "01AOS", b"%PDF-1.7 new")
        self.drive.change("01FILEB", b"release two")
        second = self.cycle(sharepoint_scope=sp_scope())
        self.assertEqual([(r["round"], r["state"], r["cursor"]) for r in self.roots(second)],
                         [("incremental", "complete", "drive_delta")])
        calls = [unquote(u) for _m, u in self.drive.calls[mark:]]
        self.assertFalse([u for u in calls if u.endswith("/children") or "/children?" in u])
        self.assertEqual(len([u for u in calls if "/root/delta" in u]), 1)
        self.assertNotIn("token=latest", next(u for u in calls if "/root/delta" in u))
        counts = second["sharepoint"]["targets"][0]["roots"][0]["counts"]
        self.assertEqual((counts["feed_changes"], counts["versions_new"],
                          counts["versions_superseded"]), (2, 2, 1))
        self.assertFalse([u for u in calls if "01FILEA" in u])   # unchanged: not read
        store = SharePointCaptureStore(self.state)
        try:
            found = store.resolve(ts.version_handle(ts.DRIVE, "01FILEN", '"{01FILEN},1"'),
                                  CAPTURE, audience())
            self.assertEqual(found["row"]["path"], ts.QUALITY_FOLDER + "/new packet.pdf")
        finally:
            store.close()
        self.assertTrue(all(m == "GET" for m, _u in self.drive.calls))

    def test_a_change_the_feed_cannot_settle_turns_the_run_into_a_full_walk(self):
        self.cycle(sharepoint_scope=sp_scope())
        self.drive.delete("01FILEA")
        run = self.cycle(sharepoint_scope=sp_scope())
        root = run["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((root["round"], root["full_walk_reason"], root["complete"]),
                         ("full", "structural_change", True))
        self.assertEqual(root["counts"]["absent_after_resync"], 1)
        # A walked folder renamed: also a full walk, which re-keys its files.
        self.drive.items[f"{ts.DRIVE}:01LOTS"]["name"] = "Lots 2026"
        self.drive._logged(ts.DRIVE, "01LOTS")
        root = self.cycle(sharepoint_scope=sp_scope())["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((root["round"], root["full_walk_reason"]), ("full", "structural_change"))
        # A populated folder moved in: the feed lists only the folder, so a walk must
        # capture what it holds before the root can be complete.
        self.drive.folder("01ELSE", "Elsewhere")
        self.drive.file("01FILEX", "moved in.pdf", "01ELSE", b"%PDF-1.7 moved")
        self.cycle(sharepoint_scope=sp_scope())
        self.drive.move("01ELSE", "01AOS")
        root = self.cycle(sharepoint_scope=sp_scope())["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((root["round"], root["full_walk_reason"], root["complete"]),
                         ("full", "structural_change", True))
        store = SharePointCaptureStore(self.state)
        try:
            self.assertEqual(store.current(ts.DRIVE, "01FILEX")["path"],
                             ts.QUALITY_FOLDER + "/Elsewhere/moved in.pdf")
        finally:
            store.close()

    def test_a_feed_link_outside_the_collection_is_a_full_walk_with_its_reason(self):
        self.drive.function_links = True     # delta(token='...'): not the request's path
        first = self.cycle(sharepoint_scope=sp_scope())
        second = self.cycle(sharepoint_scope=sp_scope())
        root = second["sharepoint"]["targets"][0]["roots"][0]
        self.assertTrue(first["complete"])
        self.assertEqual((root["round"], root["full_walk_reason"], root["complete"]),
                         ("full", "feed_unavailable", True))

    def test_a_cut_walk_resumes_and_the_feed_still_carries_what_changed_meanwhile(self):
        # A complete root the budget reaches before its drive's
        # feed is read is deferred: still complete and following the feed (its walked
        # folders and the drive's cursor kept); a walk cut midway resumes from its
        # position, and the feed after it starts where the walk did, so a file that
        # changed in a folder walked before the cut is still captured.
        self.cycle(sharepoint_scope=sp_scope())
        root = self.cycle(sharepoint_scope=sp_scope(), budget_seconds=0
                          )["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((root["round"], root["deferred"], root["complete"], root["cursor"]),
                         (None, True, True, "drive_delta"))
        self.drive.folder("01BIG", "Big", parent="01AOS")
        for n in range(4):
            self.drive.file(f"01BIG{n}", f"b{n}.pdf", "01BIG", b"%%PDF-1.7 big %d" % n)
        cut = self.cycle(sharepoint_scope=sp_scope(), budget_seconds=2,
                         monotonic=self.drive.downloads)
        self.assertEqual([(r["round"], r["state"], r["cursor"]) for r in self.roots(cut)],
                         [("full", "in_progress", "walk_position")])
        self.drive.change("01FILEA", b"%PDF-1.7 revised")   # its folder is walked already
        done = self.cycle(sharepoint_scope=sp_scope())["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((done["full_walk_reason"], done["complete"],
                          done["counts"]["versions_new"]), ("resumed", True, 2))
        after = self.cycle(sharepoint_scope=sp_scope())
        self.assertEqual([(r["round"], r["state"]) for r in self.roots(after)],
                         [("incremental", "complete")])
        store = SharePointCaptureStore(self.state)
        try:
            self.assertEqual(store.current(ts.DRIVE, "01FILEA")["etag"], '"{01FILEA},2"')
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM items WHERE "
                                              "superseded_at IS NULL").fetchone()[0], 6)
        finally:
            store.close()

    def test_a_scope_larger_than_one_budget_completes_more_every_run_until_all(self):
        # Live 2026-10-11 (capture.sharepoint_estate): 25 of 102 roots complete, 75 cut
        # by the time budget. Every run took the roots in the scope's order, so the
        # leading ones spent each budget again, and a complete root the budget cut forgot
        # its walked folders and was walked whole again. Each download costs one second
        # and a run has three: one large root first (k = 3 budgets), then N=1/10/60 roots
        # of one file. The large root completes within k + 1 runs, complete roots grow at
        # least every k + 1 runs and never fall, all complete within the downloads' worth
        # of runs, every file read and captured once, and then each complete root reads
        # only its drive's change feed.
        for small in (1, 10, 60):
            with self.subTest(small=small):
                self.state = Path(self.tmp.name) / f"state-{small}"
                self.drive = DeltaDrive()
                self.drive.folder("01BIG", "Big")
                for n in range(9):
                    self.drive.file(f"01BIG{n}", f"b{n}.txt", "01BIG", b"big %d" % n,
                                    mime="text/plain")
                roots = ["/Big"]
                for n in range(small):
                    self.drive.folder(f"01R{n:02d}", f"R{n:02d}")
                    self.drive.file(f"01R{n:02d}F", "one.txt", f"01R{n:02d}", b"one %d" % n,
                                    mime="text/plain")
                    roots.append(f"/R{n:02d}")
                scope = capture_sharepoint.CaptureScope(
                    [{"id": f"t{k}", "drive_id": ts.DRIVE, "folders": roots[k:k + 30]}
                     for k in range(0, len(roots), 30)])
                bound, runs, stale, big = [], 0, 0, None
                while not bound or bound[-1] < len(roots):
                    runs += 1
                    self.assertLessEqual(runs, -(-(9 + small) // 3) + 2, bound)
                    spent = self.drive.downloads()
                    run = self.cycle(sharepoint_scope=scope, budget_seconds=3,
                                     monotonic=lambda: self.drive.downloads() - spent)
                    bound.append(run["complete_bound"])
                    if big is None and self.roots(run)[0]["complete"]:
                        big = runs
                    stale += sum(1 for f in run["folders"] if f["complete"] and f["deferred"])
                    self.assertEqual(run["complete"], all(f["complete"] and not f["deferred"]
                                                          for f in run["folders"]))
                    if runs == 1:   # a root never reached is in the store's count, cut
                        store = SharePointCaptureStore(self.state)
                        try:
                            self.assertEqual(store.db.execute(
                                "SELECT COUNT(*), SUM(reason='time_budget') FROM roots"
                            ).fetchone()[:], (len(roots), len(roots) - bound[0]))
                        finally:
                            store.close()
                self.assertEqual(sorted(bound), bound)
                self.assertLessEqual(big, 4, bound)
                grown = [0] + bound
                self.assertEqual([i for i in range(len(grown) - 4)
                                  if grown[i + 4] <= grown[i]], [], bound)
                if small > 1:
                    self.assertGreater(stale, 0)
                self.assertEqual(self.drive.downloads(), 9 + small)   # each file read once
                store = SharePointCaptureStore(self.state)
                try:
                    self.assertEqual(store.db.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT item_id) FROM items").fetchone()[:],
                        (9 + small, 9 + small))
                finally:
                    store.close()
                self.drive.change("01R00F", b"one changed")
                mark = len(self.drive.calls)
                run = self.cycle(sharepoint_scope=scope)
                self.assertTrue(run["complete"])
                self.assertEqual({f["round"] for f in self.roots(run)}, {"incremental"})
                calls = [unquote(u) for _m, u in self.drive.calls[mark:]]
                self.assertFalse([u for u in calls if u.endswith("/children")
                                  or "/children?" in u])
                self.assertEqual(len([u for u in calls if "/root/delta" in u]), 1)
                self.assertEqual(self.drive.downloads(), 9 + small + 1)

    def test_a_complete_root_deferred_after_its_feed_was_read_misses_no_change(self):
        # A complete root the budget defers has not applied the change feed its run
        # read: its drive keeps the cursor that root has not followed, so its next run
        # still captures the change. Each download costs one second of a three-second run.
        self.drive.folder("01BIG", "Big")
        for n in range(9):
            self.drive.file(f"01BIG{n}", f"b{n}.txt", "01BIG", b"big %d" % n,
                            mime="text/plain")
        scope = sp_scope(folders=[ts.QUALITY_FOLDER, "/Big"])

        def cycle(budget=None):
            spent = self.drive.downloads()
            return self.cycle(sharepoint_scope=scope, budget_seconds=budget,
                              monotonic=lambda: self.drive.downloads() - spent)

        first = cycle(3)
        self.assertEqual([(f["complete"], f["deferred"]) for f in self.roots(first)],
                         [(True, False), (False, False)])
        self.drive.change("01FILEA", b"%PDF-1.7 revised")
        second = cycle(3)   # the feed is read, then the Big walk spends the budget
        self.assertEqual([(f["complete"], f["deferred"]) for f in self.roots(second)],
                         [(True, True), (False, False)])
        third = cycle()
        self.assertTrue(third["complete"])
        self.assertEqual([f["round"] for f in self.roots(third)], ["incremental", "full"])
        store = SharePointCaptureStore(self.state)
        try:
            self.assertEqual(store.current(ts.DRIVE, "01FILEA")["etag"], '"{01FILEA},2"')
        finally:
            store.close()

    def roots_of(self, count: int, files: int) -> list[str]:
        """``count`` roots of ``files`` text files each on one drive."""
        roots = []
        for n in range(count):
            self.drive.folder(f"01R{n:02d}", f"R{n:02d}")
            for k in range(files):
                self.drive.file(f"01R{n:02d}F{k}", f"f{k}.txt", f"01R{n:02d}",
                                b"x %d %d" % (n, k), mime="text/plain")
            roots.append(f"/R{n:02d}")
        return roots

    def test_roots_of_one_drive_under_churn_converge_once_it_stops(self):
        # One change-feed cursor per drive, held back
        # (or dropped) while a complete root was deferred, never converged under churn:
        # the replay since it outgrew one budget (decision
        # 2026-10-12-capture-cursor-convergence); the base code re-walked every root a
        # budget cut. Four roots of twelve files on one drive, each Graph call one second
        # of a 10-second run (B), three files change after each of 40 budgeted runs, then
        # quiet budgeted runs: within ceil(W/B) + 2 of them (W: the calls of one full
        # capture) every root is complete and follows only its feed, and every stored
        # eTag is the source's.
        roots = self.roots_of(4, 12)
        scope = sp_scope(folders=roots)
        state, self.state = self.state, Path(self.tmp.name) / "measure"
        mark = len(self.drive.calls)
        self.cycle(sharepoint_scope=scope)
        work, self.state = len(self.drive.calls) - mark, state
        files = [f"01R{n:02d}F{k}" for n in range(4) for k in range(12)]
        for number in range(40):
            mark = len(self.drive.calls)
            self.cycle(sharepoint_scope=scope, budget_seconds=10,
                       monotonic=lambda: len(self.drive.calls) - mark)
            for j in range(3):
                self.drive.change(files[(number * 7 + j * 17) % len(files)],
                                  b"v%d %d" % (number, j))
        for quiet in range(1, -(-work // 10) + 3):
            mark = len(self.drive.calls)
            run = self.cycle(sharepoint_scope=scope, budget_seconds=10,
                             monotonic=lambda: len(self.drive.calls) - mark)
            if run["complete"] and {f["round"] for f in self.roots(run)} == {"incremental"}:
                break
        self.assertEqual((run["complete"], {f["round"] for f in self.roots(run)}),
                         (True, {"incremental"}), (quiet, run["complete_bound"]))
        store = SharePointCaptureStore(self.state)
        try:
            self.assertEqual([i for i in files if store.current(ts.DRIVE, i)["etag"]
                              != self.drive.items[f"{ts.DRIVE}:{i}"]["etag"]], [])
        finally:
            store.close()

    def test_a_deferred_root_catches_up_and_a_root_ahead_reads_nothing_twice(self):
        # Decision 2026-10-12-capture-cursor-convergence (2, 3): root B follows past a
        # change in root A's folder while the budget defers A (one download per run).
        # A keeps its position, so a later run captures A's change from the one feed read
        # (from A's, the oldest position); B passes over the entries it applied already
        # (their eTags are captured): no read of its item again.
        roots = self.roots_of(2, 2)
        scope = sp_scope(folders=[roots[1], roots[0]])   # B first, A second
        self.cycle(sharepoint_scope=scope)
        self.drive.change("01R00F0", b"a changed")
        self.drive.change("01R01F0", b"b changed")
        spent = self.drive.downloads()
        second = self.cycle(sharepoint_scope=scope, budget_seconds=1,
                            monotonic=lambda: self.drive.downloads() - spent)
        self.assertEqual([(f["round"], f["complete"], f["deferred"])
                          for f in self.roots(second)],
                         [("incremental", True, False), (None, True, True)])
        mark = len(self.drive.calls)
        third = self.cycle(sharepoint_scope=scope)
        self.assertEqual([(f["round"], f["complete"]) for f in self.roots(third)],
                         [("incremental", True), ("incremental", True)])
        calls = [unquote(u) for _m, u in self.drive.calls[mark:]]
        self.assertEqual([u for u in calls if "01R01F0" in u], [])
        self.assertEqual(len([u for u in calls if "/root/delta" in u]), 1)
        store = SharePointCaptureStore(self.state)
        try:
            self.assertEqual([store.current(ts.DRIVE, i)["etag"] for i in ("01R00F0",
                                                                           "01R01F0")],
                             ['"{01R00F0},2"', '"{01R01F0},2"'])
        finally:
            store.close()

    def test_a_legacy_drive_cursor_seeds_every_walked_root(self):
        # Decision 2026-10-12-capture-cursor-convergence (5): a store written with one
        # cursor per drive gives it to every walked root of the drive as its position
        # (read time unknown: the oldest), then drops it; the next run follows the feed.
        roots = self.roots_of(2, 1)
        scope = sp_scope(folders=roots)
        self.cycle(sharepoint_scope=scope)
        store = SharePointCaptureStore(self.state)
        try:
            delta = store.position(ts.DRIVE, roots[0])["delta_url"]
            with store.db:
                store.db.execute("DELETE FROM root_cursors")
                store.db.execute("INSERT INTO drive_cursors VALUES (?,?,?)",
                                 (ts.DRIVE, delta, "2026-01-01T00:00:00.000000Z"))
        finally:
            store.close()
        self.drive.change("01R01F0", b"changed")
        run = self.cycle(sharepoint_scope=scope)
        self.assertEqual([(f["round"], f["complete"]) for f in self.roots(run)],
                         [("incremental", True), ("incremental", True)])
        store = SharePointCaptureStore(self.state)
        try:
            self.assertEqual(store.current(ts.DRIVE, "01R01F0")["etag"], '"{01R01F0},2"')
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM drive_cursors"
                                              ).fetchone()[0], 0)
            self.assertTrue(all(store.position(ts.DRIVE, r) for r in roots))
        finally:
            store.close()

    def big_folder(self) -> None:
        self.drive.folder("01BIG", "Big", parent="01AOS")
        for n in range(4):
            self.drive.file(f"01BIG{n}", f"b{n}.pdf", "01BIG", b"%%PDF-1.7 big %d" % n)

    def test_a_walk_whose_first_run_had_no_feed_cursor_takes_one_as_it_starts(self):
        # The drive's feed read failed in the walk's first
        # run, so the walk had no cursor; the run that ended it stored none and every
        # root of the drive walked again. The walk takes one as it starts.
        self.big_folder()
        self.drive.delta_failures = 2      # the run's feed read and its fallback
        cut = self.cycle(sharepoint_scope=sp_scope(), budget_seconds=2,
                         monotonic=self.drive.downloads)
        self.assertEqual([(r["state"], r["cursor"]) for r in self.roots(cut)],
                         [("in_progress", "walk_position")])
        done = self.cycle(sharepoint_scope=sp_scope())["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((done["full_walk_reason"], done["complete"]), ("resumed", True))
        after = self.cycle(sharepoint_scope=sp_scope())
        self.assertEqual([(r["round"], r["state"]) for r in self.roots(after)],
                         [("incremental", "complete")])

    def test_a_walk_longer_than_full_walk_hours_is_due_again_after_it_ended(self):
        # A walk whose runs spanned more than full_walk_hours
        # was due another full walk as soon as it ended (timed from its first run).
        scope = capture_sharepoint.CaptureScope(
            [{"id": "quality", "drive_id": ts.DRIVE, "folders": [ts.QUALITY_FOLDER]}],
            full_walk_hours=1)
        self.big_folder()
        self.cycle(sharepoint_scope=scope, budget_seconds=2, monotonic=self.drive.downloads)
        store = SharePointCaptureStore(self.state)
        with store.db:   # its first run started long ago
            store.db.execute("UPDATE walk_positions SET started_at='2026-01-01T00:00:00.000000Z'")
        store.close()
        done = self.cycle(sharepoint_scope=scope)["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((done["full_walk_reason"], done["complete"]), ("resumed", True))
        after = self.cycle(sharepoint_scope=scope)
        self.assertEqual([(r["round"], r["state"]) for r in self.roots(after)],
                         [("incremental", "complete")])

    def test_the_full_walk_repeats_after_full_walk_hours(self):
        scope = capture_sharepoint.CaptureScope(
            [{"id": "quality", "drive_id": ts.DRIVE, "folders": [ts.QUALITY_FOLDER]}],
            full_walk_hours=1)
        self.cycle(sharepoint_scope=scope)
        store = SharePointCaptureStore(self.state)
        with store.db:
            store.db.execute("UPDATE root_walks SET walked_at='2026-01-01T00:00:00.000000Z'")
        store.close()
        root = self.cycle(sharepoint_scope=scope)["sharepoint"]["targets"][0]["roots"][0]
        self.assertEqual((root["round"], root["full_walk_reason"]), ("full", "walk_due"))


def synthetic_sources(archive_bytes: bytes) -> dict:
    """A plx-eval-exposed-sources/v1 document shaped like evals/exposed/sources.json over
    the fakes' synthetic originals (the real pins name real originals)."""
    member = b"member bytes of the archive"
    return {"schema": capture_breadth.SOURCES_SCHEMA,
            "drive_items": [
                {"source_id": "doc", "drive_id": ts.DRIVE, "item_id": "01FILEB",
                 "e_tag": '"{01FILEB},1"', "sha256": sha256(b"release one").hexdigest(),
                 "sha256_basis": "bytes"},
                {"source_id": "zip", "drive_id": ts.DRIVE, "item_id": "01ZIP",
                 "e_tag": '"{01ZIP},1"', "sha256": sha256(archive_bytes).hexdigest(),
                 "sha256_basis": "bytes"},
                {"source_id": "elsewhere", "drive_id": ts.DRIVE, "item_id": "01NONE",
                 "e_tag": '"{01NONE},1"', "sha256": None}],
            "archive_members": [{"source_id": "zip-member", "archive": "zip",
                                 "member": "folder/inner.txt",
                                 "sha256": sha256(member).hexdigest()}],
            "mail_messages": [
                {"source_id": "msg", "message_id": "AAMkREST-ID-NOT-IMMUTABLE=",
                 "id_type": "rest", "received_at": "2026-09-20T09:00:00Z"},
                {"source_id": "twins", "message_id": "AAMkOTHER=", "id_type": "rest",
                 "received_at": "2026-09-21T10:00:00Z"}],
            "mail_attachments": [
                {"source_id": "att", "message": "msg", "name": "confirmation.pdf",
                 "sha256": sha256(tm.ATTACHMENT_BYTES).hexdigest()}]}


class ExposedSourceTests(BreadthCase):
    def test_exposed_sources_resolve_from_the_captures_by_source_id(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("folder/inner.txt", b"member bytes of the archive")
        self.drive.file("01ZIP", "examples.zip", "01AOS", buffer.getvalue(),
                        mime="application/zip")
        self.graph.put_in(BOX_R, "AAMkIMMUTABLE-1=", "CK-1", received="2026-09-20T09:00:00Z",
                          attachments=[tm.file_attachment("ATT-1")])
        for n in (1, 2):
            self.graph.put_in(BOX_S, f"AAMkTWIN-{n}=", "CK-1", received="2026-09-21T10:00:00Z")
        self.cycle(CaptureScope([{"address": UPN_R}, {"address": UPN_S}]), sp_scope())
        path = Path(self.tmp.name) / "sources.json"
        path.write_text(json.dumps(synthetic_sources(buffer.getvalue())))
        entries = capture_breadth.load_sources(path)
        ids = ["doc", "zip-member", "msg", "att", "twins", "elsewhere", "missing"]
        mail, drives = MailCaptureStore(self.state), SharePointCaptureStore(self.state)
        try:
            found = capture_breadth.locate_sources(entries, ids, mail_store=mail,
                                                   sharepoint_store=drives, actor=CAPTURE,
                                                   audience=audience())
            hidden = capture_breadth.locate_sources(entries, ids, mail_store=mail,
                                                    sharepoint_store=drives, actor=WITHHELD,
                                                    audience=audience())
        finally:
            mail.close()
            drives.close()
        status = {i["source_id"]: i["status"] for i in found["items"]}
        self.assertEqual(status, {"doc": "resolved", "zip-member": "resolved",
                                  "msg": "resolved", "att": "resolved", "twins": "ambiguous",
                                  "elsewhere": "not_captured",
                                  "missing": "unknown_source_id"})
        handles = {i["source_id"]: i.get("l1_handle") for i in found["items"]}
        self.assertTrue(handles["msg"].startswith(f"graph-mail://{BOX_R}/AAMkIMMUTABLE-1"))
        self.assertIn("#attachment=ATT-1&sha256=", handles["att"])
        self.assertEqual(found["resolved"], 4)
        self.assertEqual(hidden["resolved"], 0)       # per-reader trimming

    def test_every_source_the_f7_to_f9_exposed_items_cite_is_locatable(self):
        items = sorted(str(p) for f in ("F7", "F8", "F9")
                       for p in EXPOSED.glob(f"dev-{f}-*-exposed.json"))
        self.assertGreaterEqual(len(items), 9)
        entries = capture_breadth.load_sources(EXPOSED / "sources.json")
        ids = capture_breadth.cited_source_ids(items)
        self.assertTrue(ids)
        self.assertEqual([i for i in ids if i not in entries], [])
        self.cycle(CaptureScope([{"address": UPN_R}]), sp_scope())
        mail, drives = MailCaptureStore(self.state), SharePointCaptureStore(self.state)
        try:
            found = capture_breadth.locate_sources(entries, ids, mail_store=mail,
                                                   sharepoint_store=drives, actor=CAPTURE,
                                                   audience=audience())
        finally:
            mail.close()
            drives.close()
        # Every cited kind has a locator; with no matching capture each reads absent.
        self.assertEqual({i["kind"] for i in found["items"]},
                         {"drive_items", "archive_members", "mail_messages",
                          "mail_attachments"})
        self.assertFalse([i for i in found["items"]
                          if i["status"] in ("resolved", "unknown_source_id")])


class CliTests(BreadthCase):
    def inputs(self):
        """The CLI's scope and audience files (one mailbox, the Quality root) and a sender
        routing SharePoint requests to the drive fake, mail to the mail fake."""
        tmp = Path(self.tmp.name)
        (tmp / "mail.json").write_text(json.dumps({
            "schema": capture_mail.SCOPE_SCHEMA,
            "mailboxes": [{"address": UPN_R, "folders": ["inbox", "sentitems"]}]}))
        (tmp / "sp.json").write_text(json.dumps({
            "schema": capture_sharepoint.SCOPE_SCHEMA,
            "targets": [{"id": "quality", "drive_id": ts.DRIVE,
                         "folders": [ts.QUALITY_FOLDER]}]}))
        (tmp / "audience.json").write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": {f"mail:{UPN_R}": [PRINCIPAL], "sharepoint:*": [PRINCIPAL]},
            "classifications": {"mail:*": "restricted"}}))

        def sender(method, url, headers, body, timeout, max_bytes):
            parts = urlsplit(url)
            if parts.hostname == ts.SP_HOST or parts.path.startswith("/v1.0/drives/") \
                    or parts.path.startswith("/v1.0/sites/"):
                return self.drive(method, url, headers, body, timeout, max_bytes)
            return self.graph(method, url, headers, body, timeout, max_bytes)

        return tmp, sender

    def test_capture_breadth_cli_reports_counts_only(self):
        self.graph.put_in(BOX_R, "R-1", "CK-1", attachments=[tm.file_attachment("ATT-1")])
        self.graph.put_in(BOX_R, "R-SENT", "CK-1", folder="sentitems")
        tmp, sender = self.inputs()
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = graph_read.main(
                ["capture-breadth", "--state-dir", str(tmp / "state"), "--mail-scope",
                 str(tmp / "mail.json"), "--sharepoint-scope", str(tmp / "sp.json"),
                 "--live-audience", str(tmp / "audience.json"), "--page-size", "2",
                 "--budget-seconds", "600",
                 "--sources", str(EXPOSED / "sources.json"),
                 "--eval-item", str(EXPOSED / "dev-F8-0001-exposed.json")],
                environ=ENV, sender=sender)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report)
        self.assertEqual((report["check"], report["evidence_label"]),
                         ("capture.breadth", "injected-transport"))
        summary = report["summary"]
        self.assertEqual((summary["complete_bound"], summary["bound_total"],
                          summary["folders_budget_cut"]), (3, 3, 0))
        self.assertEqual((summary["rerun_initial_rounds"], summary["rerun_incremental_roots"],
                          summary["sentitems_folders_read"]), (0, 1, 1))
        self.assertEqual(report["exposed_sources"]["total"], 2)
        self.assertFalse(any(report["leak_check"].values()))
        text = out.getvalue()
        for secret in (UPN_R, BOX_R, "R-1", "ATT-1", ts.DRIVE, "01FILEA", tm.SUBJECT_SECRET,
                       "do-not-leak", "release.txt"):
            self.assertNotIn(secret, text)


    def test_a_round_a_budget_cut_in_cycle_one_reads_resumed_in_cycle_two(self):
        # A mail folder the budget cut in cycle 1 (mid-round, or
        # before its first round) goes on in cycle 2; that is a resumed partial unit, not
        # a rerun of its initial round (which failed the check's ok).
        for n in range(1, 4):
            self.graph.put_in(BOX_R, f"R-{n}", "CK-1")
        self.graph.put_in(BOX_R, "R-SENT", "CK-1", folder="sentitems")
        tmp, sender = self.inputs()
        parser = argparse.ArgumentParser()
        capture_breadth.add_cli_arguments(parser)
        args = parser.parse_args([
            "--state-dir", str(tmp / "state"), "--mail-scope", str(tmp / "mail.json"),
            "--sharepoint-scope", str(tmp / "sp.json"), "--live-audience",
            str(tmp / "audience.json"), "--page-size", "2", "--runs", "2",
            "--budget-seconds", "4"])
        report = capture_breadth.run_cli(args, environ=ENV, sender=sender,
                                         sleep=lambda s: None,
                                         monotonic=lambda: len(self.graph.delta_calls()))
        first = {(f["source"], f.get("folder")): (f["reason"], f["cursor"])
                 for f in report["runs"][0]["folders"]}
        self.assertEqual(first, {("mail", "inbox"): ("time_budget", "next_url"),
                                 ("mail", "sentitems"): ("time_budget", None),
                                 ("sharepoint", None): (None, "drive_delta")})
        summary = report["summary"]
        self.assertEqual((summary["rerun_initial_rounds"], summary["rounds_resumed"],
                          summary["complete_bound"], summary["bound_total"]), (0, 2, 3, 3))
        self.assertTrue(report["ok"], summary)

    def test_a_round_the_page_budget_stopped_reads_resumed_in_cycle_two(self):
        # Decided: a mail round that goes on from the page
        # cursor the page budget left (the live check's 20 pages per folder) is resumed,
        # a partial unit, never a rerun failing the check's ok.
        for n in range(1, 6):
            self.graph.put_in(BOX_R, f"R-{n}", "CK-1")
        tmp, sender = self.inputs()
        parser = argparse.ArgumentParser()
        capture_breadth.add_cli_arguments(parser)
        args = parser.parse_args([
            "--state-dir", str(tmp / "state"), "--mail-scope", str(tmp / "mail.json"),
            "--sharepoint-scope", str(tmp / "sp.json"), "--live-audience",
            str(tmp / "audience.json"), "--page-size", "2", "--runs", "2",
            "--mail-page-budget", "1"])
        report = capture_breadth.run_cli(args, environ=ENV, sender=sender,
                                         sleep=lambda s: None)
        inbox = [(f["reason"], f["cursor"]) for run in report["runs"] for f in run["folders"]
                 if f.get("folder") == "inbox"]
        self.assertEqual(inbox, [("page_budget", "next_url")] * 2)
        summary = report["summary"]
        self.assertEqual((summary["rerun_initial_rounds"], summary["rounds_resumed"]), (0, 1))
        self.assertTrue(report["ok"], summary)


if __name__ == "__main__":
    unittest.main()
