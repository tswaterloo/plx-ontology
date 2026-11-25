"""ASK-GENIE §20 #7: SharePoint file cap removal (streaming download with chunked
sha256/QuickXorHash + disk_guard headroom, typed not_text status for non-text binaries,
no default 20 MB / 100 MB / 200-page caps in capture or document_understanding).

Evidence label: injected-transport (no network, synthetic files and disk guard)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations import capture_sharepoint as cs
from integrations import captured_text, document_understanding as du, graph_read
from integrations.live_source_guard import SourceUnavailable
from operating.disk_guard import DiskRefused


def _minimal_pdf(pages: int) -> bytes:
    """Build a valid minimal PDF with ``pages`` pages, each containing ``Page N``."""
    objs: list[bytes] = []
    kids = []
    # 1: Catalog, 2: Pages, 3: Font
    objs.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objs.append(b"")  # placeholder for 2: Pages
    objs.append(b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    next_id = 4
    for i in range(1, pages + 1):
        page_id = next_id
        content_id = next_id + 1
        next_id += 2
        kids.append(f"{page_id} 0 R".encode())
        stream = f"BT /F1 12 Tf 72 720 Td (Page {i} text) Tj ET".encode()
        objs.append(
            f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >> endobj\n".encode()
        )
        objs.append(
            f"{content_id} 0 obj << /Length {len(stream)} >> stream\n".encode()
            + stream + b"\nendstream endobj\n"
        )
    objs[1] = (b"2 0 obj << /Type /Pages /Count " + str(pages).encode()
               + b" /Kids [" + b" ".join(kids) + b"] >> endobj\n")
    header = b"%PDF-1.4\n"
    offsets = []
    body = bytearray(header)
    for o in objs:
        offsets.append(len(body))
        body.extend(o)
    xref_pos = len(body)
    body.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        body.extend(f"{off:010d} 00000 n \n".encode())
    body.extend(
        f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(body)


class SharePointCapRemovalTests(unittest.TestCase):
    def test_streaming_quick_xor_hash_matches_whole_bytes(self):
        for n in (0, 1, 7, 159, 160, 161, 320, 1000, 10000):
            data = bytes((i * 37 + 13) % 256 for i in range(n))
            expected = graph_read.quick_xor_hash(data)
            for step in (1, 17, 159, 160, 161, 4096):
                h = graph_read.QuickXorHasher()
                for lo in range(0, len(data), step):
                    h.update(data[lo:lo + step])
                self.assertEqual(h.base64_digest(), expected, (n, step))
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "f.bin"
                p.write_bytes(data)
                self.assertEqual(graph_read.quick_xor_hash_file(p, chunk_bytes=137), expected)

    def test_non_text_binary_classification(self):
        for name in ("clip.mp4", "promo.MOV", "setup.exe", "label.psd", "art.ai", "dash.pbix"):
            self.assertTrue(cs.is_non_text_file({"name": name}), name)
        for name in ("spec.pdf", "sheet.xlsx", "deck.pptx", "memo.docx", "pack.zip",
                     "notes.txt", "scan.png", "coa.jpg"):
            self.assertFalse(cs.is_non_text_file({"name": name}), name)

    def test_extract_document_and_capture_text_have_no_default_20mb_or_200_page_cap(self):
        # > 20 MB plain text extracts whole instead of failing or returning exceeds_bound
        big = (b"PO100555 batch update line\n") * (du.MAX_BYTES // 27 + 200)
        self.assertGreater(len(big), du.MAX_BYTES)
        found = du.extract_document(big, "text/plain",
                                    original_url="https://example.invalid/big.txt",
                                    filename="big.txt")
        self.assertEqual(found.status, "extracted")
        self.assertEqual(len(found.text), len(big))
        status, text = captured_text.capture_text(
            big, {"name": "big.txt", "mime_type": "text/plain",
                  "original_url": "https://example.invalid/big.txt"})
        self.assertEqual(status, "extracted")
        self.assertEqual(len(text or ""), len(big))

        # > 200-page PDF reads every page with no skipped_pages by default
        pdf_bytes = _minimal_pdf(205)
        doc = du.extract_document(pdf_bytes, "application/pdf",
                                  original_url="https://example.invalid/long.pdf",
                                  filename="long.pdf")
        self.assertEqual(doc.status, "extracted")
        self.assertIsNone(doc.skipped_pages)
        self.assertEqual(len(doc.pages), 205)
        self.assertIn("Page 205 text", doc.text)

        # Explicit max_pages bound still works when requested
        bounded = du.extract_document(pdf_bytes, "application/pdf",
                                      original_url="https://example.invalid/long.pdf",
                                      filename="long.pdf", max_pages=10)
        self.assertEqual(len(bounded.pages), 10)
        self.assertEqual(bounded.skipped_pages, (11, 205))

    def test_streaming_download_refuses_cleanly_on_disk_floor(self):
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_sharepoint as tcs
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            drive = tcs.FakeDrive()
            drive.folder("01SPECS", "Specs")
            drive.file("01FILEA", "spec.txt", "01SPECS", b"hello streaming world", mime="text/plain")
            api, transport = graph_read.build_graph(
                tcs.ENV, sender=drive, retry=BoundedBackoff(sleep=lambda s: None,
                                                            jitter=lambda: 0.0))
            reader = graph_read.SharePointReader(api, tcs.capture_audience(), tcs.DRIVE,
                                                 transport=transport)
            # Normal streaming download succeeds and verifies hashes on disk
            read = reader.get_content(tcs.CAPTURE, "01FILEA", dest_path=dest)
            self.assertEqual(read["content_status"], "read")
            self.assertEqual(read["content_path"], dest)
            self.assertTrue(dest.exists())
            self.assertEqual(hashlib.sha256(dest.read_bytes()).hexdigest(), read["content_sha256"])

            # DiskRefused from headroom fails closed and leaves no partial file behind
            class NoRoom:
                def room(self, _written: int) -> None:
                    raise DiskRefused("no room")

            dest2 = Path(tmp) / "out2.bin"
            with self.assertRaises(SourceUnavailable) as raised:
                reader.get_content(tcs.CAPTURE, "01FILEA", dest_path=dest2, headroom=NoRoom())
            self.assertIn("disk_floor_reached", str(raised.exception))
            self.assertFalse(dest2.exists())

    def test_uncapped_capture_marks_non_text_binary_without_downloading(self):
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_sharepoint as tcs
        with tempfile.TemporaryDirectory() as tmp:
            drive = tcs.FakeDrive()
            drive.folder("01SPECS", "Specs")
            drive.file("01FILEA", "spec.txt", "01SPECS", b"hello spec", mime="text/plain")
            drive.file("01VID", "promo.mp4", "01SPECS", b"video-bytes", mime="video/mp4")
            drive.fail_download["01VID"] = 500  # must never be downloaded
            store = cs.SharePointCaptureStore(Path(tmp) / "sp")
            try:
                api, transport = graph_read.build_graph(
                    tcs.ENV, sender=drive, retry=BoundedBackoff(sleep=lambda s: None,
                                                                jitter=lambda: 0.0))
                reader = graph_read.SharePointReader(api, tcs.capture_audience(), tcs.DRIVE,
                                                     transport=transport)
                cap = cs.SharePointCapture(store, lambda _d: reader, tcs.CAPTURE)
                run = cap.run(tcs.scope(folders=["/Specs"]))
                root = run["targets"][0]["roots"][0]
                self.assertTrue(root["complete"])
                self.assertEqual(root["counts"]["contents_not_text"], 1)
                vid = store.current(tcs.DRIVE, "01VID")
                self.assertEqual(vid["content_status"], cs.NOT_TEXT)
                self.assertEqual(vid["text_status"], "binary_no_text")
                (comp,) = store.completeness(tcs.CAPTURE, tcs.capture_audience())["roots"]
                self.assertEqual(comp["content_not_text"], 1)
                self.assertTrue(comp["complete"])
            finally:
                store.close()

    def test_streaming_download_429_honours_retry_after_and_defaults_when_omitted(self):
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_sharepoint as tcs
        for header, expected_wait in (({"Retry-After": "7"}, 7.0),
                                      ({}, float(cs.DISCOVERY_DEFAULT_WAIT_SECONDS))):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as tmp:
                drive = tcs.FakeDrive()
                drive.folder("01SPECS", "Specs")
                drive.file("01FILEA", "spec.txt", "01SPECS", b"retried ok", mime="text/plain")
                download_attempts = {"n": 0}

                def sender(method, url, headers, body, timeout, max_bytes):
                    if "download.example" in url or "/download/" in url or "preauth" in url or "sharepoint.com" in url:
                        download_attempts["n"] += 1
                        if download_attempts["n"] == 1:
                            return 429, dict(header), b""
                    return drive(method, url, headers, body, timeout, max_bytes)

                waits: list[float] = []
                store = cs.SharePointCaptureStore(Path(tmp) / "sp")
                try:
                    api, transport = graph_read.build_graph(
                        tcs.ENV, sender=sender, retry=BoundedBackoff(sleep=lambda s: None,
                                                                     jitter=lambda: 0.0))
                    reader = graph_read.SharePointReader(api, tcs.capture_audience(), tcs.DRIVE,
                                                         transport=transport)
                    cap = cs.SharePointCapture(store, lambda _d: reader, tcs.CAPTURE,
                                               sleep=waits.append)
                    run = cap.run(tcs.scope(folders=["/Specs"]))
                    root = run["targets"][0]["roots"][0]
                    self.assertTrue(root["complete"])
                    self.assertEqual(store.current(tcs.DRIVE, "01FILEA")["content_status"],
                                     cs.CAPTURED)
                    self.assertEqual(waits, [expected_wait])
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
