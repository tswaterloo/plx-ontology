"""The attachment fast path (``integrations.attach_fetch``) and the Graph ``$batch`` rule.

Boundaries: the guard accepts a ``$batch`` POST only when every inner request is a GET it
would send itself (checked before I/O and again inside the sender); the downloader writes
only its own store (the mail store keeps one writer), keeps the people exclusion
fail-closed and never requests an attachment before its message passed; ingest's apply
step lands the fetched bytes and readings through the capture's write path; per-mailbox
429s back off and retry; no row is lost or duplicated at N=1/10/60. Page-capped OCR
names every page it left unread (``skipped_by_cap``, cap ``ocr_pages``) in the reading every
reader gets; the image prefilter only ever leaves an image unread (``unsupported``, noted);
the mail store's own unread images and scans are planned without a withheld mailbox, read
by the same pool and applied as new text versions only where still unread.
Fixtures: the in-process fake Graph of ``test_capture_mail``. Evidence label:
injected-transport.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

from integrations import attach_fetch, capture_mail, captured_text as ct, graph_read
from integrations import live_source_guard as guard
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff, ReadOnlyGuardError
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_mail_compact as tcm

GRAPH = "https://graph.microsoft.com/v1.0"
MSG_URL = f"/users/{tm.BOX_A}/messages/MSG-1"


def batch_body(requests):
    return json.dumps({"requests": requests}).encode()


class BatchGuardTests(unittest.TestCase):
    """The one non-GET Graph shape this module adds: a $batch of GETs, nothing else."""

    def ok(self, **extra):
        return {"id": "1", "method": "GET", "url": MSG_URL, **extra}

    def test_a_batch_of_gets_passes_both_checks(self):
        body = batch_body([self.ok(headers={"Prefer": 'IdType="ImmutableId"'}),
                           {**self.ok(), "id": "2"}])
        self.assertEqual(len(guard.check_batch_request(guard.GRAPH_BATCH_URL, body,
                                                       environ={})), 2)
        guard._refuse_non_read_send("POST", guard.GRAPH_BATCH_URL, body)   # no raise

    def test_every_other_shape_is_refused_before_io_and_in_the_sender(self):
        refused = {
            "inner_post": [self.ok(method="POST")],
            "inner_lowercase": [self.ok(method="get")],
            "inner_patch": [self.ok(method="PATCH")],
            "inner_body": [self.ok(body={"x": 1})],
            "depends_on": [self.ok(dependsOn=["0"])],
            "absolute_url": [self.ok(url=GRAPH + MSG_URL)],
            "protocol_relative": [self.ok(url="//evil.example/x")],
            "action_path": [self.ok(url=f"/users/{tm.BOX_A}/messages/MSG-1/send")],
            "send_mail": [self.ok(url=f"/users/{tm.BOX_A}/sendMail")],
            "nested_batch": [self.ok(url="/$batch")],
            "off_grammar": [self.ok(url="/subscriptions")],
            "header": [self.ok(headers={"Authorization": "Bearer x"})],
            "duplicate_id": [self.ok(), self.ok()],
            "too_many": [{**self.ok(), "id": str(i)} for i in range(21)],
            "empty": [],
        }
        for name, requests in refused.items():
            body = batch_body(requests)
            with self.subTest(name), self.assertRaises(ReadOnlyGuardError):
                guard.check_batch_request(guard.GRAPH_BATCH_URL, body, environ={})
            with self.subTest(name + ":sender"), self.assertRaises(ReadOnlyGuardError):
                guard._refuse_non_read_send("POST", guard.GRAPH_BATCH_URL, body)
        repeated = ('{"requests":[{"id":"1","method":"POST","method":"GET","url":"%s"}]}'
                    % MSG_URL).encode()
        for body in (b"not json", json.dumps({"requests": [self.ok()], "x": 1}).encode(),
                     json.dumps([self.ok()]).encode(), repeated,
                     batch_body([self.ok(headers={"Prefer": [1]})])):
            with self.assertRaises(ReadOnlyGuardError):
                guard.check_batch_request(guard.GRAPH_BATCH_URL, body, environ={})
            with self.assertRaises(ReadOnlyGuardError):
                guard._refuse_non_read_send("POST", guard.GRAPH_BATCH_URL, body)
        good = batch_body([self.ok()])
        for url in (guard.GRAPH_BATCH_URL + "?x=1", GRAPH + "/beta/$batch",
                    "https://graph.microsoft.com/beta/$batch"):
            with self.assertRaises(ReadOnlyGuardError):
                guard.check_batch_request(url, good, environ={})
        # The generic request path still refuses any Graph POST, the batch URL included.
        transport = guard.GuardedTransport(lambda *a: (200, {}, b"{}"), environ={})
        with self.assertRaises(ReadOnlyGuardError):
            transport.request("POST", guard.GRAPH_BATCH_URL, {}, form={"requests": "x"})


class BatchingGraph:
    """The fake Graph behind a $batch endpoint: the real sender check first, then each
    inner GET answered by ``tm.FakeGraph``. ``throttle``: inner attachment GETs answered
    429 this many times."""

    def __init__(self, graph: tm.FakeGraph) -> None:
        self.graph, self.throttle, self.batches = graph, 0, []
        self.throttle_batch = 0   # whole $batch POSTs answered 429

    @property
    def calls(self):
        return self.graph.calls

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if method == "POST" and urlsplit(url).path == "/v1.0/$batch":
            guard._refuse_non_read_send(method, url, body)   # the real last-line check
            if self.throttle_batch:
                self.throttle_batch -= 1
                return 429, {"retry-after": "0"}, b"{}"
            requests = json.loads(body)["requests"]
            self.batches.append(len(requests))
            responses = []
            for inner in requests:
                if self.throttle and "/attachments/" in inner["url"]:
                    self.throttle -= 1
                    responses.append({"id": inner["id"], "status": 429,
                                      "headers": {"Retry-After": "0"}, "body": {}})
                    continue
                status, out_headers, raw = self.graph(
                    "GET", GRAPH + inner["url"], {**headers, **inner.get("headers", {})},
                    None, timeout, max_bytes)
                responses.append({"id": inner["id"], "status": status,
                                  "headers": dict(out_headers), "body": json.loads(raw or b"{}")})
            return 200, {}, json.dumps({"responses": responses}).encode()
        return self.graph(method, url, headers, body, timeout, max_bytes)


class FastPathCase(tcm.DeferredAttachmentsTests):
    """The deferred-attachments fixture (three deferred PDFs over two mailboxes); its
    own tests are not run again here."""

    def fast(self, env=None, *, per_mailbox=2, threads=4, budget=600, batch=True):
        self.fetch_dir = Path(self.tmp.name) / "fetch"
        self.batching = getattr(self, "batching", None) or BatchingGraph(self.graph)
        api, _ = graph_read.build_graph(env or tm.ENV, sender=self.batching,
                                        retry=BoundedBackoff(sleep=lambda s: None,
                                                             jitter=lambda: 0.0))
        reader = GraphMailReader(api, tm.capture_audience())
        fetch = attach_fetch.FetchStore(self.fetch_dir)
        self.addCleanup(fetch.close)
        planned = fetch.plan(self.store)
        try:
            reader.prepare_people()
        except Exception:  # noqa: BLE001 - the CLI refuses here; nothing requested
            return planned, None, fetch
        withheld = self.store.withheld_mailboxes(reader.people)
        down = attach_fetch.Downloader(fetch, reader, tm.CAPTURE, threads=threads,
                                       per_mailbox=per_mailbox, batch=batch,
                                       free_bytes=lambda p: 1 << 50)
        with reader.directory_cache():
            report = down.run(fetch.todo(), withheld=withheld, budget_seconds=budget)
        return planned, report, fetch

    def read(self):
        return attach_fetch.read_pass(self.fetch_dir, workers=2, budget_seconds=600,
                                      reader=attach_fetch.inline_reader)

    def apply(self, env=None):
        api, _ = graph_read.build_graph(env or tm.ENV, sender=self.graph)
        return capture_mail.apply_fetched_pass(
            self.store, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
            self.fetch_dir, budget_seconds=600)


for _name in [n for n in dir(tcm.DeferredAttachmentsTests) if n.startswith("test")]:
    setattr(FastPathCase, _name, None)


class FastPathTests(FastPathCase):

    def test_fetch_read_apply_lands_every_deferred_attachment(self):
        before = len(self.graph.calls)
        planned, report, fetch = self.fast()
        self.assertEqual(planned["added"], 3)
        self.assertEqual((report["fetched"], report["left"], report["failed"]), (3, 0, 0))
        self.assertTrue(self.batching.batches)   # read through $batch
        self.assertLessEqual(max(self.batching.batches), 4)   # Exchange's per-mailbox 4
        # Bytes only: the mail store is untouched until ingest applies.
        self.assertEqual({r[0] for r in self.rows().values()},
                         {capture_mail.ATTACHMENT_DEFERRED})
        # No attachment GET before its message's exclusion read.
        paths = [unquote(urlsplit(c[1]).path) for c in self.graph.calls[before:]]
        for mid in ("MSG-1", "MSG-2"):
            first_attachment = min(i for i, p in enumerate(paths)
                                   if f"/messages/{mid}/attachments/" in p)
            self.assertTrue(any(p.endswith(f"/messages/{mid}") for p in paths[:first_attachment]))
        # Nothing applied before the reading is there.
        self.assertEqual(self.apply()["awaiting_reading"], 3)
        read = self.read()
        self.assertEqual(read["read"], 3)
        counts = self.apply()
        self.assertEqual((counts["applied"], counts["remaining"]), (3, 0))
        self.assertEqual(self.rows(), {"ATT-1": ("captured", "extracted"),
                                       "ATT-2": ("captured", ct.TEXT_OCR),
                                       "ATT-3": ("captured", "extracted")})
        handle = self.handle("MSG-1", "CK-1", "ATT-1", self.layer)
        with tcm.fresh_memos():
            row = self.store.resolve(handle, tcm.READER, self.audience, text=True)["row"]
            self.assertIn("4500012345", row["content_text"])
            # a restricted reader outside the mailbox still sees nothing
            self.assertIsNone(self.store.resolve(handle, tcm.OUTSIDER, self.audience))
        again = self.apply()   # idempotent: nothing left to apply
        self.assertEqual((again["applied"], again["not_deferred"]), (0, 3))

    def test_the_people_exclusion_stays_fail_closed(self):
        unguarded = {k: v for k, v in tm.ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        before = len(self.graph.calls)
        _planned, report, _fetch = self.fast(unguarded)
        self.assertIsNone(report)
        self.assertFalse([c for c in self.graph.calls[before:] if "/messages" in c[1]])
        self.assertEqual(self.apply(unguarded)["error"], "people_exclusion_unavailable")
        # Excluding B: B's mailbox is never requested; a message in A that B sent is
        # excluded and its attachments never requested; the apply marks it excluded.
        self.graph.messages[tm.BOX_A]["MSG-1"]["from"] = {
            "emailAddress": {"address": tm.UPN_B}}
        excluding_b = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        before = len(self.graph.calls)
        _planned, report, fetch = self.fast(excluding_b)
        self.assertEqual((report["withheld_skipped"], report["excluded"], report["fetched"]),
                         (1, 2, 0))
        sent = [unquote(c[1]) for c in self.graph.calls[before:]]
        self.assertFalse([u for u in sent if "MSG-2" in u or tm.BOX_B in u])
        self.assertFalse([u for u in sent if "/attachments/" in u])
        counts = self.apply(excluding_b)
        self.assertEqual((counts["excluded_messages"], counts["applied"]), (2, 0))
        self.assertEqual(self.rows()["ATT-1"][0], capture_mail.ATTACHMENT_EXCLUDED)
        self.assertEqual(self.rows()["ATT-3"][0], capture_mail.ATTACHMENT_DEFERRED)
        # B's row fetched while B was readable is not applied once B is withheld.
        _planned, report, _fetch = self.fast()
        self.assertEqual(report["fetched"], 1)
        self.read()
        counts = self.apply(excluding_b)
        self.assertEqual((counts["withheld"], counts["applied"]), (1, 0))
        self.assertEqual(self.rows()["ATT-3"][0], capture_mail.ATTACHMENT_DEFERRED)

    def test_a_moved_on_version_is_left_and_a_throttled_mailbox_retries(self):
        self.graph.messages[tm.BOX_B]["MSG-2"]["changeKey"] = "CK-9"
        self.batching = BatchingGraph(self.graph)
        self.batching.throttle = 2
        _planned, report, _fetch = self.fast()
        self.assertEqual((report["fetched"], report["changed_since"], report["throttled"]),
                         (2, 1, 2))
        self.read()
        counts = self.apply()
        self.assertEqual(counts["applied"], 2)
        self.assertEqual(self.rows()["ATT-3"][0], capture_mail.ATTACHMENT_DEFERRED)

    def test_a_throttled_batch_and_a_lost_guard_leave_rows_to_do(self):
        from unittest import mock
        from integrations import people_exclusion
        self.batching = BatchingGraph(self.graph)
        self.batching.throttle_batch = 12   # past get_batch's own retries
        before = len(self.graph.calls)
        with mock.patch.object(GraphMailReader, "_excluded_party",
                               side_effect=people_exclusion.ExclusionUnavailable("excluded_people_unresolved")):
            _planned, report, fetch = self.fast()
        self.assertEqual((report["fetched"], report.get("people_guard_lost")), (0, 1))
        self.assertEqual({tuple(r) for r in fetch.db.execute(
            "SELECT status, tries FROM work")}, {("todo", 0)})
        self.assertFalse([c for c in self.graph.calls[before:] if "/attachments/" in c[1]])
        _planned, report, fetch = self.fast()   # the next run reads them all
        self.assertEqual(report["fetched"], 3)

    def test_a_reading_past_its_time_is_the_capture_gap_status(self):
        self.fast()
        read = attach_fetch.read_pass(self.fetch_dir, workers=1, budget_seconds=600,
                                      seconds_each=0.001)
        self.assertEqual(read["by_status"], {capture_mail.EXTRACTION_TIMED_OUT: 3})
        self.assertEqual(self.apply()["applied"], 3)
        self.assertEqual({r[1] for r in self.rows().values()},
                         {capture_mail.EXTRACTION_TIMED_OUT})

    def test_a_row_read_since_by_ingest_is_not_applied_again(self):
        self.fast()
        self.read()
        meta = json.loads(self.store.db.execute(
            "SELECT metadata FROM attachments WHERE attachment_id='ATT-3'").fetchone()[0])
        self.store.fill_attachment(self.message("MSG-2", "CK-2", tm.BOX_B),
                                   {"id": "ATT-3", "metadata": meta,
                                    "content_status": "unavailable"})
        counts = self.apply()
        self.assertEqual((counts["applied"], counts["not_deferred"]), (2, 1))
        self.assertEqual(self.rows()["ATT-3"][0], "unavailable")

    def test_without_batch_the_same_rows_land(self):
        _planned, report, _fetch = self.fast(batch=False)
        self.assertEqual(report["fetched"], 3)
        self.assertFalse(self.batching.batches)


class CardinalityTests(FastPathCase):
    maxDiff = None
    """N deferred attachments over two mailboxes: each fetched, read and applied once."""

    def setUp(self):
        tcm.CompactCase.setUp(self)

    def build(self, n):
        contents = {}
        for i in range(n):
            box = tm.BOX_A if i % 2 else tm.BOX_B
            content = tcm.pdf([[f"PO Number: 45000{i:05d}"]])
            contents[f"ATT-{i}"] = content
            self.graph.put(box, f"MSG-{i // 3}-{box[-1]}", "CK-1", attachments=[
                tcm.attachment(f"ATT-{j}", contents.get(f"ATT-{j}") or tcm.pdf(
                    [[f"PO Number: 45000{j:05d}"]]), f"f{j}.pdf", "application/pdf")
                for j in range(n) if (tm.BOX_A if j % 2 else tm.BOX_B) == box
                and j // 3 == i // 3])
        self.capture(defer_ocr=True).run(tm.SCOPE)
        for aid, handle, meta in [(r[0], r[1], json.loads(r[2])) for r in self.store.db.execute(
                "SELECT attachment_id, message_handle, metadata FROM attachments")]:
            self.store.fill_attachment(handle, {"id": aid, "metadata": meta,
                                                "content_status": capture_mail.ATTACHMENT_DEFERRED})
        return contents

    def test_n_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.tearDown_store()
                self.build(n)
                planned, report, fetch = self.fast(threads=8)
                self.assertEqual(planned["added"], n, report)
                self.assertEqual(report["fetched"], n, report)
                self.assertEqual(fetch.db.execute(
                    "SELECT COUNT(*) FROM work WHERE status='fetched'").fetchone()[0], n)
                self.assertEqual(self.read()["read"], n)
                counts = self.apply()
                self.assertEqual((counts["applied"], counts["remaining"]), (n, 0))
                self.assertEqual(self.store.db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE content_status='captured'"
                ).fetchone()[0], n)

    def tearDown_store(self):
        """A fresh store, fake Graph and fetch dir for each N."""
        import shutil
        self.store.close()
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(Path(self.tmp.name) / "fetch", ignore_errors=True)
        self.graph = tm.FakeGraph()
        self.batching = None
        self.store = capture_mail.MailCaptureStore(self.state, clock=tm.Clock())


def _image(text: str | None, size=(900, 260), grain: bool = True) -> bytes:
    """A PNG: black text on white (a pasted table), or a smooth colour gradient (a photo)."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    if text is None:
        import random
        grain = random.Random(5)
        image = Image.new("RGB", size)
        image.putdata([(x * 200 // size[0] + grain.randrange(40), y * 200 // size[1]
                        + grain.randrange(40), (x + y) % 200 + grain.randrange(40))
                       for y in range(size[1]) for x in range(size[0])])
    else:
        image = Image.new("L", size, 255)
        try:
            font = ImageFont.load_default(size=40)
        except TypeError:
            font = ImageFont.load_default()
        ImageDraw.Draw(image).text((30, 90), text, fill=0, font=font)
        if grain:   # a scan's paper grain: bytes like a real paste
            import random
            noise = random.Random(3)
            image.putdata([max(0, p - noise.randrange(12)) for p in image.getdata()])
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _scan_pdf(pages: list[str | None]) -> bytes:
    """A PDF of image-only pages (each a rendered line of text, or blank) and text pages
    (``"text:<line>"``), in order."""
    import io
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=(612, 792))
    for line in pages:
        if line is not None and line.startswith("text:"):
            page.drawString(72, 720, line[5:])
        else:
            png = _image(line or "", size=(1700, 2200), grain=False)
            page.drawImage(ImageReader(io.BytesIO(png)), 0, 0, 612, 792)
        page.showPage()
    page.save()
    return out.getvalue()


