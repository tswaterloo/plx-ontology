"""The document reader's worker, run on a Spark (``integrations.spark_doc_reader`` ships the
repository's ``integrations`` and ``operating`` packages beside it and runs it over an ssh
pipe with the host's Python: pypdf, Pillow and poppler, no tesseract).

Reads frames on stdin (4-byte big-endian length, then the body; length 0 ends): a request is
a JSON frame ``{"name", "content_type", "parts"}`` followed by ``parts`` frames of the
file's bytes (joined; one when absent; a file of any size is cut into frames of at most
``PART`` bytes, so no size is refused);
the answer is one JSON frame ``{"status", "text", "seconds"}``. The reading is the capture's
own extractor (``captured_text.capture_text`` with ``defer_ocr=True``: the text layer only; a
page without one is ``ocr_pending``/``ocr_unavailable``, never guessed), under an alarm so one
bad file ends as ``extraction_timed_out`` and the worker goes on. Nothing is logged or kept:
no text, name or byte leaves the pipe or lands on this disk.
"""
from __future__ import annotations

import json
import signal
import struct
import sys
import time

TIMEOUT_S = 120


def read_frame(stream) -> bytes | None:
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack(">I", head)
    if size == 0:
        return None
    body = stream.read(size)
    if len(body) != size:
        raise EOFError("frame_truncated")
    return body


def read_content(stream, parts: int) -> bytes | None:
    """The ``parts`` frames of one file's bytes joined, or None at the end of input."""
    pieces = []
    for _ in range(parts):
        piece = read_frame(stream)
        if piece is None:
            return None
        pieces.append(piece)
    return b"".join(pieces)


def write_frame(stream, body: bytes) -> None:
    stream.write(struct.pack(">I", len(body)))
    stream.write(body)
    stream.flush()


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _over_bound(content: bytes) -> bool:
    """Whether the extractor's bounded reading would leave part of ``content`` unread: its
    bytes, or a PDF's pages, past the box extractor's bounds."""
    from integrations import document_understanding as du
    if len(content) > du.MAX_BYTES:
        return True
    if content[:5] != b"%PDF-":
        return False
    pages = du.pdf_page_count(content)
    return pages is not None and pages > du.MAX_PAGES


def read_one(meta: dict, content: bytes) -> dict:
    from integrations import captured_read_whole, captured_text
    started = time.monotonic()
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(TIMEOUT_S)
    record = {"name": meta.get("name") or "", "content_type": meta.get("content_type") or ""}
    try:
        if _over_bound(content):
            status, text = captured_read_whole.read_whole(content, record)
        else:
            status, text = captured_text.capture_text(content, record, defer_ocr=True)
            if status == captured_text.TEXT_EXCEEDS_BOUND:   # the text, not the bytes, was over
                status, text = captured_read_whole.read_whole(content, record)
    except _Timeout:
        status, text = "extraction_timed_out", None
    except Exception as exc:  # noqa: BLE001 - a failure is a status
        status, text = "failed", None
    finally:
        signal.alarm(0)
    return {"status": status, "text": text, "seconds": round(time.monotonic() - started, 2)}


def main() -> int:
    out = sys.stdout.buffer
    write_frame(out, json.dumps({"hello": "spark_doc_worker/1"}).encode())
    while True:
        head = read_frame(sys.stdin.buffer)
        if head is None:
            return 0
        meta = json.loads(head)
        content = read_content(sys.stdin.buffer, int(meta.get("parts") or 1))
        if content is None:
            return 1
        write_frame(out, json.dumps(read_one(meta, content)).encode())


if __name__ == "__main__":
    sys.exit(main())
