"""One attachment-text extraction process of the mail capture (``capture_mail._Extractors``).

Started by the capture with a scrubbed environment (``capture_mail.extraction_env``: no
token, key or secret) and spoken to over its stdin/stdout: each request is a
length-prefixed JSON header, then the attachment's bytes; each answer is one
length-prefixed JSON document, ``[text status, text, readings]``. JSON both ways, never pickle: a parser defect a crafted
file reaches here can at worst send a wrong answer, never code, to the credentialed capture.
It reads no source and writes nothing; it ends at end of input. Not a CLI for people.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from typing import BinaryIO

HEADER = struct.Struct(">IQ")   # JSON header bytes, content bytes


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks, left = [], size
    while left:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def serve(source: BinaryIO, sink: BinaryIO) -> None:
    from integrations.capture_mail import extract_attachment

    while True:
        head = _read_exact(source, HEADER.size)
        if head is None:
            return
        header_size, content_size = HEADER.unpack(head)
        header = _read_exact(source, header_size)
        content = _read_exact(source, content_size)
        if header is None or content is None:
            return
        request = json.loads(header)
        try:
            status, text, readings = extract_attachment(
                content, request["attachment"], request["defer_ocr"], request["defer_all"],
                readings=request.get("readings", True))
        except Exception:  # noqa: BLE001 - an extractor defect is this reading's status
            status, text, readings = "failed", None, None
        answer = json.dumps([status, text, readings], ensure_ascii=False).encode("utf-8")
        sink.write(struct.pack(">Q", len(answer)) + answer)
        sink.flush()


def main() -> None:
    import os

    # The answers go to a private copy of stdout; anything a parser prints goes to stderr
    # (discarded by the capture), never into the answer stream.
    if sys.argv[1:2] == ["--nice"]:
        # An OCR reading's process (``capture_mail.OCR_NICE``): it and the poppler and
        # tesseract it runs take the cores the capture leaves idle.
        nice = int(sys.argv[2])
        os.nice(nice)
        # With autogroup scheduling a process's nice ranks it only within its session
        # (this process leads its own): the session's group is lowered too, so the
        # group's weight is not one more full share beside the capture's. Pacing only.
        try:
            Path("/proc/self/autogroup").write_text(str(nice))
        except OSError:
            pass
    answers = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    serve(sys.stdin.buffer, answers)


if __name__ == "__main__":
    main()
