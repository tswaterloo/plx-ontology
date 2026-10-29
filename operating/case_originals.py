"""Operator-supplied originals for cases: stored, fingerprinted, OCRed, citable.

Implements ``plx.case_attach_original``.
An employee (the operator) places a document, for example Ricardo's AO scan,
in the case runtime's originals inbox on the Workbench host, inside the
directory named for the principal who may read it
(``originals-inbox/staff:quality/scan.pdf``); the investigating agent attaches
it to a case by its file name. The bytes are copied into a
content-addressed store, their SHA-256 recorded, and their text read through
the existing extraction contract (``extract_document``, with local OCR:
macOS Vision or poppler + tesseract). The original then resolves through
``original://`` citations, exactly like ``api://`` rows do, so findings and
calculations can cite it and every read re-checks it.

Rules:

* Only a regular file directly inside a principal directory of the inbox,
  never a path, link or directory; at most ``MAX_BYTES``; a PDF (``%PDF-``
  magic) or UTF-8 text such as Markdown, the type detected from the bytes,
  never from the name (``media_type_of``). Nothing is fetched from a URL and
  nothing is written outside the case runtime.
* A text original is its own text: no extractor runs. Its pages are line
  windows (``#page=<n>`` names lines ``lines[0]..lines[1]``, 1-based, a line
  ending at ``\n``), each small enough that one page read fits one MCP
  response (``operating.mcp_server.MAX_RESULT_BYTES``) whole; a single line
  longer than that spans pages. Page text is labelled
  ``text_source: "original_text"``.
* Audience: exactly the principal whose inbox directory held the file, as the
  operator declared by placing it there. Only an actor holding that principal
  can attach it (or learn that the name exists), never ``*``. Classification
  is the actor's clearance. A reader outside the audience, of another tenant
  or with a lower clearance cannot resolve the original, so case items citing
  it are withheld from them.
* Attaching consumes the inbox file: it is claimed by an atomic rename before
  it is read and deleted once stored, so it cannot be attached again by
  anyone; a refused file is put back.
* ``original://<id>`` resolves to the file's identity and extraction summary;
  ``original://<id>#page=<n>`` to that page's text. Every resolve re-hashes
  the stored bytes: a changed or missing file fails closed
  (``original_content_changed`` / ``original_missing``).
* OCR text is machine-read. Pages read by OCR are listed in ``ocr_pages`` and
  their text is labelled ``text_source: "ocr"``: it is evidence of what the
  scan appears to say, for a person to confirm, never a verified value. A
  failed extraction still stores the original, with ``extraction.status``
  saying why its text is unavailable. OCR reads a PDF over the page cap on its
  first pages only; the rest is ``extraction.skipped_by_cap`` (the extraction
  contract's field: first and last page, ``cap``, ``limit``), so its pages are
  never the whole file's text.
* Re-extraction (``plx.case_attach_original`` with ``citation`` and ``reason``) is
  explicit: after OCR is enabled or repaired, a reader of the original reads its stored bytes again
  as a new immutable version (``original://<id>#extraction=<k>``,
  ``...#extraction=<k>&page=<n>``) with who, when, why and the OCR setting.
  The bytes, id, audience and earlier versions never change, so every earlier
  citation keeps naming the text it named; ``#page=<n>`` is always version 1.
* Source originals (``store_source``): bytes the case runtime read from a live
  source (a SharePoint drive item, ``plx.case_attach_original`` drive_id + item_id) are
  stored the same way, with an immutable provenance record (drive, item, eTag,
  cTag, path, attach-time source ACL, the ``sharepoint://`` citation). Such an
  original is readable only while the store's ``source_gate(provenance, actor)``
  says the reading actor may read that source item now (the case runtime
  re-reads the item's metadata and permissions live for the reader on every
  read; source down or no gate configured -> nobody). Its audience principals
  are only an upper bound; the attach-time ACL is history, never access.
  A PDF mail attachment (provenance ``source: graph-mail-attachment``: mailbox,
  message, attachment, sha256 and the byte-pinned ``graph-mail://...#attachment=``
  citation, ``CaseRuntime.attach_mail_attachment_original``) is gated the same
  way: the case runtime re-reads the message's metadata for the reader (mail
  scope, audience, owner ACL, folder provenance) on every read. An original seeded
  from a recorded guarded capture (provenance ``source: RECORDED_CAPTURE_SOURCE``: the
  capture's route, drive item, eTag, source ACL and ``captured_at``; evaluation state,
  never re-downloaded) is gated by its recorded permissions (``CaseRuntime``: a live
  re-read where the host has a reader for the drive, else the recorded ones only while
  the capture is recent).
* Captured case originals (``captured``, ``operating.captured_originals``): the files
  ``scripts.case_originals read`` holds in ``<state>/case-originals`` resolve here as
  ``original://co-|nr-|rv-<12 hex>`` (and ``#page=<n>``), read in place (never copied) and
  readable only for a reader the gate of their source item admits now.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterator, Mapping
from uuid import uuid4

from integrations.document_understanding import MAX_BYTES, MAX_PAGES, extract_document
from ontology.core import LEVELS, Actor, ValidationError

from .cases import CitationUnresolvable, actor_label
from .entitlements import _AGENT_PREFIXES  # "agents are never principals" (one rule)
from .ontology_tools_evidence import DEFAULT_READ_CHARS

SCHEME = "original://"
RECORDED_CAPTURE_SOURCE = "sharepoint-recorded-capture"
INBOX_DIR = "originals-inbox"
STORE_DIR = "originals"
DB_FILE = "originals.sqlite"
PDF_MAGIC = b"%PDF-"
PDF_MEDIA_TYPE = "application/pdf"
TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"
TEXT_METHOD = "utf8-text-lines"
# A text page's budget as one tools/call result serializes it (the text JSON-escaped
# in structuredContent and escaped again inside the text content), leaving the rest of
# operating.mcp_server.MAX_RESULT_BYTES (which imports this module, so it is named here,
# not imported) for the view's other keys, the firewall's envelopes and the freshness
# reserve; and at most as many characters as one default plx.read returns (under
# injection_guard.ENVELOPE_CHARS).
TEXT_PAGE_SERVED_BYTES = 30_000
TEXT_PAGE_CHARS = DEFAULT_READ_CHARS
# Control characters a text original never holds (tab, newline, form feed and
# carriage return are text); bytes holding one are not a text original.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_LINE = re.compile(r"[^\n]*\n|[^\n]+")
# Extraction needs an https URL for claim provenance; this reserved-TLD
# placeholder never leaves the store and is never fetched.
_PROVENANCE_BASE = "https://case-original.invalid/"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,199}$")
_PRINCIPAL_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@\-]{0,199}$")
_ID = re.compile(r"^orig-[0-9a-f]{32}$")
# ``#page=<n>`` names the attach-time extraction (version 1); a later explicit
# re-extraction k >= 2 is named ``#extraction=<k>`` / ``#extraction=<k>&page=<n>``,
# so every handle keeps naming the same immutable text.
_HANDLE = re.compile(r"^original://(orig-[0-9a-f]{32})"
                     r"(?:#(?:extraction=([2-9]|[1-9][0-9])(?:&page=([1-9][0-9]{0,3}))?"
                     r"|page=([1-9][0-9]{0,3})))?$")
MAX_EXTRACTIONS = 20  # versions per original, attach-time extraction included
_REASON_MAX = 1000

_DDL = """
CREATE TABLE IF NOT EXISTS original (
  original_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  tenant TEXT NOT NULL,
  classification TEXT NOT NULL,
  audience_json TEXT NOT NULL,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  attached_by TEXT NOT NULL,
  attached_at TEXT NOT NULL,
  extraction_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS original_no_update BEFORE UPDATE ON original
BEGIN SELECT RAISE(ABORT, 'original_is_immutable'); END;
CREATE TABLE IF NOT EXISTS reextraction (
  original_id TEXT NOT NULL REFERENCES original(original_id),
  version INTEGER NOT NULL CHECK (version >= 2),
  extracted_at TEXT NOT NULL,
  extracted_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  local_ocr INTEGER NOT NULL,
  extraction_json TEXT NOT NULL,
  PRIMARY KEY (original_id, version)
);
CREATE TRIGGER IF NOT EXISTS reextraction_no_update BEFORE UPDATE ON reextraction
BEGIN SELECT RAISE(ABORT, 'extraction_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS reextraction_no_delete BEFORE DELETE ON reextraction
BEGIN SELECT RAISE(ABORT, 'extraction_is_immutable'); END;
CREATE TABLE IF NOT EXISTS original_source (
  original_id TEXT PRIMARY KEY REFERENCES original(original_id),
  provenance_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS original_source_no_update BEFORE UPDATE ON original_source
BEGIN SELECT RAISE(ABORT, 'original_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS original_source_no_delete BEFORE DELETE ON original_source
BEGIN SELECT RAISE(ABORT, 'original_is_immutable'); END;
"""


def media_type_of(content: bytes) -> str | None:
    """``application/pdf`` (``%PDF-`` magic), UTF-8 text (non-empty, strict UTF-8, no
    control characters but tab, newline, form feed, carriage return), else None."""
    if content.startswith(PDF_MAGIC):
        return PDF_MEDIA_TYPE
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return TEXT_MEDIA_TYPE if text and not _CONTROL.search(text) else None


_JSON_TWO = '"\\'                 # escaped as two characters (\" \\)
_JSON_SHORT = "\n\r\t\b\f"           # escaped as backslash and one letter
_JSON_UNICODE = re.compile(r"[\x00-\x07\x0b\x0e-\x1f]")   # escaped as \u00XX


def _served_bytes(text: str) -> int:
    """UTF-8 bytes of ``text`` as a JSON string plus that string as a JSON string again
    (``json.dumps(..., ensure_ascii=False)`` twice), counted without building either:
    each is 2 quotes plus every character, and only a quote, a backslash or a control
    character is escaped (once: 2 or 6 characters; twice: 4, 3 or 7)."""
    size = 8 + 2 * len(text.encode("utf-8"))
    line = text[:-1] if text.endswith("\n") else text   # a text page's line, mostly
    if line.isprintable():
        return size + 4 * (line.count('"') + line.count("\\")) + 3 * (len(text) - len(line))
    return (size + 4 * sum(text.count(c) for c in _JSON_TWO)
            + 3 * sum(text.count(c) for c in _JSON_SHORT)
            + 11 * len(_JSON_UNICODE.findall(text)))


def _text_extraction(content: bytes) -> dict[str, Any]:
    """A text original's own text as line-window pages (module docstring)."""
    text = content.decode("utf-8")
    pages: list[dict[str, Any]] = []
    start = size = chars = 0
    first = line = 1

    def close(end: int, last: int) -> None:
        pages.append({"page": len(pages) + 1, "start": start, "end": end,
                      "lines": [first, last]})

    position = 0
    for match in _LINE.finditer(text):
        piece = match.group(0)
        cost = _served_bytes(piece)
        if position > start and (size + cost > TEXT_PAGE_SERVED_BYTES
                                 or chars + len(piece) > TEXT_PAGE_CHARS):
            close(position, line - 1)
            start, size, chars, first = position, 0, 0, line
        if cost > TEXT_PAGE_SERVED_BYTES or len(piece) > TEXT_PAGE_CHARS:
            # One line longer than a page: it spans pages, each named by that line.
            for char in piece:
                step = _served_bytes(char)
                if position > start and (size + step > TEXT_PAGE_SERVED_BYTES
                                         or chars + 1 > TEXT_PAGE_CHARS):
                    close(position, line)
                    start, size, chars, first = position, 0, 0, line
                size, chars, position = size + step, chars + 1, position + 1
        else:
            size, chars, position = size + cost, chars + len(piece), position + len(piece)
        line += piece.endswith("\n")
    if position > start:
        close(position, line - (text.endswith("\n")))
    return {"status": "extracted" if text.strip() else "no_text", "method": TEXT_METHOD,
            "text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "pages": pages, "ocr_pages": [], "warnings": []}


def _page_handle(original_id: str, version: int, page: int) -> str:
    if version == 1:
        return f"{SCHEME}{original_id}#page={page}"
    return f"{SCHEME}{original_id}#extraction={version}&page={page}"


def _file_handle(original_id: str, version: int) -> str:
    return SCHEME + original_id + ("" if version == 1 else f"#extraction={version}")


def _extraction_summary(extraction: Mapping[str, Any]) -> dict[str, Any]:
    summary = {"status": extraction["status"], "method": extraction["method"],
               "error": extraction.get("error"), "page_count": len(extraction["pages"]),
               "ocr_pages": extraction["ocr_pages"], "warnings": extraction["warnings"],
               "text_sha256": extraction.get("text_sha256")}
    # Only MarkItDown text and capped reads carry these; other views are unchanged.
    for key in ("conversion", "skipped_by_cap"):
        if key in extraction:
            summary[key] = extraction[key]
    return summary


# What makes two extractions the same result (a repeat is not a new version).
_EXTRACTION_IDENTITY = ("status", "method", "error", "text", "pages", "ocr_pages", "warnings",
                        "conversion", "skipped_by_cap")


def _public_provenance(source: Mapping[str, Any]) -> dict[str, Any]:
    """Provenance as shown to a reader who passed the gate.

    No audience keys and no attach-time permission list (grantee identities,
    roles, links): only the ACL status and grant count, like the live read
    path; the full record stays in the immutable provenance row.
    """
    view = {key: value for key, value in source.items()
            if key not in ("audience_keys", "source_acl")}
    acl = source.get("source_acl")
    if isinstance(acl, Mapping):
        view["source_acl_status"] = acl.get("status")
        view["source_acl_grant_count"] = len(acl.get("grants") or [])
    return view


class OriginalError(ValueError):
    """A refused attach request, with a stable ``code``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _visible(record: Mapping[str, Any], actor: Actor) -> bool:
    """Tenant match, clearance >= classification, audience ∩ principals (no ``*``)."""
    if not isinstance(actor, Actor) or record["tenant"] != actor.tenant:
        return False
    if LEVELS[record["classification"]] > LEVELS[actor.clearance]:
        return False
    return bool(set(json.loads(record["audience_json"])) & (set(actor.principals) - {"*"}))


class OriginalStore:
    """Content-addressed originals with per-original audience, under one directory."""

    def __init__(self, root: str | Path, *, local_ocr: bool = False,
                 extractor: Any = extract_document, source_gate: Any = None,
                 captured: Any = None) -> None:
        self.root = Path(root)
        self.inbox = self.root / INBOX_DIR
        self.store = self.root / STORE_DIR
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.store.mkdir(parents=True, exist_ok=True)
        self.path = self.root / DB_FILE
        self.local_ocr = local_ocr
        self._extract = extractor
        # (provenance, actor) -> bool for originals read from a live source;
        # None: no source original is readable (default deny).
        self._source_gate = source_gate
        # The capture store's case originals (``operating.captured_originals``): their
        # ``original://co-|nr-|rv-...`` citations are read there, behind their source's
        # gate; None: none are served.
        self.captured = captured
        with self._connect() as db:
            db.executescript(_DDL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # ---- attach ---------------------------------------------------------

    def _inbox_file(self, actor: Actor, name: Any,
                    principal: Any) -> tuple[str, Path]:
        """(principal, path) of ``name`` in an inbox directory the actor may read."""
        if not isinstance(name, str) or _NAME.fullmatch(name) is None or ".." in name:
            raise OriginalError("invalid_inbox_name",
                                "a file name directly inside a principal's inbox directory")
        held = sorted(item for item in set(actor.principals) - {"*"}
                      if _PRINCIPAL_DIR.fullmatch(item))
        if principal is not None:
            if principal not in held:
                raise OriginalError("inbox_principal_not_held")
            held = [principal]
        found = []
        for item in held:
            directory = self.inbox / item
            try:
                if not stat.S_ISDIR(os.lstat(directory).st_mode):
                    continue  # a link or file named like a principal is ignored
                os.lstat(directory / name)
            except OSError:
                continue
            found.append((item, directory / name))
        if not found:
            raise OriginalError("inbox_file_not_found", name)
        if len(found) > 1:
            raise OriginalError("inbox_name_ambiguous",
                                "the name exists in several of your principal directories; "
                                "pass inbox_principal")
        return found[0]

    def _read_inbox(self, path: Path, name: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise OriginalError("inbox_file_not_found", name) from None
        except OSError:
            # A symlink (ELOOP) or anything else that is not a plain readable file.
            raise OriginalError("inbox_file_not_a_regular_file", name) from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OriginalError("inbox_file_not_a_regular_file", name)
            if info.st_size > MAX_BYTES:
                raise OriginalError("original_too_large", f"at most {MAX_BYTES} bytes")
            chunks, total = [], 0
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise OriginalError("original_too_large", f"at most {MAX_BYTES} bytes")
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        content = b"".join(chunks)
        if media_type_of(content) is None:
            raise OriginalError("unsupported_original_type", "PDF or UTF-8 text originals only")
        return content

    def _write_blob(self, digest: str, content: bytes) -> None:
        target = self.store / digest
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return
        temporary = self.store / f".{digest}.{uuid4().hex}.tmp"
        with open(temporary, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    def _extraction(self, original_id: str, content: bytes, filename: str) -> dict[str, Any]:
        if media_type_of(content) == TEXT_MEDIA_TYPE:
            return _text_extraction(content)
        try:
            document = self._extract(content, PDF_MEDIA_TYPE,
                                     original_url=_PROVENANCE_BASE + original_id,
                                     filename=filename, local_pdf_ocr=self.local_ocr)
        except ValidationError as exc:
            return {"status": "failed", "error": str(exc)[:200], "method": None,
                    "text": "", "pages": [], "ocr_pages": [], "warnings": []}
        pages = [{"page": int(page["page"]), "start": int(page["start"]),
                  "end": int(page["end"])} for page in document.pages]
        ocr_pages = sorted(int(page) for page in getattr(document, "ocr_pages", ()))
        result = {"status": document.status, "method": document.method, "text": document.text,
                  "text_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
                  "pages": pages, "ocr_pages": ocr_pages, "warnings": list(document.warnings)}
        conversion = getattr(document, "conversion", None)
        if conversion is not None:  # MarkItDown text keeps its provenance
            result["conversion"] = json.loads(json.dumps(conversion))
        skipped = getattr(document, "skipped_pages", None)
        if skipped is not None:
            # OCR read pages 1-MAX_PAGES of a longer PDF: the extraction contract's
            # field (ExtractedDocument.evidence_attributes), so no reader takes the
            # pages read for the whole file.
            result["skipped_by_cap"] = {"first_page": int(skipped[0]),
                                        "last_page": int(skipped[1]),
                                        "cap": "pages", "limit": MAX_PAGES}
        return result

    def attach(self, actor: Actor, inbox_name: Any,
               inbox_principal: Any = None) -> dict[str, Any]:
        """Claim, store and extract one inbox file; audience = its directory principal.

        The same bytes attached again for the same audience return the same id.
        """
        if not isinstance(actor, Actor):
            raise OriginalError("invalid_actor")
        principal, path = self._inbox_file(actor, inbox_name, inbox_principal)
        members = {principal}
        claimed = path.with_name(f".claim-{uuid4().hex}")
        try:
            os.rename(path, claimed)  # atomic: one attacher wins; a link stays a link
        except FileNotFoundError:
            raise OriginalError("inbox_file_not_found", inbox_name) from None
        try:
            content = self._read_inbox(claimed, inbox_name)
        except BaseException:
            if not os.path.lexists(path):
                os.rename(claimed, path)
            raise
        try:
            result = self._store(actor, inbox_name, content, members)
        except BaseException:
            if not os.path.lexists(path):
                os.rename(claimed, path)  # a failed store never loses the operator's file
            raise
        claimed.unlink(missing_ok=True)
        return result

    def store_source(self, actor: Actor, filename: str, content: bytes,
                     provenance: Mapping[str, Any]) -> dict[str, Any]:
        """Store bytes the case runtime read from a live source, with provenance.

        The caller has already checked the actor's source audience and read
        the bytes under its byte cap; this checks the type and size again,
        stores content-addressed, extracts, and records ``provenance`` immutably.
        The same source version (``identity``) with the same bytes for the same
        audience returns the same id (``duplicate``).
        """
        if not isinstance(actor, Actor):
            raise OriginalError("invalid_actor")
        if not isinstance(content, bytes) or len(content) > MAX_BYTES:
            raise OriginalError("original_too_large", f"at most {MAX_BYTES} bytes")
        media_type = media_type_of(content)
        if media_type is None:
            raise OriginalError("unsupported_original_type", "PDF or UTF-8 text originals only")
        if (not isinstance(provenance, Mapping) or not isinstance(provenance.get("source"), str)
                or not isinstance(provenance.get("identity"), list)):
            raise OriginalError("invalid_provenance")
        if not isinstance(filename, str) or _NAME.fullmatch(filename) is None or ".." in filename:
            filename = "source-original." + ("pdf" if media_type == PDF_MEDIA_TYPE else "txt")
        members = {item for item in set(actor.principals) - {"*"}
                   if _PRINCIPAL_DIR.fullmatch(item)}
        if not members:
            raise OriginalError("no_audience_principal")
        return self._store(actor, filename, content, members, dict(provenance))

    def _store(self, actor: Actor, inbox_name: str, content: bytes,
               members: set[str], provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        audience_json = json.dumps(sorted(members))
        identity = [actor.tenant, digest, sorted(members), actor.clearance]
        if provenance is not None:
            # A source original is its own original (its own gate), never the
            # inbox original with the same bytes.
            identity += [provenance["source"], provenance["identity"]]
        original_id = "orig-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
        with self._connect() as db:
            existing = db.execute("SELECT * FROM original WHERE original_id = ?",
                                  (original_id,)).fetchone()
            source = self._source(db, original_id)
        if existing is not None:
            self._write_blob(digest, content)  # restores a missing blob; same bytes
            return {**self._summary(existing, source), "duplicate": True}
        self._write_blob(digest, content)
        extraction = self._extraction(original_id, content, inbox_name)
        record = {"original_id": original_id, "sha256": digest, "tenant": actor.tenant,
                  "classification": actor.clearance, "audience_json": audience_json,
                  "filename": inbox_name, "media_type": media_type_of(content),
                  "byte_size": len(content), "attached_by": actor_label(actor),
                  "attached_at": datetime.now(timezone.utc).isoformat(),
                  "extraction_json": json.dumps(extraction, sort_keys=True)}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT OR IGNORE INTO original VALUES (:original_id, :sha256, "
                           ":tenant, :classification, :audience_json, :filename, "
                           ":media_type, :byte_size, :attached_by, :attached_at, "
                           ":extraction_json)", record)
                if provenance is not None:
                    db.execute("INSERT OR IGNORE INTO original_source VALUES (?, ?)",
                               (original_id, json.dumps(provenance, sort_keys=True)))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            stored = db.execute("SELECT * FROM original WHERE original_id = ?",
                                (original_id,)).fetchone()
            source = self._source(db, original_id)
        return {**self._summary(stored, source), "duplicate": stored["attached_at"] != record[
            "attached_at"]}

    @staticmethod
    def _source(db: sqlite3.Connection, original_id: str) -> dict[str, Any] | None:
        row = db.execute("SELECT provenance_json FROM original_source WHERE original_id = ?",
                         (original_id,)).fetchone()
        return None if row is None else json.loads(row["provenance_json"])

    def _readable(self, record: Mapping[str, Any], source: Mapping[str, Any] | None,
                  actor: Actor) -> bool:
        """Audience of the stored original and, for a source original, the gate now."""
        if not _visible(record, actor):
            return False
        if source is None:
            return True
        if self._source_gate is None:
            return False
        try:
            return bool(self._source_gate(source, actor))
        except Exception:  # noqa: BLE001 - a failing gate denies, never leaks
            return False

    # ---- resolve --------------------------------------------------------

    @staticmethod
    def _summary(record: Mapping[str, Any],
                 source: Mapping[str, Any] | None = None) -> dict[str, Any]:
        extraction = json.loads(record["extraction_json"])
        summary = {"original_id": record["original_id"],
                "citation": SCHEME + record["original_id"],
                "page_citations": [_page_handle(record["original_id"], 1, page["page"])
                                   for page in extraction["pages"]],
                "sha256": record["sha256"], "filename": record["filename"],
                "media_type": record["media_type"], "bytes": record["byte_size"],
                "audience": json.loads(record["audience_json"]),
                "classification": record["classification"],
                "attached_by": record["attached_by"],
                "extraction": _extraction_summary(extraction)}
        if source is not None:
            summary["provenance"] = _public_provenance(source)
        return summary

    @staticmethod
    def _versions(db: sqlite3.Connection, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every extraction of an original, oldest first: version 1 is attach time."""
        versions = [{"version": 1, "extracted_at": record["attached_at"],
                     "extracted_by": record["attached_by"], "reason": "attached",
                     "extraction": json.loads(record["extraction_json"])}]
        for row in db.execute("SELECT * FROM reextraction WHERE original_id = ? "
                              "ORDER BY version", (record["original_id"],)):
            versions.append({"version": row["version"], "extracted_at": row["extracted_at"],
                             "extracted_by": row["extracted_by"], "reason": row["reason"],
                             "local_ocr": bool(row["local_ocr"]),
                             "extraction": json.loads(row["extraction_json"])})
        return versions

    @staticmethod
    def _version_entry(original_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
        entry = {key: value for key, value in item.items() if key != "extraction"}
        entry.update(citation=_file_handle(original_id, item["version"]),
                     page_citations=[_page_handle(original_id, item["version"], page["page"])
                                     for page in item["extraction"]["pages"]],
                     extraction=_extraction_summary(item["extraction"]))
        return entry

    # ---- re-extract -----------------------------------------------------

    def reextract(self, actor: Actor, handle: Any, reason: Any) -> dict[str, Any]:
        """Read an attached original's stored bytes again through the extraction
        contract (with this host's current OCR setting) as a new, explicit version.

        The original's bytes, id, audience and every earlier extraction stay as
        they were; earlier citations keep naming the text they named. The
        stored bytes are re-hashed first (a changed file is refused). An
        extraction identical to any version already held is not a new
        version (``duplicate``: the newest matching version is returned). Only a reader of the original may re-extract it.
        """
        if not isinstance(actor, Actor):
            raise OriginalError("invalid_actor")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
            raise OriginalError("invalid_reason", f"1-{_REASON_MAX} characters")
        try:
            # The whole handle must resolve for the actor (a named version or
            # page included), not only the original it belongs to.
            _, record = self._resolve(handle, actor)
            content = (self.store / record["sha256"]).read_bytes()
        except CitationUnresolvable as exc:
            raise OriginalError(str(exc)) from None
        except OSError:
            raise OriginalError("original_missing") from None
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise OriginalError("original_content_changed")
        original_id = record["original_id"]
        extraction = self._extraction(original_id, content, record["filename"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                versions = self._versions(db, record)
                latest = versions[-1]
                same = [item for item in versions
                        if all(item["extraction"].get(key) == extraction.get(key)
                               for key in _EXTRACTION_IDENTITY)]
                if same:
                    # Identical to a version already held (the newest such one):
                    # switching OCR off and on again adds nothing.
                    match = same[-1]
                    db.execute("COMMIT")
                    result = {**self._version_entry(original_id, match),
                              "original_id": original_id, "sha256": record["sha256"],
                              "local_ocr_enabled": self.local_ocr, "duplicate": True}
                    if match["version"] > 1:
                        before = versions[match["version"] - 2]
                        result.update(previous_version=before["version"],
                                      previous_extraction=_extraction_summary(
                                          before["extraction"]))
                    return result
                if len(versions) >= MAX_EXTRACTIONS:
                    raise OriginalError("extraction_limit_reached", str(MAX_EXTRACTIONS))
                item = {"version": latest["version"] + 1,
                        "extracted_at": datetime.now(timezone.utc).isoformat(),
                        "extracted_by": actor_label(actor), "reason": reason.strip(),
                        "local_ocr": self.local_ocr, "extraction": extraction}
                db.execute("INSERT INTO reextraction VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (original_id, item["version"], item["extracted_at"],
                            item["extracted_by"], item["reason"], int(self.local_ocr),
                            json.dumps(extraction, sort_keys=True)))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        return {**self._version_entry(original_id, item), "original_id": original_id,
                "sha256": record["sha256"], "previous_version": latest["version"],
                "previous_extraction": _extraction_summary(latest["extraction"]),
                "local_ocr_enabled": self.local_ocr, "duplicate": False}

    def _captured(self, handle: Any) -> Any:
        """The captured-originals reader serving ``handle``, or None (an inbox or source
        original of this store). A captured citation on a host without one reads as
        absent."""
        from .captured_originals import CapturedOriginals
        if not CapturedOriginals.handles(handle):
            return None
        if self.captured is None:
            raise CitationUnresolvable("original_not_visible")
        return self.captured

    def resolve(self, handle: Any, actor: Actor) -> dict[str, Any]:
        """Content-stable view of an original (or one page) for ``actor``, or raise."""
        captured = self._captured(handle)
        if captured is not None:
            return captured.resolve(handle, actor)
        return self._resolve(handle, actor)[0]

    def read(self, handle: Any, actor: Actor) -> dict[str, Any]:
        """``resolve`` plus, for a file citation, the page citations it contains.

        Kept separate from ``resolve`` so the content-stable view a case digests
        is unchanged; this is what a client reads to find the pages to cite.
        """
        captured = self._captured(handle)
        if captured is not None:
            return captured.read(handle, actor)
        view, record = self._resolve(handle, actor)
        if "page" in view:
            return view  # page reads stay bounded by their text alone
        with self._connect() as db:
            versions = self._versions(db, record)
        version = view.get("extraction_version", 1)
        pages = versions[version - 1]["extraction"]["pages"]
        # A compact, bounded listing (at most MAX_EXTRACTIONS entries, no
        # reasons, warnings or page lists); a version's own file citation
        # carries its full summary and page citations.
        listing = [{"version": item["version"],
                    "citation": _file_handle(record["original_id"], item["version"]),
                    "extracted_at": item["extracted_at"],
                    "status": item["extraction"]["status"],
                    "method": item["extraction"]["method"],
                    "page_count": len(item["extraction"]["pages"]),
                    "ocr_page_count": len(item["extraction"]["ocr_pages"]),
                    "text_sha256": item["extraction"].get("text_sha256")}
                   for item in versions]
        return {**view, "page_citations": [_page_handle(record["original_id"], version,
                                                        page["page"]) for page in pages],
                "extractions": listing, "latest_extraction_version": versions[-1]["version"]}

    def text_document(self, handle: Any, actor: Actor) -> dict[str, Any]:
        """One extraction version's whole text for ``actor``, after ``resolve``'s checks.

        ``handle`` is a file citation (``original://<id>`` or
        ``original://<id>#extraction=<k>``), never a page citation. The same
        audience, live source gate and byte re-hash as every read run first, so
        an original the actor may not read raises exactly as for its pages.
        Returns the content-stable ``view`` plus ``text``, ``pages`` (offsets
        into ``text``), ``ocr_pages``, ``warnings`` and ``text_sha256``; for a
        caller that needs whole-document spans (``operating.doc_metadata``).
        """
        captured = self._captured(handle)
        if captured is not None:
            return captured.text_document(handle, actor)
        match = _HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None or match.group(3) or match.group(4):
            raise CitationUnresolvable("original_file_citation_required")
        view, record = self._resolve(handle, actor)
        version = view.get("extraction_version", 1)
        if version == 1:
            extraction = json.loads(record["extraction_json"])
        else:
            with self._connect() as db:
                later = db.execute("SELECT extraction_json FROM reextraction WHERE "
                                   "original_id = ? AND version = ?",
                                   (record["original_id"], version)).fetchone()
            if later is None:  # pragma: no cover - _resolve found it; rows are immutable
                raise CitationUnresolvable("original_extraction_not_found")
            extraction = json.loads(later["extraction_json"])
        text = extraction.get("text") or ""
        return {"view": view, "text": text, "pages": list(extraction.get("pages") or []),
                "ocr_pages": list(extraction.get("ocr_pages") or []),
                "warnings": list(extraction.get("warnings") or []),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def read_pdf(self, handle: Any, actor: Actor) -> dict[str, Any]:
        """The stored PDF bytes of one original for ``actor``, only after its checks.

        ``handle`` is the file citation ``original://<id>``. In order, before any
        byte is read: the actor is an ``Actor``; the original exists and is
        readable now (tenant, clearance, audience ∩ principals without ``*``, and
        for a source original the live source gate), exactly as for its text; and
        the principal that grants it is an employee principal, never an agent one
        (``agent:``/``sp_``, the rule of ``operating.entitlements``): whole
        document bytes go to a person's viewer, never to an agent identity. Then
        the bytes are re-hashed and must still be the recorded PDF. Every refusal
        reads ``original_not_visible`` (or the integrity error, after the checks),
        so a refused reader cannot tell a real original from a missing one.
        Returns ``{"content": bytes, "sha256", "filename", "media_type", "bytes"}``.
        """
        match = _HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None or match.group(2) or match.group(3) or match.group(4):
            raise CitationUnresolvable("original_file_citation_required")
        if not isinstance(actor, Actor):
            raise CitationUnresolvable("original_not_visible")
        original_id = match.group(1)
        with self._connect() as db:
            record = db.execute("SELECT * FROM original WHERE original_id = ?",
                                (original_id,)).fetchone()
            source = None if record is None else self._source(db, original_id)
        if record is None or not self._readable(record, source, actor):
            raise CitationUnresolvable("original_not_visible")
        granting = {item for item in set(json.loads(record["audience_json"]))
                    & (set(actor.principals) - {"*"})
                    if not item.startswith(_AGENT_PREFIXES)}
        if not granting:
            raise CitationUnresolvable("original_not_visible")
        try:
            content = (self.store / record["sha256"]).read_bytes()
        except OSError:
            raise CitationUnresolvable("original_missing") from None
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise CitationUnresolvable("original_content_changed")
        if record["media_type"] != "application/pdf" or not content.startswith(PDF_MAGIC):
            raise CitationUnresolvable("original_not_pdf")
        return {"content": content, "sha256": record["sha256"],
                "filename": record["filename"], "media_type": "application/pdf",
                "bytes": len(content)}

    def _resolve(self, handle: Any, actor: Actor) -> tuple[dict[str, Any], Any]:
        match = _HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None:
            raise CitationUnresolvable("invalid_original_citation")
        original_id = match.group(1)
        version = int(match.group(2) or 1)
        page = match.group(3) or match.group(4)
        with self._connect() as db:
            record = db.execute("SELECT * FROM original WHERE original_id = ?",
                                (original_id,)).fetchone()
            source = None if record is None else self._source(db, original_id)
            if record is None or not self._readable(record, source, actor):
                raise CitationUnresolvable("original_not_visible")
            later = (None if version == 1 else db.execute(
                "SELECT * FROM reextraction WHERE original_id = ? AND version = ?",
                (original_id, version)).fetchone())
        if version != 1 and later is None:
            raise CitationUnresolvable("original_extraction_not_found")
        blob = self.store / record["sha256"]
        try:
            content = blob.read_bytes()
        except OSError:
            raise CitationUnresolvable("original_missing") from None
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise CitationUnresolvable("original_content_changed")
        extraction = json.loads(record["extraction_json"] if later is None
                                else later["extraction_json"])
        view: dict[str, Any] = {
            "citation": handle, "original_id": original_id, "sha256": record["sha256"],
            "filename": record["filename"], "media_type": record["media_type"],
            "bytes": record["byte_size"], "extraction": _extraction_summary(extraction),
            "evidence_mode": "operator-supplied original"}
        if source is not None:
            # Only source originals carry this; inbox views (and their recorded
            # digests) are unchanged.
            view["evidence_mode"] = f"{source['source']} original (bytes read at attach)"
            view["provenance"] = _public_provenance(source)
        if later is not None:
            # Version 1 views stay exactly as before, so recorded digests hold.
            view.update(extraction_version=version, extracted_at=later["extracted_at"],
                        extracted_by=later["extracted_by"], extraction_reason=later["reason"],
                        extraction_local_ocr=bool(later["local_ocr"]))
        if page is not None:
            number = int(page)
            spans = [item for item in extraction["pages"] if item["page"] == number]
            if not spans:
                raise CitationUnresolvable("original_page_not_found")
            text = extraction["text"][spans[0]["start"]:spans[0]["end"]]
            view.update(page=number, text=text,
                        text_source="original_text" if extraction["method"] == TEXT_METHOD
                        else "ocr" if number in extraction["ocr_pages"] else
                        "machine_converted" if "conversion" in extraction else "text_layer")
            if "lines" in spans[0]:
                view["lines"] = list(spans[0]["lines"])
        return view, record

    def list_visible(self, actor: Actor, limit: int = 50,
                     max_source_checks: int = 20) -> list[dict[str, Any]]:
        """Newest-first originals ``actor`` may read now.

        A source original needs a live check (``source_gate``); at most
        ``max_source_checks`` are made per listing, and source originals beyond
        that bound are left out (unverified reads as withheld, never listed).
        """
        with self._connect() as db:
            rows = db.execute("SELECT * FROM original ORDER BY attached_at DESC").fetchall()
            sources = {row["original_id"]: self._source(db, row["original_id"]) for row in rows}
        found, checks = [], 0
        for row in rows:
            if len(found) >= limit:
                break
            source = sources[row["original_id"]]
            if source is not None and _visible(row, actor):
                if checks >= max_source_checks:
                    continue
                checks += 1
            if self._readable(row, source, actor):
                found.append(self._summary(row, source))
        return found


__all__ = ["INBOX_DIR", "OriginalError", "OriginalStore", "RECORDED_CAPTURE_SOURCE", "SCHEME",
           "TEXT_MEDIA_TYPE", "media_type_of"]
