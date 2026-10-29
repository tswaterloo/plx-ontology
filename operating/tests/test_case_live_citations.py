"""Live-source citations in cases: graph-mail://, sharepoint:// and bc:// (injected transport).

Evidence label: ``injected-transport``. The real GET-only readers
(``integrations.graph_read`` / ``integrations.bc_read``) run against the
in-process ``FakeSender`` of the adapter tests; nothing touches the network and
no credential is real. Every case tool is driven through ``MCPServer.handle``.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import quote, urlsplit

from integrations.document_understanding import ExtractedDocument
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_live_source_adapters import (
    COMPANY, DRIVE, ENV, MSG_PATH, PO_ID, SP_HOST, TENANT_ID, VINCE_ID, FakeSender, drive_item,
    j, message)
from ontology.core import Actor
from operating.case_runtime import (CaseRuntime, LiveReaders, LiveSourceNotConfigured,
                                    build_live_readers)
from operating import source_reads
from operating.mcp_server import HOST_READS, MCPServer, source_read
from operating.tests.firewall_view import client_result

MAILBOX = "vince@petralabx.com"
QUALITY = frozenset({"staff:quality", "staff:shared"})
VINCE = frozenset({"staff:vince", "staff:shared"})
SHARED = frozenset({"staff:shared"})
QDRIVE = quote(DRIVE, safe="")  # citations percent-encode the drive id  # in every case audience, granted no live source
AO_PDF = b"%PDF-1.4\n% synthetic AO-26005B scan\n" + b"x" * 200
BODY = "Hello team,\nConfirmed PO5447: 50,000 pcs, ship 25 Oct.\n"
BC_BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"
SP_BASE = f"/v1.0/drives/{DRIVE}"
DOWNLOAD = f"https://{SP_HOST}/sites/QCQA/_layouts/15/download.aspx?tempauth=DL-do-not-leak"
MAIL_METADATA = f"graph-mail://{VINCE_ID}/m1@ck1"
LIVE_AUDIENCE = {f"mail:{MAILBOX}": ["staff:vince"],
                 f"sharepoint:{DRIVE}:/AOs/*": ["staff:quality"],
                 "bc:production:*": ["staff:vince"]}


def _extractor(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
    text = "AO-26005B\nPLANNED 1509"
    return ExtractedDocument("application/pdf", "x", "extracted", "stub-ocr", text, (), (), (),
                             ({"page": 1, "start": 0, "end": len(text)},), ocr_pages=(1,))


class LiveCitationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state"
        self.mirror_audience = self.root / "mirror-audience.json"
        self.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.*": ["staff:shared"]}}), encoding="utf-8")
        self.message = dict(message("m1", "ck1"), body={"contentType": "text", "content": BODY})
        self.item = drive_item("AO1", "AO-26005B.pdf", folder="/AOs/2026 - AOs", content=AO_PDF,
                               etag="\"{AO1},1\"")
        self.po = {"@odata.etag": "W/\"JzE5OzEyMzQ1Njc4OTA7MDA7Jw==\"", "id": PO_ID,
                   "number": "PO5447", "totalAmountIncludingTax": 1234.10, "lineCount": 3,
                   "lastModifiedDateTime": "2026-09-28T08:00:00Z"}
        self.sender = FakeSender({
            f"{MSG_PATH}/m1": lambda url: j(200, self.message),
            MSG_PATH: lambda url: j(200, {"value": [self.message]}),
            f"{SP_BASE}/items/AO1": lambda url: j(200, dict(self.item, **(
                {"@microsoft.graph.downloadUrl": DOWNLOAD} if "downloadUrl" in url else {}))),
            f"{SP_BASE}/root:/AOs/2026 - AOs:": j(200, drive_item(
                "FOLDER-AO", "2026 - AOs", folder="/AOs", is_folder=True)),
            f"{SP_BASE}/items/FOLDER-AO/children": lambda url: j(200, {"value": [
                dict(self.item, parentReference=dict(self.item["parentReference"],
                                                     id="FOLDER-AO"))]}),
            f"{SP_HOST}/sites/QCQA/_layouts/15/download.aspx": lambda url: (200, {}, AO_PDF),
            f"{BC_BASE}/companies": j(200, {"value": [
                {"id": COMPANY, "name": "PETRA", "@odata.etag": "W/\"1\""}]}),
            f"{BC_BASE}/companies({COMPANY})/purchaseOrders": lambda url: (
                200, {}, json.dumps({"value": [self.po]}).encode()),
            f"{BC_BASE}/companies({COMPANY})/purchaseOrders({PO_ID})": lambda url: (
                200, {}, json.dumps(self.po).encode()),
        })
        self.request_id = 10

    def live(self, audiences: dict | None = None, **kwargs) -> LiveReaders:
        audience = LiveSourceAudience(LIVE_AUDIENCE if audiences is None else audiences,
                                      tenant="plx", **kwargs)
        return build_live_readers(audience, ENV, graph_sender=self.sender,
                                  bc_sender=self.sender)

    def runtime(self, live: LiveReaders | None = None) -> CaseRuntime:
        runtime = CaseRuntime(self.state, client=None, audience=self.mirror_audience,
                              tenant="plx", originals_options={"extractor": _extractor},
                              live=live)
        self.addCleanup(runtime.close)
        return runtime

    def server(self, runtime: CaseRuntime, principals: frozenset[str]) -> MCPServer:
        server = MCPServer(case_runtime=runtime,
                           actor=Actor("plx", principals, clearance="restricted"))
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "live-case-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server: MCPServer, name: str, arguments: dict | None = None) -> dict:
        self.request_id += 1
        # A host read (the drivers' and evaluators' raw citation read) is host code; every
        # tool result goes through the output boundary.
        host = name in HOST_READS
        result = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": arguments or {}}},
                               host_code=host)["result"]
        return result if host else client_result(name, result)

    def ok(self, server: MCPServer, name: str, arguments: dict | None = None) -> dict:
        if name in source_reads.READS:  # a driver's direct read, not a host tool
            return source_read(server, name, arguments or {})
        result = self.call(server, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def refused(self, server: MCPServer, name: str, arguments: dict | None = None) -> str:
        if name in source_reads.READS:
            with self.assertRaises(RuntimeError) as caught:
                source_read(server, name, arguments or {})
            return str(caught.exception).split(" refused: ", 1)[1]
        result = self.call(server, name, arguments)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def cite(self, server: MCPServer, case_id: str) -> dict:
        """Cite message m1 as the retired plx.case_cite_mail did: plx.read, then a finding
        citing its body-pinned cite_as. Returns the read."""
        read = self.ok(server, "plx.read", {"citation": MAIL_METADATA})
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "The supplier confirmed the order by mail.",
            "kind": "fact", "status": "supported", "citations": [read["cite_as"]["citation"]]})
        return read

    def open_case(self, server: MCPServer) -> str:
        return self.ok(server, "plx.case_open", {"question": "AO-26005B / PO5447",
                                                 "audience": ["staff:shared"]})["case_id"]

    def source_calls(self) -> list[tuple[str, str]]:
        return [(c[0], c[1]) for c in self.sender.calls
                if "login.microsoftonline.com" not in c[1]]

    def downloads(self) -> list[str]:
        return [c[1] for c in self.sender.calls if urlsplit(c[1]).hostname == SP_HOST]


class NotConfiguredTests(LiveCitationTestCase):
    def test_keyless_host_builds_no_reader(self):
        live = build_live_readers(LiveSourceAudience(LIVE_AUDIENCE, tenant="plx"), {})
        self.assertEqual(live.configured(), {"graph-mail": False, "sharepoint_drives": [],
                                             "bc": False})

    def test_every_live_scheme_refuses_with_source_not_configured(self):
        for live in (None, build_live_readers(LiveSourceAudience(LIVE_AUDIENCE, tenant="plx"),
                                              {})):
            runtime = self.runtime(live)
            server = self.server(runtime, VINCE)
            case_id = self.open_case(server)
            for handle in (f"graph-mail://{VINCE_ID}/m1@ck1",
                           f"sharepoint://{DRIVE}/AO1@x", f"bc://production/{COMPANY}/"
                           f"purchaseOrders/{PO_ID}@x"):
                text = self.refused(server, "plx.case_add_finding", {
                    "case_id": case_id, "statement": "s", "kind": "fact",
                    "status": "supported", "citations": [handle]})
                self.assertIn("source_not_configured", text)
                status = self.ok(server, "plx.source_citation", {"citation": handle})
                self.assertEqual((status["ok"], status["error"]),
                                 (False, "source_not_configured"))
                with self.assertRaises(LiveSourceNotConfigured):
                    runtime.resolve_citation(handle, Actor("plx", VINCE))
            read = self.ok(server, "plx.read", {"citation": f"graph-mail://{VINCE_ID}/m1@ck1"})
            self.assertEqual(read["status"], "unresolvable")
            self.assertIn("source_not_configured", {gap["reason"] for gap in read["gaps"]})
            self.assertIn("source_not_configured", self.refused(
                server, "plx.case_attach_original",
                {"case_id": case_id, "drive_id": DRIVE, "item_id": "AO1"}))
            for source, extra in (("mail", {"mailbox": MAILBOX}),
                                  ("sharepoint", {"drive_id": DRIVE}), ("bc", {})):
                self.assertIn("source_not_configured", self.refused(
                    server, "live.list", {"source": source, **extra}))
        self.assertEqual(self.sender.calls, [])

    def test_unconfigured_drive_is_not_configured(self):
        server = self.server(self.runtime(self.live()), QUALITY)
        case_id = self.open_case(server)
        self.assertIn("source_not_configured", self.refused(
            server, "plx.case_attach_original",
            {"case_id": case_id, "drive_id": "b!other-drive", "item_id": "AO1"}))
        self.assertEqual(self.downloads(), [])


class MailCitationTests(LiveCitationTestCase):
    """A mail message enters a case through plx.read (its body-pinned cite_as) and
    plx.case_add_finding: the parity of the retired plx.case_cite_mail."""

    METADATA = MAIL_METADATA

    def test_read_gives_a_body_pinned_citation_and_the_case_keeps_only_its_hash(self):
        runtime = self.runtime(self.live())
        server = self.server(runtime, VINCE)
        case_id = self.open_case(server)
        read = self.cite(server, case_id)
        digest = sha256(BODY.encode()).hexdigest()
        pinned = read["cite_as"]["citation"]
        self.assertTrue(pinned.startswith(
            f"{self.METADATA}#body_sha256={digest}&shown_sha256="))
        self.assertEqual((read["status"], read["content"]["untrusted_text"]), ("resolved", BODY))
        self.assertEqual(read["content"]["origin"]["citation"], self.METADATA)
        self.assertNotIn("row.source_acl", read["fields"])
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        [finding] = view["findings"]["supported"]
        # The digest the case recorded is the one plx.read gave for cite_as (body pinned).
        self.assertEqual(finding["citations"][0]["digest"], read["cite_as"]["digest"])
        self.assertEqual(finding["citations"][0]["mirror"]["status"], "current")
        # The case record holds the hash, never the body text.
        stored = b"".join(path.read_bytes() for path in self.state.rglob("*.sqlite*"))
        self.assertNotIn(b"Confirmed PO5447", stored)
        # The host read of the pinned citation returns metadata and the hash, not the body.
        status = self.ok(server, "plx.source_citation", {"citation": pinned})
        self.assertEqual((status["status"], status["body_sha256"]), ("current", digest))
        self.assertNotIn("body_text", status["record"])
        # A pinned citation reads as itself: no second pin is offered.
        again = self.ok(server, "plx.read", {"citation": pinned})
        self.assertEqual((again["status"], again["digest"]),
                         ("resolved", read["cite_as"]["digest"]))
        self.assertNotIn("cite_as", again)
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))

    def test_reading_actor_without_mail_audience_sees_it_withheld(self):
        runtime = self.runtime(self.live())
        case_id = self.open_case(self.server(runtime, VINCE))
        pinned = self.cite(self.server(runtime, VINCE), case_id)["cite_as"]["citation"]
        other = self.server(runtime, SHARED)
        view = self.ok(other, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        [withheld] = view["findings"]["withheld"]
        self.assertIsNone(withheld["statement"])
        self.assertEqual(self.ok(other, "plx.source_citation", {"citation": pinned}),
                         {"ok": False, "error": "citation_unresolvable", "citation": pinned})
        for handle in (self.METADATA, pinned):
            read = self.ok(other, "plx.read", {"citation": handle})
            self.assertEqual(read["status"], "withheld")
            self.assertNotIn("content", read)
            self.assertNotIn("cite_as", read)
        self.assertEqual(self.refused(other, "plx.case_add_finding", {
            "case_id": case_id, "statement": "s", "kind": "fact", "status": "supported",
            "citations": [pinned]}), "citation_unresolvable_for_actor")
        self.assertEqual(self.refused(other, "live.list",
                                      {"source": "mail", "mailbox": MAILBOX}),
                         "live_source_not_readable_for_actor")

    def test_changed_message_is_never_support(self):
        runtime = self.runtime(self.live())
        server = self.server(runtime, VINCE)
        case_id = self.open_case(server)
        listed = self.ok(server, "live.list", {"source": "mail", "mailbox": MAILBOX})
        metadata = listed["items"][0]["citation"]
        self.assertEqual(metadata, self.METADATA)
        cited = self.cite(server, case_id)["cite_as"]["citation"]
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "Supplier confirmed PO5447", "kind": "fact",
            "status": "supported", "citations": [metadata]})
        # Same changeKey, different body: the body-pinned citation changed.
        self.message["body"] = {"contentType": "text", "content": BODY + "PS: price +2%"}
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual([f["id"] for f in view["findings"]["supported"]], ["F2"])
        [changed] = view["findings"]["citation_content_changed"]
        self.assertEqual((changed["id"], changed["status"], changed["recorded_status"]),
                         ("F1", None, "supported"))
        self.assertTrue(changed["citations"][0]["mirror"]["changed_since"])
        # plx.read now pins the new body: another citation, never the one cited.
        repinned = self.ok(server, "plx.read", {"citation": metadata})["cite_as"]
        self.assertNotEqual(repinned["citation"], cited)
        self.assertIn(sha256((BODY + "PS: price +2%").encode()).hexdigest(),
                      repinned["citation"])
        # A new changeKey: the metadata citation changed too; neither is support now.
        self.message = dict(self.message, changeKey="ck2")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["citation_content_changed"]), 2)
        for handle in (cited, metadata):
            self.assertIn("citation_changed_since", self.refused(server, "plx.case_add_finding", {
                "case_id": case_id, "statement": "again", "kind": "fact",
                "status": "supported", "citations": [handle]}))
        # Recording that the source moved on (not as support) is allowed.
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "The message changed after it was cited",
            "kind": "fact", "status": "unknown", "citations": [metadata]})
        current = self.ok(server, "plx.source_citation", {"citation": metadata})
        self.assertEqual((current["status"], current["current_citation"]),
                         ("changed_since", f"graph-mail://{VINCE_ID}/m1@ck2"))
        read = self.ok(server, "plx.read", {"citation": metadata})
        self.assertEqual((read["status"], read["current_citation"]),
                         ("changed_since", current["current_citation"]))
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "Current version", "kind": "fact",
            "status": "supported", "citations": [current["current_citation"]]})


class SharePointOriginalTests(LiveCitationTestCase):
    def attach(self, runtime: CaseRuntime, **arguments) -> tuple[MCPServer, str, dict]:
        server = self.server(runtime, QUALITY)
        case_id = self.open_case(server)
        result = self.ok(server, "plx.case_attach_original",
                         {"case_id": case_id, "drive_id": DRIVE, "item_id": "AO1", **arguments})
        return server, case_id, result

    def test_ao_pdf_becomes_an_original_with_byte_identity_and_provenance(self):
        runtime = self.runtime(self.live())
        server, case_id, result = self.attach(runtime)
        original = result["original"]
        self.assertEqual(original["sha256"], sha256(AO_PDF).hexdigest())
        self.assertEqual(original["bytes"], len(AO_PDF))
        provenance = original["provenance"]
        self.assertEqual((provenance["drive_id"], provenance["item_id"], provenance["etag"],
                          provenance["ctag"], provenance["path"]),
                         (DRIVE, "AO1", "\"{AO1},1\"", "\"c:{E1},1\"",
                          "/AOs/2026 - AOs/AO-26005B.pdf"))
        self.assertTrue(provenance["source_citation"].endswith(
            "#sha256=" + sha256(AO_PDF).hexdigest()))
        # Status and grant count only; never the grantee list.
        self.assertNotIn("source_acl", provenance)
        self.assertIn("source_acl_status", provenance)
        self.assertNotIn("audience_keys", provenance)
        stored = runtime.originals.store / original["sha256"]
        self.assertEqual(stored.read_bytes(), AO_PDF)
        self.assertEqual(len(self.downloads()), 1)
        self.assertNotIn("tempauth", json.dumps(result))
        # The existing original pipeline: page citations with OCR text.
        page = self.ok(server, "plx.source_citation", {"citation": original["citation"] + "#page=1"})
        self.assertEqual((page["text"], page["text_source"]), ("AO-26005B\nPLANNED 1509", "ocr"))
        self.assertEqual(page["provenance"]["item_id"], "AO1")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        [finding] = view["findings"]["supported"]
        self.assertEqual([c["handle"] for c in finding["citations"]],
                         [original["citation"], f"sharepoint://{QDRIVE}/AO1@%22%7BAO1%7D%2C1%22"])
        # Attaching the same item version again is the same original.
        again = self.ok(server, "plx.case_attach_original",
                        {"case_id": case_id, "drive_id": DRIVE, "item_id": "AO1"})
        self.assertEqual((again["original"]["original_id"], again["original"]["duplicate"]),
                         (original["original_id"], True))
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))

    def test_other_actor_cannot_attach_or_read_the_original(self):
        runtime = self.runtime(self.live())
        _server, case_id, result = self.attach(runtime)
        handle = result["original"]["citation"]
        other = self.server(runtime, SHARED)
        self.assertIn("sharepoint_item_unavailable", self.refused(
            other, "plx.case_attach_original",
            {"case_id": case_id, "drive_id": DRIVE, "item_id": "AO1"}))
        self.assertEqual(len(self.downloads()), 1)  # the denied attach downloaded nothing
        self.assertFalse(self.ok(other, "plx.source_citation", {"citation": handle})["ok"])
        view = self.ok(other, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["withheld"]), 1)

    def test_audience_revocation_and_unconfigured_host_deny_the_stored_original(self):
        runtime = self.runtime(self.live())
        _server, _case_id, result = self.attach(runtime)
        handle = result["original"]["citation"]
        actor = Actor("plx", QUALITY, clearance="restricted")
        self.assertEqual(runtime.resolve_citation(handle, actor)["sha256"],
                         sha256(AO_PDF).hexdigest())
        revoked = self.runtime(self.live({f"mail:{MAILBOX}": ["staff:vince"],
                                          f"sharepoint:{DRIVE}:/AOs/*": ["staff:vince"]}))
        self.assertFalse(revoked.read_original(handle, actor)["ok"])
        self.assertEqual(revoked.originals.list_visible(actor), [])
        keyless = self.runtime(None)
        self.assertFalse(keyless.read_original(handle, actor)["ok"])

    def test_byte_cap_refuses_before_download(self):
        runtime = self.runtime(self.live())
        server = self.server(runtime, QUALITY)
        case_id = self.open_case(server)
        self.assertIn("original_too_large", self.refused(
            server, "plx.case_attach_original",
            {"case_id": case_id, "drive_id": DRIVE, "item_id": "AO1", "max_bytes": 64}))
        self.assertEqual(self.downloads(), [])
        self.assertEqual(runtime.originals.list_visible(Actor("plx", QUALITY, "restricted")), [])

    def test_changed_item_is_content_changed_while_bytes_stay_verifiable(self):
        runtime = self.runtime(self.live())
        server, case_id, result = self.attach(runtime)
        self.item = dict(self.item, eTag="\"{AO1},2\"")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        [changed] = view["findings"]["citation_content_changed"]
        statuses = [c["status"] for c in changed["citations"]]
        self.assertEqual(statuses, ["resolved", "citation_content_changed"])
        original = self.ok(server, "plx.source_citation",
                           {"citation": result["original"]["citation"]})
        self.assertTrue(original["ok"])

    def test_listing_the_ao_folder_gives_citable_items(self):
        server = self.server(self.runtime(self.live()), QUALITY)
        listed = self.ok(server, "live.list", {
            "source": "sharepoint", "drive_id": DRIVE, "path": "/AOs/2026 - AOs"})
        [item] = listed["items"]
        self.assertEqual((item["id"], item["path"]), ("AO1", "/AOs/2026 - AOs/AO-26005B.pdf"))
        self.assertTrue(item["citation"].startswith(f"sharepoint://{QDRIVE}/AO1@"))
        self.assertTrue(listed["completeness"]["complete"])


class MailPinTests(LiveCitationTestCase):
    def test_change_key_drift_alone_keeps_a_pinned_citation_supported(self):
        runtime = self.runtime(self.live())
        server = self.server(runtime, VINCE)
        case_id = self.open_case(server)
        cited = self.cite(server, case_id)["cite_as"]["citation"]
        # Read / flagged: Exchange bumps the changeKey; body and shown fields unchanged.
        self.message = dict(self.message, changeKey="ck-read", isRead=True,
                            lastModifiedDateTime="2026-10-01T08:00:00Z")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(len(view["findings"]["supported"]), 1)
        status = self.ok(server, "plx.source_citation", {"citation": cited})
        self.assertEqual((status["status"], status["change_key_changed"]), ("current", True))
        # A shown field (subject) changes: no longer support.
        self.message = dict(self.message, subject="Customer PO: PO99999")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["citation_content_changed"]), 1)

    def test_mail_listing_is_one_graph_page(self):
        server = self.server(self.runtime(self.live()), VINCE)
        self.ok(server, "live.list", {"source": "mail", "mailbox": MAILBOX,
                                                 "limit": 200})
        pages = [url for _, url in self.source_calls() if urlsplit(url).path == MSG_PATH]
        self.assertEqual(len(pages), 1)
        self.assertIn("%24top=50", pages[0])


class SharePointOriginalAccessTests(LiveCitationTestCase):
    """The stored original follows the item's current access, read live each time."""

    PERMS = {"value": [{"roles": ["read"], "grantedToV2": {"user": {
        "email": "ricardo@petralabx.com", "id": "r-1"}}}]}

    def setUp(self) -> None:
        super().setUp()
        self.perms = self.PERMS
        self.sender.routes[f"{SP_BASE}/items/AO1/permissions"] = lambda url: j(200, self.perms)

    def live_acl(self) -> LiveReaders:
        return self.live({**LIVE_AUDIENCE, f"sharepoint:{DRIVE}:/AOs/Restricted/*": []},
                         identities={"staff:quality": ["ricardo@petralabx.com"]},
                         source_acl_required=[f"sharepoint:{DRIVE}:*"])

    def attach(self) -> tuple[CaseRuntime, MCPServer, str, str]:
        runtime = self.runtime(self.live_acl())
        server = self.server(runtime, QUALITY)
        case_id = self.open_case(server)
        original = self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "drive_id": DRIVE, "item_id": "AO1"})["original"]
        self.assertEqual(original["provenance"]["source_acl_status"], "read")
        shown = json.dumps(original["provenance"])
        self.assertNotIn("r-1", shown)  # the grantee's id: the permission list stays stored
        self.assertNotIn("grants", shown)
        self.assertEqual(original["provenance"]["source_acl_grant_count"], 1)
        page = self.ok(server, "plx.source_citation",
                       {"citation": original["citation"] + "#page=1"})
        self.assertIn("PLANNED", page["text"])
        return runtime, server, case_id, original["citation"]

    def assert_denied(self, runtime: CaseRuntime, server: MCPServer, case_id: str,
                      handle: str) -> None:
        actor = Actor("plx", QUALITY, clearance="restricted")
        self.assertFalse(self.ok(server, "plx.source_citation",
                                 {"citation": handle + "#page=1"})["ok"])
        self.assertFalse(runtime.read_original(handle, actor)["ok"])
        self.assertEqual(runtime.originals.list_visible(actor), [])
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["withheld"]), 1)
        # Re-extraction is gated the same way.
        self.assertIn("original_not_visible", self.refused(
            server, "plx.case_attach_original",
            {"case_id": case_id, "citation": handle, "reason": "retry"}))

    def test_upstream_permission_removal_denies_the_stored_original(self):
        runtime, server, case_id, handle = self.attach()
        self.perms = {"value": [{"roles": ["read"], "grantedToV2": {"user": {
            "email": "someone-else@petralabx.com", "id": "x-1"}}}]}
        self.assert_denied(runtime, server, case_id, handle)
        # Re-attaching while removed is refused too; nothing downloads.
        before = len(self.downloads())
        self.assertIn("sharepoint_item_unavailable", self.refused(
            server, "plx.case_attach_original",
            {"case_id": case_id, "drive_id": DRIVE, "item_id": "AO1"}))
        self.assertEqual(len(self.downloads()), before)
        # Access restored upstream: readable again (same stored bytes).
        self.perms = self.PERMS
        actor = Actor("plx", QUALITY, clearance="restricted")
        self.assertTrue(runtime.read_original(handle, actor)["ok"])

    def test_move_into_a_restricted_folder_denies_the_stored_original(self):
        runtime, server, case_id, handle = self.attach()
        self.item = dict(self.item, parentReference=dict(
            self.item["parentReference"], path=f"/drives/{DRIVE}/root:/AOs/Restricted"))
        self.assert_denied(runtime, server, case_id, handle)

    def test_source_down_denies_the_stored_original(self):
        runtime, server, case_id, handle = self.attach()
        self.sender.routes[f"{SP_BASE}/items/AO1"] = j(503, {"error": {"code": "down"}})
        self.assert_denied(runtime, server, case_id, handle)

    def test_listing_bounds_live_checks(self):
        runtime, _server, _case_id, _handle = self.attach()
        actor = Actor("plx", QUALITY, clearance="restricted")
        reads = lambda: sum(1 for _, url in self.source_calls()  # noqa: E731
                            if urlsplit(url).path == f"{SP_BASE}/items/AO1")
        before = reads()
        self.assertEqual(runtime.originals.list_visible(actor, max_source_checks=0), [])
        self.assertEqual(reads(), before)
        self.assertEqual(len(runtime.originals.list_visible(actor, max_source_checks=1)), 1)
        self.assertEqual(reads(), before + 1)


