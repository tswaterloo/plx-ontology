"""Compact mail storage (``integrations.capture_mail``, module docstring *Storage*).

The capture keeps bodies and texts zlib-compressed, drops an attachment original over
``DROP_ORIGINALS_ABOVE`` once every reading of it is kept (its ``.dropped`` form), keeps
the bytes a pending OCR still needs (bounded: past ``max_pending_ocr_bytes`` new PDFs are
deferred) and compacts an existing store once (``compact``). Every reader of the text --
resolve (the claim gate's rows), the mention documents, the typed fields -- reads the
same before and after a drop; a restricted reader sees nothing more. Fixtures: the
in-process fake Graph of ``test_capture_mail``. Evidence label: injected-transport.
"""

from __future__ import annotations

import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import zlib

from integrations import capture_mail, captured_text as ct, graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader, quick_xor_hash
from integrations.live_source_guard import BoundedBackoff
from integrations.tests import test_capture_mail as tm
from ontology import document_fields
from operating import claim_checks

READER, OUTSIDER = tm.READER_A, tm.OUTSIDER


def fresh_memos():
    """Empty process-wide reading memos, so a reading is made again from the store."""
    stack = contextlib.ExitStack()
    for module, name in ((ct, "_CACHE"), (ct, "_MEMBER_READINGS"), (document_fields, "_FIELDS")):
        stack.enter_context(mock.patch.object(module, name, getattr(module, name).empty()))
    return stack


def pdf(pages):
    from operating.tests.test_attested_missing import pdf as make
    return make(pages)


def attachment(aid, content, name, mime):
    return {**tm.file_attachment(aid, content, name=name), "contentType": mime,
            "size": len(content)}


def forms(store, digest):
    return {suffix: ct.blob_path(store.blobs, digest, suffix).exists()
            for suffix in ("", ct.COMPRESSED_SUFFIX, ct.DROPPED_SUFFIX)}


class CompactCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.graph = tm.FakeGraph()
        self.store = MailCaptureStore(self.state, clock=tm.Clock())
        self.addCleanup(self.store.close)
        self.audience = tm.reader_audience()

    def reader(self):
        api, _ = graph_read.build_graph(tm.ENV, sender=self.graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        return GraphMailReader(api, tm.capture_audience())

    def capture(self, **kwargs):
        kwargs.setdefault("sleep", lambda s: None)
        return MailCapture(self.store, self.reader(), tm.CAPTURE, page_size=50, **kwargs)

    def handle(self, mid, ck, aid, content):
        return (f"{graph_read.mail_citation(tm.BOX_A, mid, ck)}#attachment={aid}"
                f"&sha256={sha256(content).hexdigest()}")

    def snapshot(self, handles):
        """Everything a reader reads of the text, made afresh from the store: resolve
        (whole text and each page), the mention documents and the typed fields."""
        from ontology.project import MailCaptures
        with fresh_memos():
            out = {}
            for handle in handles:
                out[handle] = [self.store.resolve(handle, READER, self.audience, text=True)]
                out[handle] += [self.store.resolve(f"{handle}&page={n}", READER,
                                                   self.audience, text=True) for n in (1, 2, 3)]
            captures = MailCaptures(self.state)
            try:
                out["documents"] = [(d.citation, d.kind, d.fields, d.text_withheld)
                                    for d in captures.documents()]
                out["documentFields"] = [
                    (r.l1_handle, json.dumps(r.row, sort_keys=True, default=str))
                    for r in captures.records(["documentFields"])["documentFields"]]
            finally:
                captures.close()
        return out


class DroppedOriginalTests(CompactCase):
    def test_a_dropped_original_reads_the_same_everywhere_and_only_for_its_reader(self):
        from integrations.doc_convert import DOCX
        from integrations.tests.office_fixtures import PURCHASE_ORDER_BODY, make_word
        from operating.tests.test_attested_missing import customer_po
        po = pdf(customer_po("4500012345", "OR0003") + [["Page two of the order"]])
        word = make_word(PURCHASE_ORDER_BODY)
        small = b"a small note kept as it is\n"
        sheet = b"sku,qty\n" + b"".join(b"FG-%04d,%d\n" % (n, n) for n in range(400))
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            attachment("ATT-1", po, "po.pdf", "application/pdf"),
            attachment("ATT-2", word, "po.docx", DOCX),
            attachment("ATT-3", small, "note.txt", "text/plain"),
            attachment("ATT-4", sheet, "stock.txt", "text/plain")])
        handles = [self.handle("MSG-1", "CK-1", a, c) for a, c in (
            ("ATT-1", po), ("ATT-2", word), ("ATT-3", small), ("ATT-4", sheet))]
        with mock.patch("integrations.doc_convert.installed_backend", return_value=None):
            self.assertTrue(self.capture().run(tm.SCOPE)["complete"])
            kept = self.snapshot(handles)
            # Nothing dropped at the shipped bound: these fixtures are small.
            self.assertTrue(all(forms(self.store, sha256(c).hexdigest())[""]
                                for c in (po, word, small)))
            with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", len(small)):
                result = self.store.compact(vacuum=False)
            # The text file is its own text: kept, compressed.
            self.assertEqual(result["outcomes"], {"dropped": 2, "kept_small": 1,
                                                  "kept_text": 1})
            dropped = self.snapshot(handles)
        self.assertEqual(dropped, kept)
        self.assertEqual(kept[handles[0]][1]["row"]["content_text"].splitlines()[0],
                         "PURCHASE ORDER")
        self.assertIsNone(kept[handles[0]][3])   # no third page, before and after
        for content in (po, word):
            digest = sha256(content).hexdigest()
            self.assertEqual(forms(self.store, digest), {"": False, ".z": False,
                                                         ".dropped": True})
        self.assertEqual(forms(self.store, sha256(small).hexdigest())[""], True)
        self.assertEqual(forms(self.store, sha256(sheet).hexdigest()),
                         {"": False, ".z": True, ".dropped": False})
        self.assertEqual(self.store.attachment_content(handles[3], READER, self.audience),
                         sheet)
        # The claim gate's rows still check a value stated in the dropped PDF's page.
        claims = claim_checks.parse_value_claims(
            [{"value": "PO Number: 4500012345", "citation": handles[0] + "&page=1",
              "field": "row.content_text"}], [handles[0] + "&page=1"], [])
        item = {"id": "F1", "texts": ["The page reads PO Number: 4500012345."],
                "citations": [{"handle": handles[0] + "&page=1", "status": "resolved"}],
                "value_claims": claims}
        with fresh_memos():
            checked = claim_checks.check_items([item], rows=self.store.rows(READER,
                                                                            self.audience))
            self.assertTrue(checked["passed"], checked["failures"])
            self.assertFalse(claim_checks.check_items(
                [item], rows=self.store.rows(OUTSIDER, self.audience))["passed"])
        # Bytes: none served; the original verifies by its record, hashes as the bytes.
        self.assertIsNone(self.store.attachment_content(handles[0], READER, self.audience))
        self.assertTrue(self.store.attachment_verified(handles[0], READER, self.audience))
        self.assertEqual(self.store.attachment_hashes(handles[0], READER, self.audience),
                         {"sha256": sha256(po).hexdigest(), "bytes": len(po),
                          "quick_xor": quick_xor_hash(po)})
        # The typed fields of the claim gate's document read: the same row from the
        # reading the drop kept as from the bytes.
        row = {"name": "po.pdf", "content_type": "application/pdf"}
        with fresh_memos():
            reading = self.store.attachment_reading(handles[0], READER, self.audience)
            from_reading = document_fields.pinned_document_row(handles[0], handles[0], None,
                                                               row, reading=reading)
            self.assertTrue(from_reading["ok"], from_reading)
            self.assertEqual(from_reading, document_fields.pinned_document_row(
                handles[0], handles[0], po, row))
        # Another reading (other bytes or with an OCR text it is not of) is refused.
        self.assertFalse(document_fields.pinned_document_row(
            handles[0], handles[0], None, row, "other text", reading=reading)["ok"])
        # Re-fetch: one GET of the attachment, checked against the pin, never stored.
        calls = len(self.graph.calls)
        self.assertEqual(self.store.refetch_original(handles[0], READER, self.audience,
                                                     self.reader(), tm.CAPTURE), po)
        self.assertGreater(len(self.graph.calls), calls)
        self.assertFalse(forms(self.store, sha256(po).hexdigest())[""])
        self.assertEqual(self.store.original_source(handles[0], READER, self.audience),
                         {"mailbox_id": tm.BOX_A, "message_id": "MSG-1",
                          "attachment_id": "ATT-1", "content_sha256": sha256(po).hexdigest(),
                          "bytes": len(po), "stored": False})
        # A restricted reader gets nothing from any of them.
        for read in (self.store.attachment_reading, self.store.attachment_hashes,
                     self.store.original_source, self.store.attachment_content):
            self.assertIsNone(read(handles[0], OUTSIDER, self.audience), read.__name__)
        self.assertFalse(self.store.attachment_verified(handles[0], OUTSIDER, self.audience))
        calls = len(self.graph.calls)
        self.assertIsNone(self.store.refetch_original(handles[0], OUTSIDER, self.audience,
                                                      self.reader(), tm.CAPTURE))
        self.assertEqual(len(self.graph.calls), calls)
        # Changed at the source: the re-read bytes are not the pinned ones.
        self.graph.attachments[(tm.BOX_A, "MSG-1")][0] = attachment(
            "ATT-1", po + b"\n", "po.pdf", "application/pdf")
        self.assertIsNone(self.store.refetch_original(handles[0], READER, self.audience,
                                                      self.reader(), tm.CAPTURE))

    def test_an_altered_or_foreign_dropped_record_reads_as_nothing(self):
        po = pdf([["PURCHASE ORDER", "PO Number: 4500012345"]])
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            attachment("ATT-1", po, "po.pdf", "application/pdf")])
        handle = self.handle("MSG-1", "CK-1", "ATT-1", po)
        with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 1):
            self.capture().run(tm.SCOPE)   # dropped as it is captured
        digest = sha256(po).hexdigest()
        self.assertEqual(forms(self.store, digest), {"": False, ".z": False, ".dropped": True})
        with fresh_memos():
            self.assertIn("4500012345", self.store.resolve(handle, READER, self.audience,
                                                           text=True)["row"]["content_text"])
        path = ct.blob_path(self.store.blobs, digest, ct.DROPPED_SUFFIX)
        kept = path.read_bytes()
        document = json.loads(ct.inflate(kept))
        (key,) = document["readings"]
        document["readings"][key]["pages"][0][1] = "PO Number: 4599999999"
        for altered in (
                zlib.compress(json.dumps(document).encode()),   # checksum
                kept[:-3],                                                   # cut short
                zlib.compress(b'{"format": "other"}')):
            path.write_bytes(altered)
            with fresh_memos(), self.subTest(altered=altered[:8]):
                self.assertIsNone(self.store.resolve(handle, READER, self.audience,
                                                     text=True)["row"]["content_text"])
                self.assertFalse(self.store.attachment_verified(handle, READER, self.audience))
                self.assertIsNone(self.store.attachment_hashes(handle, READER, self.audience))
        path.write_bytes(kept)
        # A name or type the drop made no reading for is not guessed.
        with fresh_memos():
            self.assertIsNone(ct.captured_text(self.store.blobs, {
                "content_sha256": digest, "name": "other.pdf", "content_type": "application/pdf"}))
            self.assertIsNotNone(ct.captured_text(self.store.blobs, {
                "content_sha256": digest, "name": "po.pdf", "content_type": "application/pdf"}))

    def test_bodies_and_texts_are_stored_compressed_and_read_back(self):
        body = "Please confirm 120 cases at the agreed price. " * 200
        from integrations.doc_convert import DOCX
        from integrations.tests.office_fixtures import PURCHASE_ORDER_BODY, make_word
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", body=body, attachments=[
            attachment("ATT-1", make_word(PURCHASE_ORDER_BODY), "po.docx", DOCX)])
        with mock.patch("integrations.doc_convert.installed_backend", return_value=None):
            self.capture().run(tm.SCOPE)
        message = graph_read.mail_citation(tm.BOX_A, "MSG-1", "CK-1")
        (body_digest,) = [r[0] for r in self.store.db.execute(
            "SELECT body_sha256 FROM messages")]
        (text_digest,) = [r[0] for r in self.store.db.execute(
            "SELECT text_sha256 FROM attachments")]
        for digest in (body_digest, text_digest):
            self.assertEqual(forms(self.store, digest), {"": False, ".z": True,
                                                         ".dropped": False})
        self.assertEqual(self.store.resolve(message, READER, self.audience)["row"]["body_text"],
                         body)
        stored, size = self.store.db.execute("SELECT stored_bytes, bytes FROM blobs WHERE "
                                             "sha256=?", (body_digest,)).fetchone()
        self.assertLess(stored, size // 10)
        from ontology.project import MailCaptures
        captures = MailCaptures(self.state)
        try:
            (doc,) = [d for d in captures.documents() if d.citation == message]
        finally:
            captures.close()
        self.assertIn(("row.body_text", body), [(f.field_path, f.text) for f in doc.fields])


def pending(status):
    """``capture_text`` as a host with OCR that defers it: a PDF is ``ocr_pending`` with
    its text layer at capture, read as ``extracted_ocr`` by the OCR pass later."""
    real = capture_mail.capture_text

    def fake(content, record, *, max_chars=None, defer_ocr=False):
        if (record.get("name") or "").endswith(".pdf"):
            return (status["capture"], "layer text") if defer_ocr else \
                (ct.TEXT_OCR, f"OCR text of {record['name']}")
        return real(content, record, max_chars=max_chars, defer_ocr=defer_ocr)
    return fake


class PendingOcrTests(CompactCase):
    def test_pending_ocr_keeps_bytes_until_read_and_its_bound_defers_new_pdfs(self):
        scan1 = pdf([["scanned page one"]])
        scan2 = pdf([["scanned page two"], ["and more"]])
        note = b"notes " * 100
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            attachment("ATT-1", scan1, "scan1.pdf", "application/pdf")])
        self.graph.put(tm.BOX_A, "MSG-2", "CK-2", attachments=[
            attachment("ATT-2", scan2, "scan2.pdf", "application/pdf"),
            attachment("ATT-3", note, "note.txt", "text/plain")])
        h1, h2 = (self.handle("MSG-1", "CK-1", "ATT-1", scan1),
                  self.handle("MSG-2", "CK-2", "ATT-2", scan2))
        d1, d2 = sha256(scan1).hexdigest(), sha256(scan2).hexdigest()
        engine = mock.patch("integrations.document_understanding.local_ocr_available",
                            return_value=False)
        with mock.patch.object(capture_mail, "capture_text", pending(
                {"capture": ct.OCR_PENDING})), \
                mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 1), engine as ocr:
            # Run 1, no OCR pass: the first scan waits (bytes kept, the backlog at its
            # bound), so the second PDF is not downloaded; the text file is.
            run = self.capture(defer_ocr=True, max_pending_ocr_bytes=len(scan1)).run(tm.SCOPE)
            folder = run["mailboxes"][0]["folders"][0]
            self.assertEqual((folder["complete"], folder["reason"]),
                             (False, "attachments_deferred"))
            # Deferred when its message was captured and again by the round's repair.
            self.assertEqual(folder["counts"]["attachments_deferred"], 2)
            self.assertEqual(forms(self.store, d1)[""], True)
            self.assertEqual(self.store.pending_ocr_bytes(), len(scan1))
            statuses = dict(self.store.db.execute(
                "SELECT attachment_id, content_status FROM attachments"))
            self.assertEqual(statuses, {"ATT-1": "captured", "ATT-2": "deferred:ocr_backlog",
                                        "ATT-3": "captured"})
            (box,) = self.store.completeness(READER, self.audience)["mailboxes"]
            self.assertEqual((box["complete"], box["deferred_attachments"]), (False, 1))
            with fresh_memos():
                layer = self.store.resolve(h1, READER, self.audience, text=True)
            # Run 2, OCR on this host: the scan is read, then its bytes go; the deferred
            # PDF is still deferred (the repair ran before the reading).
            ocr.return_value = True
            run = self.capture(defer_ocr=True, max_pending_ocr_bytes=len(scan1)).run(tm.SCOPE)
            self.assertEqual(run["mailboxes"][0]["folders"][0]["reason"],
                             "attachments_deferred")
            self.assertEqual(forms(self.store, d1), {"": False, ".z": False, ".dropped": True})
            self.assertEqual(self.store.pending_ocr_bytes(), 0)
            with fresh_memos():
                read = self.store.resolve(h1, READER, self.audience, text=True)
                first = self.store.resolve(h1, READER, self.audience, text=True,
                                           text_version=1)
            self.assertEqual((read["row"]["content_text"], read["row"]["text_version"]),
                             ("OCR text of scan1.pdf", 2))
            # The reading made before OCR still resolves, from the kept reading.
            self.assertEqual(first["row"]["content_text"], layer["row"]["content_text"])
            # Run 3: under the bound, the deferred PDF is read, OCR-read, dropped.
            run = self.capture(defer_ocr=True, max_pending_ocr_bytes=len(scan1)).run(tm.SCOPE)
            folder = run["mailboxes"][0]["folders"][0]
            self.assertTrue(folder["complete"], folder)
            self.assertEqual(folder["counts"]["attachments_recovered"], 1)
        self.assertEqual(forms(self.store, d2), {"": False, ".z": False, ".dropped": True})
        with fresh_memos():
            self.assertEqual(self.store.resolve(h2, READER, self.audience, text=True)["row"]
                             ["content_text"], "OCR text of scan2.pdf")
        (box,) = self.store.completeness(READER, self.audience)["mailboxes"]
        self.assertEqual((box["complete"], box["deferred_attachments"]), (True, 0))

    def test_never_read_and_untyped_failures_keep_their_bytes(self):
        scan = pdf([["page"]])
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            attachment("ATT-1", scan, "scan.pdf", "application/pdf")])
        with mock.patch.object(capture_mail, "capture_text", pending(
                {"capture": ct.OCR_UNAVAILABLE})), \
                mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 1), \
                mock.patch("integrations.document_understanding.local_ocr_available",
                           return_value=False):
            self.capture(defer_ocr=True).run(tm.SCOPE)
        digest = sha256(scan).hexdigest()
        for status in (None, ct.OCR_UNAVAILABLE, ct.OCR_PENDING, "ocr_failed:tool_exit"):
            with self.subTest(status=status), \
                    mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 1):
                self.store.db.execute("UPDATE attachments SET text_status=?", (status,))
                self.assertEqual(self.store.settle_original(digest), "kept_pending")
        self.assertTrue(forms(self.store, digest)[""])


