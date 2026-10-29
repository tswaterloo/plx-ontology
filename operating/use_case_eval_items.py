"""Pinned originals for use-case evaluation: one capture route and the proof of a capture.

A delivered use-case document that an evaluation is anchored to (questions citing
its line numbers, arithmetic over the figures it states) is pinned by the SHA-256
of its exact bytes. ``capture_shared_original`` is the only way such an original
enters our state: the guarded shared-link reader
(``integrations.shared_link.read_shared_link`` over ``graph_read.build_graph``'s
GET-only ``GuardedTransport``; a link off the tenant's SharePoint/OneDrive hosts,
or any request outside the closed grammar, is refused before I/O). There is no
parameter that accepts bytes, a path or another client. The bytes read must hash
to the expected SHA-256 and to the drive citation's ``#sha256=``; a mismatch
stores nothing. A match is pinned content-addressed in the use-case intake state
(``<state>/use_cases/originals/<sha256>``, ``IntakeState.pin``) and recorded in
``<state>/use_cases/pinned_captures.jsonl`` with its ``sharepoint://`` drive
citation, drive/item/eTag, its path, the item's source ACL at read time and the
capturing principals. The sharing URL is never stored or returned.

``captured_original`` is the proof a consumer needs before it reads the body: a
recorded capture of that SHA-256 by this route whose stored bytes still hash to
it. Without one the body read is unproven and refused (``original_capture_missing``
/ ``original_capture_changed``). ``line_spans`` reads 1-based inclusive line
anchors over those exact bytes (a line ends at ``\\n``; a final newline opens no
line) and returns digests, never text.

Evidence: tests use an injected transport; the live capture is live-unverified
until live check ``eval.use_case_proposal`` has a verifier result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from ontology.core import Actor
from operating.use_case_intake import IntakeState

CAPTURE_SCHEMA = "plx-use-case-pinned-capture/v1"
CAPTURES_FILE = "pinned_captures.jsonl"
ROUTE = "integrations.shared_link.read_shared_link"
MAX_CAPTURE_BYTES = 20_000_000
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CaptureRefused(ValueError):
    """A capture or a body read that is not proven; ``code`` names why (never a value)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def capture_shared_original(state_dir: str | Path, sharing_url: str, *, expected_sha256: str,
                            actor: Actor, audience: Any, environ: Mapping[str, str],
                            sender: Any = None, clock: Callable[[], datetime] | None = None,
                            max_bytes: int = MAX_CAPTURE_BYTES) -> dict[str, Any]:
    """Read ``sharing_url`` through the guarded shared-link reader as ``actor`` and pin
    it when its bytes hash to ``expected_sha256``; returns the capture record."""
    from integrations.graph_read import build_graph
    from integrations.shared_link import read_shared_link

    if not isinstance(expected_sha256, str) or not _HEX64.match(expected_sha256):
        raise CaptureRefused("expected_sha256_invalid")
    api, transport = build_graph(environ, sender=sender)
    read = read_shared_link(api, transport, audience, actor, sharing_url,
                            max_bytes=max_bytes, read_permissions=True)
    if read.get("content_status") != "read" or not isinstance(read.get("content"), bytes):
        raise CaptureRefused("original_exceeds_cap")
    content = read["content"]
    digest = sha256(content).hexdigest()
    if digest != read.get("content_sha256") or \
            not str(read.get("drive_citation", "")).endswith(f"#sha256={digest}"):
        raise CaptureRefused("capture_digest_inconsistent")
    if digest != expected_sha256:
        raise CaptureRefused("sha256_mismatch")  # nothing pinned, nothing recorded
    record = read.get("record") or {}
    files = IntakeState(state_dir)
    with files.lock():
        stored = files.pin(content)
        capture = {
            "schema": CAPTURE_SCHEMA, "route": ROUTE, "source": read.get("source"),
            "captured_at": (clock or (lambda: datetime.now(timezone.utc)))()
            .astimezone(timezone.utc).isoformat(),
            "sha256": digest, "bytes": len(content), "stored": stored,
            "drive_citation": read["drive_citation"], "drive_id": read.get("drive_id"),
            "item_id": read.get("item_id"), "etag": read.get("etag"),
            "name": record.get("name"), "path": record.get("path"),
            "source_acl": record.get("source_acl"),
            "captured_by": sorted(actor.principals)}
        descriptor = os.open(files.root / CAPTURES_FILE,
                             os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(capture, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return capture


def captured_original(state_dir: str | Path, expected_sha256: str
                      ) -> tuple[bytes, dict[str, Any]]:
    """The bytes and newest capture record of ``expected_sha256``, only when a capture by
    ``ROUTE`` was recorded and the stored bytes still hash to it."""
    files = IntakeState(state_dir)
    path = files.root / CAPTURES_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    captures = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn append is no proof
        if isinstance(entry, Mapping) and entry.get("schema") == CAPTURE_SCHEMA and \
                entry.get("route") == ROUTE and entry.get("sha256") == expected_sha256:
            captures.append(dict(entry))
    if not captures:
        raise CaptureRefused("original_capture_missing")
    capture = captures[-1]
    try:
        content = (files.root / "originals" / expected_sha256).read_bytes()
    except OSError:
        raise CaptureRefused("original_capture_changed") from None
    if sha256(content).hexdigest() != expected_sha256:
        raise CaptureRefused("original_capture_changed")
    return content, capture


def lines_of(content: bytes) -> list[bytes]:
    """The original's lines as anchored (1-based inclusive, ``\\n``-terminated)."""
    lines = content.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return [line[:-1] if line.endswith(b"\r") else line for line in lines]


def line_spans(content: bytes, spans: Sequence[Sequence[int]]) -> list[dict[str, Any]]:
    """Each ``[first, last]`` anchor: in range or not, and the SHA-256 of its lines."""
    lines = lines_of(content)
    out = []
    for span in spans:
        first, last = int(span[0]), int(span[1])
        ok = 1 <= first <= last <= len(lines)
        out.append({"lines": [first, last], "in_range": ok, "excerpt_sha256": sha256(
            b"\n".join(lines[first - 1:last])).hexdigest() if ok else None})
    return out


__all__ = ["CAPTURES_FILE", "CAPTURE_SCHEMA", "CaptureRefused", "ROUTE",
           "capture_shared_original", "captured_original", "line_spans", "lines_of"]