class BusinessCentralCitationTests(LiveCitationTestCase):
    def test_bc_record_cited_computed_and_changed(self):
        runtime = self.runtime(self.live())
        server = self.server(runtime, VINCE)
        case_id = self.open_case(server)
        companies = self.ok(server, "live.list", {"source": "bc"})
        self.assertEqual(companies["items"][0]["id"], COMPANY)
        listed = self.ok(server, "live.list", {
            "source": "bc", "company_id": COMPANY, "entity_set": "purchaseOrders",
            "equals_field": "number", "equals_value": "PO5447"})
        handle = listed["items"][0]["citation"]
        self.assertTrue(handle.startswith(f"bc://production/{COMPANY}/purchaseOrders/{PO_ID}@"))
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "BC PO5447 exists", "kind": "fact",
            "status": "supported", "citations": [handle]})
        calc = self.ok(server, "plx.case_compute", {
            "case_id": case_id, "expression": "total / lines", "unit": "USD",
            "inputs": {"total": {"citation": handle,
                                 "field": "record.fields.totalAmountIncludingTax",
                                 "unit": "USD"},
                       "lines": {"citation": handle, "field": "record.fields.lineCount",
                                 "unit": "1"}}})["calculation"]
        self.assertTrue(calc["calc_id"])
        # Reader without a BC grant: refused on write, withheld on read.
        other = self.server(runtime, SHARED)
        self.assertEqual(self.refused(other, "plx.case_add_finding", {
            "case_id": case_id, "statement": "x", "kind": "fact", "status": "supported",
            "citations": [handle]}), "citation_unresolvable_for_actor")
        other_view = self.ok(other, "plx.case_get", {"case_id": case_id})
        self.assertEqual(len(other_view["findings"]["withheld"]), 1)
        self.assertEqual(other_view["calculations"][0]["content"],
                         "withheld_citation_unavailable")
        # The record changes in BC: the finding and the calculation report it.
        self.po["@odata.etag"] = "W/\"JzE5Ozk5OTk7MDA7Jw==\""
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["citation_content_changed"]), 1)
        self.assertEqual(view["calculations"][0]["inputs_status"], "citation_content_changed")
        self.assertIn("citation_changed_since", self.refused(server, "plx.case_compute", {
            "case_id": case_id, "expression": "total", "unit": "USD",
            "inputs": {"total": {"citation": handle,
                                 "field": "record.fields.totalAmountIncludingTax",
                                 "unit": "USD"}}}))
        self.assertTrue(all(method == "GET" for method, _ in self.source_calls()))