def deferred_scope():
    return capture_mail.CaptureScope(
        [{"address": tm.UPN_A, "folders": ["inbox"]}, {"address": tm.UPN_B, "folders": ["inbox"]}],
        window_days=90, internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC,
        ocr="deferred")


class DeferredOcrTests(CompactCase):
    """Scope ``ocr`` ``deferred`` / ``compact --defer-ocr``: a scan waiting for OCR drops its
    bytes too (``ocr_deferred``), its text layer still read and a named gap on every
    answer; ``ocr-deferred`` re-fetches it, OCR-reads it and drops the bytes again."""

    def setUp(self):
        super().setUp()
        from operating.tests.test_attested_missing import customer_po
        self.scan = pdf(customer_po("4500012345", "OR0003"))
        self.blank = pdf([[""]])
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            attachment("ATT-1", self.scan, "scan.pdf", "application/pdf"),
            attachment("ATT-2", self.blank, "blank.pdf", "application/pdf")])
        self.h1 = self.handle("MSG-1", "CK-1", "ATT-1", self.scan)
        self.h2 = self.handle("MSG-1", "CK-1", "ATT-2", self.blank)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(capture_mail, "capture_text", pending(
            {"capture": ct.OCR_PENDING})))
        self.patches.enter_context(mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 1))
        self.patches.enter_context(mock.patch(
            "integrations.document_understanding.local_ocr_available", return_value=False))

    def ocr_pass(self, **kwargs):
        kwargs.setdefault("budget_seconds", 60)
        return capture_mail.ocr_deferred_pass(self.store, self.reader(), tm.CAPTURE,
                                              tm.capture_audience(), **kwargs)

    def test_a_deferred_scan_drops_its_bytes_names_the_gap_and_is_read_later(self):
        run = self.capture(defer_ocr=True).run(deferred_scope())
        self.assertTrue(run["complete"], run)
        for content in (self.scan, self.blank):
            self.assertEqual(forms(self.store, sha256(content).hexdigest()),
                             {"": False, ".z": False, ".dropped": True})
        self.assertEqual(self.store.pending_ocr_bytes(), 0)
        (box,) = self.store.completeness(READER, self.audience)["mailboxes"]
        self.assertEqual((box["complete"], box["ocr_deferred_attachments"]), (True, 2))
        with fresh_memos():
            scan = self.store.resolve(self.h1, READER, self.audience, text=True)["row"]
            blank = self.store.resolve(self.h2, READER, self.audience, text=True)["row"]
            first = self.store.resolve(self.h1, READER, self.audience, text=True,
                                       text_version=1)["row"]
            self.assertIsNone(self.store.resolve(self.h1, OUTSIDER, self.audience, text=True))
            fields = {r.l1_handle.split("#")[0][-60:]: r.row for r in
                      self.snapshot_records()}
        # The text layer is still read; the answer names the gap, never "empty".
        self.assertEqual((scan["text_status"], scan["gaps"]),
                         ("ocr_deferred", [capture_mail.ocr_deferred_gap()]))
        self.assertIn("PO Number: 4500012345", scan["content_text"])
        self.assertEqual(first["text_status"], ct.OCR_PENDING)
        self.assertEqual(first["content_text"], scan["content_text"])
        self.assertIsNone(blank["content_text"])
        self.assertEqual(blank["gaps"], [capture_mail.ocr_deferred_gap()])
        (po_fields,) = fields.values()   # the typed fields of the scan's text layer
        self.assertEqual(po_fields["profile"], "customer_po")
        self.assertIn("ocr_deferred", [g["reason"] for g in po_fields["evidence"]["gaps"]])
        # A field its read pages do not state is unread, never "not stated".
        self.assertEqual(po_fields["fields"]["vendor"]["gap"], "not_stated_in_read_pages")
        with fresh_memos():
            reading = self.store.attachment_reading(self.h2, READER, self.audience)
            found = document_fields._fields(reading, self.h2)
        self.assertTrue(reading.ocr_deferred)
        self.assertEqual(found["status"], "ocr_deferred")   # never no_text
        # The pass: one GET each, re-hashed, OCR-read, stored, dropped again.
        calls = len(self.graph.calls)
        with mock.patch.object(capture_mail, "_ocr_engine", return_value=True):
            counts = self.ocr_pass()
        self.assertEqual({k: counts[k] for k in ("selected", "read", "texts_ocr",
                                                 "originals_dropped", "refetch_failed",
                                                 "left")}, {
            "selected": 2, "read": 2, "texts_ocr": 2, "originals_dropped": 2,
            "refetch_failed": 0, "left": 0})
        self.assertGreater(len(self.graph.calls), calls)
        for content in (self.scan, self.blank):
            self.assertEqual(forms(self.store, sha256(content).hexdigest()),
                             {"": False, ".z": False, ".dropped": True})
        with fresh_memos():
            read = self.store.resolve(self.h1, READER, self.audience, text=True)["row"]
            before = self.store.resolve(self.h1, READER, self.audience, text=True,
                                        text_version=2)["row"]
        self.assertEqual((read["content_text"], read["text_status"]),
                         ("OCR text of scan.pdf", ct.TEXT_OCR))
        self.assertNotIn("gaps", read)
        # The deferred reading still resolves as cited, with its gap.
        self.assertEqual((before["text_status"], before["content_text"], before["gaps"]),
                         ("ocr_deferred", scan["content_text"],
                          [capture_mail.ocr_deferred_gap()]))
        (box,) = self.store.completeness(READER, self.audience)["mailboxes"]
        self.assertEqual(box["ocr_deferred_attachments"], 0)

    def snapshot_records(self):
        from ontology.project import MailCaptures
        captures = MailCaptures(self.state)
        try:
            return list(captures.records(["documentFields"])["documentFields"])
        finally:
            captures.close()

    def test_changed_at_the_source_or_no_budget_stays_deferred(self):
        self.capture(defer_ocr=True).run(deferred_scope())
        self.graph.attachments[(tm.BOX_A, "MSG-1")][0] = attachment(
            "ATT-1", self.scan + b"\n", "scan.pdf", "application/pdf")
        self.assertEqual(self.ocr_pass(budget_seconds=0)["budget_spent"], 1)
        counts = self.ocr_pass()
        self.assertEqual((counts["read"], counts["refetch_failed"], counts["left"]), (1, 1, 1))
        with fresh_memos():
            row = self.store.resolve(self.h1, READER, self.audience, text=True)["row"]
        self.assertEqual((row["text_status"], row["gaps"]),
                         ("ocr_deferred", [capture_mail.ocr_deferred_gap()]))
        # Without a local OCR engine, the reading still needs OCR: nothing is stored.
        self.graph.attachments[(tm.BOX_A, "MSG-1")][0] = attachment(
            "ATT-1", self.scan, "scan.pdf", "application/pdf")
        with mock.patch.object(capture_mail, "capture_text",
                               return_value=(ct.OCR_UNAVAILABLE, "layer text")):
            self.assertEqual(self.ocr_pass()["left_deferred"], 1)
        self.assertEqual(self.store.ocr_deferred_count(), 1)

    def test_compact_defer_ocr_drops_pending_scans_only_when_asked(self):
        self.capture(defer_ocr=True).run(tm.SCOPE)   # inline: the scans wait with bytes
        digest = sha256(self.scan).hexdigest()
        self.assertTrue(forms(self.store, digest)[""])
        self.assertEqual(self.store.compact(vacuum=False)["outcomes"], {"kept_pending": 2})
        with self.store.db:   # a never-read attachment keeps its bytes even so
            self.store.db.execute("UPDATE attachments SET text_status=NULL WHERE "
                                  "attachment_id='ATT-2'")
        self.store.close()
        out = []
        with mock.patch("builtins.print", side_effect=out.append):
            code = capture_mail.main(["compact", "--state-dir", str(self.state),
                                      "--no-vacuum", "--defer-ocr"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out[0])["outcomes"], {"dropped": 1, "kept_pending": 1})
        self.store = MailCaptureStore(self.state, clock=tm.Clock())
        self.assertFalse(forms(self.store, digest)[""])
        self.assertEqual(self.store.ocr_deferred_count(), 1)

    def test_the_ocr_deferred_cli_needs_an_engine_and_reads_through_the_guarded_reader(self):
        self.capture(defer_ocr=True).run(deferred_scope())
        audience = Path(self.tmp.name) / "audience.json"
        audience.write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": {f"mail:{tm.UPN_A}": ["svc:mail-capture"],
                          f"mail:{tm.UPN_B}": ["svc:mail-capture"]},
            "classifications": {"mail:*": "restricted"}}))
        argv = ["--state-dir", str(self.state), "--live-audience", str(audience),
                "--budget-seconds", "60"]
        self.store.close()
        out = []
        with mock.patch("builtins.print", side_effect=out.append):
            with mock.patch.object(capture_mail, "_ocr_engine", return_value=False):
                self.assertEqual(capture_mail.ocr_deferred_cli(argv, environ=tm.ENV,
                                                               sender=self.graph), 2)
            with mock.patch.object(capture_mail, "_ocr_engine", return_value=True):
                code = capture_mail.ocr_deferred_cli(argv, environ=tm.ENV, sender=self.graph)
        self.assertEqual(json.loads(out[0])["error"], "ocr_engine_unavailable")
        self.assertEqual(code, 0, out)
        report = json.loads(out[1])
        self.assertEqual((report["read"], report["left"]), (2, 0))
        self.assertNotIn("4500012345", out[1])   # counts only
        self.store = MailCaptureStore(self.state, clock=tm.Clock())

    def test_the_scope_ocr_mode_is_closed(self):
        with self.assertRaises(capture_mail.CaptureError) as caught:
            capture_mail.CaptureScope.from_json({
                "schema": capture_mail.SCOPE_SCHEMA, "mailboxes": [{"address": tm.UPN_A}],
                "ocr": "later"}, allowlist=tm.SYNTHETIC)
        self.assertEqual(caught.exception.code, "scope_ocr_invalid")
        self.assertEqual(capture_mail.CaptureScope.from_json({
            "schema": capture_mail.SCOPE_SCHEMA, "mailboxes": [{"address": tm.UPN_A}],
            "ocr": "deferred"}, allowlist=tm.SYNTHETIC).ocr, "deferred")


