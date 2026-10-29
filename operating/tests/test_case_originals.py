"""Operator-supplied originals on cases (isolated-fixture only).

Evidence label: ``isolated-fixture``. The originals store reads files from its
own inbox in a temporary directory; extraction runs either through a stub or
through real local OCR on a synthetic scan (skipped when neither macOS Vision
nor poppler + tesseract is installed). Nothing touches a network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import unittest
from unittest.mock import patch

from integrations import document_understanding
from integrations.document_understanding import ExtractedDocument
from ontology.core import Actor, ValidationError
from operating.case_originals import OriginalError, OriginalStore
from operating.cases import CaseStore, CitationUnresolvable

FIXTURE = Path(__file__).with_name("fixtures") / "ao-26005b-scan-fixture.pdf"
OPS = Actor("plx", frozenset({"staff:operations"}))
QUALITY = Actor("plx", frozenset({"staff:quality"}))
OPS_QUALITY = Actor("plx", frozenset({"staff:operations", "staff:quality"}))
MINIMAL_PDF = b"%PDF-1.4\n% synthetic\n"


def _importable(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _stub(text="PLANNED 1509\nFILLED 1502", ocr_pages=(1,), fail=None):
    calls = []

    def extract(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
        calls.append({"url": original_url, "filename": filename, "ocr": local_pdf_ocr})
        if fail:
            raise ValidationError(fail)
        first = text.split("\n", 1)[0]
        pages = ({"page": 1, "start": 0, "end": len(first)},
                 {"page": 2, "start": len(first) + 1, "end": len(text)})
        return ExtractedDocument("application/pdf", "x", "extracted", "stub-ocr", text, (), (),
                                 ("ocr_text_requires_identity_review",), pages,
                                 ocr_pages=tuple(ocr_pages))
    extract.calls = calls
    return extract


def ocr_scan(count, page_text=lambda number: f"Page marker M{number:03d}"):
    """The real extraction contract over a ``count``-page scan whose local OCR backend is a
    stand-in: it reads ``page_text(n)`` per page and, like the real backends, pages
    1-``max_pages`` only with ``first_pages`` (else the PDF is over the bound)."""
    def read(content, *, ocr=True, max_pages=None, first_pages=False):
        pages = count if max_pages is None else min(count, max_pages)
        if pages < count and not first_pages:
            raise ValidationError("PDF page count exceeds bound")
        text, spans = "", []
        for number in range(1, pages + 1):
            spans.append({"page": number, "start": len(text),
                          "end": len(text) + len(page_text(number))})
            text += page_text(number) + "\n"
        return text, tuple(spans), "extracted", (), frozenset(range(1, pages + 1)), count
    return patch.multiple(document_understanding, _local_ocr_backend=lambda: "portable",
                          _portable_pdf_text=read)


class OriginalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.extract = _stub()
        self.store = OriginalStore(self.root, extractor=self.extract, local_ocr=True)

    def put(self, name="scan.pdf", content=MINIMAL_PDF, principal="staff:operations") -> str:
        directory = self.store.inbox / principal
        directory.mkdir(exist_ok=True)
        (directory / name).write_bytes(content)
        return name

    def test_attach_stores_fingerprints_and_resolves_pages_for_the_audience(self):
        attached = self.store.attach(OPS, self.put())
        self.assertFalse(attached["duplicate"])
        self.assertEqual(attached["audience"], ["staff:operations"])
        self.assertEqual(attached["extraction"]["method"], "stub-ocr")
        self.assertEqual(attached["extraction"]["ocr_pages"], [1])
        self.assertEqual(len(attached["page_citations"]), 2)
        # Extraction never sees a real URL; the placeholder is a reserved TLD.
        self.assertTrue(self.extract.calls[0]["url"].startswith("https://case-original.invalid/"))
        self.assertTrue(self.extract.calls[0]["ocr"])
        whole = self.store.resolve(attached["citation"], OPS)
        self.assertEqual(whole["sha256"], attached["sha256"])
        self.assertNotIn("text", whole)
        page1 = self.store.resolve(attached["page_citations"][0], OPS)
        self.assertEqual((page1["page"], page1["text"], page1["text_source"]),
                         (1, "PLANNED 1509", "ocr"))
        page2 = self.store.resolve(attached["page_citations"][1], OPS)
        self.assertEqual((page2["text"], page2["text_source"]), ("FILLED 1502", "text_layer"))
        for handle in (attached["citation"] + "#page=3", attached["citation"] + "#page=0",
                       "original://orig-xyz", "api://public.x/1@1", None):
            with self.assertRaises(CitationUnresolvable, msg=handle):
                self.store.resolve(handle, OPS)

    def test_readers_outside_the_audience_tenant_or_clearance_cannot_resolve(self):
        attached = self.store.attach(OPS, self.put())
        for actor in (QUALITY, Actor("other", frozenset({"staff:operations"})),
                      Actor("plx", frozenset({"*"})),
                      Actor("plx", frozenset({"staff:operations"}), clearance="public")):
            with self.assertRaises(CitationUnresolvable) as caught:
                self.store.resolve(attached["citation"], actor)
            self.assertEqual(str(caught.exception), "original_not_visible")
        self.assertEqual(self.store.list_visible(QUALITY), [])
        self.assertEqual(len(self.store.list_visible(OPS)), 1)

    def test_audience_is_the_principal_directory_that_held_the_file(self):
        quality = self.store.attach(OPS_QUALITY, self.put(principal="staff:quality"))
        self.assertEqual(quality["audience"], ["staff:quality"])
        with self.assertRaises(CitationUnresolvable):
            self.store.resolve(quality["citation"], OPS)
        # An actor without the principal cannot attach or probe that directory.
        self.put("q.pdf", principal="staff:quality")
        for principal, code in ((None, "inbox_file_not_found"),
                                ("staff:quality", "inbox_principal_not_held"),
                                ("*", "inbox_principal_not_held")):
            with self.assertRaises(OriginalError) as caught:
                self.store.attach(OPS, "q.pdf", principal)
            self.assertEqual(caught.exception.code, code, principal)
        self.assertTrue((self.store.inbox / "staff:quality" / "q.pdf").exists())
        # A name in two of the actor's directories needs an explicit choice, and a
        # '*' principal never opens a directory.
        self.put("both.pdf")
        self.put("both.pdf", principal="staff:quality")
        with self.assertRaises(OriginalError) as caught:
            self.store.attach(OPS_QUALITY, "both.pdf")
        self.assertEqual(caught.exception.code, "inbox_name_ambiguous")
        chosen = self.store.attach(OPS_QUALITY, "both.pdf", "staff:operations")
        self.assertEqual(chosen["audience"], ["staff:operations"])
        with self.assertRaises(OriginalError) as caught:
            self.store.attach(Actor("plx", frozenset({"*"})), "both.pdf")
        self.assertEqual(caught.exception.code, "inbox_file_not_found")

    def test_attaching_consumes_the_file_and_a_refusal_puts_it_back(self):
        self.store.attach(OPS, self.put())
        self.assertEqual(list((self.store.inbox / "staff:operations").iterdir()), [])
        with self.assertRaises(OriginalError) as caught:
            self.store.attach(OPS, "scan.pdf")
        self.assertEqual(caught.exception.code, "inbox_file_not_found")
        self.put("notes.pdf", b"\x00\xff neither a PDF nor text")
        with self.assertRaises(OriginalError):
            self.store.attach(OPS, "notes.pdf")
        self.assertEqual([path.name for path in
                          (self.store.inbox / "staff:operations").iterdir()], ["notes.pdf"])
        self.put("scan.pdf")
        with patch.object(self.store, "_read_inbox", side_effect=OSError("I/O error")):
            with self.assertRaises(OSError):
                self.store.attach(OPS, "scan.pdf")
        self.assertEqual(sorted(path.name for path in
                                (self.store.inbox / "staff:operations").iterdir()),
                         ["notes.pdf", "scan.pdf"])
        with patch.object(self.store, "_store", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.attach(OPS, "scan.pdf")
        self.assertTrue((self.store.inbox / "staff:operations" / "scan.pdf").exists())

    def test_local_ocr_is_off_unless_the_host_enables_it(self):
        extract = _stub()
        store = OriginalStore(self.root / "default", extractor=extract)
        (store.inbox / "staff:operations").mkdir()
        (store.inbox / "staff:operations" / "scan.pdf").write_bytes(MINIMAL_PDF)
        store.attach(OPS, "scan.pdf")
        self.assertFalse(extract.calls[0]["ocr"])

    def test_same_bytes_and_audience_return_the_same_original(self):
        first = self.store.attach(OPS, self.put("a.pdf"))
        again = self.store.attach(OPS, self.put("a.pdf"))
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["original_id"], first["original_id"])
        self.assertEqual(len(self.extract.calls), 1)
        other = self.store.attach(OPS_QUALITY, self.put("a.pdf", principal="staff:quality"))
        self.assertNotEqual(other["original_id"], first["original_id"])

    def test_only_plain_pdf_files_directly_inside_a_principal_directory_are_read(self):
        directory = self.store.inbox / "staff:operations"
        directory.mkdir()
        outside = self.root / "secret.pdf"
        outside.write_bytes(MINIMAL_PDF)
        os.symlink(outside, directory / "link.pdf")
        (directory / "dir.pdf").mkdir()
        (self.store.inbox / "top.pdf").write_bytes(MINIMAL_PDF)  # not in a principal dir
        self.put("notes.pdf", b"PK\x03\x04\x00\xff binary, neither PDF nor UTF-8 text")
        for name, code in (("../secret.pdf", "invalid_inbox_name"),
                           ("sub/x.pdf", "invalid_inbox_name"),
                           (str(outside), "invalid_inbox_name"),
                           (".hidden.pdf", "invalid_inbox_name"),
                           ("", "invalid_inbox_name"), (None, "invalid_inbox_name"),
                           ("missing.pdf", "inbox_file_not_found"),
                           ("top.pdf", "inbox_file_not_found"),
                           ("link.pdf", "inbox_file_not_a_regular_file"),
                           ("dir.pdf", "inbox_file_not_a_regular_file"),
                           ("notes.pdf", "unsupported_original_type")):
            with self.assertRaises(OriginalError, msg=name) as caught:
                self.store.attach(OPS, name)
            self.assertEqual(caught.exception.code, code, name)
        # The refused link is put back as a link, never followed.
        self.assertTrue((directory / "link.pdf").is_symlink())
        # A principal directory that is itself a link is ignored.
        linked = self.root / "elsewhere"
        linked.mkdir()
        (linked / "x.pdf").write_bytes(MINIMAL_PDF)
        os.symlink(linked, self.store.inbox / "staff:quality")
        with self.assertRaises(OriginalError) as caught:
            self.store.attach(OPS_QUALITY, "x.pdf")
        self.assertEqual(caught.exception.code, "inbox_file_not_found")
        with patch("operating.case_originals.MAX_BYTES", 10):
            with self.assertRaises(OriginalError) as caught:
                self.store.attach(OPS, self.put("big.pdf", MINIMAL_PDF))
            self.assertEqual(caught.exception.code, "original_too_large")
        self.assertEqual(list(self.store.store.iterdir()), [])

    def test_changed_or_missing_stored_bytes_fail_closed(self):
        attached = self.store.attach(OPS, self.put())
        blob = self.store.store / attached["sha256"]
        blob.write_bytes(MINIMAL_PDF + b"tampered")
        with self.assertRaises(CitationUnresolvable) as caught:
            self.store.resolve(attached["page_citations"][0], OPS)
        self.assertEqual(str(caught.exception), "original_content_changed")
        blob.unlink()
        with self.assertRaises(CitationUnresolvable) as caught:
            self.store.resolve(attached["citation"], OPS)
        self.assertEqual(str(caught.exception), "original_missing")
        # Re-attaching the same bytes restores the stored copy.
        self.store.attach(OPS, self.put())
        self.assertEqual(self.store.resolve(attached["citation"], OPS)["sha256"],
                         attached["sha256"])

    def test_pdf_bytes_are_served_only_after_the_audience_check(self):
        attached = self.store.attach(OPS, self.put())
        served = self.store.read_pdf(attached["citation"], OPS)
        self.assertEqual((served["content"], served["sha256"], served["media_type"]),
                         (MINIMAL_PDF, attached["sha256"], "application/pdf"))
        missing = "original://orig-" + "0" * 32

        def refused(handle, actor):
            with self.assertRaises(CitationUnresolvable) as caught:
                self.store.read_pdf(handle, actor)
            return str(caught.exception)

        # Outside the audience, tenant or clearance, a wildcard, or no Actor at all:
        # refused exactly like an original that does not exist.
        for actor in (QUALITY, Actor("other", frozenset({"staff:operations"})),
                      Actor("plx", frozenset({"*"})),
                      Actor("plx", frozenset({"staff:operations"}), clearance="public"),
                      "staff:operations"):
            with self.subTest(actor=actor):
                self.assertEqual(refused(attached["citation"], actor), "original_not_visible")
        self.assertEqual(refused(missing, OPS), "original_not_visible")
        # Only the file citation names the bytes; a page or version is text, not a file.
        for handle in (attached["page_citations"][0], attached["citation"] + "#extraction=2",
                       "https://example.invalid/x.pdf", None):
            with self.subTest(handle=handle):
                self.assertEqual(refused(handle, OPS), "original_file_citation_required")
        # The checks come before the bytes are read: an outsider learns nothing about a
        # tampered file, the reader gets the integrity error, never the changed bytes.
        blob = self.store.store / attached["sha256"]
        blob.write_bytes(MINIMAL_PDF + b"tampered")
        self.assertEqual(refused(attached["citation"], QUALITY), "original_not_visible")
        self.assertEqual(refused(attached["citation"], OPS), "original_content_changed")
        blob.unlink()
        self.assertEqual(refused(attached["citation"], OPS), "original_missing")

    def test_pdf_bytes_need_an_employee_principal_never_an_agent_one(self):
        # operating.entitlements: agents are never principals. An original whose only
        # granting principal is an agent identity yields its text, never its bytes.
        bot = Actor("plx", frozenset({"agent:quality-bot"}))
        attached = self.store.attach(bot, self.put(principal="agent:quality-bot"))
        self.assertEqual(self.store.resolve(attached["citation"], bot)["sha256"],
                         attached["sha256"])
        with self.assertRaises(CitationUnresolvable) as caught:
            self.store.read_pdf(attached["citation"], bot)
        self.assertEqual(str(caught.exception), "original_not_visible")
        # An employee principal of the audience grants it even beside an agent one.
        mixed = Actor("plx", frozenset({"agent:quality-bot", "staff:operations"}))
        employee = self.store.attach(OPS, self.put(name="second.pdf"))
        self.assertEqual(self.store.read_pdf(employee["citation"], mixed)["content"],
                         MINIMAL_PDF)

    def test_source_original_bytes_follow_the_live_source_gate(self):
        allowed = {"value": True}
        store = OriginalStore(self.root / "sourced", extractor=_stub(),
                              source_gate=lambda provenance, actor: allowed["value"])
        stored = store.store_source(OPS, "po.pdf", MINIMAL_PDF,
                                    {"source": "sharepoint-drive", "identity": ["d", "i", "e"]})
        self.assertEqual(store.read_pdf(stored["citation"], OPS)["content"], MINIMAL_PDF)
        allowed["value"] = False  # the item's ACL changed, or the source is down
        with self.assertRaises(CitationUnresolvable) as caught:
            store.read_pdf(stored["citation"], OPS)
        self.assertEqual(str(caught.exception), "original_not_visible")

    def test_text_original_is_paged_under_the_response_cap_and_gated_for_its_reader(self):
        # A Markdown original (type from its bytes, not its .pdf-free name): line-window
        # pages that each fit one MCP response whole, provenance and source ACL kept.
        from operating import mcp_server
        lines = [f'| row {n} | "quoted" ✓ 10,032 lb | {"x" * (n % 90)} |' for n in range(1, 1500)]
        lines.insert(700, "Z" * 70_000)   # one line longer than a page spans pages
        text = "# Release map\n" + "\n".join(lines) + "\n"
        allowed = {"value": True}
        store = OriginalStore(self.root / "sourced", extractor=_stub(fail="never_called"),
                              source_gate=lambda provenance, actor: allowed["value"])
        acl = {"status": "read", "grants": [{"identity": "vince@example.com"}]}
        stored = store.store_source(OPS, "Release Map.md", text.encode(), {
            "source": "sharepoint-drive", "identity": ["d", "i", "e"], "source_acl": acl})
        self.assertEqual(stored["media_type"], "text/plain; charset=utf-8")
        self.assertEqual(stored["provenance"]["source_acl_grant_count"], 1)
        self.assertNotIn("source_acl", stored["provenance"])
        whole, previous = "", 0
        for citation in stored["page_citations"]:
            view = store.resolve(citation, OPS)
            served = mcp_server._original_page_result(view, 0, len(text))
            self.assertTrue(served["text_complete"], citation)
            self.assertLessEqual(mcp_server._envelope_bytes(served),
                                 mcp_server.MAX_RESULT_BYTES, citation)
            self.assertEqual(view["text_source"], "original_text")
            self.assertIn(view["lines"][0], (previous, previous + 1))
            whole, previous = whole + view["text"], view["lines"][1]
        self.assertEqual((whole, previous), (text, 1501))
        self.assertEqual(store.text_document(stored["citation"], OPS)["text"], text)
        # A restricted principal outside the audience, or any reader once the source ACL
        # no longer allows it, reads nothing and lists nothing.
        for reader, gate in ((QUALITY, True), (OPS, False)):
            allowed["value"] = gate
            with self.assertRaises(CitationUnresolvable):
                store.resolve(stored["page_citations"][0], reader)
            self.assertEqual(store.list_visible(reader), [])
        # Bytes that are neither PDF nor UTF-8 text are refused, whatever the name says.
        for content in (b"\xff\xfe# not utf-8", b"text\x00with a NUL", b""):
            with self.assertRaises(OriginalError) as caught:
                store.store_source(OPS, "notes.md", content,
                                   {"source": "sharepoint-drive", "identity": ["d", "i", "x"]})
            self.assertEqual(caught.exception.code, "unsupported_original_type")

    def test_a_scan_over_the_page_cap_records_the_pages_ocr_skipped(self):
        """Directive 84dda19af1d5: OCR reads pages 1-200 of a 300-page scan and the stored
        extraction says the rest were skipped by the cap (the extraction contract's
        ``skipped_by_cap``) in every view; a scan within the cap has no such field."""
        store = OriginalStore(self.root, local_ocr=True)  # the real extract_document
        for count, skipped in ((300, {"first_page": 201, "last_page": 300, "cap": "pages",
                                      "limit": document_understanding.MAX_PAGES}),
                               (150, None)):
            with self.subTest(pages=count), ocr_scan(count):
                name = self.put(f"scan-{count}.pdf", MINIMAL_PDF + str(count).encode(),
                                "staff:quality")
                attached = store.attach(QUALITY, name)
                extraction = attached["extraction"]
                self.assertEqual((extraction["status"], extraction["page_count"],
                                  extraction["ocr_pages"][-1]),
                                 ("extracted", min(count, 200), min(count, 200)))
                self.assertEqual(extraction.get("skipped_by_cap"), skipped)
                for view in (store.resolve(attached["citation"], QUALITY),
                             store.read(attached["citation"], QUALITY)):
                    self.assertEqual(view["extraction"].get("skipped_by_cap"), skipped)
                if skipped:
                    self.assertIn("pdf_pages_skipped_by_cap:201-300", extraction["warnings"])
                    self.assertEqual(len(store.read(attached["citation"], QUALITY)
                                         ["page_citations"]), 200)
                # Re-reading the same bytes gives the same capped result: no new version.
                again = store.reextract(QUALITY, attached["citation"], "OCR again")
                self.assertEqual((again["duplicate"], again["version"]), (True, 1))

    def test_failed_extraction_still_stores_the_original_and_says_why(self):
        store = OriginalStore(self.root / "failing",
                              extractor=_stub(fail="local_pdf_ocr_unavailable"))
        (store.inbox / "staff:operations").mkdir()
        (store.inbox / "staff:operations" / "scan.pdf").write_bytes(MINIMAL_PDF)
        attached = store.attach(OPS, "scan.pdf")
        self.assertEqual(attached["extraction"]["status"], "failed")
        self.assertIn("local_pdf_ocr_unavailable", attached["extraction"]["error"])
        self.assertEqual(attached["page_citations"], [])
        self.assertEqual(store.resolve(attached["citation"], OPS)["extraction"]["status"],
                         "failed")

    @unittest.skipUnless(shutil.which("pdftotext") or _importable("pypdf"),
                         "no PDF text reader installed")
    def test_a_pdf_over_the_page_bound_names_the_pages_it_did_not_read(self):
        """Directive d82b7c38fc7b: pages 1-200 are read and the rest is a structured
        skipped range in the original's extraction, not only a warning."""
        from integrations.tests.test_doc_convert import make_pdf

        store = OriginalStore(self.root / "long")
        (store.inbox / "staff:operations").mkdir()
        (store.inbox / "staff:operations" / "long.pdf").write_bytes(
            make_pdf([[f"Page marker M{n:03d}"] for n in range(1, 206)]))
        with patch("integrations.doc_convert.installed_backend", return_value=None):
            attached = store.attach(OPS, "long.pdf")
        extraction = attached["extraction"]
        self.assertEqual((extraction["status"], extraction["page_count"],
                          extraction["skipped_by_cap"]),
                         ("extracted", 200, {"first_page": 201, "last_page": 205,
                                             "cap": "pages", "limit": 200}))
        self.assertEqual(store.resolve(attached["citation"], OPS)["extraction"]
                         ["skipped_by_cap"]["first_page"], 201)

    def test_stored_records_are_immutable(self):
        import sqlite3
        attached = self.store.attach(OPS, self.put())
        db = sqlite3.connect(self.store.path)
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE original SET audience_json = '[\"staff:quality\"]' "
                       "WHERE original_id = ?", (attached["original_id"],))

    def test_case_findings_citing_an_original_are_gated_by_its_audience(self):
        cases = CaseStore(self.root / "cases.sqlite", citation_resolver=self.store.resolve)
        case_id = cases.open(OPS_QUALITY, "What does the AO scan say?")
        attached = self.store.attach(OPS_QUALITY, self.put(principal="staff:quality"))
        cases.finding(OPS_QUALITY, case_id, "The scan's page 1 reads PLANNED 1509.", "fact",
                      "supported", [attached["page_citations"][0]])
        view = cases.get(OPS, case_id)
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["withheld"]), 1)
        self.assertNotIn("1509", json.dumps(view))
        self.assertEqual(len(cases.get(QUALITY, case_id)["findings"]["supported"]), 1)