class CaseDbHoldsNoLiveContentTests(LiveCitationTestCase):
    def test_case_events_hold_digests_not_live_records(self):
        runtime = self.runtime(self.live())
        server = self.server(runtime, VINCE)
        case_id = self.open_case(server)
        listed = self.ok(server, "live.list", {
            "source": "bc", "company_id": COMPANY, "entity_set": "purchaseOrders"})
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "cited", "kind": "fact", "status": "supported",
            "citations": [listed["items"][0]["citation"]]})
        db = sqlite3.connect(runtime.cases.path)
        payloads = " ".join(row[0] for row in db.execute("SELECT payload_json FROM case_event"))
        db.close()
        self.assertNotIn("1234.1", payloads)
        self.assertIn(listed["items"][0]["citation"], payloads)


class KeylessHostTests(LiveCitationTestCase):
    def test_stdio_case_host_with_live_audience_and_no_keys_refuses_typed(self):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[2]
        live = self.root / "live-audience.json"
        live.write_text(json.dumps({"schema": "plx-live-source-audience/v1", "tenant": "plx",
                                    "audiences": LIVE_AUDIENCE}), encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MICROSOFT_GRAPH_", "SHAREPOINT_", "BC_", "AZURE_"))}
        from operating.tests.test_session_identity import employee_host_args

        # A live host: one verified employee (no free-text principal).
        identity, ticket = employee_host_args(self.root / "identity", ["staff:vince"])
        env.update(ticket)
        command = [sys.executable, "-m", "operating.mcp_server", "--state-dir",
                   str(self.root / "host"), "--tenant", "plx", *identity,
                   "--source-mirror-audience", str(self.mirror_audience),
                   "--live-source-audience", str(live), "--sharepoint-drive", DRIVE]
        handle = f"bc://production/{COMPANY}/purchaseOrders/{PO_ID}@x"
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "host-test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "plx.read", "arguments": {"citation": handle}}}]
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60,
                              env=env,
                              input="".join(json.dumps(item) + "\n" for item in messages))
        self.assertEqual(done.returncode, 0, done.stderr)
        reply = [json.loads(line) for line in done.stdout.splitlines()][1]
        answer = reply["result"]["structuredContent"]
        self.assertEqual(answer["status"], "unresolvable")
        self.assertIn("source_not_configured", {gap["reason"] for gap in answer["gaps"]})
        # A start-up ticket is single use: the next start presents a fresh one.
        _identity, again = employee_host_args(self.root / "identity", ["staff:vince"])
        refused = subprocess.run(command[:-2], cwd=root, capture_output=True, text=True,
                                 timeout=60, env={**env, **again}, input="")
        self.assertEqual(refused.returncode, 0, refused.stderr)
        without_mirror = [arg for arg in command if arg != str(self.mirror_audience)
                          and arg != "--source-mirror-audience"]
        refused = subprocess.run(without_mirror, cwd=root, capture_output=True, text=True,
                                 timeout=60, env=env, input="")
        self.assertEqual(refused.returncode, 2)
        # Live sources feed only the case runtime: the host has no mode without one.
        self.assertIn("the following arguments are required: --source-mirror-audience",
                      refused.stderr)


if __name__ == "__main__":
    unittest.main()
