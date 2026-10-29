"""SharePoint documents answered through the case host (operating/case_tools_host.py).

The real user path: an MCP ``tools/call`` on ``MCPServer`` over ``CaptureCaseHostTools``
with a versioned SharePoint capture (integrations/capture_sharepoint.py, filled over the
capture tests' in-process FakeDrive) projected under the curated schema with mention
links. A scanned PDF (no text layer; generated here and OCR-read by the local poppler +
tesseract) names a purchase order on its second page only, so its mention comes from
OCR text with a page citation, never from the file name. Every captured file is
labelled ``as_of_capture``; a reader without the file's permission gets nothing; a file
moved or revoked since the projection is not served; a live re-read (injected
transport) applies a revocation at once.

Evidence label: ``injected-transport``. No network, no credentials.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import gc
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote
import warnings

from integrations import capture_sharepoint as cs
from integrations import capture_store
from integrations import capture_teams
from integrations import document_understanding, graph_read
from integrations.capture_sharepoint import SharePointCapture, SharePointCaptureStore
from integrations.document_understanding import local_ocr_available
from integrations.graph_read import SharePointReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_sharepoint import (CAPTURE, DRIVE, EMPLOYEE, ENV,
                                                        NAME_SECRET, OUTSIDER, QUALITY,
                                                        QUALITY_FOLDER, RICARDO, SALES,
                                                        TEXT_SECRET, Clock,
                                                        capture_audience, reader_audience,
                                                        scanned_pdf, scope, seeded_drive)
from ontology import mentions
from ontology import schema as schema_loader
from ontology.project import SharePointCaptures, build_store
from ontology.tests.test_container_members import zipped
from ontology.tests.test_mail_documents import StaticPortal, po
from ontology.types import Actor
from operating.case_runtime import CaseRuntime, LiveReaders
from operating import ontology_tools_evidence
from operating.case_tools_host import (LIVE_READ_GETS, REREAD_GETS, CaptureCaseHostTools,
                                       CaseCaptures)
from operating.employee_permissions import (EmployeePermissionPolicy,
                                           EmployeePermissionResolver, EntraDirectoryRead,
                                           PORTAL_DEPARTMENT_CODES)
from operating.mcp_server import EVIDENCE_META_KEY, MCPServer
from operating import host as host_module
from operating.tests.test_case_tools_host import _client
from operating.tests.test_host import ENV as ENV_HOST, HostCase
from operating.tests.test_employee_permissions import (OID, POLICY, QA_GROUP, Calls, member,
                                                       portal_user)

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
CAPTURED = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)   # the capture Clock
SOON = CAPTURED + dt.timedelta(hours=1)        # within the host's max capture age
LATER = CAPTURED + dt.timedelta(days=3)        # past it
SCAN = "01SCAN"
SCAN_PO = "PO100235"
PURCHASE_ORDERS = "public.app_purchase_order"


def ocr_skip_reason() -> str | None:
    if not local_ocr_available():
        return "poppler and tesseract are not installed (CI has them)"
    if not shutil.which("pdftoppm"):   # the scanned fixture is rasterized with it (macOS too)
        return "pdftoppm is not installed (needed to generate the scanned fixture)"
    try:
        import reportlab  # noqa: F401
    except BaseException as error:  # noqa: BLE001 - a broken cryptography backend panics
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return (f"reportlab import failed ({type(error).__name__}; needed to generate "
                "the scanned fixture)")
    return None


class SharePointHostCase(unittest.TestCase):
    """A capture of the quality folder (the seeded files plus a scanned PDF), its
    projection with two Portal purchase orders, and MCP servers over them."""

    max_content_bytes = cs.MAX_CONTENT_BYTES
    scanned = True   # False: the seeded files only (no scanned PDF, no OCR needed)
    read_content = True   # False: a metadata-only capture (no bytes read)

    def setUp(self):
        reason = ocr_skip_reason() if self.scanned else None
        if reason:
            self.skipTest(reason)
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "sharepoint"
        self.drive = self.seed()
        if self.scanned:
            self.drive.file(SCAN, "packet-scan.pdf", "01LOTS",
                            scanned_pdf(["Assembly packet scan", f"Purchase order {SCAN_PO}"]),
                            acl=(RICARDO, SALES))
        self.store = SharePointCaptureStore(self.state, clock=Clock())
        self.addCleanup(self.store.close)
        api, self.transport = graph_read.build_graph(ENV, sender=self.drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        self.api = api
        self.capture = SharePointCapture(self.store, lambda d: SharePointReader(
            api, capture_audience(), d, transport=self.transport, read_permissions=True,
            max_acl_reads=cs.MAX_ACL_READS), CAPTURE, max_content_bytes=self.max_content_bytes,
            read_content=self.read_content)
        run = self.capture.run(self.capture_scope())
        assert run["complete"] or not self.read_content, run
        self.projection = self.root / "ontology.sqlite"
        portal = StaticPortal([
            po(PURCHASE_ORDERS, {"id": "p1"}, {"id": "p1", "po_number": "PO100234"}),
            po(PURCHASE_ORDERS, {"id": "p2"}, {"id": "p2", "po_number": SCAN_PO})])
        with SharePointCaptures(self.state) as files:
            build_store(self.projection, schema_loader.load_schema(),
                        {"portal_api": portal, "sharepoint": files}, clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links})

    def seed(self):
        return seeded_drive()

    def capture_scope(self):
        return scope()

    def handle(self, item_id: str, version: int = 1) -> str:
        return cs.version_handle(DRIVE, item_id, f'"{{{item_id}}},{version}"')

    def mirror_audience(self, portal_granted: bool) -> Path:
        path = self.root / f"mirror-audience-{portal_granted}.json"
        grants = {PURCHASE_ORDERS: ["staff:quality", "staff:sales"]} if portal_granted else {}
        path.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                    "audiences": grants}), encoding="utf-8")
        return path

    def server(self, actor, *, portal_granted=True, live_drive=False, audience=None,
               resolver=None, live_api=None, clock=SOON, projection=True,
               **host) -> MCPServer:
        audience = audience or reader_audience()
        live = LiveReaders(audience=audience)
        if live_drive:
            api, transport = live_api or (self.api, self.transport)
            live.sharepoint = {DRIVE: SharePointReader(api, audience, DRIVE,
                                                       transport=transport,
                                                       read_permissions=True)}
            live.graph_transport = transport
        runtime = CaseRuntime(self.root / "case-state", client=None,
                              audience=self.mirror_audience(portal_granted), tenant="plx",
                              live=live, employee_permissions=resolver)
        self.addCleanup(runtime.close)
        # Case citations of the capture, as operating.mcp_server.SessionHost wires them.
        runtime.captures = CaseCaptures(runtime, sharepoint_capture_dir=self.state,
                                        store_path=self.projection if projection else None,
                                        clock=lambda: clock, **host)
        tools = self.tools = CaptureCaseHostTools(
            runtime, actor, store_path=self.projection if projection else None,
            sharepoint_capture_dir=self.state,
            clock=lambda: clock, **host)
        server = MCPServer(case_runtime=runtime, actor=actor, ontology=tools)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "sharepoint-host-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def ok(self, server: MCPServer, name: str, arguments: dict) -> dict:
        result = _client(server, name, arguments)   # also checks the output contract
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    @staticmethod
    def labels(answer: dict, reason: str = "as_of_capture") -> dict[str, dict]:
        return {gap["subject"]: gap for gap in answer.get("gaps", ())
                if gap["reason"] == reason}


class DocumentQuestionTests(SharePointHostCase):
    def test_documents_answer_with_a_page_cited_ocr_mention_labelled_as_of_capture(self):
        server = self.server(EMPLOYEE)
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        scan = self.handle(SCAN)
        (document,) = [d for d in answer["documents"] if d["citation"] == scan]
        self.assertEqual(document["kind"], "sharepoint_file")
        (mention,) = document["mentions"]
        self.assertEqual((mention["identifier_kind"], mention["normalized"]),
                         ("petra_po", SCAN_PO))
        self.assertTrue(mention["object_id"].startswith("PurchaseOrder:"))
        span = mention["span"]
        # From the OCR text of page 2, cited by page; not from the file name.
        self.assertEqual(span["untrusted_text"], SCAN_PO)
        self.assertEqual(span["origin"], {"citation": scan + "#page=2",
                                          "field_path": "row.content_text"})
        self.assertNotIn(SCAN_PO, document["title"]["untrusted_text"])
        label = self.labels(answer)[scan]
        self.assertTrue(label["detail"].startswith("as of capture 2026-10-01T12:00:"))
        self.assertNotIn("live-verified", json.dumps(answer))
        self.assertIn(scan, self.labels(answer, "ocr_text_unconfirmed"))
        self.assertIn("sharepoint", {f["source"] for f in answer["meta"]["freshness"]})
        self.assertNotIn("mention_span_unreadable", {g["reason"] for g in answer["gaps"]})
        # The cited page reads back through plx.read, from the capture, labelled.
        page = self.ok(server, "plx.read", {"citation": scan + "#page=2"})
        self.assertEqual(page["status"], "resolved")
        self.assertIn(SCAN_PO, page["content"]["untrusted_text"])
        self.assertNotIn("Assembly packet", page["content"]["untrusted_text"])
        self.assertIn(scan, self.labels(page))

    def test_with_portal_unreadable_the_capture_text_still_answers_by_page(self):
        # The reader holds no Portal grant (as when the Portal API is down): no
        # purchase-order object to link to, so the host searches the captured text.
        answer = self.ok(self.server(EMPLOYEE, portal_granted=False), "plx.documents",
                         {"query": SCAN_PO, "sources": ["sharepoint"]})
        (document,) = answer["documents"]
        self.assertEqual(document["citation"], self.handle(SCAN))
        (mention,) = document["mentions"]
        self.assertNotIn("object_id", mention)
        self.assertEqual((mention["tier"], mention["external_assertion"]), ("lead", True))
        self.assertEqual(mention["span"]["origin"]["citation"], self.handle(SCAN) + "#page=2")
        self.assertNotIn("no_match", {g["reason"] for g in answer["gaps"]})
        self.assertIn("capture:sharepoint", answer["searched"]["scopes"])
        self.assertIn(self.handle(SCAN), self.labels(answer))

    def test_the_reused_identifier_lookup_follows_the_capture_and_the_reader(self):
        # One host answers many questions from one lookup over the captured text; a
        # capture committed since is searched by the next question, and another reader
        # never gets what the first one's lookup holds.
        server = self.server(EMPLOYEE, portal_granted=False)
        question = {"query": SCAN_PO, "sources": ["sharepoint"]}
        indexed = mock.patch.object(SharePointCaptureStore, "index", autospec=True,
                                    side_effect=SharePointCaptureStore.index)
        with indexed as index:
            for _ in range(2):
                answer = self.ok(server, "plx.documents", question)
                self.assertEqual([d["citation"] for d in answer["documents"]],
                                 [self.handle(SCAN)])
        self.assertEqual(index.call_count, 1)
        self.drive.file("01NOTE", "note.txt", "01LOTS", b"Purchase order PO100236",
                        mime="text/plain", acl=(RICARDO, SALES))
        self.capture.run(scope())
        answer = self.ok(server, "plx.documents",
                         {"query": "PO100236", "sources": ["sharepoint"]})
        self.assertEqual([d["citation"] for d in answer["documents"]],
                         [self.handle("01NOTE")])
        hidden = self.tools.call("plx.documents", question, OUTSIDER)
        self.assertEqual(hidden["documents"], [])
        self.assertNotIn(self.handle(SCAN), json.dumps(hidden))

    def test_find_returns_the_captured_file_labelled_as_of_capture(self):
        answer = self.ok(self.server(QUALITY), "plx.find",
                         {"query": "packet-scan", "object_type": "SharePointFile"})
        self.assertEqual(answer["status"], "found", json.dumps(answer)[:600])
        self.assertIn(self.handle(SCAN), json.dumps(answer["candidates"]))
        self.assertIn(self.handle(SCAN), self.labels(answer))


class PermissionTests(SharePointHostCase):
    def assert_nothing(self, server: MCPServer, *secrets: str) -> None:
        found = self.ok(server, "plx.find", {"query": "pricing",
                                             "object_type": "SharePointFile"})
        documents = self.ok(server, "plx.documents", {"query": "PO100234",
                                                      "sources": ["sharepoint"]})
        read = _client(server, "plx.read", {"citation": self.handle("01FILEC")})
        self.assertEqual(found["candidates"], [])
        self.assertNotIn(self.handle("01FILEA"), json.dumps(documents))
        # The read echoes the citation it was given and nothing of the file.
        self.assertNotEqual(read["structuredContent"]["status"], "resolved")
        self.assertNotIn("content", read["structuredContent"])
        self.assertNotIn("fields", read["structuredContent"])
        text = json.dumps([found, documents, read["structuredContent"]["gaps"]])
        for secret in (NAME_SECRET, "01FILEC", "Secret supplier", *secrets):
            self.assertNotIn(secret, text)

    def test_a_reader_without_the_files_permission_gets_no_name_path_or_count(self):
        # Sales holds the folder but not the item permissions of the packet and the
        # pricing file; the outsider holds nothing.
        self.assert_nothing(self.server(EMPLOYEE))
        self.assert_nothing(self.server(OUTSIDER), "release.txt", "packet-scan")
        quality = self.ok(self.server(QUALITY), "plx.find", {
            "query": "pricing", "object_type": "SharePointFile"})
        self.assertEqual(quality["status"], "found")   # the same question, a reader allowed

    def test_a_live_shaped_employee_with_the_folder_but_not_the_item_sees_nothing(self):
        # An employee resolved through the employee policy into staff:quality (the
        # folder grant whose source identity is Ricardo's), whose own identity is not
        # on any item: the employee's item ACL withholds every file.
        employee = Actor("plx", frozenset({"employee:" + OID}))
        resolver = EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**POLICY, "principals": {
                "staff:quality": POLICY["principals"]["staff:quality"]}}),
            Calls(portal_user("MANAGER", email="someone.else@example-plx.com")),
            Calls(member(QA_GROUP)))
        server = self.server(employee, resolver=resolver)
        self.assert_nothing(server, "release.txt", "packet-scan")
        scan = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertEqual(scan["documents"], [])
        # The same employee, whose identity is on the scanned file, is served it.
        allowed = self.server(employee, resolver=EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**POLICY, "principals": {
                "staff:quality": POLICY["principals"]["staff:quality"]}}),
            Calls(portal_user("MANAGER", email=SALES)), Calls(member(QA_GROUP))))
        answer = self.ok(allowed, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertEqual([d["citation"] for d in answer["documents"]], [self.handle(SCAN)])

    def test_a_file_moved_out_of_scope_does_not_serve_its_stale_name_or_path(self):
        release = self.handle("01FILEB")
        before = self.ok(self.server(QUALITY), "plx.find",
                         {"query": "release", "object_type": "SharePointFile"})
        self.assertIn(release, json.dumps(before))
        self.drive.move("01FILEB", "01RESTRICTED")    # out of the captured folder
        run = self.capture.run(scope())
        self.assertEqual(run["targets"][0]["roots"][0]["counts"]["moved_out_of_scope"], 1)
        # The projection still holds the old name and path; the host serves neither.
        for actor in (QUALITY, EMPLOYEE):
            server = self.server(actor)
            after = self.ok(server, "plx.find", {"query": "release",
                                                 "object_type": "SharePointFile"})
            self.assertEqual(after["candidates"], [])
            text = json.dumps(after) + json.dumps(_client(server, "plx.read",
                                                          {"citation": release}))
            self.assertNotIn("release.txt", text)
            self.assertNotIn("/Quality/AOs/Lots", text)

    def test_a_file_moved_inside_the_scope_is_not_served_under_its_projected_path(self):
        self.drive.move("01FILEB", "01AOS")           # same eTag, another captured folder
        self.capture.run(scope())
        self.assertEqual(self.store.current(DRIVE, "01FILEB")["path"],
                         "/Quality/AOs/release.txt")
        server = self.server(QUALITY)
        after = self.ok(server, "plx.find",
                        {"query": "release", "object_type": "SharePointFile"})
        self.assertEqual(after["candidates"], [])
        self.assertNotIn("/Quality/AOs/Lots", json.dumps(after))
        # Its read says why: the index is stale, not that the reader lacks access.
        read = self.ok(server, "plx.read", {"citation": self.handle("01FILEB")})
        self.assertEqual(HostBoundTests.refusal(read), ("unresolvable", "capture_stale"))
        self.assertNotIn("content", read)
        self.assertNotIn("/Quality/AOs/Lots", json.dumps(read))
        # A reader the file's permissions exclude still gets the permission refusal.
        denied = self.ok(self.server(OUTSIDER), "plx.read",
                         {"citation": self.handle("01FILEB")})
        self.assertNotIn("capture_stale", json.dumps(denied))
        self.assertNotEqual(denied["status"], "resolved")

    def test_a_live_re_read_applies_a_revocation_at_once_and_labels_the_basis(self):
        scan = self.handle(SCAN)
        server = self.server(EMPLOYEE, live_drive=True)
        # A search is decided on the capture and never re-read live; it says so.
        requests = self.transport.requests_sent
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertIn("a search result is not re-read live", self.labels(answer)[scan]["detail"])
        self.assertEqual(self.transport.requests_sent, requests)
        # plx.read of the cited file re-reads it live.
        read = self.ok(server, "plx.read", {"citation": scan})
        self.assertIn("re-read live now", self.labels(read)[scan]["detail"])
        requests = self.transport.requests_sent
        self.drive.items[f"{DRIVE}:{SCAN}"]["acl"] = [RICARDO]   # sales removed upstream
        revoked = self.ok(server, "plx.read", {"citation": scan})
        self.assertNotEqual(revoked["status"], "resolved")
        self.assertNotIn(SCAN_PO, json.dumps(revoked))
        self.assertGreater(self.transport.requests_sent, requests)  # read again, not cached
        self.assertTrue(all(method == "GET" for method, _url in self.drive.calls))
        # Without a live reader for the drive the capture's recorded permissions decide,
        # and the label says so.
        recorded = self.ok(self.server(EMPLOYEE), "plx.documents",
                           {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertIn("as recorded at that capture", self.labels(recorded)[scan]["detail"])


class RevokedHistoryTests(SharePointHostCase):
    """A revocation captured after an older version (the seeded text file; no OCR)."""

    scanned = False

    def test_an_older_version_is_withheld_once_the_latest_capture_revokes_the_employee(self):
        # The employee holds the folder through staff:quality (Ricardo's identity, on
        # every version) and is on the release file's own ACL by its own identity.
        employee = Actor("plx", frozenset({"employee:" + OID}))
        resolver = EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**POLICY, "principals": {
                "staff:quality": POLICY["principals"]["staff:quality"]}}),
            Calls(portal_user("MANAGER", email=SALES)), Calls(member(QA_GROUP)))
        old = self.handle("01FILEB")
        before = self.ok(self.server(employee, resolver=resolver), "plx.read",
                         {"citation": old})
        self.assertEqual(before["status"], "resolved")
        self.drive.change("01FILEB", b"Lot release, revised.")
        self.drive.items[f"{DRIVE}:01FILEB"]["acl"] = [RICARDO]   # the employee removed
        self.capture.run(scope())
        self.assertEqual(self.store.current(DRIVE, "01FILEB")["l1_handle"],
                         self.handle("01FILEB", 2))
        # No live reader for the drive: the capture decides, and its latest version of
        # the item no longer admits the employee, so neither version is served.
        server = self.server(employee, resolver=resolver)
        for handle in (old, self.handle("01FILEB", 2)):
            read = self.ok(server, "plx.read", {"citation": handle})
            self.assertEqual(HostBoundTests.refusal(read),
                             ("withheld", "sharepoint_item_refused"), handle)
            self.assertNotIn("content", read)
            self.assertNotIn("fields", read)
            self.assertNotIn(TEXT_SECRET, json.dumps(read))
        # The folder holder whose own identity is still on the item reads both.
        self.assertEqual(self.ok(self.server(QUALITY), "plx.read", {"citation": old})["status"],
                         "resolved")

    def test_a_case_finding_citing_a_captured_file_follows_the_capture_gate(self):
        """A finding cites the captured release file (no live reader for the drive): it
        resolves through the gate plx.read reads the capture with, so once the latest
        capture revokes the employee it is withheld from them, and for quality the
        superseded version is no longer support."""
        employee = Actor("plx", frozenset({"employee:" + OID}))
        resolver = EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**POLICY, "principals": {
                "staff:quality": POLICY["principals"]["staff:quality"]}}),
            Calls(portal_user("MANAGER", email=SALES)), Calls(member(QA_GROUP)))
        statement = "The lot release names the order."
        writer = self.server(employee, resolver=resolver)
        case_id = self.ok(writer, "plx.case_open", {"question": "Was the lot released?",
                                                    "audience": ["staff:quality"]})["case_id"]
        finding = self.ok(writer, "plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": statement, "citations": [self.handle("01FILEB")]}
        )["finding"]["finding_id"]

        def findings(actor, **options) -> tuple[dict, str]:
            view = self.ok(self.server(actor, **options), "plx.case_get", {"case_id": case_id})
            return ({bucket: [f["id"] for f in items]
                     for bucket, items in view["findings"].items() if items}, json.dumps(view))

        self.assertEqual(findings(QUALITY)[0], {"supported": [finding]})
        # A pin the capture does not hold is refused, never recorded.
        wrong = _client(writer, "plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported", "statement": statement,
            "citations": [self.handle("01FILEB") + "#sha256=" + "0" * 64]})
        self.assertTrue(wrong["isError"], wrong)
        self.drive.change("01FILEB", b"Lot release, revised.")
        self.drive.items[f"{DRIVE}:01FILEB"]["acl"] = [RICARDO]   # the employee removed
        self.capture.run(scope())
        revoked, text = findings(employee, resolver=resolver)
        self.assertEqual(revoked, {"withheld": [finding]})
        for hidden in (statement, self.handle("01FILEB"), "release.txt"):
            self.assertNotIn(hidden, text)
        self.assertEqual(findings(QUALITY)[0], {"citation_content_changed": [finding]})


class CaseCitationGateTests(SharePointHostCase):
    """A case tool call resolves its captured citations through one gate (directive
    7509edfeab71): one live re-read and GET bound per call however many citations, and
    the projection plx.read reads (``capture_stale``)."""

    scanned = False
    notes = 12

    def seed(self):
        drive = seeded_drive()
        for n in range(self.notes):
            drive.file(f"01NOTE{n:02d}", f"note-{n:02d}.txt", "01LOTS", f"Note {n}".encode(),
                       mime="text/plain", acl=(RICARDO, SALES))
        return drive

    def items_read(self, since: int) -> set[str]:
        return {url.split("/items/")[1].split("/")[0].split("?")[0]
                for _method, url in self.drive.calls[since:] if "/items/" in url}

    def test_a_case_get_with_many_citations_stays_within_one_calls_bound(self):
        server = self.server(QUALITY, live_drive=True, max_live_rereads=2,
                             max_live_gets=2 * REREAD_GETS)
        notes = [self.handle(f"01NOTE{n:02d}") for n in range(self.notes)]
        case_id = self.ok(server, "plx.case_open", {"question": "What do the notes say?",
                                                    "audience": ["staff:quality"]})["case_id"]
        for call in ("plx.case_add_finding", "plx.case_get", "plx.case_get"):
            mark, sent = len(self.drive.calls), self.transport.requests_sent
            if call == "plx.case_add_finding":
                self.ok(server, call, {"case_id": case_id, "kind": "fact",
                                       "status": "supported", "statement": "Twelve notes.",
                                       "citations": notes})
            else:   # every citation resolved, each call within its own one bound
                view = self.ok(server, call, {"case_id": case_id})
                self.assertEqual({bucket: len(items) for bucket, items
                                  in view["findings"].items() if items}, {"supported": 1})
            self.assertEqual(len(self.items_read(mark)), 2, (call, self.items_read(mark)))
            self.assertLessEqual(self.transport.requests_sent - sent, 2 * REREAD_GETS)
        self.assertTrue(all(method == "GET" for method, _url in self.drive.calls))

    def test_resolves_outside_a_call_check_moves_on_one_projection_reader(self):
        # The captured-originals gate resolves file after file outside any case tool call
        # (a gate per resolve). A moved file is refused as plx.read refuses it and an
        # unmoved one is served, each time; no resolve opens the projection again (a tool
        # table per gate read every audience key and BC unit of the store: 1.3 GB and
        # about 2 s per file on the served projection).
        from operating import ontology_tools as ot
        from operating.cases import CitationUnresolvable
        release, note = self.handle("01FILEB"), self.handle("01NOTE00")
        self.drive.move("01FILEB", "01AOS")           # same eTag, another captured folder
        self.capture.run(scope())
        server = self.server(QUALITY)
        self.assertEqual(HostBoundTests.refusal(self.ok(server, "plx.read",
                                                        {"citation": release})),
                         ("unresolvable", "capture_stale"))
        captures, actor = server.case_runtime.captures, server.actor
        opened, open_store = [], ot.StoreReader.open
        with mock.patch.object(ot.StoreReader, "open",
                               side_effect=lambda path: opened.append(path) or open_store(path)):
            for _ in range(3):
                with self.assertRaises(CitationUnresolvable):
                    captures(release, actor)
                self.assertEqual(captures(note, actor)["status"], "current")
            self.assertLessEqual(len(opened), 1, opened)
            # A newer generation published over the path: the host's generation check of
            # the next request closes the older file's reader at once, and the next
            # resolve reads the newer one.
            older = captures.projection()
            published = self.projection.with_name("next.sqlite")
            shutil.copyfile(self.projection, published)
            os.replace(published, self.projection)
            with self.tools.request():
                pass
            with self.assertRaises(sqlite3.ProgrammingError):
                older.conn.execute("SELECT 1")
            seen = len(opened)
            with self.assertRaises(CitationUnresolvable):
                captures(release, actor)
            self.assertEqual(len(opened), seen + 1)
        # Shutdown (the runtime's close) leaves no capture reader's database open.
        current = captures.projection()
        del captures, server, older
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.doCleanups()
            self.tools = None
            gc.collect()
        with self.assertRaises(sqlite3.ProgrammingError):
            current.conn.execute("SELECT 1")
        self.assertEqual([str(w.message) for w in caught
                          if "unclosed database" in str(w.message)], [])

    def test_a_file_moved_since_the_projection_is_refused_for_a_case_as_for_plx_read(self):
        release = self.handle("01FILEB")
        statement = "The lot release names the order."
        server = self.server(QUALITY)
        case_id = self.ok(server, "plx.case_open", {"question": "Was the lot released?",
                                                    "audience": ["staff:quality"]})["case_id"]
        finding = {"case_id": case_id, "kind": "fact", "status": "supported",
                   "statement": statement, "citations": [release]}
        self.ok(server, "plx.case_add_finding", finding)
        self.drive.move("01FILEB", "01AOS")           # same eTag, another captured folder
        self.capture.run(scope())
        server = self.server(QUALITY)
        read = self.ok(server, "plx.read", {"citation": release})
        self.assertEqual(HostBoundTests.refusal(read), ("unresolvable", "capture_stale"))
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual([bucket for bucket, items in view["findings"].items() if items],
                         ["withheld"])
        refused = _client(server, "plx.case_add_finding", finding)
        self.assertTrue(refused["isError"], refused)
        self.assertNotIn("/Quality/AOs", json.dumps([view, refused]))


class HostCaseCaptureTests(HostCase):
    """``host serve``'s console and ``host mcp`` resolve a case citation of the host's
    captures through the gates plx.read reads them with, over the host's projection
    (``capture_stale`` as for plx.read), and report the Portal the host captures as
    captured (``--host-captures-portal``)."""

    def started(self, command: str) -> CaseRuntime:
        """The case runtime ``host <command>`` serves, captured as it starts serving."""
        from operator_ui import case_console
        from operating import mcp_server
        from operating import session_identity as si
        from operating.tests.test_session_identity import SECRET, Fixture
        built = []
        if command == "serve":
            real = case_console.CaseRuntime

            def runtime(*args, **kwargs):
                built.append(real(*args, **kwargs))
                return built[-1]
            patches = (mock.patch.object(case_console, "CaseRuntime", runtime),
                       mock.patch.object(case_console, "serve", lambda **kw: None))
            argv = ["--port", "0", "--interval-seconds", "3600"]
            env = ENV_HOST
        else:
            patches = (mock.patch.object(mcp_server, "serve_stdio",
                                         lambda server, **_: built.append(server.case_runtime)),)
            argv = ["--session-ticket-env", "T"]
            env = {**ENV_HOST, "T": Fixture.ticket(author=si.AUTHOR_AGENT),
                   si.SESSION_TICKET_SECRET_ENV: SECRET}
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.dict(os.environ, env))
            code, _out, err = self.main(command, "--state-dir", str(self.state), "--config",
                                        str(self.config), *argv)
        self.assertEqual((code, len(built)), (0, 1), err)
        self.addCleanup(built[0].close)
        return built[0]

    def test_host_serve_and_host_mcp_wire_the_case_capture_hook(self):
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV_HOST)
        for command in ("serve", "mcp"):
            captures = self.started(command).captures
            self.assertIsInstance(captures, CaseCaptures, command)
            self.assertEqual((captures.mail_capture_dir, captures.sharepoint_capture_dir,
                              captures.store_path),
                             (self.state.resolve(), self.state.resolve(),
                              host_module.projection_path(self.state)), command)
        self.assertIn("--host-captures-portal", host_module.console_arguments(host, 0))

    def test_every_host_mode_parses_the_argv_the_host_starts_it_with(self):
        """Each process the host starts parses its argv with no unknown argument and
        reads the freshness bounds the host passes (a console that refused
        ``--freshness-bound`` left ``host serve`` unable to start)."""
        import argparse
        from operator_ui import case_console
        from operating import mcp_http_host, mcp_server
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV_HOST)
        bounds = host_module.freshness_bounds(host.config)
        self.assertTrue(bounds)
        real = argparse.ArgumentParser.parse_args

        class Parsed(Exception):
            pass

        def parsed(parser, args=None, namespace=None):
            raise Parsed(real(parser, args, namespace))

        modes = {
            "serve": (case_console.main, host_module.console_arguments(host, 0)),
            "mcp": (mcp_server.main,
                    host_module.mcp_arguments(host, session_ticket_env="T")),
            "mcp-http": (mcp_http_host.main,
                         host_module.mcp_arguments(host, session_ticket_env=None)
                         + ["--entra-resource", str(self.state / "entra.json"),
                            "--bind", "127.0.0.1", "--port", "0"])}
        for mode, (main, argv) in modes.items():
            with mock.patch.object(argparse.ArgumentParser, "parse_args", parsed), \
                    contextlib.redirect_stderr(open(os.devnull, "w")) as quiet, \
                    self.assertRaises(Parsed, msg=mode) as caught:
                self.addCleanup(quiet.close)
                main(argv)
            (args,) = caught.exception.args
            self.assertEqual(mcp_server.freshness_bounds(args.freshness_bound), bounds, mode)

    def test_host_serve_console_applies_the_freshness_bounds_and_refuses_bad_ones(self):
        from operator_ui import case_console
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ=ENV_HOST)
        tickets, built = [], []
        real = case_console.TicketSessions

        def sessions(*args, **kwargs):
            tickets.append(kwargs["build_objects"])
            return real(*args, **kwargs)

        with mock.patch.object(case_console, "TicketSessions", sessions), \
                mock.patch.object(case_console, "serve", lambda **kw: None), \
                mock.patch.dict(os.environ, ENV_HOST):
            code, _out, err = self.main("serve", "--state-dir", str(self.state), "--config",
                                        str(self.config), "--port", "0",
                                        "--interval-seconds", "3600")
        self.assertEqual((code, len(tickets)), (0, 1), err)
        self.assertIsNotNone(tickets[0])
        reader = mock.Mock(actor=Actor("plx", frozenset({"employee:" + OID})))
        with mock.patch.object(case_console, "object_workspace_for",
                               lambda *a, **kw: built.append(kw)):
            tickets[0](reader)
        self.assertEqual(built[0]["freshness_bounds"], host_module.freshness_bounds(
            host.config))
        # An unknown family or a bad bound fails closed: the console does not start.
        argv = host_module.console_arguments(host, 0)
        for bad in ("nowhere=60", "bc=0"):
            with contextlib.redirect_stderr(open(os.devnull, "w")) as quiet, \
                    self.assertRaises(SystemExit, msg=bad) as caught:
                self.addCleanup(quiet.close)
                case_console.main(argv + ["--freshness-bound", bad])
            self.assertEqual(caught.exception.code, 2, bad)


class SessionEvidenceItemPermissionTests(SharePointHostCase):
    """Session evidence and the captures' item permissions (directives 05282fd4ab50,
    4a4cb9c9c360): the claim gate of the MCPServer's case paths binds evidence item by
    item, not per capture root."""

    scanned = False

    def counting(self, server):
        """A count of the three files the reader holds (the host labels a count of
        captured files as_of_capture without listing them, which binds no claim; listed
        whole here so the item permissions alone decide), with a money cell per file and a
        second group of the first two."""
        files = [self.handle(item) for item in ("01FILEA", "01FILEB", "01FILEC")]
        found = self.ok(server, "plx.find", {"object_type": "SharePointFile", "filters": [
            {"property": "name", "op": "present"}]})
        real = server.ontology.call

        def group(number, handles):
            return {"key": {"g": number}, "value": str(len(handles)), "currency": "USD",
                    "inputs": len(handles), "markings": [], "inputs_citations": [{
                        "l1_handle": handle, "field_path": "row.name",
                        "digest": "0" * 64, "lineage": "sharepoint"} for handle in handles]}

        def listed(name, arguments, actor):
            out = real(name, arguments, actor)
            if name != "plx.aggregate":
                return out
            return dict(out, gaps=[], groups=[group(1, files), group(2, files[:2])])

        def count() -> dict:
            with mock.patch.object(server.ontology, "call", listed):
                return _client(server, "plx.aggregate", {"set": found["set"],
                                                         "measure": {"op": "count"}})
        return files, count

    def test_a_permission_re_read_drops_only_the_evidence_of_the_items_it_changed(self):
        server = self.server(QUALITY)
        files, count = self.counting(server)
        # The claim cites the two files the reader keeps and states the count of three.
        case_id = self.ok(server, "plx.case_open", {"question": "How many files?"})["case_id"]
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "3 files are in the folder.", "kind": "fact",
            "status": "supported", "citations": files[:2], "value_claims": [{
                "value": "3", "op": "count", "role": "record_count",
                "of": [{"citation": handle} for handle in files[:2]]}]})

        def failures() -> list[str]:
            checked = self.ok(server, "plx.case_get", {"case_id": case_id,
                                                       "section": "claims"})
            return [failure["code"] for failure in checked["failures"]]

        # The host's Teams capture holds a channel membership read before the count.
        teams = capture_teams.open_store(server.case_runtime.state_dir,
                                         LiveSourceAudience({}, tenant="plx"))
        self.addCleanup(teams.close)
        run = teams.begin_run(capture_teams.CHANNEL_KIND.name, "teams:channel:t/c",
                              ["teams:channel:t:c"])["run_id"]
        teams.set_unit_acl(run, {"status": "read", "identities": [SALES]})
        self.assertNotIn(EVIDENCE_META_KEY, count()["_meta"])
        self.assertEqual(failures(), [])
        # Routine re-reads that change no item's permissions keep the evidence: the
        # membership read again as it was, a SharePoint run open (its items being read)
        # and a whole cadence run of the capture.
        teams.set_unit_acl(run, {"status": "read", "identities": [SALES]})
        self.store.save_root(DRIVE, QUALITY_FOLDER, status="in_progress", reason=None,
                             round_started_at=self.store.now())
        self.assertEqual(failures(), [])
        self.assertTrue(self.capture.run(scope())["complete"])
        self.assertEqual(failures(), [])
        # A membership that changed may admit another record to the group: the count is
        # a lower bound, no longer the exact count of three.
        teams.set_unit_acl(run, {"status": "read", "identities": [SALES, RICARDO]})
        self.assertEqual(failures(), ["claim_value_mismatch"])
        count()
        self.assertEqual(failures(), [])
        # A membership kept past the store's serving bound (a transient members read
        # failure keeps it, unrefreshed) no longer decides: it moves like a change.
        self.assertEqual(capture_store.UNIT_ACL_MAX_AGE, dt.timedelta(hours=48))
        aged = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48, minutes=1)
                ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with contextlib.closing(sqlite3.connect(teams.path)) as conn, conn:
            conn.execute("UPDATE unit_acl SET recorded_at = ?", (aged,))
        self.assertEqual(failures(), ["claim_value_mismatch"])
        # A SharePoint permission re-read revokes the third file for the reader; the
        # projection and the grant stay the same. What the third file gates goes (the
        # count listing it, its money cell); the first two files' evidence stays.
        self.drive.items[f"{DRIVE}:01FILEC"]["acl"] = [SALES]
        self.assertTrue(self.capture.run(scope())["complete"])
        self.assertNotEqual(self.ok(server, "plx.read", {"citation": files[2]})["status"],
                            "resolved")
        self.assertEqual(failures(), ["claim_value_mismatch"])
        returned = server._returned()
        self.assertEqual(sorted(returned["currencies"]),
                         [(handle, "row.name") for handle in sorted(files[:2])])
        self.assertEqual([(entry["records"], entry["complete"])   # one per count read
                          for entry in returned["counts"]], [(files[:2], False)] * 2)

    def test_a_store_that_records_no_permission_positions_keeps_nothing_and_says_why(self):
        server = self.server(QUALITY)
        _, count = self.counting(server)
        conn = sqlite3.connect(self.state / cs.STORE_DIR / cs.DB_FILE)
        conn.execute("DROP TABLE root_permissions")   # a store from before permission re-reads
        conn.commit()
        conn.close()
        answer = count()
        self.assertFalse(answer["isError"], answer)
        self.assertEqual(answer["_meta"][EVIDENCE_META_KEY],
                         {"kept": False, "reason": "permissions_unrecorded"})
        returned = server._returned()
        self.assertEqual((returned["counts"], returned["currencies"]), ([], {}))


class ContainerMemberHostTests(SharePointHostCase):
    """A member of a captured zip is gated through its container by the host."""

    scanned = False

    def seed(self):
        drive = seeded_drive()
        drive.file("01ZIP", "po-packet.zip", "01AOS",
                   zipped([("conf.txt", b"Confirmation for PO100234")]),
                   mime="application/zip", acl=(RICARDO, SALES))
        return drive

    def test_a_member_is_served_with_its_container_and_withheld_with_it(self):
        conn = sqlite3.connect(self.projection)
        self.addCleanup(conn.close)
        object_id, handle = conn.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v USING "
            "(object_id) WHERE o.object_type = 'ContainerMember'").fetchone()
        # An employee on the zip's item ACL: the member resolves, is findable and read.
        employee = Actor("plx", frozenset({"employee:" + OID}))
        allowed = self.server(employee, resolver=EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**POLICY, "principals": {
                "staff:quality": POLICY["principals"]["staff:quality"]}}),
            Calls(portal_user("MANAGER", email=SALES)), Calls(member(QA_GROUP))))
        self.assertEqual(self.ok(allowed, "plx.object", {"object_id": object_id})["object"][
            "object_id"], object_id)
        found = self.ok(allowed, "plx.find", {"query": "conf.txt",
                                              "object_type": "ContainerMember"})
        self.assertEqual([c["object"]["object_id"] for c in found["candidates"]], [object_id])
        read = self.ok(allowed, "plx.read", {"citation": handle})
        self.assertEqual(read["status"], "resolved")
        self.assertIn("Confirmation for PO100234", json.dumps(read))
        # Revoked upstream and re-read live: the member is withheld with its container.
        self.drive.items[f"{DRIVE}:01ZIP"]["acl"] = [RICARDO]
        server = self.server(EMPLOYEE, live_drive=True)
        for citation in (self.handle("01ZIP"), handle):
            denied = self.ok(server, "plx.read", {"citation": citation})
            self.assertEqual(HostBoundTests.refusal(denied),
                             ("withheld", "sharepoint_item_refused"), citation)
            self.assertNotIn("Confirmation for PO100234", json.dumps(denied))
        hidden = _client(server, "plx.object", {"object_id": object_id})["structuredContent"]
        self.assertEqual(hidden["error"]["code"], "object_not_found")


MAP_HTML = (b"<html><head><script>var x='SCRIPTONLY'</script></head><body><h1>Screening "
            b"Map</h1><p>Cost per bottle: blank + landed cost, divided by good bottles "
            b"returned.</p></body></html>")
MAP_SPEC = b'{"step": "Testing priority uses a look-ahead window", "rate": 1.10}'


class MarkupFileHostTests(SharePointHostCase):
    """``plx.read`` of the process maps as the agent reads them: an HTML file captured
    ``unsupported`` before HTML was read, and an HTML map and its JSON spec inside a zip,
    read from their pinned bytes now; a file with no extracted text is a
    ``text_not_extracted`` gap, never a complete read with nothing in it."""

    scanned = False

    def seed(self):
        drive = seeded_drive()
        drive.file("01HTML", "Quote Map.html", "01AOS", MAP_HTML, mime="text/html",
                   acl=(RICARDO, SALES))
        drive.file("01MAPZ", "Screening Map.zip", "01AOS",
                   zipped([("Map/Screening Map.html", MAP_HTML),
                           ("Map/data/map-spec.json", MAP_SPEC)]),
                   mime="application/zip", acl=(RICARDO, SALES))
        drive.file("01BLOB", "label.bin", "01AOS", b"\x00\x01binary",
                   mime="application/octet-stream", acl=(RICARDO, SALES))
        return drive

    def member(self, name: str) -> str:
        conn = sqlite3.connect(self.projection)
        self.addCleanup(conn.close)
        handles = [h for (h,) in conn.execute(
            "SELECT v.l1_handle FROM objects o JOIN object_versions v USING (object_id) "
            "WHERE o.object_type = 'ContainerMember'")]
        return next(h for h in handles if quote(name, safe="") in h)

    def test_html_and_json_read_with_their_text_and_a_textless_file_says_so(self):
        # As captured before this change: no stored text, status unsupported.
        self.store.db.execute("UPDATE items SET text_status = 'unsupported', text_sha256 = "
                              "NULL WHERE item_id = '01HTML'")
        self.store.db.commit()
        server = self.server(EMPLOYEE, portal_granted=False)
        for citation, phrase in ((self.handle("01HTML"), "divided by good bottles returned"),
                                 (self.member("Screening Map.html"), "good bottles returned"),
                                 (self.member("map-spec.json"), MAP_SPEC.decode())):
            with self.subTest(citation=citation):
                read = self.ok(server, "plx.read", {"citation": citation})
                self.assertEqual(read["status"], "resolved")
                self.assertIn(phrase, read["content"]["untrusted_text"])
                self.assertNotIn("SCRIPTONLY", json.dumps(read))
                self.assertTrue(read["completeness"]["complete"])
                self.assertNotIn("text_not_extracted", [g["reason"] for g in read["gaps"]])
        blob = self.handle("01BLOB")
        read = self.ok(server, "plx.read", {"citation": blob})
        self.assertEqual(read["status"], "resolved")
        self.assertNotIn("content", read)
        gap = self.labels(read, "text_not_extracted")[blob]
        self.assertEqual(gap["state"], "not_ingested")
        self.assertIn("unsupported", gap["detail"])
        self.assertEqual((read["completeness"]["complete"], read["completeness"]["reason"]),
                         (False, "text_not_extracted"))
        self.assertIn(blob, self.labels(read))   # still labelled as of capture
        # A metadata field asked for alone is no text read: no gap, complete.
        name = self.ok(server, "plx.read", {"citation": blob, "field_path": "row.name"})
        self.assertEqual(self.labels(name, "text_not_extracted"), {})
        self.assertTrue(name["completeness"]["complete"])
        # A reader without the files' permission is refused and told nothing of them.
        hidden = self.server(OUTSIDER, portal_granted=False)
        for citation in (self.handle("01HTML"), self.member("Screening Map.html"), blob):
            denied = self.ok(hidden, "plx.read", {"citation": citation})
            self.assertNotEqual(denied["status"], "resolved")
            self.assertNotIn("good bottles", json.dumps(denied))
            self.assertNotIn("text_not_extracted", json.dumps(denied))


class HostBoundTests(SharePointHostCase):
    """The host's bounds on capture-time permissions and on live re-reads."""

    def item_gets(self, item_id: str, since: int) -> int:
        return sum(1 for _m, url in self.drive.calls[since:] if f"/items/{item_id}" in url)

    def test_capture_time_permissions_past_the_max_age_need_a_live_re_read(self):
        scan = self.handle(SCAN)
        old = self.server(EMPLOYEE, clock=LATER)
        self.assertNotEqual(self.ok(old, "plx.read", {"citation": scan})["status"], "resolved")
        # A search serves the stored row at any capture age on its recorded
        # permissions, labelled with its capture time, the oldest named in the
        # freshness note: no gap, not incomplete for age alone, and no request sent.
        for server in (old, self.server(EMPLOYEE, live_drive=True, clock=LATER)):
            requests = self.transport.requests_sent
            answer = self.ok(server, "plx.documents",
                             {"query": SCAN_PO, "sources": ["sharepoint"]})
            self.assertIn(scan, [d["citation"] for d in answer["documents"]])
            label = self.labels(answer)[scan]["detail"]
            self.assertTrue(label.startswith("as of capture 2026-10-01T12:00:"), label)
            self.assertIn("as recorded at that capture", label)
            (fresh,) = [f for f in answer["meta"]["freshness"] if f["source"] == "sharepoint"]
            self.assertTrue(fresh["captured_at"].startswith("2026-10-01T12:00:"))
            self.assertIn("permissions as of capture; plx.read re-checks live",
                          fresh["label"])
            self.assertNotIn("capture_too_old", json.dumps(answer))
            self.assertNotEqual(answer["completeness"].get("reason"), "partial_scope")
            self.assertEqual(self.transport.requests_sent, requests)
        live = self.server(EMPLOYEE, live_drive=True, clock=LATER)
        # plx.read of the file re-reads it live and serves it, labelled with that basis.
        read = self.ok(live, "plx.read", {"citation": scan})
        self.assertEqual(read["status"], "resolved")
        self.assertIn("re-read live now", self.labels(read)[scan]["detail"])
        # No re-read allowed in the call: the old capture alone decides nothing.
        spent = self.server(EMPLOYEE, live_drive=True, clock=LATER, max_live_rereads=0)
        self.assertNotEqual(self.ok(spent, "plx.read", {"citation": scan})["status"],
                            "resolved")
        # A young capture is served on its recorded permissions, and says so; within
        # the age bound the freshness entry carries no age note.
        young = self.ok(self.server(EMPLOYEE, clock=SOON), "plx.documents",
                        {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertIn("as recorded at that capture", self.labels(young)[scan]["detail"])
        self.assertFalse([f for f in young["meta"]["freshness"]
                          if "re-checks live" in f.get("label", "")])
        # A capture dated after the host's clock decides nothing either (skew or a
        # forged stamp never extends the bound).
        early = self.server(EMPLOYEE, clock=CAPTURED - dt.timedelta(hours=1))
        self.assertEqual(self.refusal(self.ok(early, "plx.read", {"citation": scan})),
                         ("unresolvable", "capture_too_old"))

    def test_aggregate_counts_over_captured_files_are_labelled_as_of_capture(self):
        server = self.server(QUALITY)
        found = self.ok(server, "plx.find", {"query": "pdf", "object_type": "SharePointFile"})
        self.assertIn("set", found, json.dumps(found)[:600])
        counted = self.ok(server, "plx.aggregate", {"set": found["set"],
                                                    "measure": {"op": "count"}})
        self.assertGreater(sum(int(g["value"]) for g in counted["groups"]), 0)
        label = self.labels(counted)["sharepoint"]
        self.assertIn("as of 2026-10-01T12:00:", label["detail"])
        self.assertIn("sharepoint", {f["source"] for f in counted["meta"]["freshness"]})

    def test_one_calls_re_read_bound_is_not_reset_by_a_read_inside_it(self):
        server = self.server(QUALITY, live_drive=True, max_live_rereads=1)
        tools = self.tools
        reads = []

        def read_two(arguments, session_actor):
            for item in (SCAN, "01FILEB"):
                read = tools.evidence.reader.read(self.handle(item), QUALITY)
                reads.append({g["reason"]: g["detail"] for g in read["gaps"]})
            return {"ok": True}

        tools.register_tool("test.read_two", read_two)
        mark = len(self.drive.calls)
        tools.call("test.read_two", {}, QUALITY)
        self.assertIn("re-read live now", reads[0]["as_of_capture"])
        self.assertIn("live re-read bound is reached", reads[1]["as_of_capture"])
        self.assertEqual(self.item_gets("01FILEB", mark), 0)   # never re-read
        # The next call has its own bound again.
        page = self.ok(server, "plx.read", {"citation": self.handle("01FILEB")})
        self.assertIn("re-read live now", self.labels(page)[self.handle("01FILEB")]["detail"])

    def test_every_gate_built_inside_one_call_draws_from_the_calls_one_re_read_bound(self):
        # A captured original gated inside a tool call (CaseCaptures.gate_decision, as
        # CapturedOriginals.search and original_text gate it) got a gate of its own with
        # a fresh bound: one plx.documents call built 92 gates and started 127 re-reads
        # against a per-call bound of 50.
        from operating.captured_originals import FAILED
        server = self.server(QUALITY, live_drive=True, max_live_rereads=1, clock=LATER)
        tools, captures = self.tools, server.case_runtime.captures
        items = (SCAN, "01FILEB", "01FILEC")
        decided = []

        def gate_all(arguments, session_actor):
            decided.append(captures.gate_decision(self.handle(items[0]), QUALITY))
            with captures.call():   # a case tool's own gate, nested in the call
                decided.extend(captures.gate_decision(self.handle(item), QUALITY)
                               for item in items[1:])
            decided.append(captures.gate_decision(self.handle(items[0]), QUALITY))
            return {"ok": True}

        tools.register_tool("test.gate_all", gate_all)
        mark = len(self.drive.calls)
        tools.call("test.gate_all", {}, QUALITY)
        self.assertEqual([self.item_gets(item, mark) > 0 for item in items],
                         [True, False, False])
        # Past the bound a capture past the age bound decides nothing (failed, not a
        # refusal by the reader's permissions); the re-read answer is reused.
        self.assertEqual(decided, [True, FAILED, FAILED, True])
        # The next call has its own bound again; outside a call a resolve still gets
        # a gate and a bound of its own.
        mark = len(self.drive.calls)
        tools.call("test.gate_all", {}, QUALITY)
        self.assertEqual(decided[4:], [True, FAILED, FAILED, True])
        self.assertIs(captures.gate_decision(self.handle("01FILEB"), QUALITY), True)
        self.assertGreater(self.item_gets("01FILEB", mark), 0)

    def test_a_documents_question_sends_no_live_request_and_reports_unsearched_files(self):
        scan = self.handle(SCAN)
        # Captured text alone (no projection): only the file naming the identifier is
        # listed, and the files the capture stored no text for (two PDFs it could not
        # read), reported unsearched, which the reader must be allowed before they are
        # counted. A search is decided on the capture: no live request at all.
        unread = ("01FILEA", "01FILEC")
        for item in unread:
            self.assertIsNone(self.store.current(DRIVE, item)["text_sha256"], item)
        server = self.server(QUALITY, live_drive=True, projection=False)
        mark, sent = len(self.drive.calls), self.transport.requests_sent
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertEqual([d["citation"] for d in answer["documents"]], [scan])
        self.assertIn("a search result is not re-read live", self.labels(answer)[scan]["detail"])
        self.assertEqual(self.transport.requests_sent, sent)
        self.assertTrue(self.labels(answer, "text_not_read")["capture:sharepoint"][
            "detail"].startswith(f"{len(unread)} captured SharePoint file"))
        self.assertEqual(self.item_gets("01FILEB", mark), 0)   # its text names no SCAN_PO
        # Through the projection every candidate file is gated on the young capture,
        # whatever the call's bound: no GET is sent.
        for budget in (REREAD_GETS - 1, REREAD_GETS, 2 * REREAD_GETS):
            sent = self.transport.requests_sent
            bounded = self.ok(self.server(QUALITY, live_drive=True, max_live_gets=budget),
                              "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
            self.assertEqual(self.transport.requests_sent, sent)
            self.assertIn(scan, [d["citation"] for d in bounded["documents"]])
            self.assertIn("as recorded at that capture", self.labels(bounded)[scan]["detail"])

    def test_a_transient_source_failure_serves_the_capture_labelled_with_the_failure(self):
        scan = self.handle(SCAN)
        failing_calls = []

        def failing(method, url, headers, body, timeout, max_bytes):
            if "login.microsoftonline.com" in url:
                return self.drive(method, url, headers, body, timeout, max_bytes)
            failing_calls.append((method, url))
            return 503, {}, b""

        down = graph_read.build_graph(ENV, sender=failing, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        server = self.server(QUALITY, live_drive=True, live_api=down)
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertIn(scan, [d["citation"] for d in answer["documents"]])
        self.assertEqual(failing_calls, [])   # a search never re-reads live
        history = self.ok(server, "plx.read", {"citation": scan})
        self.assertTrue(failing_calls)
        self.assertTrue(all(method == "GET" for method, _url in failing_calls))
        # The failure stops further re-reads in the call: one item's retries at most.
        self.assertEqual(len({url.split("/items/")[1].split("/")[0].split("?")[0]
                              for _m, url in failing_calls}), 1)
        self.assertEqual(history["status"], "resolved")
        self.assertIn(scan, self.labels(history, "live_reread_failed"))
        # Past the capture age a failed re-read serves nothing.
        old = self.server(QUALITY, live_drive=True, live_api=down, clock=LATER)
        self.assertNotEqual(self.ok(old, "plx.read", {"citation": scan})["status"], "resolved")
        # A deletion is a refusal, not a transient failure.
        self.drive.delete(SCAN)
        gone = self.ok(self.server(QUALITY, live_drive=True), "plx.read", {"citation": scan})
        self.assertEqual(self.refusal(gone), ("withheld", "sharepoint_item_refused"))
        self.assertNotIn(SCAN_PO, json.dumps(gone))

    @staticmethod
    def refusal(read: dict) -> tuple[str, str | None]:
        """(status, the reason of its one refusal gap) of a plx.read answer."""
        reasons = [g["reason"] for g in read.get("gaps", ()) if g["subject"] == "sharepoint"]
        return read["status"], reasons[0] if len(reasons) == 1 else None

    def failing_api(self, status: int, calls: list):
        """A Graph API whose item GETs answer ``status`` (the token POST succeeds)."""
        def sender(method, url, headers, body, timeout, max_bytes):
            if "login.microsoftonline.com" in url:
                return self.drive(method, url, headers, body, timeout, max_bytes)
            calls.append((method, url))
            return status, {}, b""
        return graph_read.build_graph(ENV, sender=sender, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))

    def test_a_refused_captured_file_is_a_refusal_never_a_live_read_in_its_place(self):
        scan = self.handle(SCAN)
        # Too old, and the call may make no re-read: refused for age, nothing sent.
        server = self.server(EMPLOYEE, live_drive=True, clock=LATER, max_live_rereads=0)
        sent, mark = self.transport.requests_sent, len(self.drive.calls)
        read = self.ok(server, "plx.read", {"citation": scan})
        self.assertEqual(self.refusal(read), ("unresolvable", "capture_too_old"))
        self.assertIn("live re-read bound is reached",
                      self.labels(read, "capture_too_old")["sharepoint"]["detail"])
        self.assertNotIn("content", read)
        self.assertEqual(self.transport.requests_sent, sent)
        self.assertEqual(self.drive.calls[mark:], [])
        # Too old and the re-read fails: refused with the failure, and each read sends
        # no more than its one failed re-read (a search sends none).
        calls: list = []
        down = self.server(QUALITY, live_drive=True, clock=LATER,
                           live_api=self.failing_api(503, calls))
        self.assertIn(scan, json.dumps(self.ok(down, "plx.documents", {
            "query": SCAN_PO, "sources": ["sharepoint"]})))   # served on the capture
        self.assertEqual(calls, [])
        read = self.ok(down, "plx.read", {"citation": scan})
        one_reread = len(calls)
        self.assertGreater(one_reread, 0)
        self.assertEqual(self.refusal(read), ("unresolvable", "capture_too_old"))
        self.assertIn("retried.http_503",
                      self.labels(read, "capture_too_old")["sharepoint"]["detail"])
        self.ok(down, "plx.read", {"citation": scan})
        self.assertEqual(len(calls), 2 * one_reread)
        self.assertTrue(all(method == "GET" for method, _url in calls))
        # A live re-read that refuses (revoked upstream): a refusal, not read again live.
        live = self.server(EMPLOYEE, live_drive=True)
        self.drive.items[f"{DRIVE}:{SCAN}"]["acl"] = [RICARDO]
        mark = len(self.drive.calls)
        read = self.ok(live, "plx.read", {"citation": scan})
        self.assertEqual(self.refusal(read), ("withheld", "sharepoint_item_refused"))
        self.assertNotIn("content", read)
        self.assertGreater(self.item_gets(SCAN, mark), 0)
        self.assertLessEqual(len(self.drive.calls) - mark, REREAD_GETS)   # one re-read

    def test_an_unreadable_capture_refuses_the_file_and_reads_nothing_live(self):
        scan = self.handle(SCAN)
        server = self.server(QUALITY, live_drive=True)
        mark = len(self.drive.calls)
        with mock.patch.object(SharePointCaptureStore, "resolve",
                               side_effect=sqlite3.OperationalError("database is locked")):
            read = self.ok(server, "plx.read", {"citation": scan})
        self.assertEqual(self.refusal(read), ("unresolvable", "capture_unreadable"))
        self.assertNotIn("content", read)
        self.assertNotIn("database is locked", json.dumps(read))
        self.assertEqual(self.drive.calls[mark:], [])   # not read live in its place
        # Readable again: served.
        self.assertEqual(self.ok(server, "plx.read", {"citation": scan})["status"],
                         "resolved")

    def throttled_api(self, throttles: int, calls: list):
        """A fresh Graph API (its own transport: the first request is the token POST)
        whose every GET answers 429 ``throttles`` times before the fake drive does,
        retried within the read by the bounded backoff."""
        seen: dict[str, int] = {}

        def sender(method, url, headers, body, timeout, max_bytes):
            calls.append((method, url))
            seen[url] = seen.get(url, 0) + 1
            if method == "GET" and seen[url] % (throttles + 1):
                return 429, {}, b""
            return self.drive(method, url, headers, body, timeout, max_bytes)
        return graph_read.build_graph(ENV, sender=sender, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))

    def test_the_get_bound_is_never_exceeded_inside_one_re_read(self):
        scan = self.handle(SCAN)
        # Unbounded, one throttled re-read needs more requests than its estimate.
        calls: list = []
        free = self.server(QUALITY, live_drive=True, live_api=self.throttled_api(2, calls))
        self.assertEqual(self.ok(free, "plx.read", {"citation": scan})["status"], "resolved")
        self.assertGreater(len(calls), REREAD_GETS)
        # Bounded at REREAD_GETS: the re-read starts (its estimate fits) and the request
        # that would exceed the bound is refused before I/O, so the capture decides.
        calls = []
        api = self.throttled_api(2, calls)
        bounded = self.server(QUALITY, live_drive=True, live_api=api,
                              max_live_gets=REREAD_GETS)
        read = self.ok(bounded, "plx.read", {"citation": scan})
        self.assertEqual(len(calls), REREAD_GETS)
        self.assertEqual(api[1].requests_sent, REREAD_GETS)
        self.assertTrue(all(method == "GET" or "login.microsoftonline.com" in url
                            for method, url in calls))
        self.assertEqual(read["status"], "resolved")
        detail = self.labels(read)[scan]["detail"]
        self.assertIn("live re-read bound is reached", detail)
        self.assertNotIn("re-read live now", detail)
        # Past the capture age the cut-off re-read decides nothing: refused.
        calls = []
        old = self.server(QUALITY, live_drive=True, live_api=self.throttled_api(2, calls),
                          max_live_gets=REREAD_GETS, clock=LATER)
        read = self.ok(old, "plx.read", {"citation": scan})
        self.assertEqual(self.refusal(read), ("unresolvable", "capture_too_old"))
        self.assertNotIn("content", read)
        self.assertEqual(len(calls), REREAD_GETS)

    def test_the_get_bound_is_never_exceeded_inside_one_live_read(self):
        uncaptured = self.handle(SCAN, version=2)   # a version the capture never held
        calls: list = []
        free = self.server(QUALITY, live_drive=True, live_api=self.throttled_api(2, calls))
        self.ok(free, "plx.read", {"citation": uncaptured})
        self.assertGreater(len(calls), LIVE_READ_GETS)
        calls = []
        api = self.throttled_api(2, calls)
        bounded = self.server(QUALITY, live_drive=True, live_api=api,
                              max_live_gets=LIVE_READ_GETS)
        read = self.ok(bounded, "plx.read", {"citation": uncaptured})
        self.assertEqual(api[1].requests_sent, LIVE_READ_GETS)
        self.assertEqual(self.refusal(read), ("unresolvable", "live_read_budget"))
        self.assertFalse(read.get("fields"))
        self.assertNotIn("content", read)

    def test_http_410_on_a_re_read_is_a_refusal_not_a_transient_failure(self):
        scan = self.handle(SCAN)
        calls: list = []
        server = self.server(QUALITY, live_drive=True, live_api=self.failing_api(410, calls))
        # A search lists the file on its young capture and sends nothing; the read of it
        # re-reads it live and the 410 refuses it.
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertIn(scan, [d["citation"] for d in answer["documents"]])
        self.assertEqual(calls, [])
        read = self.ok(server, "plx.read", {"citation": scan})
        self.assertNotIn("live_reread_failed", json.dumps(read))
        self.assertEqual(self.refusal(read), ("withheld", "sharepoint_item_refused"))
        self.assertNotIn("content", read)
        self.assertTrue(calls)
        self.assertTrue(all(method == "GET" for method, _url in calls))

    def test_a_live_read_of_an_uncaptured_file_shares_the_calls_re_read_bound(self):
        server = self.server(QUALITY, live_drive=True, max_live_rereads=1)
        tools = self.tools
        uncaptured = self.handle(SCAN, version=2)   # a version the capture never held
        reads = []

        def read_two(order):
            def call(arguments, session_actor):
                reads.clear()
                for handle in order:
                    reads.append(tools.evidence.reader.read(handle, QUALITY))
                return {"ok": True}
            return call

        # The re-read of a captured file spends the bound: the live read is refused
        # without a request.
        tools.register_tool("test.captured_first", read_two([self.handle("01FILEB"),
                                                              uncaptured]))
        tools.call("test.captured_first", {}, QUALITY)
        mark = len(self.drive.calls)
        self.assertEqual(reads[0]["status"], "resolved")
        self.assertEqual((reads[1]["status"], reads[1]["reason"]),
                         ("unresolvable", "live_read_budget"))   # the reader's own shape
        self.assertEqual(self.drive.calls[mark:], [])
        # The live read spends it first: the captured file's re-read is not made.
        tools.register_tool("test.live_first", read_two([uncaptured, self.handle("01FILEB")]))
        tools.call("test.live_first", {}, QUALITY)
        self.assertGreater(len(self.drive.calls), mark)   # the live read was sent
        self.assertNotEqual(reads[0]["reason"], "live_read_budget")
        self.assertIn("live re-read bound is reached",
                      self.labels(reads[1])[self.handle("01FILEB")]["detail"])
        self.assertEqual(self.item_gets("01FILEB", mark), 0)
        # A call's live reads stay within its GET bound.
        bounded = self.server(QUALITY, live_drive=True, max_live_gets=LIVE_READ_GETS - 1)
        sent = self.transport.requests_sent
        read = self.ok(bounded, "plx.read", {"citation": uncaptured})
        self.assertEqual(self.refusal(read), ("unresolvable", "live_read_budget"))
        self.assertEqual(self.transport.requests_sent, sent)

    def test_plx_read_takes_a_page_for_a_page_cited_document(self):
        scan = self.handle(SCAN)
        server = self.server(EMPLOYEE)
        page = self.ok(server, "plx.read", {"citation": scan, "page": 2})
        self.assertEqual(page["status"], "resolved")
        self.assertIn(SCAN_PO, page["content"]["untrusted_text"])
        self.assertNotIn("Assembly packet", page["content"]["untrusted_text"])
        self.assertEqual(page["content"]["origin"]["citation"], scan + "#page=2")
        self.assertIn(scan, self.labels(page))
        self.assertEqual(self.ok(server, "plx.read", {"citation": scan + "#page=2", "page": 2}
                                 )["content"]["origin"]["citation"], scan + "#page=2")
        for arguments in ({"citation": scan, "page": 9},
                          {"citation": scan + "#page=1", "page": 2}):
            missing = self.ok(server, "plx.read", arguments)
            self.assertNotEqual(missing["status"], "resolved")
            self.assertNotIn("content", missing)
        # A reader without the file's permission gets no page either.
        denied = self.ok(self.server(OUTSIDER), "plx.read", {"citation": scan, "page": 2})
        self.assertNotIn("content", denied)


class LiveRefusalKindTests(SharePointHostCase):
    """The refusal kind a live re-read leaves for the scorer
    (``CaseCaptures.gate_decision``): only the reader's permissions decide a refusal. A
    deletion (404, 410) or a transient failure of the permission read (429, 5xx,
    timeout) is ``failed``. Every one answers the agent the same bytes."""

    scanned = False
    FILE = "01FILEB"

    def permissions_api(self, answer):
        """A Graph API over the fake drive whose item permission GETs ``answer`` (a status,
        or an exception raised in transit); every other request is the drive's."""
        def sender(method, url, headers, body, timeout, max_bytes):
            if method == "GET" and url.split("?")[0].endswith("/permissions"):
                if isinstance(answer, BaseException):
                    raise answer
                return answer, {}, b""
            return self.drive(method, url, headers, body, timeout, max_bytes)
        return graph_read.build_graph(ENV, sender=sender, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))

    def decided(self, live_api=None):
        """(gate kind, the agent's plx.read answer bytes) for the employee now."""
        handle = self.handle(self.FILE)
        server = self.server(EMPLOYEE, live_drive=True, live_api=live_api)
        kind = server.case_runtime.captures.gate_decision(handle, EMPLOYEE)
        read = self.ok(self.server(EMPLOYEE, live_drive=True, live_api=live_api),
                       "plx.read", {"citation": handle})
        read.get("meta", {}).pop("as_known_at", None)
        return kind, json.dumps(read, sort_keys=True)

    def test_only_a_permission_decision_is_a_refusal_and_every_answer_reads_alike(self):
        from operating.captured_originals import FAILED, REFUSED
        self.assertIs(self.decided()[0], True)
        item = self.drive.items[f"{DRIVE}:{self.FILE}"]
        # A transient failure of the live permission read: failed, not refused.
        transient = {name: self.decided(self.permissions_api(answer)) for name, answer in (
            ("429", 429), ("503", 503), ("500", 500), ("timeout", TimeoutError("timed out")),
            ("reset", ConnectionResetError("connection reset")))}
        item["acl"] = [RICARDO]   # revoked upstream: decided by the reader's permissions
        refused_kind, refused = self.decided()
        self.assertEqual(refused_kind, REFUSED)
        self.assertIn("sharepoint_item_refused", refused)
        self.assertNotIn(TEXT_SECRET, refused)
        for name, decided in transient.items():
            with self.subTest(transient=name):   # the agent's answer: byte-identical
                self.assertEqual(decided, (FAILED, refused))
        item["acl"] = [RICARDO, SALES]
        # Deleted at the source: a live 404 (itemNotFound) or 410 is failed.
        calls: list = []
        self.assertEqual(self.decided(HostBoundTests.failing_api(self, 410, calls)),
                         (FAILED, refused))
        self.drive.delete(self.FILE)
        self.assertEqual(self.decided(), (FAILED, refused))


class OverCapTests(SharePointHostCase):
    """The capture's content cap below the scanned packet: the packet is a recorded
    cap-exceeded outcome, read as such through the documents tools."""

    max_content_bytes = 200

    def test_an_over_cap_file_reads_as_cap_exceeded_and_is_reported_unsearched(self):
        scan = self.handle(SCAN)
        current = self.store.current(DRIVE, SCAN)
        self.assertEqual(current["content_status"], "exceeds_cap")
        (root,) = self.store.completeness(CAPTURE, capture_audience())["roots"]
        self.assertTrue(root["complete"])
        server = self.server(EMPLOYEE, portal_granted=False)
        read = self.ok(server, "plx.read", {"citation": scan})
        self.assertEqual(read["status"], "resolved")
        self.assertNotIn("content", read)
        self.assertEqual(read["fields"]["row.content_status"]["untrusted_text"], "exceeds_cap")
        gap = self.labels(read, "content_exceeds_cap")[scan]
        self.assertIn(f"({json.loads(current['record'])['size']} bytes)", gap["detail"])
        self.assertIn(scan, self.labels(read))
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertEqual(answer["documents"], [])
        self.assertIn("capture:sharepoint", self.labels(answer, "content_exceeds_cap"))
        self.assertFalse(answer["completeness"]["complete"])
        # A reader without the file's permission is told nothing about it.
        hidden = self.ok(self.server(OUTSIDER, portal_granted=False), "plx.documents",
                         {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertNotIn("content_exceeds_cap", json.dumps(hidden))


class OverBoundTests(SharePointHostCase):
    """A file whose bytes are captured but over the text extractor's bound (a scope cap
    above its 20 MB; the bound is 20 bytes here, so release.txt is over it)."""

    scanned = False

    def seed(self):
        drive = seeded_drive()   # bytes of its own: no earlier reading of them is cached
        drive.items[f"{DRIVE}:01FILEB"]["content"] = b"Over the bound: PO100234 released."
        return drive

    def setUp(self):
        bound = mock.patch.object(document_understanding, "MAX_BYTES", 20)
        bound.start()
        self.addCleanup(bound.stop)
        super().setUp()

    def test_a_file_over_the_extractor_bound_is_an_exceeds_bound_gap_never_silence(self):
        # Directive 515540eaf702 (3): such a file yielded no gap, so a search reported
        # nothing in it without saying it was never read.
        release = self.handle("01FILEB")
        current = self.store.current(DRIVE, "01FILEB")
        self.assertEqual((current["content_status"], current["text_status"]),
                         ("captured", "exceeds_bound"))
        server = self.server(EMPLOYEE, portal_granted=False)
        read = self.ok(server, "plx.read", {"citation": release})
        self.assertEqual(read["status"], "resolved")
        self.assertNotIn("content", read)
        self.assertIn(release, self.labels(read, "exceeds_bound"))
        answer = self.ok(server, "plx.documents", {"query": "PO100234",
                                                   "sources": ["sharepoint"]})
        self.assertEqual(answer["documents"], [])
        self.assertIn("capture:sharepoint", self.labels(answer, "exceeds_bound"))
        self.assertFalse(answer["completeness"]["complete"])
        # A reader without the file's permission is told nothing about it.
        hidden = self.ok(self.server(OUTSIDER, portal_granted=False), "plx.documents",
                         {"query": "PO100234", "sources": ["sharepoint"]})
        self.assertNotIn("exceeds_bound", json.dumps(hidden))


NOTE = "01NOTE"
NOTE_PO = "PO100236"


class StoredTextLookupTests(SharePointHostCase):
    """devloop/DECISIONS/2026-10-28-identifier-lookup.md: an identifier question reads
    the text the capture stored, through an index kept beside the store, and never
    extracts or OCR-reads at query time. Each card session was a new process that
    re-extracted every captured file (263 s cold on 2,220 files, 175 s of it on 46
    ``ocr_pending`` scans)."""

    def seed(self):
        drive = seeded_drive()
        drive.file(NOTE, "note.txt", "01LOTS", f"Purchase order {NOTE_PO}".encode(),
                   mime="text/plain", acl=(RICARDO, SALES))
        return drive

    @contextlib.contextmanager
    def new_process(self):
        """A process that has read nothing yet (empty reading memo and index copy), in
        which every extractor run is counted."""
        from integrations import captured_text
        from operating import case_tools_host
        counted = mock.patch.object(document_understanding, "extract_document",
                                    side_effect=document_understanding.extract_document)
        with mock.patch.object(captured_text, "_CACHE", captured_text._CACHE.empty()), \
                mock.patch.dict(case_tools_host._INDEXES, clear=True), counted as extract:
            yield extract

    def ask(self, actor, query):
        return self.ok(self.server(actor, portal_granted=False), "plx.documents",
                       {"query": query, "sources": ["sharepoint"]})

    def kept_index(self) -> Path:
        from operating.case_tools_host import identifier_index_path
        return identifier_index_path(self.store.root)

    def test_no_question_extracts_and_a_new_process_reads_the_kept_index(self):
        with self.new_process() as extract:
            empty = self.ask(EMPLOYEE, "PO999999")   # no file names it
        self.assertEqual(empty["documents"], [])
        self.assertEqual(extract.call_count, 0)
        self.assertEqual(self.kept_index().stat().st_mode & 0o777, 0o600)
        # A new process takes the index kept beside the store: no stored text is read.
        from integrations import captured_text
        unread = mock.patch.object(captured_text, "stored_text",
                                   side_effect=AssertionError("stored text read again"))
        with self.new_process() as extract, unread:
            again = self.ask(EMPLOYEE, "PO999999")
            found = self.ask(EMPLOYEE, SCAN_PO)
        for answer in (empty, again):
            answer.get("meta", {}).pop("as_known_at", None)
        self.assertEqual(again, empty)
        # The matching scan is read as plx.read serves it: from its pinned OCR text.
        self.assertEqual([d["citation"] for d in found["documents"]], [self.handle(SCAN)])
        self.assertEqual(found["documents"][0]["mentions"][0]["span"]["origin"]["citation"],
                         self.handle(SCAN) + "#page=2")
        self.assertEqual(extract.call_count, 0)

    def test_the_index_names_what_a_fresh_reading_of_every_captured_file_names(self):
        # Equivalence: per current file, the identifiers by page and the unsearched
        # reason the stored text gives are those a fresh reading of the bytes gives.
        from integrations import captured_text
        from operating.case_tools_host import file_index
        store = SharePointCaptureStore.open_read_only(self.state)
        self.addCleanup(store.close)
        with self.new_process():
            index = file_index(store)
        statuses = set()
        for row in store.db.execute("SELECT * FROM items WHERE superseded_at IS NULL"):
            handle = row["l1_handle"]
            file = {**json.loads(row["record"]), **{k: row[k] for k in (
                "content_status", "content_sha256", "text_status", "text_sha256")}}
            statuses.add(row["text_status"])
            reading = captured_text.captured_text(store.blobs, file)
            text = None if reading is None else reading.text
            named = None
            if text is not None:
                paged = mentions.is_paged_file(file)
                named = [list(key) for key in dict.fromkeys(
                    (m["identifier_kind"], m["normalized"],
                     mentions.cited_page(f.citation)[1] if paged else None)
                    for f in mentions.file_text_fields(handle, "row.content_text", text,
                                                       paged=paged)
                    for m in mentions.extract(f.text) if m["normalized"])]
            entry = index[handle]
            self.assertEqual(entry["named"] or [], named or [], handle)
            if named is None:
                self.assertIsNotNone(entry["why"], handle)   # unsearched is never silent
        self.assertTrue({"extracted", "extracted_ocr"} <= statuses, statuses)

    def test_a_file_without_stored_text_is_a_text_not_read_gap_never_read_now(self):
        note = self.handle(NOTE)
        stored = self.store.current(DRIVE, NOTE)["text_sha256"]
        self.store.fill_text(note, "unsupported", None)   # the capture stored no text
        with self.new_process() as extract:
            answer = self.ask(EMPLOYEE, NOTE_PO)
        self.assertEqual(extract.call_count, 0)
        self.assertNotIn(note, [d["citation"] for d in answer["documents"]])
        gap = self.labels(answer, "text_not_read")["capture:sharepoint"]
        self.assertEqual(gap["state"], "not_ingested")
        self.assertTrue(gap["detail"].startswith("1 captured SharePoint file"), gap)
        self.assertFalse(answer["completeness"]["complete"])
        # A reader without the file's permission is told nothing about it.
        hidden = self.ask(OUTSIDER, NOTE_PO)
        self.assertNotIn("text_not_read", json.dumps(hidden))
        self.assertNotIn(note, json.dumps(hidden))
        # Scanned pages left for a later OCR pass: the stored text layer is searched,
        # and a question it does not answer names the file unsearched.
        self.store.fill_text(note, "ocr_pending", stored)
        with self.new_process() as extract:
            layer = self.ask(EMPLOYEE, NOTE_PO)
            other = self.ask(EMPLOYEE, "PO999999")
        self.assertIn(note, [d["citation"] for d in layer["documents"]])
        self.assertIn("capture:sharepoint", self.labels(other, "text_not_read"))
        self.assertEqual(other["documents"], [])

    def test_a_kept_index_that_is_foreign_or_forged_never_serves_a_mention(self):
        with self.new_process():
            self.ask(EMPLOYEE, "PO999999")
        path = self.kept_index()
        kept = json.loads(path.read_text(encoding="utf-8"))
        # A forged entry claims the note names PO999999: the file's own text is read
        # before anything is served, so nothing is.
        for entry in kept["files"].values():
            if entry["named"] and [n for n in entry["named"] if n[1] == NOTE_PO]:
                entry["named"].append(["petra_po", "PO999999", None])
        path.write_text(json.dumps(kept), encoding="utf-8")
        with self.new_process():
            forged = self.ask(EMPLOYEE, "PO999999")
        self.assertEqual(forged["documents"], [])
        # Another code's index or a broken file is not used: the stored text answers.
        for text in (json.dumps({**kept, "code": "0" * 64}), "{not json"):
            path.write_text(text, encoding="utf-8")
            with self.new_process():
                answer = self.ask(EMPLOYEE, NOTE_PO)
            self.assertEqual([d["citation"] for d in answer["documents"]],
                             [self.handle(NOTE)])

    def test_every_one_of_sixty_new_files_is_indexed_once(self):
        # Cardinality: N=1, 10 and 60 files captured since the index was kept are each
        # added once, and each identifier finds exactly its own file.
        from operating.case_tools_host import file_index
        with self.new_process():
            self.ask(EMPLOYEE, "PO999999")
        added = 0
        for count in (1, 10, 60):
            for number in range(added, added + count):
                self.drive.file(f"01N{number:03d}", f"n{number}.txt", "01LOTS",
                                f"Purchase order PO2{number:05d}".encode(),
                                mime="text/plain", acl=(RICARDO, SALES))
            added += count
            self.capture.run(scope())
            store = SharePointCaptureStore.open_read_only(self.state)
            try:
                index = file_index(store)
            finally:
                store.close()
            for number in range(added):
                named = index[self.handle(f"01N{number:03d}")]["named"]
                self.assertEqual(named, [["petra_po", f"PO2{number:05d}", None]])
            answer = self.ask(EMPLOYEE, f"PO2{added - 1:05d}")
            self.assertEqual([d["citation"] for d in answer["documents"]],
                             [self.handle(f"01N{added - 1:03d}")])


class IndexCodeTests(unittest.TestCase):
    """The kept identifier index is keyed by a digest of every input its entries are made
    from: a change to any of them rebuilds it, never reuses entries made by other code."""

    def code(self):
        from operating import case_tools_host
        case_tools_host._index_code.cache_clear()
        self.addCleanup(case_tools_host._index_code.cache_clear)
        return case_tools_host._index_code()

    def test_a_change_to_any_input_of_the_index_changes_its_digest(self):
        from integrations import captured_text
        base = self.code()
        self.assertIsNotNone(base)

        def other(*_args, **_kwargs):   # any other code in an input's place
            return None

        for target, name, value in (
                (mentions, "text_pages", other),
                (captured_text, "read_dropped", other),
                (captured_text, "decode_reading", other),
                (document_understanding, "pdf_page_count", other),
                (document_understanding, "MAX_PAGES", document_understanding.MAX_PAGES + 1),
                (mentions, "PAGE_BREAK", "\x0c\x0c")):
            with self.subTest(input=name), mock.patch.object(target, name, value):
                self.assertNotEqual(self.code(), base)
        self.assertEqual(self.code(), base)   # the same inputs: the same digest


class MetadataOnlyStoreTests(SharePointHostCase):
    """A store a metadata-only capture wrote (``read_content=False``): its files' bytes
    were never read, so the host never takes it for content: each served file is labelled
    and a lookup reports the files unsearched, never the identifier absent."""

    scanned = False
    read_content = False

    def test_a_metadata_only_file_is_labelled_and_reported_unsearched(self):
        release = self.handle("01FILEB")
        current = self.store.current(DRIVE, "01FILEB")
        self.assertEqual((current["content_status"], current["text_status"]),
                         (cs.CONTENT_SKIPPED, "not_read"))
        self.assertFalse(self.store.completeness(CAPTURE, capture_audience())["complete"])
        server = self.server(EMPLOYEE, portal_granted=False)
        read = self.ok(server, "plx.read", {"citation": release})
        self.assertEqual(read["status"], "resolved")
        self.assertNotIn("content", read)
        self.assertIn(release, self.labels(read, "content_not_read"))
        answer = self.ok(server, "plx.documents", {"query": "PO100234",
                                                   "sources": ["sharepoint"]})
        self.assertNotIn(release, [d["citation"] for d in answer["documents"]])
        gap = self.labels(answer, "content_not_read")["capture:sharepoint"]
        self.assertEqual(gap["state"], "not_ingested")
        self.assertFalse(answer["completeness"]["complete"])
        # A reader without the files' permission is told nothing about them.
        hidden = self.ok(self.server(OUTSIDER, portal_granted=False), "plx.documents",
                         {"query": "PO100234", "sources": ["sharepoint"]})
        self.assertNotIn("content_not_read", json.dumps(hidden))


class PageCapTests(SharePointHostCase):
    """Directives 754ef76ea79b and 0bfff43dc217 (devloop/DECISIONS/
    pdf-cap-readers-contract.md): a captured PDF over the page bound is read on pages
    1-200; plx.read and plx.documents carry a typed ``skipped_by_cap`` naming the file and
    the pages not read, so a page past the bound or a search with no match on the pages
    read is never taken for absence. A PDF within the bound with no match is the control;
    BOUNDARY is one page over the bound (page 201 not read, page 1 read)."""

    scanned = False
    CAPPED, WHOLE, BOUNDARY = "01CAPPED", "01WHOLE", "01BOUNDARY"
    FIRST_PO, LAST_PO = "PO100301", "PO100302"   # BOUNDARY's pages 1 and 201

    def seed(self):
        from integrations.tests.test_doc_convert import make_pdf
        drive = seeded_drive()
        pages = [["Lot packet"]] + [[f"Lot sheet {n}"] for n in range(2, 251)]
        pages[229] = [f"Purchase order {SCAN_PO}"]   # page 230: past the bound
        drive.file(self.CAPPED, "lot-packet.pdf", "01LOTS", make_pdf(pages),
                   acl=(RICARDO, SALES))
        drive.file(self.WHOLE, "lot-sheet.pdf", "01LOTS", make_pdf(pages[:3]),
                   acl=(RICARDO, SALES))
        boundary = [[f"Purchase order {self.FIRST_PO}"]] + pages[1:200] \
            + [[f"Purchase order {self.LAST_PO}"]]
        drive.file(self.BOUNDARY, "lot-boundary.pdf", "01LOTS", make_pdf(boundary),
                   acl=(RICARDO, SALES))
        return drive

    def setUp(self):
        for patch in (mock.patch("integrations.doc_convert.installed_backend",
                                 return_value=None),
                      mock.patch.object(document_understanding, "local_ocr_available",
                                        return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()

    def test_a_capped_pdf_reads_and_searches_with_its_skipped_pages_named(self):
        capped, whole = self.handle(self.CAPPED), self.handle(self.WHOLE)
        server = self.server(EMPLOYEE, portal_granted=False)
        read = self.ok(server, "plx.read", {"citation": capped})
        self.assertEqual(read["status"], "resolved")
        self.assertIn("Lot sheet 200", read["content"]["untrusted_text"])
        self.assertNotIn(SCAN_PO, read["content"]["untrusted_text"])
        self.assertIn("pages 201-250", self.labels(read, "skipped_by_cap")[capped]["detail"])
        self.assertIn(capped, self.labels(read))
        # A page past the bound answers with the gap; a page read, or one past the
        # file's last page, is unchanged.
        past = self.ok(server, "plx.read", {"citation": capped, "page": 230})
        self.assertEqual((past["status"], "content" in past), ("unresolvable", False))
        self.assertIn("pages 201-250",
                      self.labels(past, "skipped_by_cap")[capped + "#page=230"]["detail"])
        within = self.ok(server, "plx.read", {"citation": capped, "page": 200})
        self.assertEqual(within["content"]["untrusted_text"].strip(), "Lot sheet 200")
        for answer in (within, self.ok(server, "plx.read", {"citation": capped, "page": 251}),
                       self.ok(server, "plx.read", {"citation": whole})):
            self.assertEqual(self.labels(answer, "skipped_by_cap"), {})
        # A search with no match on the pages read names the capped file and its pages
        # not read; the whole PDF without a match (control) gets no such gap.
        answer = self.ok(server, "plx.documents", {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertEqual(answer["documents"], [])
        self.assertEqual(set(self.labels(answer, "skipped_by_cap")),
                         {capped, self.handle(self.BOUNDARY)})
        self.assertIn("pages 201-250", self.labels(answer, "skipped_by_cap")[capped]["detail"])
        self.assertIn("no_match", {g["reason"] for g in answer["gaps"]})
        self.assertEqual((answer["completeness"]["complete"],
                          answer["completeness"]["reason"]), (False, "partial_scope"))
        # Past the files named one by one, the rest are counted in one gap, never dropped.
        with mock.patch.object(ontology_tools_evidence, "CAPPED_LISTED", 0):
            counted = self.ok(server, "plx.documents", {"query": SCAN_PO,
                                                        "sources": ["sharepoint"]})
        self.assertEqual([(subject, gap["detail"].split(" ", 1)[0]) for subject, gap
                          in self.labels(counted, "skipped_by_cap").items()],
                         [("capture:sharepoint", "2")])
        # A reader without the file's permission is told nothing about it.
        outsider = self.server(OUTSIDER, portal_granted=False)
        hidden = [self.ok(outsider, "plx.documents",
                          {"query": SCAN_PO, "sources": ["sharepoint"]}),
                  self.ok(outsider, "plx.read", {"citation": capped, "page": 230})]
        self.assertNotIn("skipped_by_cap", json.dumps(hidden))
        self.assertNotIn(self.CAPPED, json.dumps(hidden[0]))   # the read echoes its citation
        self.assertNotIn("pages 201", json.dumps(hidden))

    def test_a_capped_pdf_waiting_for_ocr_is_counted_in_one_gap(self):
        # Page-capped and its scanned pages left for OCR: named once, by its pages not
        # read (the more specific gap), never also counted as text not read.
        capped = self.handle(self.CAPPED)
        stored = self.store.current(DRIVE, self.CAPPED)["text_sha256"]
        self.store.fill_text(capped, "ocr_pending", stored)
        answer = self.ok(self.server(EMPLOYEE, portal_granted=False), "plx.documents",
                         {"query": SCAN_PO, "sources": ["sharepoint"]})
        self.assertIn("pages 201-250", self.labels(answer, "skipped_by_cap")[capped]["detail"])
        self.assertEqual(self.labels(answer, "text_not_read"), {})

    def test_an_identifier_only_on_the_page_past_the_bound_is_no_absence(self):
        """A 201-page PDF: its identifier on page 201 (not read) answers no match with
        the file's ``skipped_by_cap`` gap and complete false; its identifier on page 1
        (read) still answers the file with the mention cited by page."""
        boundary = self.handle(self.BOUNDARY)
        server = self.server(EMPLOYEE, portal_granted=False)
        missed = self.ok(server, "plx.documents", {"query": self.LAST_PO,
                                                   "sources": ["sharepoint"]})
        self.assertEqual(missed["documents"], [])
        self.assertIn("pages 201-201",
                      self.labels(missed, "skipped_by_cap")[boundary]["detail"])
        self.assertEqual((missed["completeness"]["complete"],
                          missed["completeness"]["reason"]), (False, "partial_scope"))
        found = self.ok(server, "plx.documents", {"query": self.FIRST_PO,
                                                  "sources": ["sharepoint"]})
        self.assertEqual([doc["citation"] for doc in found["documents"]], [boundary])
        self.assertEqual({(m["normalized"], m["span"]["origin"]["citation"])
                          for m in found["documents"][0]["mentions"]},
                         {(self.FIRST_PO, boundary + "#page=1")})


    def test_a_finding_citing_a_page_not_read_is_refused_page_not_read(self):
        """Directive bf3d8e57cd44 (1): a case finding cites a page of a captured PDF only
        as its reading read it; page 230 of the capped file (past the cap) and a page past
        a file's last page are refused ``page_not_read``, a page read is recorded, and a
        reader without the file is refused without that reason. A cap recorded without
        its range (a partial ``skipped_by_cap``) reads as pages not read, never raises."""
        capped, whole = self.handle(self.CAPPED), self.handle(self.WHOLE)
        server = self.server(EMPLOYEE, portal_granted=False)
        case_id = self.ok(server, "plx.case_open", {"question": "Was the lot packed?",
                                                    "audience": ["staff:sales"]})["case_id"]

        def add(actor_server, citation: str) -> dict:
            return _client(actor_server, "plx.case_add_finding", {
                "case_id": case_id, "kind": "fact", "status": "supported",
                "statement": "The lot packet names the order.", "citations": [citation]})

        for citation in (capped + "#page=230", capped + "#page=251", whole + "#page=4"):
            refused = add(server, citation)
            self.assertTrue(refused["isError"], citation)
            self.assertIn("page_not_read", json.dumps(refused), citation)
        self.assertFalse(add(server, capped + "#page=200")["isError"])
        outsider = add(self.server(OUTSIDER, portal_granted=False), capped + "#page=230")
        self.assertTrue(outsider["isError"])
        self.assertNotIn("page_not_read", json.dumps(outsider))
        resolve = cs.SharePointCaptureStore.resolve

        def partial(store, *args, **kwargs):
            view = resolve(store, *args, **kwargs)
            if view is not None and "skipped_by_cap" in view["row"]:
                view["row"]["skipped_by_cap"] = {"first_page": 201, "cap": "pages"}
            return view

        with mock.patch.object(cs.SharePointCaptureStore, "resolve", partial):
            past = self.ok(server, "plx.read", {"citation": capped, "page": 230})
        self.assertEqual(past["status"], "unresolvable")
        self.assertIn("pages past the page cap",
                      self.labels(past, "skipped_by_cap")[capped + "#page=230"]["detail"])

    def test_a_search_withholds_a_stored_row_without_a_recorded_permission_list(self):
        handle = self.handle("01FILEB")
        query = {"query": "PO100234", "sources": ["sharepoint"]}
        # Folder grants alone (no item permission required by the audience): only the
        # search's own rule keeps a row with no recorded permission list out.
        folders = LiveSourceAudience(
            {f"sharepoint:{DRIVE}:{QUALITY_FOLDER}": ["staff:quality"],
             f"sharepoint:{DRIVE}:{QUALITY_FOLDER}/*": ["staff:quality"]},
            tenant="plx", identities={"staff:quality": [RICARDO]})
        before = self.ok(self.server(QUALITY, clock=LATER, audience=folders),
                         "plx.documents", query)
        self.assertIn(handle, [d["citation"] for d in before["documents"]])
        for acl in ({}, {"status": "unavailable", "identities": [], "grants": []},
                    {"status": "read", "identities": [], "grants": []},
                    {"status": "derived", "identities": [], "grants": []}):
            with self.subTest(acl=acl):
                self.store.db.execute("UPDATE items SET source_acl=? WHERE item_id=?",
                                      (json.dumps(acl), "01FILEB"))
                self.store.db.commit()
                answer = self.ok(self.server(QUALITY, clock=LATER, audience=folders),
                                 "plx.documents", query)
                self.assertNotIn(handle, json.dumps(answer))
                self.assertNotIn(TEXT_SECRET, json.dumps(answer))

    def test_an_old_capture_answers_a_search_and_a_read_re_reads_it(self):
        """Directive bf3d8e57cd44 (3), under the search policy: on a capture past the
        age bound a search serves the matching file on its recorded permissions (no
        request, no age gap), and plx.read of it re-reads it live; a reader the
        recorded permissions refuse gets nothing."""
        boundary = self.handle(self.BOUNDARY)
        query = {"query": self.FIRST_PO, "sources": ["sharepoint"]}
        server = self.server(EMPLOYEE, portal_granted=False, live_drive=True, clock=LATER)
        sent = self.transport.requests_sent
        found = self.ok(server, "plx.documents", query)
        self.assertEqual([doc["citation"] for doc in found["documents"]], [boundary])
        self.assertEqual(self.transport.requests_sent, sent)
        self.assertEqual(self.labels(found, "capture_too_old"), {})
        self.assertIn(boundary, self.labels(found))
        self.assertEqual(self.ok(server, "plx.read", {"citation": boundary})["status"],
                         "resolved")
        self.assertGreater(self.transport.requests_sent, sent)
        hidden = self.ok(self.server(OUTSIDER, portal_granted=False, live_drive=True,
                                     clock=LATER), "plx.documents", query)
        self.assertEqual(hidden["documents"], [])

class FileClearanceTests(SharePointHostCase):
    """The Portal's file clearance (portal/src/lib/auth/file-clearance.ts@790256c) on
    every SharePoint read path of the host: release.txt carries a Finance
    DocumentCategory (TAX_FORM), open.txt none, and a Staff Scratch file belongs to
    Portal user ``cmuser0000001``. All three grant the reader's item ACL, so only the
    security class decides. The column snapshot is written into the capture directly,
    as the capture holds it once it reads listItem columns."""

    scanned = False
    SCRATCH = "01SCRATCH"
    QC_DOC = "01QCDOC"   # a BATCH COA (access.ts@1d3eaf2:88-93): admins and QC staff only
    ITEMS = ("01FILEB", "01OPEN", SCRATCH, QC_DOC)

    def seed(self):
        drive = seeded_drive()
        drive.file("01OPEN", "open.txt", "01LOTS", b"Open note for PO100234",
                   mime="text/plain", acl=(RICARDO, SALES))
        drive.folder("01BENCH", "Staff Scratch")
        drive.folder("01OWNER", "cmuser0000001", parent="01BENCH")
        drive.folder("01YEAR", "2026", parent="01OWNER")
        drive.file(self.SCRATCH, "bench.txt", "01YEAR", b"Bench note for PO100234",
                   mime="text/plain", acl=(RICARDO, SALES))
        drive.folder("01BATCHES", "Batches", parent="01AOS")
        drive.file(self.QC_DOC, "coa.txt", "01BATCHES", b"Batch COA for PO100234",
                   mime="text/plain", acl=(RICARDO, SALES))
        return drive

    def capture_scope(self):
        return scope(folders=[QUALITY_FOLDER, "/Staff Scratch"])

    def setUp(self):
        super().setUp()
        with self.store.db:
            for row_id, raw, item in self.store.db.execute(
                    "SELECT rowid, record, item_id FROM items WHERE item_id IN (?, ?)",
                    ("01FILEB", self.QC_DOC)).fetchall():
                record = json.loads(raw)
                record["list_item_fields"] = {
                    "source": "sharepoint-listitem-fields/v1", "drive_etag": record["etag"],
                    "fields": {"DocumentCategory": "TAX_FORM" if item == "01FILEB" else "coa"}}
                self.store.db.execute("UPDATE items SET record = ? WHERE rowid = ?",
                                      (json.dumps(record), row_id))

    def employee(self, role: str, department: str | None, user_id: str = "cmuser0000001",
                 portal_codes: frozenset[str] | None = None) -> MCPServer:
        """``portal_codes``: the reader's Portal memberships (default: the Portal confirms
        the department Entra names)."""
        if portal_codes is None:
            code = PORTAL_DEPARTMENT_CODES.get((department or "").casefold())
            portal_codes = frozenset({code} if code else ())
        directory = None if department is None else (
            lambda principal: EntraDirectoryRead(
                "read", OID, department=department, group_ids=frozenset({QA_GROUP}),
                source="graph:directory@fixture", read_at="2026-10-01T09:00:00Z"))
        groups = {} if directory is not None else {"groups": Calls(member(QA_GROUP))}
        resolver = EmployeePermissionResolver(
            EmployeePermissionPolicy.from_json({**POLICY, "principals": {
                "staff:quality": POLICY["principals"]["staff:quality"]}}),
            Calls(portal_user(role, email=SALES, user_id=user_id,
                              clearance_departments=portal_codes)), directory=directory,
            **groups)
        grants = ["staff:quality"]
        audience = LiveSourceAudience(
            {f"sharepoint:{DRIVE}:{QUALITY_FOLDER}": grants,
             f"sharepoint:{DRIVE}:{QUALITY_FOLDER}/*": grants,
             f"sharepoint:{DRIVE}:/Staff Scratch": grants,
             f"sharepoint:{DRIVE}:/Staff Scratch/*": grants},
            tenant="plx", identities={"staff:quality": [RICARDO]},
            default_classification="internal", source_acl_required=[f"sharepoint:{DRIVE}:*"])
        return self.server(Actor("plx", frozenset({"employee:" + OID})), resolver=resolver,
                           audience=audience)

    def served(self, server: MCPServer) -> set[str]:
        """Every captured file this reader is served, asked every way the host answers."""
        seen: set[str] = set()
        documents = self.ok(server, "plx.documents", {"query": "PO100234",
                                                      "sources": ["sharepoint"]})
        seen |= {d["citation"] for d in documents["documents"]}
        found = self.ok(server, "plx.find", {"query": "txt", "object_type": "SharePointFile"})
        text = json.dumps(found["candidates"])
        seen |= {self.handle(i) for i in self.ITEMS
                 if self.handle(i) in text}
        for item in self.ITEMS:
            read = self.ok(server, "plx.read", {"citation": self.handle(item)})
            if read["status"] == "resolved":
                seen.add(self.handle(item))
        if "set" in found:
            counted = self.ok(server, "plx.aggregate", {"set": found["set"],
                                                        "measure": {"op": "count"}})
            self.assertEqual(sum(int(g["value"]) for g in counted["groups"]),
                             len([c for c in found["candidates"]]))
        po = self.ok(server, "plx.find", {"query": "PO100234", "object_type": "PurchaseOrder"})
        (candidate,) = po["candidates"]
        oid = candidate["object"]["object_id"]
        linked = self.ok(server, "plx.object", {"object_id": oid})
        for count in linked["linked_counts"]:
            if count["direction"] != "reverse" or not count["count"]:
                continue
            # The link count is the files this reader is served, never more.
            self.assertEqual(count["count"], len(seen & {
                self.handle(i) for i in self.ITEMS}))
            around = self.ok(server, "plx.around", {
                "from": oid, "tier_floor": "lead",
                "path": [{"link_type": count["link_type"], "direction": "reverse"}]})
            text = json.dumps(around)
            seen |= {self.handle(i) for i in self.ITEMS
                     if self.handle(i) in text}
        return seen

    def assert_no_confident_absence(self, server: MCPServer) -> None:
        found = self.ok(server, "plx.find", {"query": "release",
                                             "object_type": "SharePointFile"})
        self.assertEqual(found["candidates"], [])
        # "absence from the source is not established", never absent_in_source.
        reasons = {g["reason"] for g in found["gaps"]}
        self.assertIn("no_visible_match", reasons)
        self.assertNotIn("absent_in_source", reasons)
        self.assertNotIn("release.txt", json.dumps(found))

    def test_a_finance_reader_is_served_a_finance_category_file(self):
        served = self.served(self.employee("STAFF", "Finance"))
        self.assertEqual(served, {self.handle(i) for i in ("01FILEB", "01OPEN", self.SCRATCH)})

    def test_an_admin_outside_finance_and_an_unknown_department_are_refused(self):
        # The last reader: Entra still says Finance, the Portal membership was removed.
        for role, department, codes in (("ADMIN", "Operations", None), ("STAFF", "", None),
                                        ("STAFF", None, None), ("STAFF", "Finance", frozenset())):
            with self.subTest(role=role, department=department, codes=codes):
                server = self.employee(role, department, portal_codes=codes)
                admin = {self.handle(self.QC_DOC)} if role == "ADMIN" else set()
                self.assertEqual(self.served(server),
                                 {self.handle(i) for i in ("01OPEN", self.SCRATCH)} | admin)
                self.assert_no_confident_absence(server)

    def test_a_staff_scratch_file_is_served_to_its_owner_only(self):
        served = self.served(self.employee("SUPER_ADMIN", "Executive", "cmuser0000002"))
        self.assertEqual(served, {self.handle(i) for i in ("01FILEB", "01OPEN", self.QC_DOC)})

    def test_a_batch_qc_document_is_served_to_admins_and_quality_staff_only(self):
        # access.ts@1d3eaf2:88-93: the BATCH COA under Batches/ reaches an admin or a
        # Portal-confirmed QUALITY/QA/QC member only, on every read path.
        files = {self.handle(i) for i in ("01FILEB", "01OPEN", self.SCRATCH)}
        qc = self.handle(self.QC_DOC)
        for role, department, codes, expected in (
                ("ADMIN", "Operations", None, files - {self.handle("01FILEB")} | {qc}),
                ("STAFF", "Quality Control", None, files - {self.handle("01FILEB")} | {qc}),
                ("MANAGER", "Operations", None, files - {self.handle("01FILEB")}),
                ("STAFF", "Quality", frozenset(), files - {self.handle("01FILEB")}),
                ("STAFF", None, frozenset({"QC"}), files - {self.handle("01FILEB")})):
            with self.subTest(role=role, department=department, codes=codes):
                self.assertEqual(self.served(self.employee(role, department,
                                                           portal_codes=codes)), expected)


class LiveCheckModuleTests(unittest.TestCase):
    """``operating.sharepoint_host_check`` (the case.sharepoint_documents live check) over
    the in-process FakeDrive: counts only, and its spec's expectations hold."""

    def test_the_host_check_answers_by_page_and_leaks_nothing(self):
        reason = ocr_skip_reason()
        if reason:
            self.skipTest(reason)
        # A scanned packet named by the identifier its page names (the AO packets'
        # shape): the projection lists it by name with no object to link the mention
        # to, and the answer must still cite the captured page.
        for name in ("packet-scan.pdf", f"packet {SCAN_PO}.pdf"):
            with self.subTest(name=name):
                self._check_answers_by_page(name)

    def test_files_past_the_old_scan_bound_ahead_of_the_packet_still_answer_by_page(self):
        reason = ocr_skip_reason()
        if reason:
            self.skipTest(reason)
        # The live capture held 565 files and a per-call scan read only the first 500
        # in item order, so the AO packet after them was never searched.
        self._check_answers_by_page("packet-scan.pdf", ahead=565)

    def test_an_unanswered_question_is_counted_under_its_reason(self):
        reason = ocr_skip_reason()
        if reason:
            self.skipTest(reason)
        # The daily run of 2026-10-04: the projection listed the packet by the identifier
        # in its name, and plx.documents served that mention, not the captured page.
        unpaged = mock.patch.object(CaptureCaseHostTools, "_captured_documents",
                                    lambda host, arguments, actor, result: result)
        # A live re-read that refuses every captured file.
        refused = mock.patch.object(CaptureCaseHostTools, "_live_check",
                                    lambda host, gated, view: None)
        # The identifier lookup cut before the packet (at its file or entry bound): the
        # answer is cut, never an absence.
        from operating import case_tools_host
        files_cut = mock.patch.object(case_tools_host, "LOOKUP_FILES", 1)
        entries_cut = mock.patch.object(case_tools_host, "LOOKUP_ENTRIES", 0)
        # Two questions: the file name carries the first one's identifier only.
        for name, patch, expected in (
                (f"packet {SCAN_PO}.pdf", unpaged, {"unpaged_mention": 1, "no_document": 1}),
                ("packet-scan.pdf", unpaged, {"no_document": 2}),
                ("packet-scan.pdf", refused, {"withheld": 2}),
                ("packet-scan.pdf", files_cut, {"scan_cap": 2}),
                ("packet-scan.pdf", entries_cut, {"scan_cap": 2})):
            with self.subTest(expected=expected), patch:
                report = self._run_check(name)
                self.assertFalse(report["ok"])
                self.assertEqual((report["questions"], report["answered_with_page_citation"]),
                                 (2, 0))
                self.assertEqual(report["unanswered"], {
                    "no_document": 0, "scan_cap": 0, "unpaged_mention": 0, "withheld": 0,
                    **expected})

    def _run_check(self, name: str, ahead: int = 0) -> dict:
        import argparse
        from operating import sharepoint_host_check as check
        drive = seeded_drive()
        for at in range(ahead):   # before the packet in the store's item order
            drive.file(f"00NOTE{at:04d}", f"note {at}.txt", "01LOTS", f"Note {at}".encode(),
                       mime="text/plain", acl=(RICARDO,))
        # Page 3 names a purchase order by its label alone: asked as written ("PO: 5820"),
        # since the bare value names no identifier.
        drive.file(SCAN, name, "01LOTS",
                   scanned_pdf(["Assembly packet scan", f"Purchase order {SCAN_PO}",
                                "PO: 5820"]),
                   acl=(RICARDO, SALES))
        self.drive = drive
        with tempfile.TemporaryDirectory() as tmp:
            scope_file = Path(tmp) / "scope.json"
            scope_file.write_text(json.dumps({"schema": cs.SCOPE_SCHEMA, "targets": [
                {"id": "quality", "drive_id": DRIVE, "folders": ["/Quality/AOs"]}]}))
            audience_file = Path(tmp) / "audience.json"
            audience_file.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": [cs.CAPTURE_PRINCIPAL]}}))
            return check.run(argparse.Namespace(
                state_dir=str(Path(tmp) / "state"), scope=str(scope_file),
                live_audience=str(audience_file), principal=cs.CAPTURE_PRINCIPAL),
                environ=ENV, sender=drive, sleep=lambda s: None)

    def _check_answers_by_page(self, name: str, ahead: int = 0):
        from devloop import live_verify as lv
        report = self._run_check(name, ahead)
        drive = self.drive
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        # The identifiers captured pages name: the scanned PDF's pages 2 and 3 (the
        # unpaged text file is no page question).
        self.assertEqual((report["questions"], report["answered_with_page_citation"],
                          report["labelled_as_of_capture"]), (2, 2, 2))
        self.assertEqual(set(report["unanswered"].values()), {0})
        self.assertEqual(report["capture"]["texts_ocr"], 1)
        self.assertEqual(report["leak_check"]["denied_documents"], 0)
        # requests_sent counts the host's live re-reads too, not the capture alone.
        self.assertGreater(report["host_requests_sent"], 0)
        # host_gets_sent counts the re-read GETs alone, never the token POST.
        self.assertGreater(report["host_gets_sent"], 0)
        self.assertLess(report["host_gets_sent"], report["host_requests_sent"])
        self.assertEqual(report["requests_sent"],
                         report["capture_requests_sent"] + report["host_requests_sent"])
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        text = json.dumps(report)
        for secret in (SCAN_PO, "PO100234", "5820", NAME_SECRET, "packet", DRIVE, "01FILE",
                       "/Quality", "s3cr3t", "TOKEN"):
            self.assertNotIn(secret, text)
        root = Path(__file__).resolve().parents[2]
        spec = json.loads((root / "devloop/live-checks/case.sharepoint_documents.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="case.sharepoint_documents")
        self.assertEqual(lv.check_expectations(spec, 0, report), [])


if __name__ == "__main__":
    unittest.main()
