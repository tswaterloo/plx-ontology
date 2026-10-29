"""Read-only intake of Vince's use-case folder ("Ontology UseCases") into our own state.

Vince delivers real historical cases with known outcomes, process maps (like the
Packaging Procure-to-GL map), employee questions, desired agent actions,
locations and permissions into the AGENTS site's default document library,
folder ``/Ontology UseCases`` (a different site from ``SHAREPOINT_SITE_ID``;
the site is resolved by name at run time, ``operating.ingestion.resolve_sites``,
never by a drive id or sharing link). That folder is one read-only continuous
ingestion unit (``sp.plxcustomers.agents-use-cases`` in
``examples/ingestion_sources.live-check.json``, a ``sharepoint_folder`` read
by ``operating.ingestion`` through ``integrations.graph_read.SharePointReader``
over the GET-only ``integrations.live_source_guard`` transport). Nothing here
writes to SharePoint: there is no write path at all, and every output lands
under ``<state>/use_cases/``.

For every new or changed file of that unit (eTag differs from the last intake)
``UseCaseIntake.intake`` reads the original bytes once through
``SharePointReader.get_content`` (which re-checks the reading principal's
audience and the item's own source ACL, so an item withheld by permission is
counted and never read), pins them by SHA-256 and records one intake record:

* the original: ``sharepoint://`` citation with ``#sha256=``, drive/item/eTag,
  path, name, size, source ACL at read time, and the content-addressed copy
  ``originals/<sha256>`` in our own state;
* the detected type (``docx xlsx pptx pdf vsdx png jpg md txt eml html msg zip other``;
  magic bytes first, the file name second; a disagreement is a review reason);
* the part accounting (``operating.use_case_parts.account``): every part of the
  original (archive members recursively, OOXML parts, MIME parts, ``.msg``
  streams, PDF pages, images, annotations and embedded files) accounted exactly
  once as ``extracted``, ``unread`` (kind and reason) or ``skipped_by_cap`` (cap
  and limit). Closed world: a part is ``unread`` unless a reader for its kind
  read it, so an unhandled kind is never counted as extracted. The record is
  ``complete`` only when nothing is unread or skipped; each unread part is kept
  as its own original ``originals/<sha256>`` so a person can open it;
* the extracted text (``extractions/<sha256>.txt``): each part after a
  display-only ``[Part: <path>]`` marker; the part's ``start``/``end`` span is
  authoritative, never the marker;
* ``needs_extraction_review`` with the exact ``review_reasons``
  (``unread:<reason>``, ``skipped_by_cap:<cap>``, type warnings).

A content change appends a new record (the old one stays); an eTag change with
identical bytes is ``metadata_only`` (no new record). For each new content
version an evaluation-set stub is appended to ``evaluation_set.jsonl``: a
stable case id (from the drive item, not from document text), the known
outcome **only when the extracted text states it explicitly** (``Outcome:``,
``Known outcome:``, ``Actual outcome:``, ``Resolution:``; a placeholder such as
``TBD``, ``?`` or a question is not an outcome) with its
text span, the questions found (lines ending in ``?``) and the case's
evaluation split (``dev`` or ``held_out``, ``split_for``: from the case id only).
A stub is labelled ``draft`` ``outcome_stated`` or ``no_stated_outcome`` (never an
invented outcome) and carries ``artifact_complete``; stubs are placeholders for
a person to confirm and never evaluation truth by themselves. Their file format is the documented hand-off to the evaluation
lane (``operating/use_case_eval_stub.schema.json``); held-out stubs stay in the
live state directory and are never committed. The eval tooling's entry point
(``scripts.eval_use_case_items``, the live check's command) runs this scan and
then rebuilds the evaluation items of the delivered cases (one
``plx-eval-item/v1`` item per case unit that states an outcome and asks a
question, under ``<state>/use_cases/eval_items/``; counts only in the report).
The live scan OCRs images with local tesseract when it is installed
(``use_case_parts.local_image_ocr``). The live scan also states whether
the folder was reached (``use_case_folder``: reached, partial, empty, withheld or
unreachable with its reason).

CLI::

    python3 -m operating.use_case_intake scan --state-dir D \\
        [--sources examples/ingestion_sources.live-check.json \\
         --live-audience examples/ingestion_live_audience.live-check.json] \\
        [--detail --reader-audience R --principal P [--clearance C]] [--dot-note] [--peek]

With ``--sources``/``--live-audience`` the scan first advances the ingestion
unit and intakes its new/changed files (credentialed environment only; GET
only). It then lists the intake records since the previous scan: counts only
by default (types, review flags, part accounting, stubs; no names, paths, ids or text).
The summary states its denominators, because an exit 0 is not document coverage:
``parts_total`` = ``parts_read`` + unread (``unread_by_reason``) + skipped by a cap
(``skipped_by_cap``), and ``parts_panicked`` with ``panicked_by_kind`` (part kind,
never a name) for parts a native parser panicked on (``parser_panicked``);
``by_format`` gives each file format's parts read, unread and skipped_by_cap and
its read parts by converter (``parts_by_method``: stdlib, pypdf, pdftotext, ocr, markitdown),
next to ``markitdown_available`` and ``markitdown_ready_by_format`` for this host; ``unread_msg_properties`` counts the
``msg_property_not_decoded`` parts by property type and id (schema tags, never values;
a named property by its property set and LID; a property table counts once under each
entry it names, so these counts can sum past the parts); binary ``.msg`` metadata
(``non_content_metadata``) stays counted in ``unread_by_reason`` and by tag in
``non_content_msg_properties``, but leaves the text denominator:
``text_parts_total`` = ``parts_total`` - ``parts_non_content_metadata``;
``unread_pdf_entries`` counts the ``pdf_entry_not_parsed`` parts by their typed entry
(``use_case_parts.PDF_ENTRIES``; ``untyped`` for a record written before parts carried one);
``--detail`` adds the records an authorized reader may see (``--reader-audience``
decides per record, by path and by the item's source ACL both as taken in and as
the newest ingestion listing has it; an item that listing did not return, or no
listing at all, is withheld; the rest are only counted). A permission change
keeps the SharePoint eTag, so it is tracked from the listing's ACL, not the
eTag. A file that fails intake (a bad file, disk full, I/O) is counted by error
class and never marked seen: the rest of the folder proceeds, it is retried on
every scan up to ``FAILURE_RETRY_CAP`` attempts per eTag, and it stays counted
(``failed_items``, ``pending_total``) until a scan takes it in. The stub append
is the commit point of a record/stub pair; sequence numbers are spent before
any write, so a half-written pair never shares its ``intake_seq``. The
intake runs when this CLI is invoked (cron or operator); the ingest worker
only lists the unit. ``--dot-note`` prints a counts-only note text the controller may
post; this module posts nothing. ``--peek`` does not advance the scan marker.

Evidence: tests use an injected transport; the live path is live-unverified
until ``devloop/live-checks/ingest.use_cases.json`` has a verifier result.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Callable, Iterator, Mapping, Sequence
from zipfile import BadZipFile, ZipFile

from integrations import capture_sharepoint, doc_convert
from ontology.core import LEVELS, Actor
from operating.msg_reader import CFB_MAGIC
from operating.use_case_parts import (EXTRACTED, FAMILIES, KINDS, METHOD_FAMILIES, PDF_ENTRIES,
                                      SKIPPED, TAG_ID, TAG_TYPE, UNREAD, VOCABULARY,
                                      account, capped, local_image_ocr, part_format)


RECORD_SCHEMA = "plx-use-case-intake/v2"  # v2: part accounting
# v3 adds draft/artifact_complete; format: operating/use_case_eval_stub.schema.json
EVAL_SCHEMA = "plx-use-case-eval-stub/v3"
SCAN_SCHEMA = "plx-use-case-scan/v1"
DEFAULT_SOURCE_ID = "sp.plxcustomers.agents-use-cases"
INTAKE_DIR = "use_cases"
MAX_ITEMS_PER_SCAN = 50
FAILURE_RETRY_CAP = 5  # intake attempts per item eTag before it waits for a new upload
# One delivered original is read whole (bounded by the guard's download bound): the
# first live delivery was a 31 MB zip of case originals, over the old 20 MB cap.
MAX_CONTENT_BYTES = 100_000_000
MAX_QUESTIONS = 50
MAX_STATEMENT_CHARS = 500
TYPES = ("docx", "xlsx", "pptx", "pdf", "vsdx", "png", "jpg", "md", "txt", "eml", "html",
         "msg", "zip", "other")
# Held-out share of delivered use cases (10-DECISION #8: 60 dev / 120 held-out).
SPLIT_RULE = "plx-use-case-split/v1"
HELD_OUT_PER_DEV = 2
_UTC = timezone.utc
_EXTENSIONS = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".pdf": "pdf",
               ".vsdx": "vsdx", ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".md": "md",
               ".markdown": "md", ".txt": "txt", ".eml": "eml", ".html": "html",
               ".htm": "html", ".msg": "msg", ".zip": "zip"}
_OOXML_MAIN = {"word/document.xml": "docx", "xl/workbook.xml": "xlsx",
               "ppt/presentation.xml": "pptx", "visio/document.xml": "vsdx"}
_OUTCOME = re.compile(
    r"(?im)^[ \t]*(?:[-*>#]+[ \t]*)*(?:\*\*|__)?(?P<label>known outcome|actual outcome|"
    r"outcome|resolution)(?:\*\*|__)?[ \t]*(?:[:–—-][ \t]*)(?:\*\*|__)?"
    r"[ \t]*(?P<value>[^\r\n]*\S)[ \t\r]*$")  # CRLF text (mail) ends its lines with \r
_QUESTION = re.compile(r"(?m)^[ \t]*(?:(?:[-*>#]+|\d{1,3}[.)])[ \t]*)*(?P<q>[^\r\n]*\S\?)"
                       r"[ \t\r]*$")


class IntakeError(ValueError):
    """A configuration or state error (code only, never source content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now_iso(moment: datetime) -> str:
    return moment.astimezone(_UTC).isoformat()


def _sha(content: bytes) -> str:
    return sha256(content).hexdigest()


def case_id_for(drive_id: str, item_id: str) -> str:
    """Stable case id of one delivered artifact (its drive item, not its text)."""
    return "uc-" + sha256(f"{drive_id}/{item_id}".encode()).hexdigest()[:16]


# ------------------------------------------------------------------ detection

def detect_type(content: bytes, name: str | None) -> tuple[str, list[str]]:
    """``(type, warnings)``: magic bytes first, the file extension second."""
    suffix = PurePosixPath(name or "").suffix.lower()
    by_name = _EXTENSIONS.get(suffix)
    by_magic: str | None = None
    if content.startswith(b"%PDF-"):
        by_magic = "pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        by_magic = "png"
    elif content.startswith(b"\xff\xd8\xff"):
        by_magic = "jpg"
    elif content.startswith(CFB_MAGIC):  # OLE compound file (Outlook .msg, legacy Office)
        # Compound files carry no reliable inner marker here: the name decides .msg.
        return ("msg", []) if by_name == "msg" else (
            "other", [f"type_mismatch:{by_name}"] if by_name else [])
    elif content.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
            by_magic = next((kind for part, kind in _OOXML_MAIN.items() if part in names), "zip")
        except (BadZipFile, ValueError, OSError):
            return "other", ["zip_unreadable"]
    if by_magic is None:
        if by_name in ("md", "txt", "eml", "html"):
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError:
                return "other", ["text_not_utf8"]
            return by_name, []
        return "other", ([f"type_mismatch:{by_name}"] if by_name else [])
    if by_name is not None and by_name != by_magic:
        return by_magic, [f"type_mismatch:{by_name}"]
    return by_magic, []


def split_for(case_id: str) -> str:
    """Deterministic evaluation split of one delivered case: ``dev`` or ``held_out``.

    From the stable case id only (never from document text), so a re-upload
    keeps its split and nobody chooses which cases are visible. One in
    ``HELD_OUT_PER_DEV + 1`` is ``dev``."""
    bucket = int(sha256(f"{SPLIT_RULE}:{case_id}".encode()).hexdigest()[:8], 16)
    return "dev" if bucket % (HELD_OUT_PER_DEV + 1) == 0 else "held_out"


def _statement(text: str) -> str:
    return text[:MAX_STATEMENT_CHARS]


_PLACEHOLDER = re.compile(r"(?i)^(?:tbd|tba|tbc|n/?a|none|pending|unknown|todo|"
                          r"to be (?:determined|confirmed|added)|[?.\-\u2026_ ]+)$")


def outcome_statements(text: str) -> tuple[list[dict[str, Any]], int]:
    """``(statements, placeholders)``: every labelled outcome line with a real value
    (``label``, ``text``, ``span``), in text order, and the count of placeholder ones."""
    placeholders = 0
    stated = []
    for match in _OUTCOME.finditer(text or ""):
        raw = match["value"]
        value = raw.strip("*_ \t")
        if not value or _PLACEHOLDER.fullmatch(value) or value.endswith("?"):
            placeholders += 1
            continue
        offset = match.start("value") + raw.index(value)
        stated.append({"label": match["label"].lower(), "text": _statement(value),
                       "span": [offset, offset + len(value)]})
    return stated, placeholders


def known_outcome(text: str) -> dict[str, Any]:
    """The outcome a document states explicitly (labelled line), else not stated.

    A placeholder value (``TBD``, ``?``, ``n/a``, ``pending``, a value ending in
    ``?``) is not a stated outcome; it is reported as ``placeholder``."""
    stated, placeholders = outcome_statements(text)
    if not stated:
        return {"stated": False, "text": None, "span": None, "statements": 0,
                "placeholder": placeholders > 0}
    return {"stated": True, **stated[0], "statements": len(stated), "placeholder": False}


def questions_in(text: str, limit: int | None = MAX_QUESTIONS
                 ) -> tuple[list[dict[str, Any]], bool]:
    questions = []
    for match in _QUESTION.finditer(text or ""):
        value = match["q"].strip()
        if len(value) < 4:
            continue
        if limit is not None and len(questions) >= limit:
            return questions, True
        questions.append({"text": _statement(value), "span": [match.start("q"), match.end("q")]})
    return questions, False


# ------------------------------------------------------------------ state

class IntakeState:
    """``<state>/use_cases/``: JSONL logs, originals and the scan marker (ours only)."""

    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / INTAKE_DIR
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for sub in ("originals", "extractions"):
            (self.root / sub).mkdir(exist_ok=True, mode=0o700)
        self.records_path = self.root / "intake.jsonl"
        self.eval_path = self.root / "evaluation_set.jsonl"
        self.state_path = self.root / "state.json"

    @contextmanager
    def lock(self) -> Iterator[None]:
        descriptor = os.open(self.root / "intake.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise IntakeError("use_case_intake_in_progress") from None
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError):
            raise IntakeError("use_case_state_unreadable") from None
        state.setdefault("schema", "plx-use-case-state/v1")
        state.setdefault("seen", {})
        state.setdefault("last_seq", 0)
        state.setdefault("scans", 0)
        state.setdefault("last_scan_seq", 0)
        return state

    def save(self, state: Mapping[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    @staticmethod
    def _append(path: Path, value: Mapping[str, Any]) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_record(self, record: Mapping[str, Any]) -> None:
        self._append(self.records_path, record)

    def append_stub(self, stub: Mapping[str, Any]) -> None:
        self._append(self.eval_path, stub)

    def pin(self, content: bytes) -> str:
        """Store bytes content-addressed; returns the relative path (idempotent)."""
        digest = _sha(content)
        target = self.root / "originals" / digest
        if target.exists():
            if _sha(target.read_bytes()) != digest:
                raise IntakeError("use_case_original_store_corrupt")
        else:
            temporary = target.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        return f"originals/{digest}"

    def write_text(self, digest: str, text: str) -> str:
        target = self.root / "extractions" / f"{digest}.txt"
        descriptor = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        return f"extractions/{digest}.txt"

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def records(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.records_path)

    def stubs(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.eval_path)


# ------------------------------------------------------------------ intake

def _acl_sha(item: Mapping[str, Any]) -> str:
    acl = item.get("source_acl") if isinstance(item.get("source_acl"), Mapping) else {}
    return sha256(json.dumps(acl, sort_keys=True, default=str).encode()).hexdigest()


def _counts() -> dict[str, int]:
    return {"candidates": 0, "read": 0, "new": 0, "changed": 0, "metadata_only": 0,
            "withheld_at_read": 0, "read_errors": 0, "intake_failed": 0, "exceeds_cap": 0,
            "acl_changed": 0, "failed_capped": 0, "pending": 0}


class UseCaseIntake:
    """Intake of the new/changed files of one ingested SharePoint folder unit.

    ``reader`` is the unit's ``SharePointReader`` (GET only), ``actor`` the
    ingest principal (the same one ``operating.ingestion`` listed with).
    """

    def __init__(self, state_dir: str | Path, *, reader: Any, actor: Actor,
                 unit_id: str = DEFAULT_SOURCE_ID, max_items: int = MAX_ITEMS_PER_SCAN,
                 clock: Callable[[], datetime] | None = None,
                 ocr: Callable[[bytes], str] | None = None) -> None:
        if not isinstance(actor, Actor):
            raise IntakeError("use_case_actor_invalid")
        if not isinstance(max_items, int) or isinstance(max_items, bool) or not \
                1 <= max_items <= 500:
            raise IntakeError("use_case_max_items_invalid")
        self.state_dir = Path(state_dir)
        self.reader, self.actor, self.unit_id = reader, actor, unit_id
        self.max_items = max_items
        self.ocr = ocr  # image OCR (operating.use_case_parts.local_image_ocr), or none
        self._clock = clock or (lambda: datetime.now(_UTC))
        self.files = IntakeState(self.state_dir)

    def _candidates(self) -> list[dict[str, Any]]:
        from operating.ingestion import IngestStore

        store = IngestStore(self.state_dir)
        try:
            return [item for item in store.current_items(self.unit_id)
                    if item.get("kind") == "file"]
        finally:
            store.close()

    def intake(self) -> dict[str, Any]:
        """Read and record every new/changed file (bounded); counts only."""
        from integrations.live_source_guard import NotVisible, SourceNotFound

        counts = _counts()
        errors: dict[str, int] = {}
        with self.files.lock():
            state = self.files.load()
            seen = state["seen"]
            failed = state.setdefault("failed", {})
            todo = []
            for item in self._candidates():
                known = seen.get(item["id"]) or {}
                failure = failed.get(item["id"])
                if failure is not None and failure.get("etag") != item.get("etag"):
                    del failed[item["id"]]  # a new upload starts over
                    failure = None
                if (failure is not None
                        and int(failure.get("attempts") or 0) >= FAILURE_RETRY_CAP):
                    counts["failed_capped"] += 1  # surfaced in every scan, never dropped
                    continue
                if known.get("etag") != item.get("etag"):
                    todo.append(item)
                elif known.get("acl_sha") != _acl_sha(item):
                    # SharePoint keeps the eTag when only permissions change: no new
                    # bytes to read, but the newest ACL is what a reader is checked
                    # against (scan_report reads it from the ingest store).
                    known["acl_sha"] = _acl_sha(item)
                    counts["acl_changed"] += 1
            counts["candidates"] = len(todo)
            for item in todo:
                if counts["read"] + counts["withheld_at_read"] + counts["read_errors"] \
                        >= self.max_items:
                    counts["pending"] += 1
                    continue
                try:
                    read = self.reader.get_content(self.actor, item["id"],
                                                   max_bytes=MAX_CONTENT_BYTES)
                except (NotVisible, SourceNotFound):
                    # Withheld from the ingest principal by audience or the item's own
                    # ACL (or gone): never read, never recorded; retried next scan.
                    counts["withheld_at_read"] += 1
                    continue
                except Exception as exc:  # noqa: BLE001 - recorded by class; retried
                    counts["read_errors"] += 1
                    key = type(exc).__name__
                    errors[key] = errors.get(key, 0) + 1
                    continue
                counts["read"] += 1
                if read.get("content_status") != "read":
                    counts["exceeds_cap"] += 1
                try:
                    outcome = self._record(state, item, read)
                except Exception as exc:  # noqa: BLE001 - one bad file never blocks the rest
                    # Never marked seen: a local fault (disk full, I/O) must not drop a
                    # delivered use case. The persistent failure count is surfaced in
                    # every scan; after FAILURE_RETRY_CAP attempts at the same eTag the
                    # item waits for a new upload (or a manual reset), still counted.
                    counts["intake_failed"] += 1
                    key = type(exc).__name__
                    errors[key] = errors.get(key, 0) + 1
                    failure = failed.get(item["id"]) or {"etag": item.get("etag"),
                                                         "attempts": 0}
                    failure.update(attempts=int(failure["attempts"]) + 1, error=key)
                    failed[item["id"]] = failure
                    self._save_quietly(state)
                    continue
                failed.pop(item["id"], None)
                counts[outcome] += 1
                self.files.save(state)
            state["last_intake_at"] = _now_iso(self._clock())
            self.files.save(state)
        return {"counts": counts, "read_errors_by_class": dict(sorted(errors.items()))}

    def _save_quietly(self, state: Mapping[str, Any]) -> None:
        try:
            self.files.save(state)
        except OSError:
            pass  # disk full: the in-memory count is still reported and saved later

    def _record(self, state: dict[str, Any], item: Mapping[str, Any],
                read: Mapping[str, Any]) -> str:
        record = dict(read["record"])
        seen = state["seen"]
        previous = seen.get(record["id"]) or {}
        now = _now_iso(self._clock())
        if read.get("content_status") != "read":
            # Over the byte cap: the original is not pinned; a person must fetch it.
            digest = None
            content = b""
        else:
            content = read["content"]
            digest = read["content_sha256"]
            if _sha(content) != digest:
                raise IntakeError("use_case_content_digest_mismatch")
            if previous.get("sha256") == digest:
                previous = {key: value for key, value in previous.items() if key != "error"}
                previous.update(etag=record["etag"], citation=record["citation"],
                                acl_sha=_acl_sha(item))
                seen[record["id"]] = previous
                return "metadata_only"
        change = "changed" if previous.get("seq") else "new"
        seq = int(state["last_seq"]) + 1
        case_id = case_id_for(record["drive_id"], record["id"])
        reasons: list[str] = []
        original: dict[str, Any] = {
            "citation": record["citation"], "drive_id": record["drive_id"],
            "item_id": record["id"], "etag": record["etag"], "path": record.get("path"),
            "name": record.get("name"), "size": record.get("size"),
            "mime_type": record.get("mime_type"), "original_url": record.get("original_url"),
            "last_modified": record.get("last_modified"),
            "last_modified_by": record.get("last_modified_by"),
            "source_acl": record.get("source_acl"), "sha256": digest, "stored": None}
        entry: dict[str, Any] = {
            "schema": RECORD_SCHEMA, "intake_seq": seq, "recorded_at": now,
            "unit_id": self.unit_id, "change": change, "case_id": case_id,
            "previous_sha256": previous.get("sha256"), "original": original,
            "detected_type": "other", "type_warnings": []}
        name = record.get("name") or ""
        if digest is None:
            # Over the byte cap: never read, so the whole original is one skipped part and
            # the file name is the only type evidence.
            accounting = capped(name, record.get("size"), MAX_CONTENT_BYTES)
            by_name = _EXTENSIONS.get(PurePosixPath(name).suffix.lower())
            if by_name is not None:
                entry["detected_type"] = by_name
                entry["type_warnings"] = ["type_from_name_only"]
        else:
            original["stored"] = self.files.pin(content)
            entry["detected_type"], entry["type_warnings"] = detect_type(content, name)
            accounting = account(content, name, pin=self.files.pin, ocr=self.ocr)
        text = accounting.pop("text")
        parts = accounting["parts"]
        reasons.extend(entry["type_warnings"])
        reasons.extend(f"unread:{p['reason']}" for p in parts if p["state"] == UNREAD)
        reasons.extend(f"skipped_by_cap:{p['cap']}" for p in parts if p["state"] == SKIPPED)
        entry["accounting"] = accounting
        entry["extraction"] = {"chars": len(text), "text_ref": (
            self.files.write_text(digest, text) if text and digest else None)}
        entry["review_reasons"] = sorted(set(reasons))
        entry["needs_extraction_review"] = bool(entry["review_reasons"])
        # The sequence number is spent before any write, so a record whose stub write
        # failed is never followed by another entry with the same intake_seq.
        state["last_seq"] = seq
        self.files.save(state)
        self.files.append_record(entry)
        outcome = known_outcome(text)
        questions, truncated = questions_in(text)
        if outcome.get("statements", 0) > 1:
            outcome["note"] = "several outcome statements; the first is shown"
        self.files.append_stub({
            "schema": EVAL_SCHEMA, "case_id": case_id, "intake_seq": seq, "recorded_at": now,
            "artifact_citation": record["citation"], "sha256": digest,
            "split": split_for(case_id), "split_rule": SPLIT_RULE,
            "text_ref": entry["extraction"]["text_ref"],
            "text_sha256": _sha(text.encode("utf-8")) if text else None,
            "detected_type": entry["detected_type"], "known_outcome": outcome,
            "draft": "outcome_stated" if outcome["stated"] else "no_stated_outcome",
            "artifact_complete": accounting["complete"],
            "questions": questions, "questions_truncated": truncated,
            "text_available": bool(text), "status": "stub_unconfirmed",
            "note": "Placeholders read from the document; a person confirms the outcome, "
                    "questions, desired actions, locations and permissions before this "
                    "entry is used for evaluation."})
        seen[record["id"]] = {"etag": record["etag"], "sha256": digest, "seq": seq,
                              "citation": record["citation"], "acl_sha": _acl_sha(item)}
        return change


# ------------------------------------------------------------------ scan report

def _since(state_dir: str | Path, after_seq: int) -> tuple[list[dict], list[dict]]:
    """Committed entries after ``after_seq``. The stub append is the commit point: a
    record whose stub write failed (disk full) is an orphan and is not reported; its
    item is retried under a new sequence number."""
    files = IntakeState(state_dir)
    stubs = [s for s in files.stubs() if s.get("intake_seq", 0) > after_seq]
    committed = {s.get("intake_seq") for s in stubs}
    records = [r for r in files.records() if r.get("intake_seq", 0) > after_seq
               and r.get("intake_seq") in committed]
    return records, stubs


def _summary(records: Sequence[Mapping[str, Any]], stubs: Sequence[Mapping[str, Any]]
             ) -> dict[str, Any]:
    by_type = {kind: 0 for kind in TYPES}
    for record in records:
        by_type[record.get("detected_type", "other")] = \
            by_type.get(record.get("detected_type", "other"), 0) + 1
    reasons: dict[str, int] = {}
    for record in records:
        for reason in record.get("review_reasons", []):
            key = reason.split(":", 1)[0]
            reasons[key] = reasons.get(key, 0) + 1
    parts = [p for r in records for p in (r.get("accounting") or {}).get("parts") or []]
    unread: dict[str, int] = {}
    capped_by: dict[str, int] = {}
    panicked: dict[str, int] = {}
    # Per format (of the file each part belongs to, ``part_format``; a capped original:
    # by its name): parts read/unread/skipped and the converter family of each read part.
    by_format: dict[str, dict[str, Any]] = {}
    by_method = {family: 0 for family in FAMILIES}
    for record in records:
        for part in (record.get("accounting") or {}).get("parts") or []:
            row = by_format.setdefault(part_format(part, record.get("detected_type")), {
                "read": 0, "unread": 0, "skipped_by_cap": 0, "methods": {}})
            state = part.get("state")
            if state == EXTRACTED:
                family = METHOD_FAMILIES.get(part.get("method"), "stdlib")
                row["read"] += 1
                row["methods"][family] = row["methods"].get(family, 0) + 1
                by_method[family] += 1
            else:
                row["unread" if state == UNREAD else "skipped_by_cap"] += 1
    msg_properties: dict[str, dict[str, int]] = {}
    metadata_properties: dict[str, dict[str, int]] = {}
    pdf_entries: dict[str, int] = {}
    for part in parts:
        if part.get("state") == UNREAD:
            key = part.get("reason") or "(none)"
            unread[key] = unread.get(key, 0) + 1
            if key == "parser_panicked":
                kind = part.get("kind") if part.get("kind") in KINDS else "unknown"
                panicked[kind] = panicked.get(kind, 0) + 1
            elif key in ("msg_property_not_decoded", "non_content_metadata"):
                by_tag = msg_properties if key == "msg_property_not_decoded" \
                    else metadata_properties
                for prop_type, prop_id in _msg_tags(part.get("tag")):
                    row = by_tag.setdefault(prop_type, {})
                    row[prop_id] = row.get(prop_id, 0) + 1
            elif key == "pdf_entry_not_parsed":
                entry = part.get("entry") if part.get("entry") in PDF_ENTRIES else "untyped"
                pdf_entries[entry] = pdf_entries.get(entry, 0) + 1
        elif part.get("state") == SKIPPED:
            capped_by[str(part.get("cap"))] = capped_by.get(str(part.get("cap")), 0) + 1
    return {
        "artifacts": len(records),
        "new_artifacts": sum(r.get("change") == "new" for r in records),
        "changed_artifacts": sum(r.get("change") == "changed" for r in records),
        "by_type": {k: v for k, v in by_type.items() if v},
        "needs_extraction_review": sum(bool(r.get("needs_extraction_review")) for r in records),
        "review_reasons": dict(sorted(reasons.items())),
        "originals_pinned": sum(bool((r.get("original") or {}).get("stored")) for r in records),
        "complete_artifacts": sum(bool((r.get("accounting") or {}).get("complete"))
                                  for r in records),
        "parts": {state: sum(p.get("state") == state for p in parts)
                  for state in (EXTRACTED, UNREAD, SKIPPED)},
        # Denominators: an exit 0 is not document coverage. Every part has one state, so
        # parts_total = parts_read + unread + skipped_by_cap.
        "parts_total": len(parts),
        "parts_read": sum(p.get("state") == EXTRACTED for p in parts),
        # Binary .msg metadata is counted (unread_by_reason) but is no text to read:
        # text_parts_total = parts_total - parts_non_content_metadata.
        "parts_non_content_metadata": unread.get("non_content_metadata", 0),
        "text_parts_total": len(parts) - unread.get("non_content_metadata", 0),
        "parts_panicked": sum(panicked.values()),
        "by_format": dict(sorted(by_format.items())),
        "parts_by_method": by_method,
        # Whether this host converts PDF pages through MarkItDown (parts_by_method.markitdown).
        "markitdown_available": doc_convert.available(),
        # Per format: MarkItDown imports and that format's extra modules are installed.
        "markitdown_ready_by_format": {kind: doc_convert.available(kind)
                                       for kind in ("docx", "pdf", "pptx")},
        "panicked_by_kind": dict(sorted(panicked.items())),
        "unread_by_reason": dict(sorted(unread.items())),
        "unread_without_reason": unread.get("(none)", 0),
        # The msg_property_not_decoded parts by property type and id (schema tags only).
        "unread_msg_properties": {t: dict(sorted(ids.items()))
                                  for t, ids in sorted(msg_properties.items())},
        "non_content_msg_properties": {t: dict(sorted(ids.items()))
                                       for t, ids in sorted(metadata_properties.items())},
        # The pdf_entry_not_parsed parts by typed entry (where, and its PDF name).
        "unread_pdf_entries": dict(sorted(pdf_entries.items())),
        "skipped_by_cap": dict(sorted(capped_by.items())),
        "parts_skipped_by_cap": sum(capped_by.values()),
        "evaluation_stubs": len(stubs),
        "by_split": {split: n for split in ("dev", "held_out")
                     if (n := sum(s.get("split") == split for s in stubs))},
        "outcomes_stated": sum(bool((s.get("known_outcome") or {}).get("stated"))
                               for s in stubs),
        "questions_found": sum(len(s.get("questions") or []) for s in stubs)}


def _msg_tags(tag: Any) -> list[tuple[str, str]]:
    """``(type, id)`` of each entry an undecoded ``.msg`` part's ``tag`` names (a
    property table's is a list, one per entry it could not decode)."""
    entries = tag if isinstance(tag, list) and tag else [tag]
    return [_msg_tag(entry) for entry in entries]


def _msg_tag(tag: Any) -> tuple[str, str]:
    """``(type, id)`` of one tag; anything not tag-shaped (a record written before parts
    carried one) is ``untagged``, never shown as it is."""
    tag = tag if isinstance(tag, Mapping) else {}
    prop_type, prop_id = tag.get("type"), tag.get("id")
    return (prop_type if isinstance(prop_type, str) and TAG_TYPE.fullmatch(prop_type)
            else "untagged",
            prop_id if isinstance(prop_id, str) and TAG_ID.fullmatch(prop_id) else "untagged")


def _string_values(node: Any, into: set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _string_values(value, into)
    elif isinstance(node, list):
        for value in node:
            _string_values(value, into)
    elif isinstance(node, str):
        into.add(node)


# Stored values that are vocabulary of this module, not source data.
_VOCABULARY = frozenset({RECORD_SCHEMA, EVAL_SCHEMA, "stub_unconfirmed", DEFAULT_SOURCE_ID,
                         "held_out", SPLIT_RULE, "outcome_stated", "no_stated_outcome",
                         *VOCABULARY})


# Review reasons this module writes (their key before ":" is what the counts show).
REASON_KEYS = frozenset({"type_mismatch", "zip_unreadable", "text_not_utf8",
                         "type_from_name_only", "unread", "skipped_by_cap"})


def _is_reason(value: str) -> bool:
    key = value.split(":", 1)[0]
    return key in REASON_KEYS


def row_data_check(state_dir: str | Path, text: str) -> dict[str, int]:
    """Self-check that a counts-only output holds no intake data (strings >= 8 chars)."""
    files = IntakeState(state_dir)
    values: set[str] = set()
    for row in files.records() + files.stubs():
        _string_values(row, values)
    checked = leaked = 0
    for value in values:
        # A part's property tag is schema (a named property's set and LID), never data.
        if len(value) < 8 or value in _VOCABULARY or _is_reason(value) or TAG_ID.fullmatch(value):
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.+]+", value):
            continue
        checked += 1
        leaked += value in text
    return {"values_checked": checked, "leaked": leaked}


def _visible(record: Mapping[str, Any], audience: Any, reader: Actor,
             listings: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> bool:
    """The reader may see the item both as it was taken in and as the newest listing
    has it (path and source ACL). An item the newest traversal did not return
    (withheld, moved out, absent) or with no current listing is withheld: a
    permission change keeps the eTag, so the intake-time ACL alone is never enough."""
    original = record.get("original") or {}
    current = (listings.get(record.get("unit_id")) or {}).get(original.get("item_id"))
    if not isinstance(current, Mapping) or current.get("drive_id") != original.get("drive_id"):
        return False
    for view in (original, current):
        drive, path = view.get("drive_id"), view.get("path")
        acl = view.get("source_acl")
        if (not isinstance(drive, str) or not isinstance(path, str)
                or not isinstance(acl, Mapping)
                or not audience.visible_all(reader, [f"sharepoint:{drive}:{path}"], acl)):
            return False
    return True


def scan_report(state_dir: str | Path, *, advance: bool = True,
                detail_for: tuple[Any, Actor] | None = None,
                clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Intake records since the previous scan: counts, plus detail for an authorized reader.

    ``detail_for`` is ``(LiveSourceAudience, reader Actor)``: each record is
    shown only if that audience lets the reader see the item's path and, where
    the audience requires it, the item's own source ACL (read at intake)."""
    files = IntakeState(state_dir)
    with files.lock():
        state = files.load()
        after = int(state.get("last_scan_seq") or 0)
        records, stubs = _since(state_dir, after)
        report: dict[str, Any] = {
            "schema": SCAN_SCHEMA, "scan": int(state["scans"]) + (1 if advance else 0),
            "advanced": advance, "summary": _summary(records, stubs),
            "pending_total": 0}
        # Items whose intake failed are never dropped silently: counted every scan.
        failures = [f for f in (state.get("failed") or {}).values() if isinstance(f, dict)]
        report["failed_items"] = len(failures)
        report["failed_items_waiting_for_new_upload"] = sum(
            int(f.get("attempts") or 0) >= FAILURE_RETRY_CAP for f in failures)
        report["pending_total"] = len(failures)
        if detail_for is not None:
            audience, reader = detail_for
            from operating.ingestion import IngestStore

            store = IngestStore(state_dir)
            try:
                listings = {unit: store.current_listing(unit)
                            for unit in {r.get("unit_id") for r in records}
                            if isinstance(unit, str)}
            finally:
                store.close()
            shown = [r for r in records if _visible(r, audience, reader, listings)]
            sequences = {r["intake_seq"] for r in shown}
            report["detail"] = {
                "records": shown,
                "evaluation_stubs": [s for s in stubs if s.get("intake_seq") in sequences],
                "withheld_from_reader": len(records) - len(shown)}
        if advance:
            state["scans"] = int(state["scans"]) + 1
            state["last_scan_seq"] = int(state["last_seq"])
            state["last_scan_at"] = _now_iso((clock or (lambda: datetime.now(_UTC)))())
            files.save(state)
    return report


def dot_note(report: Mapping[str, Any], *, evidence: str) -> str:
    """Counts-only note text the controller may post to the Dot (nothing is posted here)."""
    s = report.get("summary") or {}
    parts = s.get("parts") or {}
    types = ", ".join(f"{k} {v}" for k, v in sorted((s.get("by_type") or {}).items())) or "none"
    unit = report.get("ingestion_unit") or {}
    unit_line = (f"Ingestion unit state: {unit.get('state')}"
                 + (f" ({', '.join(unit.get('reasons') or [])})" if unit.get("reasons") else "")
                 + ".\n") if unit else ""
    folder = report.get("use_case_folder") or {}
    if folder:
        unit_line += (f"Folder: {folder.get('state')}"
                      + (f" ({folder.get('reason')})" if folder.get("reason") else "") + ".\n")
    run = report.get("intake") or {}
    counts = run.get("counts") or {}
    return (
        "Use-case intake, Ontology UseCases folder (read-only; nothing was written to "
        "SharePoint).\n"
        f"{unit_line}"
        f"Since the last scan: {s.get('new_artifacts', 0)} new and "
        f"{s.get('changed_artifacts', 0)} changed artifacts (types: {types}).\n"
        f"{s.get('complete_artifacts', 0)} fully read; "
        f"{s.get('needs_extraction_review', 0)} need extraction review (parts unread: "
        f"{parts.get('unread', 0)}, skipped by a cap: {parts.get('skipped_by_cap', 0)}); "
        f"{s.get('originals_pinned', 0)} originals pinned by SHA-256.\n"
        f"Evaluation-set stubs: {s.get('evaluation_stubs', 0)} "
        f"({s.get('outcomes_stated', 0)} with an explicitly stated outcome, "
        f"{s.get('questions_found', 0)} questions found); unconfirmed placeholders.\n"
        f"Withheld by permission at read: {counts.get('withheld_at_read', 0)}; read errors: "
        f"{counts.get('read_errors', 0)}; still pending: {report.get('pending_total', 0)} "
        f"(of which {report.get('failed_items', 0)} failed intake and are retried).\n"
        f"Evidence: {evidence}. Counts only; an authorized reader gets detail with "
        "`python3 -m operating.use_case_intake scan --state-dir D --peek --detail "
        "--reader-audience R --principal P`.")


# ------------------------------------------------------------------ CLI

FOLDER_STATES = ("reached", "partial", "empty", "withheld", "unreachable")


def folder_state(unit: Mapping[str, Any], files: int) -> dict[str, Any]:
    """Whether this run reached the delivery folder, as one explicit state and reason.

    ``unreachable``: no folder was listed (site unresolved, path absent, not
    configured, source error); the reason is the unit's most specific reason,
    never silence. ``empty``: the folder was listed and nothing in it is a file
    the ingest principal may see, and nothing was withheld. ``withheld``: no
    visible file, but items were withheld by audience or item permission.
    ``partial``: the unit run did not finish cleanly, with or without visible files (a
    source error part-way through the listing); the reason is the unit's most
    specific reason. Else ``reached``. ``files`` is the unit's current visible
    file count."""
    counts = unit.get("counts") or {}
    reasons = [r for r in unit.get("reasons") or [] if isinstance(r, str)]
    if not int(counts.get("folders_listed") or 0):
        specific = [r for r in reasons if r.startswith(("site_unresolved", "source_error"))]
        return {"state": "unreachable",
                "reason": (specific or reasons or ["folder_not_listed"])[0]}
    state = unit.get("state")
    # An item withheld by permission leaves the unit incomplete with only a ``withheld:N``
    # reason: that alone is not a degraded run. Any other reason (a source error part-way
    # through), or an interrupted run, is, and it is checked before the visible-file count.
    other = [r for r in reasons if not r.startswith("withheld")]
    if state not in (None, "complete") and (files > 0 or other or not reasons):
        specific = [r for r in reasons if r.startswith("source_error")]
        return {"state": "partial", "reason": (specific or reasons or [str(state)])[0]}
    withheld = int(counts.get("withheld") or 0)
    if files <= 0:
        return ({"state": "withheld", "reason": f"withheld:{withheld}"} if withheld
                else {"state": "empty", "reason": "folder_has_no_files"})
    return {"state": "reached", "reason": None}


def _live_scan(args: argparse.Namespace, environ: Mapping[str, str], graph_sender: Any,
               clock: Callable[[], datetime] | None) -> dict[str, Any]:
    from integrations import graph_read
    from integrations.live_source_guard import SourceUnavailable
    from operating.ingestion import (IngestStore, Ingestor, live_audience_sites,
                                     load_live_audience, load_sources_file, resolve_sites)

    tenant, actor, sources = load_sources_file(args.sources, environ)
    specs = [s for s in sources if s["id"] == args.source_id]
    if not specs or specs[0]["kind"] != "sharepoint_folder":
        raise IntakeError("use_case_source_not_found")
    spec = specs[0]
    api = transport = None
    if not spec["not_configured"]:
        try:
            api, transport = graph_read.build_graph(environ, sender=graph_sender)
        except (SourceUnavailable, ValueError):
            api = None  # credentials missing: the unit reports not_configured
    sites: dict[str, str] = {}
    if api is not None and spec.get("site"):
        # The folder's site is resolved by name at run time (GET-only), only if
        # the live audience grants anything on that site.
        sites = resolve_sites([spec], api, environ,
                              granted=live_audience_sites(args.live_audience))
    audience = load_live_audience(args.live_audience, environ, sites=sites)
    if audience.tenant != tenant:
        raise IntakeError("use_case_live_audience_tenant_mismatch")
    readers: dict[str, Any] = {}
    reader = None
    if api is not None and spec.get("drive_id"):
        try:
            # Item permissions are read so each record carries its source ACL and a
            # reader's detail view can enforce it.
            reader = graph_read.SharePointReader(api, audience, spec["drive_id"],
                                                 transport=transport, read_permissions=True,
                                                 max_acl_reads=capture_sharepoint.MAX_ACL_READS)
            readers[spec["drive_id"]] = reader
        except ValueError:
            reader = None  # an invalid drive id stays unconfigured
    ingestor = Ingestor(args.state_dir, [spec], actor=actor, sharepoint=readers, clock=clock)
    try:
        unit = ingestor.run(only=[spec["id"]])["units"][0]
    finally:
        ingestor.close()
    result: dict[str, Any] = {"ingestion_unit": {
        "unit_id": unit["unit_id"], "state": unit["state"], "reasons": unit["reasons"],
        "counts": unit["counts"]}}
    store = IngestStore(args.state_dir)
    try:
        files = sum(item.get("kind") == "file" for item in store.current_items(spec["id"]))
    finally:
        store.close()
    result["use_case_folder"] = folder_state(unit, files)
    if reader is not None:
        result["intake"] = UseCaseIntake(args.state_dir, reader=reader, actor=actor,
                                         unit_id=spec["id"], max_items=args.max_items,
                                         clock=clock, ocr=local_image_ocr()).intake()
    else:
        result["intake"] = {"counts": _counts(), "read_errors_by_class": {},
                            "not_run": ("site_unresolved" if spec.get("site_unresolved")
                                        else "reader_not_configured")}
    return result


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         graph_sender: Any = None, clock: Callable[[], datetime] | None = None,
         evaluation_items: Callable[..., dict[str, Any]] | None = None) -> int:
    """The scan CLI. ``evaluation_items(state_dir, clock=)`` (eval tooling,
    ``scripts.eval_use_case_items``) adds its counts as ``evaluation_items``."""
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python3 -m operating.use_case_intake",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="intake new/changed artifacts; list since last scan")
    scan.add_argument("--state-dir", required=True, type=Path)
    scan.add_argument("--sources", type=Path)
    scan.add_argument("--live-audience", type=Path)
    scan.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    scan.add_argument("--max-items", type=int, default=MAX_ITEMS_PER_SCAN)
    scan.add_argument("--detail", action="store_true")
    scan.add_argument("--reader-audience", type=Path)
    scan.add_argument("--principal", action="append", default=[])
    scan.add_argument("--clearance", default="internal")
    scan.add_argument("--dot-note", action="store_true")
    scan.add_argument("--peek", action="store_true", help="do not advance the scan marker")
    args = parser.parse_args(argv)
    try:
        detail_for = None
        if args.detail:
            if (args.reader_audience is None or not args.principal
                    or any(not p or "*" in p for p in args.principal)
                    or args.clearance not in LEVELS):
                raise IntakeError("use_case_detail_requires_reader")
            from operating.ingestion import load_live_audience

            audience = load_live_audience(args.reader_audience, env)
            detail_for = (audience, Actor(audience.tenant, frozenset(args.principal),
                                          args.clearance))
        live: dict[str, Any] = {}
        if args.sources is not None or args.live_audience is not None:
            if args.sources is None or args.live_audience is None:
                raise IntakeError("use_case_live_requires_sources_and_audience")
            live = _live_scan(args, env, graph_sender, clock)
        report = scan_report(args.state_dir, advance=not args.peek, detail_for=detail_for,
                             clock=clock)
        report.update(live)
        if evaluation_items is not None:
            report["evaluation_items"] = evaluation_items(args.state_dir, clock=clock)
        report["reader"] = "environment" if live else "stored_state"
        if "intake" in report:
            report["pending_total"] += report["intake"]["counts"]["pending"]
        if args.dot_note:
            report["dot_note"] = dot_note(report, evidence=(
                "live read, live-unverified until the verifier records "
                "ingest.use_cases" if live else "stored intake state"))
        if detail_for is None:
            counts_text = json.dumps(report, sort_keys=True)
            report["row_data_check"] = row_data_check(args.state_dir, counts_text)
            if live:
                from operating.ingestion import row_data_check as ingest_check

                report["ingest_row_data_check"] = ingest_check(args.state_dir, counts_text)
        print(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False))
        return 0
    except IntakeError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - IngestError and friends: code only
        code = getattr(exc, "code", None)
        print(json.dumps({"error": code if isinstance(code, str) else type(exc).__name__}),
              file=sys.stderr)
        return 1


__all__ = ["DEFAULT_SOURCE_ID", "IntakeError", "IntakeState", "UseCaseIntake", "case_id_for",
           "detect_type", "dot_note", "known_outcome", "main", "outcome_statements",
           "questions_in", "row_data_check", "scan_report", "split_for"]


if __name__ == "__main__":
    raise SystemExit(main())