class RecordedCaptureGateTests(unittest.TestCase):
    def test_recorded_permissions_decide_only_while_recent_and_never_widen_an_employee(self):
        # An original seeded from a recorded guarded capture (no live reader on the host):
        # the item ACL recorded then applies to an employee as a live one would, and the
        # recorded permissions decide only within the one capture age bound the
        # SharePoint capture host uses (operating.capture_age); past it a live re-read
        # of the drive item decides.
        from datetime import datetime, timedelta, timezone
        from integrations.live_source_guard import NotVisible
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from operating.capture_age import MAX_CAPTURE_AGE
        from operating.case_originals import RECORDED_CAPTURE_SOURCE
        from operating.case_runtime import CaseRuntime, LiveReaders
        from operating.employee_permissions import (EmployeePermissionPolicy,
                                                    EmployeePermissionResolver)
        from operating.tests.test_employee_permissions import (OID, POLICY, QA_GROUP, Calls,
                                                               member, portal_user)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        audience = Path(temp.name) / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), encoding="utf-8")
        runtime = CaseRuntime(Path(temp.name) / "state", client=None, audience=audience,
                              tenant="plx", employee_permissions=EmployeePermissionResolver(
                                  EmployeePermissionPolicy.from_json(POLICY),
                                  Calls(portal_user("MANAGER")), Calls(member(QA_GROUP))))
        self.addCleanup(runtime.close)
        employee = Actor("plx", frozenset({"employee:" + OID}))   # Ricardo, in QA

        def seed(granted, age, etag):
            captured = datetime.now(timezone.utc) - age
            return runtime.originals.store_source(QUALITY, "Release Map.md", b"# Map\n", {
                "source": RECORDED_CAPTURE_SOURCE, "identity": ["b!d", "I", etag],
                "drive_id": "b!d", "item_id": "I", "etag": etag, "path": "/Docs",
                "captured_at": captured.isoformat(),
                "source_acl": {"status": "read", "identities": granted}})["citation"]
        hour, young, old = timedelta(hours=1), timedelta(hours=47), timedelta(hours=49)
        self.assertLess(young, MAX_CAPTURE_AGE)
        self.assertLess(MAX_CAPTURE_AGE, old)
        granted = seed(["ricardo@petralabx.com"], young, "e1")
        other = seed(["vince@petralabx.com"], hour, "e2")
        stale = seed(["ricardo@petralabx.com"], old, "e3")
        future = seed(["ricardo@petralabx.com"], -hour, "e4")
        self.assertTrue(runtime.read_original(granted, employee)["ok"])
        self.assertTrue(runtime.read_original(other, QUALITY)["ok"])     # not an employee
        self.assertFalse(runtime.read_original(other, employee)["ok"])   # ACL names Vince
        self.assertFalse(runtime.read_original(granted, OPS)["ok"])      # outside audience
        for citation in (stale, future):
            for reader in (employee, QUALITY):
                self.assertFalse(runtime.read_original(citation, reader)["ok"])
        self.assertEqual([o["citation"] for o in runtime.originals.list_visible(employee)],
                         [granted])

        # A live reader for the drive: the 49 h original is re-read live, and the live
        # answer decides (visible: served; revoked upstream: denied).
        class Drive:
            visible, calls = True, []

            def get_item(self, actor, item_id):
                self.calls.append(item_id)
                if not self.visible:
                    raise NotVisible("revoked")
                return {"id": item_id, "drive_id": "b!d", "path": "/Docs",
                        "name": "Release Map.md",
                        "source_acl": {"status": "read",
                                       "identities": ["ricardo@petralabx.com"]}}
        drive = Drive()
        runtime.live = LiveReaders(audience=None, sharepoint={"b!d": drive})
        self.assertTrue(runtime.read_original(stale, employee)["ok"])
        self.assertEqual(drive.calls, ["I"])
        drive.visible = False
        self.assertFalse(runtime.read_original(stale, employee)["ok"])