class CardinalityTests(CompactCase):
    def test_every_message_keeps_its_text_at_1_10_and_60(self):
        from integrations.doc_convert import DOCX
        from integrations.tests.office_fixtures import make_word, paragraph
        budget = 0.5   # seconds per message, capture and drop together
        for count in (1, 10, 60):
            with self.subTest(count=count):
                self.tearDown_store()
                for n in range(count):
                    content = make_word(paragraph(f"PO Number: 45000{n:05d}")
                                        + paragraph(f"line {n}"))
                    self.graph.put(tm.BOX_A, f"MSG-{n}", f"CK-{n}", attachments=[
                        attachment(f"ATT-{n}", content, f"po-{n}.docx", DOCX)])
                started = time.monotonic()
                with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 64), \
                        mock.patch("integrations.doc_convert.installed_backend",
                                   return_value=None):
                    run = self.capture().run(tm.SCOPE)
                spent = time.monotonic() - started
                folder = run["mailboxes"][0]["folders"][0]
                self.assertEqual((folder["counts"]["attachments_captured"],
                                  folder["counts"]["originals_dropped"]), (count, count))
                rows = self.store.db.execute(
                    "SELECT l1_handle FROM attachments ORDER BY l1_handle").fetchall()
                self.assertEqual(len({r[0] for r in rows}), count)
                with fresh_memos():
                    texts = [self.store.resolve(r[0], READER, self.audience, text=True)
                             ["row"]["content_text"] for r in rows]
                self.assertEqual(sorted(t.split("\n")[0] for t in texts),
                                 sorted(f"PO Number: 45000{n:05d}" for n in range(count)))
                self.assertLess(spent / count, budget)

    def tearDown_store(self):
        self.store.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.graph = tm.FakeGraph()
        self.store = MailCaptureStore(self.state, clock=tm.Clock())


