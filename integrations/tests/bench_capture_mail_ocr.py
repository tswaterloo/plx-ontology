"""OCR backlog drain on this host: scanned PDFs (image only, rendered here) stored awaiting
OCR, read by the capture's OCR queue (``capture_mail._OcrQueue``: extraction processes at
``OCR_NICE``, poppler + tesseract) with ``--workers`` at once. Prints scans per minute.
No source is read; the store is a temporary one.

Evidence label: isolated-fixture. Usage::

    python3 -m integrations.tests.bench_capture_mail_ocr [--scans 60] [--workers 4 14]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations import capture_mail  # noqa: E402
from integrations.capture_mail import MailCaptureStore, OCR_PENDING  # noqa: E402


def scanned_pdf(n: int) -> bytes:
    """A one-page PDF whose page is an image of typed lines (no text layer)."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.new("L", (1700, 2200), 255)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:
        font = ImageFont.load_default()
    for line in range(30):
        draw.text((120, 120 + line * 64), f"Synthetic scan {n} line {line}: order "
                  f"{1000 + n * 31 + line} quantity {line * 7} delivered", fill=0, font=font)
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=(612, 792))
    page.drawImage(ImageReader(image), 0, 0, 612, 792)
    page.save()
    return out.getvalue()


def seed(store: MailCaptureStore, scans: int) -> None:
    store.db.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                     ("bench-box", "b@x", "[]", "[]", "{}", "t", "t"))
    store.db.commit()
    for n in range(scans):
        content = scanned_pdf(n)
        digest = store.put_blob(content, sha256(content).hexdigest())
        handle = f"graph-mail://bench-box/S{n}@CK"
        store.add_version({"mailbox": "bench-box", "id": f"S{n}", "citation": handle,
                           "change_key": "CK", "content_sha256": "d"}, folder_id="F",
                          body_status="captured", attachments=[{
                              "id": "A", "l1_handle": handle + "#a", "content_sha256": digest,
                              "content_status": "captured", "text_status": OCR_PENDING,
                              "metadata": {"id": "A", "name": f"scan{n}.pdf",
                                           "content_type": "application/pdf",
                                           "size": len(content)}}])


def drain(store: MailCaptureStore, workers: int) -> dict[str, object]:
    queue = capture_mail._OcrQueue(store, workers, lambda: False, processes=True)
    started = time.monotonic()
    queue.refill(force=True)
    while queue.busy():
        queue.wait(1.0)
        queue.drain()
    took = time.monotonic() - started
    counts = queue.close()
    return {"workers": workers, "seconds": round(took, 1), "read": counts["read"],
            "texts_ocr": counts["texts_ocr"],
            "scans_per_minute": round(counts["read"] / took * 60, 1)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scans", type=int, default=60)
    parser.add_argument("--workers", type=int, nargs="+", default=[4, 14])
    args = parser.parse_args(argv)
    for workers in args.workers:
        with tempfile.TemporaryDirectory() as tmp:
            store = MailCaptureStore(Path(tmp) / "state")
            seed(store, args.scans)
            print(json.dumps(drain(store, workers)), flush=True)
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