class ReextractionTests(unittest.TestCase):
    """AO-003: explicit versioned re-extraction keeps bytes and earlier citations."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self, extractor, local_ocr) -> OriginalStore:
        return OriginalStore(self.root, extractor=extractor, local_ocr=local_ocr)

    def attach_without_ocr(self) -> dict:
        store = self.store(_stub(fail="local_pdf_ocr_unavailable: OCR disabled"), False)
        (store.inbox / "staff:operations").mkdir()
        (store.inbox / "staff:operations" / "scan.pdf").write_bytes(MINIMAL_PDF)
        return store.attach(OPS, "scan.pdf")

    def test_reextraction_after_ocr_is_enabled_adds_a_version_and_keeps_the_first(self):
        attached = self.attach_without_ocr()
        before = OriginalStore(self.root).resolve(attached["citation"], OPS)
        self.assertEqual(before["extraction"]["status"], "failed")
        extract = _stub()
        store = self.store(extract, True)
        version = store.reextract(OPS, attached["citation"], "OCR enabled on the host")
        self.assertEqual(extract.calls[0]["ocr"], True)
        self.assertEqual((version["version"], version["duplicate"], version["previous_version"],
                          version["sha256"], version["original_id"]),
                         (2, False, 1, attached["sha256"], attached["original_id"]))
        self.assertEqual(version["previous_extraction"]["status"], "failed")
        oid = attached["original_id"]
        self.assertEqual(version["citation"], f"original://{oid}#extraction=2")
        self.assertEqual(version["page_citations"],
                         [f"original://{oid}#extraction=2&page=1",
                          f"original://{oid}#extraction=2&page=2"])
        # Version 1 citations name exactly what they named before.
        self.assertEqual(store.resolve(attached["citation"], OPS), before)
        with self.assertRaises(CitationUnresolvable):
            store.resolve(f"original://{oid}#page=1", OPS)  # version 1 had no pages
        page = store.resolve(version["page_citations"][0], OPS)
        self.assertEqual((page["text"], page["text_source"], page["extraction_version"],
                          page["extraction_reason"], page["extraction_local_ocr"]),
                         ("PLANNED 1509", "ocr", 2, "OCR enabled on the host", True))
        read = store.read(attached["citation"], OPS)
        self.assertEqual([item["version"] for item in read["extractions"]], [1, 2])
        self.assertEqual(read["latest_extraction_version"], 2)
        self.assertEqual(read["page_citations"], [])  # version 1's pages
        self.assertEqual(store.read(version["citation"], OPS)["page_citations"],
                         version["page_citations"])

    def test_identical_result_is_a_duplicate_and_a_changed_one_is_a_new_version(self):
        attached = self.attach_without_ocr()
        store = self.store(_stub(), True)
        self.assertFalse(store.reextract(OPS, attached["citation"], "enable")["duplicate"])
        again = store.reextract(OPS, attached["citation"] + "#extraction=2", "again")
        self.assertEqual((again["duplicate"], again["version"]), (True, 2))
        repaired = self.store(_stub(text="PLANNED 1509\nFILLED 1503"), True)
        third = repaired.reextract(OPS, attached["citation"], "OCR repaired")
        self.assertEqual((third["version"], third["previous_version"]), (3, 2))
        # Version 2 still names its own text.
        oid = attached["original_id"]
        self.assertEqual(repaired.resolve(f"original://{oid}#extraction=2&page=2", OPS)["text"],
                         "FILLED 1502")
        self.assertEqual(repaired.resolve(f"original://{oid}#extraction=3&page=2", OPS)["text"],
                         "FILLED 1503")
        # Going back to an earlier result (OCR off, then on again) adds nothing.
        back = self.store(_stub(), True).reextract(OPS, attached["citation"], "toggled")
        self.assertEqual((back["duplicate"], back["version"], back["previous_version"]),
                         (True, 2, 1))
        off = self.store(_stub(fail="local_pdf_ocr_unavailable: OCR disabled"), False)
        self.assertEqual(off.reextract(OPS, attached["citation"], "off")["version"], 1)
        self.assertEqual(OriginalStore(self.root).read(attached["citation"], OPS)
                         ["latest_extraction_version"], 3)

    def test_reextraction_is_refused_to_outsiders_and_on_changed_bytes(self):
        attached = self.attach_without_ocr()
        store = self.store(_stub(), True)
        ghost = "original://orig-" + "0" * 32
        for actor, handle in ((QUALITY, attached["citation"]), (OPS, ghost),
                              (Actor("other", frozenset({"staff:operations"})),
                               attached["citation"])):
            with self.assertRaises(OriginalError) as refused:
                store.reextract(actor, handle, "why")
            self.assertEqual(refused.exception.code, "original_not_visible")
        for handle, reason, code in ((attached["citation"], " ", "invalid_reason"),
                                     (attached["citation"], "x" * 1001, "invalid_reason"),
                                     ("original://../x", "why", "invalid_original_citation"),
                                     (attached["citation"] + "#extraction=1", "why",
                                      "invalid_original_citation")):
            with self.assertRaises(OriginalError) as refused:
                store.reextract(OPS, handle, reason)
            self.assertEqual(refused.exception.code, code)
        with self.assertRaises(CitationUnresolvable) as missing:
            store.resolve(attached["citation"] + "#extraction=2", OPS)
        self.assertEqual(str(missing.exception), "original_extraction_not_found")
        blob = store.store / attached["sha256"]
        blob.write_bytes(blob.read_bytes() + b"%x")
        with self.assertRaises(OriginalError) as changed:
            store.reextract(OPS, attached["citation"], "why")
        self.assertEqual(changed.exception.code, "original_content_changed")

    def test_versions_are_immutable_and_capped(self):
        import sqlite3
        from operating import case_originals
        attached = self.attach_without_ocr()
        for number in range(2, case_originals.MAX_EXTRACTIONS + 1):
            self.store(_stub(text=f"PAGE {number}\nX"), True).reextract(
                OPS, attached["citation"], f"run {number}")
        with self.assertRaises(OriginalError) as capped:
            self.store(_stub(text="PAGE last\nX"), True).reextract(
                OPS, attached["citation"], "one more")
        self.assertEqual(capped.exception.code, "extraction_limit_reached")
        store = OriginalStore(self.root)
        oid = attached["original_id"]
        self.assertEqual(store.resolve(f"original://{oid}#extraction=20&page=1", OPS)["text"],
                         "PAGE 20")
        db = sqlite3.connect(store.path)
        self.addCleanup(db.close)
        for statement in ("UPDATE reextraction SET reason = 'x'",
                          "DELETE FROM reextraction"):
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute(statement)


def local_ocr_available() -> bool:
    if os.environ.get("PLX_REQUIRE_OCR") == "1" and \
            document_understanding._local_ocr_backend() != "portable":
        raise AssertionError("PLX_REQUIRE_OCR=1 but poppler and tesseract are not installed")
    return (document_understanding._local_ocr_backend() == "portable" or
            (platform.system() == "Darwin" and Path("/usr/bin/swift").is_file()))


@unittest.skipUnless(local_ocr_available(), "no local OCR backend installed")
class RealOcrTests(unittest.TestCase):
    def test_synthetic_scan_is_read_by_local_ocr_and_labelled_as_ocr(self):
        with tempfile.TemporaryDirectory() as temp:
            store = OriginalStore(Path(temp), local_ocr=True)
            (store.inbox / "staff:operations").mkdir()
            shutil.copyfile(FIXTURE, store.inbox / "staff:operations" / "AO-26005B scan.pdf")
            attached = store.attach(OPS, "AO-26005B scan.pdf")
            self.assertEqual(attached["extraction"]["status"], "extracted")
            self.assertIn(attached["extraction"]["method"],
                          ("poppler-tesseract", "macos-pdfkit-vision"))
            self.assertEqual(attached["extraction"]["ocr_pages"], [1])
            page = store.resolve(attached["page_citations"][0], OPS)
            self.assertEqual(page["text_source"], "ocr")
            for line in ("AO-26005B", "PLANNED 1509", "MANUFACTURED 1503", "FILLED 1502",
                         "DAMAGED PACKS 30"):
                self.assertIn(line, page["text"])

    def test_scan_attached_without_ocr_is_read_by_explicit_reextraction(self):
        with tempfile.TemporaryDirectory() as temp:
            store = OriginalStore(Path(temp), local_ocr=False)
            (store.inbox / "staff:operations").mkdir()
            shutil.copyfile(FIXTURE, store.inbox / "staff:operations" / "AO-26005B scan.pdf")
            attached = store.attach(OPS, "AO-26005B scan.pdf")
            self.assertEqual(attached["extraction"]["ocr_pages"], [])
            first = store.resolve(attached["citation"], OPS)
            enabled = OriginalStore(Path(temp), local_ocr=True)
            version = enabled.reextract(OPS, attached["citation"], "OCR enabled")
            self.assertEqual((version["version"], version["extraction"]["ocr_pages"]), (2, [1]))
            page = enabled.resolve(version["page_citations"][0], OPS)
            self.assertEqual(page["text_source"], "ocr")
            self.assertIn("PLANNED 1509", page["text"])
            self.assertEqual(enabled.resolve(attached["citation"], OPS), first)


if __name__ == "__main__":
    unittest.main()