class CompactCliTests(CompactCase):
    def legacy_store(self, count=3):
        """A store as captured before compact storage: bodies, texts and originals as
        they are, no stored sizes."""
        for n in range(count):
            content = pdf([[f"PO Number: 45000{n:05d}", "PURCHASE ORDER"], ["page 2"]])
            self.graph.put(tm.BOX_A, f"MSG-{n}", f"CK-{n}", body=f"body {n} " * 300,
                           attachments=[attachment(f"ATT-{n}", content + os.urandom(4096),
                                                   f"po-{n}.pdf", "application/pdf")])
        with mock.patch.object(capture_mail, "_COMPRESSED_KINDS", ()):
            self.capture().run(tm.SCOPE)
        with self.store.db:
            self.store.db.execute("UPDATE blobs SET stored_bytes=NULL")
        return [r[0] for r in self.store.db.execute(
            "SELECT l1_handle FROM attachments ORDER BY l1_handle")] + [
            r[0] for r in self.store.db.execute("SELECT l1_handle FROM messages")]

    def test_compact_prunes_compresses_and_swaps_in_a_vacuumed_file(self):
        handles = self.legacy_store()
        before = self.snapshot(handles)
        bodies = {h: self.store.resolve(h, READER, self.audience)["row"].get("body_text")
                  for h in handles}
        self.store.close()
        out = []
        with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 2048), \
                mock.patch("builtins.print", side_effect=out.append):
            code = capture_mail.main(["compact", "--state-dir", str(self.state)])
        self.assertEqual(code, 0, out)
        result = json.loads(out[0])
        self.assertEqual(result["outcomes"], {"dropped": 3})
        self.assertEqual(result["compressed"], 6)   # 3 bodies, 3 texts
        self.assertEqual(result["vacuum"], "done")
        self.assertLess(result["bytes_after"], result["bytes_before"])
        self.store = MailCaptureStore(self.state, clock=tm.Clock())
        self.assertEqual(self.snapshot(handles), before)
        self.assertEqual({h: self.store.resolve(h, READER, self.audience)["row"]
                          .get("body_text") for h in handles}, bodies)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), oct(0o600))
        # Again: nothing more to do, the same reading.
        with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 2048):
            again = self.store.compact(vacuum=False)
        self.assertEqual((again["outcomes"], again["compressed"]), ({"dropped": 3}, 0))

    def test_without_disk_for_the_new_file_it_prunes_in_place(self):
        handles = self.legacy_store(1)
        before = self.snapshot(handles)
        with mock.patch.object(capture_mail, "DROP_ORIGINALS_ABOVE", 2048):
            result = self.store.compact(free_bytes=lambda _path: 1024)
        self.assertEqual((result["vacuum"], result["outcomes"]),
                         ("skipped:disk_space", {"dropped": 1}))
        self.assertEqual(self.snapshot(handles), before)

    def test_compact_refuses_while_a_host_cycle_holds_the_lock(self):
        import fcntl
        self.legacy_store(1)
        (self.state / "host").mkdir(exist_ok=True)
        with open(self.state / "host" / "cycle.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = []
            with mock.patch("builtins.print", side_effect=out.append):
                code = capture_mail.main(["compact", "--state-dir", str(self.state)])
        self.assertEqual((code, json.loads(out[0])["error"]), (2, "host_cycle_running"))
        self.assertTrue(all(forms(self.store, r[0])[""] for r in self.store.db.execute(
            "SELECT content_sha256 FROM attachments")))
        with mock.patch("builtins.print", side_effect=out.append):
            self.assertEqual(capture_mail.main(["compact", "--state-dir",
                                                str(Path(self.tmp.name) / "none")]), 2)
        self.assertEqual(json.loads(out[-1])["error"], "mail_capture_missing")


if __name__ == "__main__":
    unittest.main()