def _ocr_tools() -> bool:
    import shutil
    try:
        import reportlab  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return all(shutil.which(t) for t in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract"))


@unittest.skipUnless(_ocr_tools(), "poppler, tesseract, reportlab or Pillow missing")
class PageCappedOcrTests(unittest.TestCase):
    """The reading contract of page-capped OCR at the capture's own boundaries."""

    def read(self, content, limits):
        return attach_fetch.inline_reader(content, {"name": "scan.pdf",
                                                    "content_type": "application/pdf"},
                                          False, limits)

    def test_pages_past_the_cap_are_named_never_dropped(self):
        content = _scan_pdf(["ORDER 4500011111", "ORDER 4500022222", "text:Terms page",
                             "ORDER 4500033333", "text:Last page"])
        status, text, _made = self.read(content, (2, 200))
        self.assertEqual(status, ct.TEXT_OCR)
        from integrations.captured_pages import text_pages
        self.assertEqual(len(text_pages(text)), 3)          # pages 1-3 read
        self.assertIn("4500022222", text)
        self.assertNotIn("4500033333", text)
        # Every reader's reading of the pinned text names pages 4-5 as not read.
        found = ct.read_text(content, "application/pdf", filename="scan.pdf", text=text)
        self.assertEqual(found.skipped_by_cap, {"first_page": 4, "last_page": 5,
                                                "cap": "ocr_pages", "limit": 2})
        self.assertEqual(found.text_withheld, "pdf_pages_skipped_by_cap:4-5")
        # The extractor's own evidence carries the same cap.
        from integrations import document_understanding as du
        with du.ocr_limits(2, 200):
            doc = du.extract_document(content, "application/pdf", original_url=URL,
                                      local_pdf_ocr=True)
        self.assertEqual(doc.evidence_attributes(URL)["extraction"]["skipped_by_cap"],
                         {"first_page": 4, "last_page": 5, "cap": "ocr_pages", "limit": 2})
        # Without limits every page is read and nothing is skipped.
        status, full, _made = self.read(content, None)
        self.assertEqual(len(text_pages(full)), 5)
        self.assertIn("4500033333", full)
        self.assertIsNone(ct.read_text(content, "application/pdf", filename="scan.pdf",
                                       text=full).skipped_by_cap)

    def test_a_capped_scan_that_read_nothing_stays_unread(self):
        status, text, _made = self.read(_scan_pdf([None, None, None]), (2, 200))
        self.assertEqual((status, text), (ct.OCR_PENDING, None))   # not "ocr_no_text"

    def test_unknown_limits_fail_closed(self):
        from integrations import document_understanding as du
        for pages, dpi in ((0, 200), (41, 200), (2, 99), (2, 301), (True, 200), (2, "200")):
            with self.subTest(pages=pages, dpi=dpi), self.assertRaises(ValueError):
                with du.ocr_limits(pages, dpi):
                    pass
        # Through the extraction process: a bad value is that reading's failure.
        extractors = capture_mail._Extractors(1, 60)
        self.addCleanup(extractors.close)
        status, text, _made = extractors.read(
            _scan_pdf(["ORDER 1"]), {"name": "s.pdf", "content_type": "application/pdf"},
            defer_ocr=False, defer_all=False, ocr_limits=(0, 10))
        self.assertEqual((status, text), ("failed", None))
        self.assertEqual(attach_fetch.main(["read", "--fetch-dir", "/nonexistent",
                                            "--budget-seconds", "1", "--ocr-dpi", "50"]), 2)
        self.assertEqual(attach_fetch.main(["read", "--fetch-dir", "/nonexistent",
                                            "--budget-seconds", "1", "--full-ocr",
                                            "--ocr-pages", "2"]), 2)

    def test_the_prefilter_skips_only_textless_looks(self):
        self.assertTrue(attach_fetch.text_likely(_image("INVOICE 4500012345 TOTAL 12.00")))
        self.assertFalse(attach_fetch.text_likely(_image(None)))
        self.assertTrue(attach_fetch.text_likely(b"not an image"))   # not judged: read


URL = "https://capture.invalid/scan.pdf"


@unittest.skipUnless(_ocr_tools(), "poppler, tesseract, reportlab or Pillow missing")
class StoredPlanTests(tcm.CompactCase):
    """The mail store's own unread images and scans: planned without a withheld
    mailbox, read by the fast path's pool (prefilter, page cap) and applied by ingest as
    new text versions, only where the attachment is still unread."""

    def setUp(self):
        super().setUp()
        self.table, self.photo = _image("INVOICE 4500012345 TOTAL 12.00"), _image(None, (420, 240))
        self.scan = _scan_pdf(["ORDER 4500011111", "ORDER 4500022222", "ORDER 4500033333"])
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            tcm.attachment("ATT-1", self.table, "table.png", "image/png"),
            tcm.attachment("ATT-2", self.photo, "photo.png", "image/png"),
            tcm.attachment("ATT-3", self.scan, "scan.pdf", "application/pdf"),
            tcm.attachment("ATT-5", self.table, "copy.png", "image/png")])
        self.graph.put(tm.BOX_B, "MSG-2", "CK-2", attachments=[
            tcm.attachment("ATT-4", self.table, "table.png", "image/png")])
        real = capture_mail.capture_text

        def backlog(content, record, **kw):   # the scan left for OCR, as behind a backlog
            if record.get("name") == "scan.pdf":
                return ct.OCR_PENDING, None
            return real(content, record, **kw)
        with mock.patch.object(capture_mail, "image_worth_reading", return_value=False), \
                mock.patch.object(capture_mail, "capture_text", backlog):
            self.capture(defer_ocr=True).run(tm.SCOPE)
        # As images were stored before mail images were read: unread.
        with self.store.db:
            self.store.db.execute("UPDATE attachments SET text_status='unsupported' WHERE "
                                  "attachment_id IN ('ATT-1','ATT-2','ATT-4','ATT-5')")
        self.fetch_dir = Path(self.tmp.name) / "fetch"

    def rows(self):
        return {r[0]: r[1] for r in self.store.db.execute(
            "SELECT attachment_id, text_status FROM attachments")}

    def apply(self, env=None):
        api, _ = graph_read.build_graph(env or tm.ENV, sender=self.graph)
        return capture_mail.apply_fetched_pass(
            self.store, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
            self.fetch_dir, budget_seconds=600)

    def test_plan_read_apply(self):
        self.assertEqual(self.rows()["ATT-3"], ct.OCR_PENDING)
        attach_fetch.FetchStore(self.fetch_dir).close()
        env = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        with mock.patch.dict(os.environ, {}, clear=False):
            # No usable exclusion: nothing is listed.
            unguarded = {k: v for k, v in tm.ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
            self.assertEqual(attach_fetch.main(["plan-stored", "--state-dir", str(self.state),
                                                "--fetch-dir", str(self.fetch_dir)],
                                               environ=unguarded), 3)
            self.assertFalse((self.fetch_dir / attach_fetch.STORED_DB).exists())
            self.assertEqual(attach_fetch.main(["plan-stored", "--state-dir", str(self.state),
                                                "--fetch-dir", str(self.fetch_dir)],
                                               environ=env), 0)
        plan = attach_fetch.StoredPlan(self.fetch_dir, read_only=True)
        self.addCleanup(plan.db.close)
        # BOX_B is withheld: its copy of the table is never listed (nor its bytes read).
        self.assertEqual(sorted(r[0] for r in plan.db.execute("SELECT kind FROM stored")),
                         ["image", "image", "image", "scan"])
        mail = capture_mail.MailCaptureStore.open_read_only(self.state, timeout=10)
        read = attach_fetch.read_pass(self.fetch_dir, workers=2, budget_seconds=600,
                                      reader=lambda c, m, i: attach_fetch.inline_reader(
                                          c, m, i, (2, 200)),
                                      ocr_limits=(2, 200), prefilter=True, mail=mail)
        self.assertEqual((read["read"], read["stored_read"], read["no_text_likely"],
                          read["page_capped"], read["digest_mismatch"], read["deduped"]),
                         (3, 3, 1, 1, 0, 1))   # the copy's bytes read once
        readings = attach_fetch.Readings(self.fetch_dir, read_only=True)
        self.addCleanup(readings.db.close)
        notes = dict(readings.db.execute("SELECT note, detail FROM notes").fetchall())
        capped = json.loads(notes[attach_fetch.NOTE_PAGE_CAP])
        self.assertEqual({k: capped[k] for k in ("first_page", "last_page", "cap", "limit")},
                         {"first_page": 3, "last_page": 3, "cap": "ocr_pages", "limit": 2})
        self.assertIn(attach_fetch.NOTE_NO_TEXT_LIKELY, notes)
        # A withheld mailbox stays as it was even when a reading of its bytes exists.
        counts = self.apply(env)["stored"]   # the exclusion checked again at apply
        self.assertEqual((counts["applied"], counts["attachments_filled"],
                          counts["withheld"]), (3, 3, 1))
        self.assertEqual(self.rows(), {"ATT-1": ct.TEXT_OCR, "ATT-2": "unsupported",
                                       "ATT-3": ct.TEXT_OCR, "ATT-4": "unsupported",
                                       "ATT-5": ct.TEXT_OCR})
        # A new text version (the earlier reading kept), cited as before; and the scan's
        # reading names its third page as not read.
        handle = self.store.db.execute(
            "SELECT l1_handle FROM attachments WHERE attachment_id='ATT-3'").fetchone()[0]
        self.assertEqual([v["text_status"] for v in self.store.text_chain(handle)],
                         [ct.OCR_PENDING, ct.TEXT_OCR])
        with tcm.fresh_memos():
            row = self.store.resolve(handle, tcm.READER, self.audience, text=True)["row"]
            self.assertIn("4500022222", row["content_text"])
            self.assertEqual(row.get("skipped_by_cap", {}).get("cap"), "ocr_pages")
            self.assertIsNone(self.store.resolve(handle, tcm.OUTSIDER, self.audience))
        again = self.apply(env)["stored"]   # idempotent: nothing still unread to fill
        self.assertEqual((again["applied"], again["withheld"]), (0, 1))
        unguarded = {k: v for k, v in tm.ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        self.assertEqual(self.apply(unguarded).get("error"), "people_exclusion_unavailable")
        self.assertEqual(self.rows()["ATT-4"], "unsupported")
        # The pages past the cap are read after all: --recap reads the capped scan in
        # full and ingest replaces the capped text (a third version), nothing else.
        recap = attach_fetch.recap_pass(self.fetch_dir, workers=1, budget_seconds=600,
                                        mail=mail, reader=attach_fetch.inline_reader)
        self.assertEqual((recap["read"], recap["replaced"]), (1, 1))
        filled = self.apply(env)["recapped"]
        self.assertEqual((filled["applied"], filled["attachments_filled"]), (1, 1))
        self.assertEqual([v["text_status"] for v in self.store.text_chain(handle)],
                         [ct.OCR_PENDING, ct.TEXT_OCR, ct.TEXT_OCR])
        with tcm.fresh_memos():
            row = self.store.resolve(handle, tcm.READER, self.audience, text=True)["row"]
            self.assertIn("4500033333", row["content_text"])
            self.assertIsNone(row.get("skipped_by_cap"))
        self.assertEqual(self.apply(env)["recapped"]["applied"], 0)   # idempotent


class IngestOptionTests(unittest.TestCase):
    def test_the_mail_child_gets_the_apply_step(self):
        import tempfile
        from operating import ingest_forever as ingest
        with tempfile.TemporaryDirectory() as tmp:
            fetch = Path(tmp) / "fetch"
            fetch.mkdir()
            spec = ingest.SOURCES["mail"]
            self.assertEqual(ingest._option(spec.fields["apply_fetched_dir"], "fetch",
                                            Path(tmp)), str(fetch.resolve()))
            with self.assertRaises(ingest.IngestError):   # not a directory: refused
                ingest._option(spec.fields["apply_fetched_dir"], "missing", Path(tmp))
            argv = spec.argv(Path(tmp), {"live_audience": "a.json",
                                         "apply_fetched_seconds": 300,
                                         "apply_fetched_dir": str(fetch)})
            self.assertIn("--apply-fetched-dir", argv)
            self.assertEqual(argv[argv.index("--apply-fetched-seconds") + 1], "300")
        import argparse
        with self.assertRaises(ValueError):   # one without the other is refused
            capture_mail.check_after(argparse.Namespace(apply_fetched_seconds=10.0))


if __name__ == "__main__":
    unittest.main()
