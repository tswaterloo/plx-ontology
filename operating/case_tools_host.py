"""What the case host adds to the generic tools it lists.

docs/decisions/2026-10-03-host-tool-budget.md phase 1: the case host keeps 22 tools
and lists ``plx.read`` in place of the retired ``plx.source_citation`` and
``plx.case_cite_mail``. ``PagedRead`` is ``plx.read`` as the host serves it: the
generic read of ``operating.ontology_tools_evidence`` (same reader, policy, statuses and
digests) plus what the retired tools gave a client, inside the frozen contract
(``operating/ontology_tool_contracts.json``):

* ``text_offset`` pages a long text (an original's page, a mail or document body, or
  with ``field_path`` one long field of a row) by characters: ``text_window`` names the
  slice, its ``next_offset`` and the SHA-256 of the whole text, so a client that pages
  to the end can check it reassembled exactly what the citation names; it also names
  the ``page`` and ``page_citation`` of a page read (the citation ``plx.read`` takes
  back) and the ``first_line``/``last_line`` it covers; ``line`` reads one line
  (1-based, numbered in the whole text) for a line-pinned question;
* ``column_offset`` pages a row too wide for one response by fields (a row that does
  not fit is paged from 0 on its own); ``truncated_fields`` names every field whose
  envelope was cut, readable whole with ``field_path`` and ``text_offset``;
* an original's file citation lists its extraction versions (``extractions``);
* a live Graph mail read returns ``cite_as``: the body-pinned citation
  (``#body_sha256=..&shown_sha256=..``, ``CaseRuntime.mail_message``) of the body it
  shows, minted from the same read, and the digest the case store records for it, so a
  finding citing it detects a changed body; a body that changed after it was shown gets
  a ``cite_as_withheld`` gap and no ``cite_as`` (kept when the extras do not fit and
  the generic read is returned), and a pin that cannot be resolved now gets a
  ``cite_as_unresolvable`` gap, never silence.

Every extra is read for the same actor through the runtime's audience-checked readers
(an original outside the audience reads as missing; a message of another version is
never pinned), so the merged read shows nothing the generic read would withhold.

``CaptureCaseHostTools`` is ``CaseHostTools`` over the versioned captures the host holds
(mail and SharePoint):

* mail (``mail_capture_dir``): its read policy grants the ``mail:<mailbox>`` units of
  the projection that ``MailCaptureStore.audience_check`` admits for the employee-gated
  reader, and its item gate re-checks every ``graph-mail://`` handle per item
  (``MailCaptureStore.item_gate``, then the case mail scope and the employee's item ACL
  as ``plx.read`` decides them);
* SharePoint (``sharepoint_capture_dir``): ``sharepoint:<drive>`` units are granted from
  ``SharePointCaptureStore.audience_check``, and every ``sharepoint://`` handle is
  re-gated per item: the capture's path keys and item permissions for the reader, the
  employee's item ACL (on the version asked for and on the item's latest captured
  version, so a revocation captured since withholds the older versions too), never a
  version gone from the scope (moved out or deleted),
  never a projected name or path the capture no longer holds, and, where the host has
  a live reader for the drive, the item re-read now (GET metadata and permissions:
  a revocation, a deletion or a move applies at once). The re-reads of one tool call
  are bounded by ``max_live_rereads``. Every live request of one tool call (mail live
  reads, the live source checks of originals, SharePoint re-reads and live reads, BC
  reads, token POSTs included) shares one bound of ``max_live_gets`` requests over
  the host's Graph and BC transports, active for the whole call (``plx.read`` through
  ``PagedRead`` included) and never reset by a read inside it. The bound is hard: a
  re-read or live SharePoint read that would exceed it is not started, and any
  request past it is refused before I/O (``live_source_guard.RequestBudget``). A read
  a refusal cut off is decided as past the bound (a ``live_read_budget`` refusal for
  a cited mail message, original, SharePoint file or BC record), never served from
  what it read before the refusal. Past
  the bound, without a live reader, or when the source fails transiently (a failure
  stops further re-reads in that call) the permissions recorded at capture decide,
  but only for a capture younger than ``max_capture_age``: an older one is served
  only when a live re-read succeeds. A transient failure does not hide a captured
  file: it is served labelled with the failure (``live_reread_failed``); HTTP 410
  (Gone) is a refusal, like a deletion. A captured file the gate refuses is a refusal
  with its reason in ``plx.read`` (``sharepoint_item_refused``, ``capture_too_old``,
  ``capture_stale`` for a file moved since the projection was built, which is served
  again once the projection is rebuilt, ``capture_unreadable``), never a live read in
  its place; a ``sharepoint://`` citation the capture does not
  hold for this reader is read live only within the same per-call bounds
  (``live_read_budget`` past them). ``plx.read``
  reads a captured file (and one page of a PDF, by ``#page=<n>`` or the ``page``
  argument) from the capture, so its text and the mention spans cited in it read for
  an allowed reader. Every answer that serves a captured file labels it
  ``as_of_capture`` with the capture time (a ``stale`` gap when the live re-read found
  a newer version), never as a live read, and an answer whose counts or totals were
  computed over captured files it does not list carries one ``as_of_capture`` label
  for them; OCR text is labelled unconfirmed, a scanned PDF the capture could not OCR
  is an ``ocr_unavailable`` gap, a file over the capture's content cap a
  ``content_exceeds_cap`` gap (its metadata captured, its text not) and one whose bytes
  are captured but over the text extractor's bound an ``exceeds_bound`` gap (its text
  never read whole; a zip/.msg/.eml among them is read member by member by the
  projection, not by this host's scan); a PDF read on its first pages only (the page
  cap) carries a ``skipped_by_cap`` gap naming the pages not read, and a page among
  them reads ``unresolvable`` with that gap, never as a page that does not exist (a
  page past the last page of a file whose text was read is ``page_not_found``); a
  case finding citing a page its reading never read (past the cap or the last page) is
  refused ``page_not_read``.
  ``plx.documents``
  with an identifier in its query also searches the captured text for this reader
  (identifiers parsed by the one grammar, each mention cited by page; a lookup by
  identifier over every captured file the reader may see, built once per capture state
  and reused, gives the files and pages naming it, and the host
  gates and re-reads only a file whose text names the identifier, or a capped PDF whose
  pages read do not, reported as a ``skipped_by_cap`` gap so that no match is never
  absence; the matching files are gated first, and a file the call's re-read bound
  drops is counted in a ``live_read_budget`` gap), so a document
  question is answered from the capture when the projection has no object to link the
  mention to (e.g. Portal is down).
* Teams channel messages are served by ``CaseHostTools`` itself (``case_host_policy``
  grants the channel unit, ``CaseRuntime.resolve_citation`` gates each row against the
  runtime's Teams capture, the read the claim gate makes); the gate below hands their
  handles to it unchanged.

Evidence label: ``isolated-fixture`` / ``injected-transport`` in tests; live-unverified.
"""

from __future__ import annotations

from bisect import bisect_right
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Iterator, Mapping

from ontology import mentions as mn
from ontology.json_contract import validate
from operating import ontology_tools as ot
from operating.capture_age import MAX_CAPTURE_AGE, capture_decides
from operating.cases import CitationUnresolvable
from operating.ontology_host import CaseHostTools
from operating.ontology_tools import ReadPolicy, ToolError, _completeness, _error, _gap
from operating.ontology_tools_evidence import (DEFAULT_LIMIT, DEFAULT_READ_CHARS,
                                               MAX_MENTIONS, PAGING_ARGUMENTS,
                                               CitationReader, EvidenceTools,
                                               RuntimeCitations, capped_gaps, field_value,
                                               skipped_gap, wanted_field)
from operating.playbook_originals import extraction_skipped

_READ_OK = ("resolved", "changed_since")
PAGE_NOT_READ = "page_not_read"
_METHOD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]{0,63}$")
_WORD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{5,18}(?:Z|[+-][0-9]{2}:[0-9]{2})$")


class _Recording(CitationReader):
    """A ``CitationReader`` that keeps the last raw read, so a page is cut from exactly
    the content the generic read decided on (one read, one digest)."""

    def __init__(self, reader: CitationReader) -> None:
        self.reader = reader
        self.last: dict[str, Any] | None = None

    def read(self, handle: str, actor: Any, *, page: int | None = None) -> dict[str, Any]:
        self.last = self.reader.read(handle, actor, page=page)
        return self.last

    def originals(self, actor: Any, limit: int) -> list[dict[str, Any]]:
        return self.reader.originals(actor, limit)

    def original_text(self, handle: str, actor: Any) -> dict[str, Any] | None:
        return self.reader.original_text(handle, actor)

    def field_text(self, handle: str, field_path: str, actor: Any) -> str | None:
        return self.reader.field_text(handle, field_path, actor)


def _text_of(value: Any) -> str | None:
    """A field as the text a window pages: a string, or JSON for an object or array."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return None


class PagedRead:
    """``plx.read`` on the case host: ``EvidenceTools`` plus paging, extraction versions
    and the mail body pin (module docstring). ``runtime``: the session's case runtime."""

    def __init__(self, evidence: EvidenceTools, runtime: Any) -> None:
        self.evidence = evidence
        self.runtime = runtime
        self.contracts = evidence.contracts
        self.limits = evidence.limits

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        """One ``plx.read``; on a ``CaptureCaseHostTools`` reader, one call of that host
        (its per-call request budget covers the read and its extras)."""
        host = getattr(self.evidence.reader, "host", None)
        if not isinstance(host, CaptureCaseHostTools):
            return self._call(arguments, actor, None)
        with host.per_call() as budget:
            return self._call(arguments, actor, budget)

    def _call(self, arguments: Any, actor: Any, budget: Any) -> dict[str, Any]:
        contract = self.contracts["tools"]["plx.read"]
        if not isinstance(arguments, dict):
            return self._checked(_error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(_error("invalid_input", "; ".join(problems)))
        if sum(key in arguments for key in PAGING_ARGUMENTS) > 1:
            return self._checked(_error(
                "invalid_input", "text_offset, column_offset and line are exclusive"))
        recording = _Recording(self.evidence.reader or CitationReader())
        tools = EvidenceTools(self.evidence.base, traverse=self.evidence.traverse,
                              reader=recording if self.evidence.reader else None)
        base_args = {k: v for k, v in arguments.items() if k not in PAGING_ARGUMENTS}
        try:
            result = tools.read_unbounded(base_args, actor)
        except ToolError as exc:
            return self._checked(_error(exc.code, exc.message, exc.hint))
        except sqlite3.Error as exc:
            return self._checked(_error(
                "store_unavailable", f"the projection store could not be read "
                f"({type(exc).__name__})", "rebuild or reopen the projection store"))
        raw = recording.last
        if result.get("status") not in _READ_OK or raw is None:
            return self._checked(result)
        if "text_offset" in arguments or "line" in arguments:
            return self._checked(self._window(result, raw, arguments))
        if "column_offset" in arguments:
            return self._checked(self._columns(result, raw, arguments["column_offset"]))
        refused = budget.refused if budget is not None else 0
        result = self._extras(result, raw, arguments, actor)
        if budget is not None and budget.refused > refused:
            # The read was whole; an extra (extraction versions, the pinned citation)
            # was cut off by the bound: its absence is said, never silent.
            result["gaps"] = [*(result.get("gaps") or []), _gap(
                "unknown", "live_read_budget", arguments["citation"][:700],
                "this call's live read bound is reached; the extraction versions or the "
                "pinned citation may be missing; ask again in a new call")][:20]
        if ot._envelope_bytes(result) > self.limits["max_result_bytes"] and raw.get("fields") \
                and "field_path" not in arguments:
            # A row too wide for one response is paged by its fields from the first.
            return self._checked(self._columns(result, raw, 0))
        return self._checked(result)

    # ---- result shaping ------------------------------------------------------

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"]["plx.read"]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if ot._envelope_bytes(result) > self.limits["max_result_bytes"]:
            return _error("result_too_large", "the answer does not fit one response",
                          "page it: text_offset (with field_path for one field) or "
                          "column_offset")
        return result

    def _fits(self, result: dict[str, Any]) -> bool:
        return ot._envelope_bytes(result) <= self.limits["max_result_bytes"]

    @staticmethod
    def _truncated(fields: Mapping[str, Any]) -> list[str]:
        return sorted(path for path, value in fields.items()
                      if isinstance(value, dict) and value.get("truncated")
                      and ot._FIELD_PATH.match(path))

    def _extras(self, result: dict[str, Any], raw: Mapping[str, Any],
                arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        handle = arguments["citation"]
        out = dict(result)
        cut = self._truncated(out.get("fields") or {})
        if cut:
            out["truncated_fields"] = cut[:400]
        if "page" in arguments or "page=" in handle:
            return out  # a page read: its text is the answer
        gaps: list[dict[str, Any]] = []
        if handle.startswith("original://"):
            listing = self._extractions(handle, actor)
            if listing:
                out.update(extractions=listing[0], latest_extraction_version=listing[1])
        elif handle.startswith("graph-mail://") and raw.get("values_citation"):
            pinned = self._pinned(raw, actor)
            if pinned is not None and "gap" in pinned:
                gaps.append(pinned["gap"])
            elif pinned is not None:
                out["cite_as"] = pinned
        if gaps:
            out["gaps"] = [*(out.get("gaps") or []), *gaps][:20]
        if self._fits(out):
            return out
        # Too large with the extras: the generic read, but never without the gap that
        # says why no pinned citation is offered.
        return dict(result, gaps=[*(result.get("gaps") or []), *gaps][:20]) if gaps \
            else result

    def _extractions(self, handle: str, actor: Any) -> tuple[list, int] | None:
        """The original's extraction versions as this reader may read them now."""
        view = self.runtime.read_original(handle, actor)
        if not view.get("ok") or not isinstance(view.get("extractions"), list):
            return None
        listing = []
        for item in view["extractions"][:20]:
            method, status = item.get("method"), item.get("status")
            digest, at = item.get("text_sha256"), item.get("extracted_at")
            entry = {"version": item["version"], "citation": item["citation"],
                     "extraction_status": status if _WORD.match(str(status)) else "unknown",
                     "method": method if isinstance(method, str) and _METHOD.match(method)
                     else None,
                     "page_count": item.get("page_count") or 0,
                     "ocr_page_count": item.get("ocr_page_count") or 0,
                     "text_sha256": digest if isinstance(digest, str) and _SHA.match(digest)
                     else None}
            if isinstance(at, str) and _TIME.match(at):
                entry["extracted_at"] = at
            listing.append(entry)
        return listing, view.get("latest_extraction_version") or listing[-1]["version"]

    def _pinned(self, raw: Mapping[str, Any], actor: Any) -> dict[str, Any] | None:
        """The body-pinned citation of the body just shown (``content_pin``, minted from
        the same read as that body), with the digest the case store records for it.
        None for an attachment, an already pinned citation or a read without a body; a
        ``gap`` instead when the source no longer holds the body shown, or when the pin
        cannot be resolved for this reader now, so no pin of a body the reader did not
        see is ever offered and its absence is always explained."""
        from .cases import _digest
        pinned = raw.get("content_pin")
        if "#" in raw["values_citation"] or not isinstance(pinned, str):
            return None  # already pinned (or an attachment): cite it as it is
        try:
            view = self.runtime.resolve_citation(pinned, actor)
        except Exception as exc:  # noqa: BLE001 - surfaced as a gap below
            return {"gap": {"state": "unknown", "reason": "cite_as_unresolvable",
                            "subject": raw["values_citation"][:700],
                            "detail": f"the body-pinned citation could not be resolved for "
                                      f"this reader now ({type(exc).__name__}); no pinned "
                                      f"citation is offered; read the message again"}}
        if view.get("status") != "current":
            return {"gap": {"state": "stale", "reason": "cite_as_withheld",
                            "subject": raw["values_citation"][:700],
                            "detail": "the message body changed after it was read; the "
                                      "body shown is no longer the source's, so no pinned "
                                      "citation is offered; read the message again"}}
        return {"citation": pinned, "digest": _digest(view)}

    def _window(self, result: dict[str, Any], raw: Mapping[str, Any],
                arguments: Mapping[str, Any]) -> dict[str, Any]:
        wanted = arguments.get("field_path")
        if wanted is not None and wanted != raw.get("content_field"):
            fields = raw.get("fields") or {}
            value = fields.get(wanted)
            if wanted not in fields:  # a key inside a JSON column: the one field reader
                got, value = wanted_field(fields, wanted)
                value = value if got is True else None
            text = _text_of(value)
            citation = raw.get("values_citation") or arguments["citation"]
            field_path, key = wanted, "fields"
        else:
            text = raw.get("content") if isinstance(raw.get("content"), str) else None
            citation = raw.get("content_citation") or arguments["citation"]
            field_path, key = raw.get("content_field"), "content"
        if text is None:
            return _error("no_text", "the citation (or field) holds no text to page",
                          "name a text field with field_path, or read without text_offset")
        total = len(text)
        starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
        line = arguments.get("line")
        if line is None:
            offset = arguments["text_offset"]
            if offset > total:
                return _error("invalid_input", f"text_offset beyond the text ({total} chars)")
        elif line > len(starts):
            return _error("invalid_input", f"line beyond the text ({len(starts)} lines)")
        else:
            offset = starts[line - 1]
        stop = total if line is None else (starts[line] - 1 if line < len(starts) else total)
        if line is not None and stop > offset and text[stop - 1] == "\r":
            stop -= 1  # a CRLF line ends before its \r, as the eval line anchors read it
        _head, page = mn.cited_page(citation)
        limit = self.limits["max_untrusted_text_chars"]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        base = {k: v for k, v in result.items() if k not in ("content", "fields")}

        def build(chars: int) -> dict[str, Any]:
            end = min(stop, offset + chars)
            envelope = ot._envelope(text[offset:end], citation, field_path, limit)
            if end - offset < total:
                envelope["truncated"] = True
            window = {"offset": offset, "chars": end - offset, "total_chars": total,
                      "next_offset": end if end < total else None, "sha256": digest,
                      "first_line": bisect_right(starts, offset),
                      "last_line": bisect_right(starts, max(offset, end - 1))}
            if page is not None:
                window.update(page=page, page_citation=citation)
            if line is not None:
                window["line_cut"] = end < stop
            if field_path and ot._FIELD_PATH.match(field_path):
                window["field_path"] = field_path
            out = dict(base, text_window=window)
            out["content" if key == "content" else "fields"] = (
                envelope if key == "content" else {field_path: envelope})
            out["completeness"] = _completeness(1, None, end < total or offset > 0,
                                                "text_window", None)
            return out

        chars = min(arguments.get("max_chars", DEFAULT_READ_CHARS), limit)
        out = build(chars)
        while chars > 1 and not self._fits(out):
            chars = max(1, chars // 2)
            out = build(chars)
        return out

    def _columns(self, result: dict[str, Any], raw: Mapping[str, Any],
                 start: int) -> dict[str, Any]:
        values = raw.get("fields") or {}
        paths = list(values)
        total = len(paths)
        if not paths:
            return _error("invalid_input", "column_offset pages the fields of a row citation")
        if start > total:
            return _error("invalid_input", f"column_offset beyond the row ({total} fields)")
        origin = raw.get("values_citation") or result["citation"]
        document = bool(raw.get("document"))
        limit = self.limits["max_untrusted_text_chars"]
        base = {k: v for k, v in result.items()
                if k not in ("content", "fields", "truncated_fields")}

        def build(count: int) -> dict[str, Any]:
            window = paths[start:start + count]
            end = start + len(window)
            fields = {path: field_value(values[path], origin, path, document, limit)
                      for path in window}
            out = dict(base, fields=fields,
                       columns={"offset": start, "returned": len(window), "total": total,
                                "next_offset": end if end < total else None})
            cut = self._truncated(fields)
            if cut:
                out["truncated_fields"] = cut
            out["completeness"] = _completeness(len(fields), total,
                                                start > 0 or end < total, "column_page",
                                                None)
            return out

        low, high = 0, min(400, total - start)  # the largest count that fits
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if self._fits(build(middle)) else (low, middle - 1)
        return build(max(low, 1) if start < total else 0)




_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?$")
_ROW_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Bounds of the identifier lookup plx.documents reads captured SharePoint text through
# (CaptureCaseHostTools._identifier_lookup): the current captured files it reads (the
# store's own read-back bound) and the (identifier, file, page) entries it holds. Past
# either the lookup is cut and an answer from it says so (scan_cap).
LOOKUP_FILES = 100_000
LOOKUP_ENTRIES = 200_000
# Graph GETs one live re-read may send: the item's metadata plus at most three pages of
# its permissions (integrations.graph_read.SharePointReader._permissions).
REREAD_GETS = 4
# Graph GETs one live read of a citation the capture does not hold may send: the record
# read, the current version's read when it changed, and the digest read
# (RuntimeCitations._read_live), each one re-read.
LIVE_READ_GETS = 3 * REREAD_GETS
# Default per-call bounds on live re-reads and on the Graph GETs they send.
MAX_LIVE_REREADS = 50
MAX_LIVE_GETS = 200
# Room plx.investigate's pages leave for the capture labels (``_label``): the summary
# label (its detail capped at 600 characters) and the SharePoint freshness entry, each
# counted twice in the envelope, always fit within it.
LABEL_RESERVE_BYTES = 2048


class CapturedCitations(RuntimeCitations):
    """``RuntimeCitations`` that reads a ``sharepoint://`` citation from the SharePoint
    capture (``CaptureCaseHostTools.captured_read``): the captured file, or the refusal
    of a captured file the host's gate refuses (never read live in its place). A
    SharePoint file the capture does not hold for this reader is read live within the
    host's per-call bounds (``CaptureCaseHostTools.may_read_live``); anything else reads
    as before, except that a read a request refusal of this call's bound cut off is a
    ``live_read_budget`` refusal (nothing of it served)."""

    def __init__(self, runtime: Any, host: "CaptureCaseHostTools", **options: Any) -> None:
        super().__init__(runtime, **options)
        self.host = host

    def read(self, handle: str, actor: Any, *, page: int | None = None) -> dict[str, Any]:
        if str(handle).startswith("sharepoint://"):
            paged = handle if page is None else _with_page(handle, page)
            captured = None if paged is None else self.host.captured_read(paged, actor)
            if captured is not None:
                return captured
            if page is None and not self.host.may_read_live(handle):
                return self._budget_refusal(handle, "the file was not read")
        with self.host.metered() as budget:
            refused = budget.refused if budget is not None else 0
            result = super().read(handle, actor, page=page)
        if budget is not None and budget.refused > refused:
            # A request inside the read hit the bound: whatever it read is partial.
            return self._budget_refusal(handle, "the read was cut off and nothing of it "
                                        "is served")
        return result

    def _budget_refusal(self, handle: str, what: str) -> dict[str, Any]:
        source = str(handle).split("://", 1)[0][:40]
        return self._out("unresolvable", "live_read_budget", gaps=[_gap(
            "unknown", "live_read_budget", source,
            f"this call's live read bound is reached; {what}; ask again in a new call")])

    def field_text(self, handle: str, field_path: str, actor: Any) -> str | None:
        if str(handle).startswith("sharepoint://") and field_path == "row.content_text":
            view = self.host.captured(actor, handle, text=True)
            text = None if view is None else view["row"].get("content_text")
            return text if isinstance(text, str) else None
        return super().field_text(handle, field_path, actor)


def _member_container(handle: str) -> str | None:
    """The container handle of a container member citation (with or without a page;
    ``integrations.captured_text.member_handle``), else None."""
    from integrations.captured_text import split_member
    from ontology.mentions import cited_page

    member = split_member(cited_page(handle)[0])
    return None if member is None else member[0]


def _with_page(handle: str, page: int) -> str | None:
    """``handle`` narrowed to one page (``#page=<n>``), or None when it already names
    another page."""
    from integrations.live_source_guard import parse_handle
    try:
        _parts, _version, extra = parse_handle(handle, "sharepoint", 2)
    except ValueError:
        return None
    if "page" in extra:
        return handle if extra["page"] == str(page) else None
    return f"{handle}{'&' if '#' in handle else '#'}page={page}"


def _with_mentions(document: Mapping[str, Any], mentions: list[dict[str, Any]]
                   ) -> dict[str, Any] | None:
    """``document`` with the captured-text ``mentions`` it does not already cite (same
    identifier at the same cited page), after its own, within ``MAX_MENTIONS``; None
    when none is new."""
    def key(mention: Mapping[str, Any]) -> tuple[Any, ...]:
        origin = mention["span"]["origin"]
        return (mention["identifier_kind"], mention["normalized"], origin["citation"],
                origin.get("field_path"))

    have = list(document.get("mentions") or ())
    known = {key(m) for m in have}
    new = [m for m in mentions if key(m) not in known]
    if not new:
        return None
    merged = [*have, *new][:MAX_MENTIONS]
    return dict(document, mentions=merged,
                snippet=document.get("snippet") or merged[0]["span"])


class CaptureCaseHostTools(CaseHostTools):
    """``CaseHostTools`` over the versioned mail and SharePoint captures the host holds
    (module docstring). Without a capture store a unit of its family is granted to
    nobody and its handles read as before (the runtime's own gate)."""

    def __init__(self, runtime: Any, actor: Any, *, mail_capture_dir: str | Path | None = None,
                 sharepoint_capture_dir: str | Path | None = None,
                 max_live_rereads: int = MAX_LIVE_REREADS,
                 max_live_gets: int = MAX_LIVE_GETS,
                 max_capture_age: timedelta = MAX_CAPTURE_AGE,
                 clock: Callable[[], datetime] | None = None, **options: Any) -> None:
        super().__init__(runtime, actor, mail_capture_dir=mail_capture_dir, **options)
        self.sharepoint_capture_dir = (None if sharepoint_capture_dir is None
                                       else Path(sharepoint_capture_dir))
        self.max_live_rereads, self.max_live_gets = max_live_rereads, max_live_gets
        self.max_capture_age = max_capture_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if self.sharepoint_capture_dir is not None:
            self.label_reserve = LABEL_RESERVE_BYTES
        self._sharepoint_store: Any = None
        self._in_call = False
        # Per call: what the SharePoint gate decided for each version handle it admitted
        # (capture time, permission basis, newer version at the source, re-read failure,
        # text and content status), the live re-reads made and their answers, the
        # drives whose source failed, and the call's live request budget.
        self._served: dict[str, dict[str, Any]] = {}
        self._refused: dict[str, tuple[str, str, str]] = {}
        self._over_budget: set[str] = set()   # refused: the re-read bound was reached
        self._live: dict[tuple, Any] = {}
        self._down: dict[str, str] = {}
        self._rereads = 0
        self._budget: Any = None
        self._lookup: _IdentifierLookup | None = None

    def close(self) -> None:
        if self._sharepoint_store is not None:
            self._sharepoint_store.close()
        self._sharepoint_store = None
        self._lookup = None
        super().close()

    def _new_call(self) -> None:
        from integrations.live_source_guard import GuardedTransport, RequestBudget
        self._served, self._refused, self._live, self._down = {}, {}, {}, {}
        self._over_budget = set()
        self._rereads = 0
        live = getattr(self.runtime, "live", None)
        transports = [t for t in (getattr(live, "graph_transport", None),
                                  getattr(live, "bc_transport", None))
                      if isinstance(t, GuardedTransport)]
        self._budget = RequestBudget(transports, self.max_live_gets) if transports else None

    @contextmanager
    def per_call(self, *, whole_block: bool = True) -> Iterator[Any]:
        """One tool call: fresh per-call state, and this call's request budget enforced
        on every live request inside the block. A call inside a call shares the outer
        call's state and budget. ``whole_block=False`` (``CaseCaptures.call``): the
        budget is enforced on this gate's own live reads only, so a case tool's other
        live reads are not refused by it, though every request of the block still
        counts against it."""
        if self._in_call:
            with self.metered() as budget:
                yield budget
            return
        self._new_call()
        self._in_call = True
        try:
            with self.metered() if whole_block else nullcontext() as budget:
                yield budget
        finally:
            self._in_call = False

    @contextmanager
    def metered(self) -> Iterator[Any]:
        """This call's request budget, enforced on the host's Graph and BC transports
        inside the block (None, and nothing enforced, when no transport is known)."""
        if self._budget is None:
            yield None
            return
        with self._budget.active() as budget:
            yield budget

    def _audience(self) -> Any:
        return getattr(getattr(self.runtime, "live", None), "audience", None)

    def _mail(self) -> tuple[Any, Any] | None:
        from integrations.capture_mail import MailCaptureStore, store_exists
        audience = self._audience()
        if self.mail_capture_dir is None or audience is None \
                or not store_exists(Path(self.mail_capture_dir)):
            return None
        return MailCaptureStore(Path(self.mail_capture_dir)), audience

    def _sharepoint(self) -> Any:
        """The SharePoint capture store, opened read-only once it exists."""
        from integrations.capture_sharepoint import SharePointCaptureStore, store_exists
        if self._sharepoint_store is None and self.sharepoint_capture_dir is not None \
                and store_exists(self.sharepoint_capture_dir):
            self._sharepoint_store = SharePointCaptureStore.open_read_only(
                self.sharepoint_capture_dir)
        return self._sharepoint_store

    def policy_now(self, actor: Any) -> ReadPolicy:
        policy = super().policy_now(actor)
        grants = dict(policy.audiences)
        principals = frozenset(actor.principals)
        mail_units = [key for key in self._reader_keys if str(key).startswith("mail:")]
        opened = self._mail() if mail_units else None
        if opened is not None:
            store, audience = opened
            try:
                check = store.audience_check(audience)
                for unit in mail_units:
                    grants[unit] = principals if check(actor, unit.partition(":")[2]) \
                        else frozenset()
            finally:
                store.close()
        sharepoint_units = [key for key in self._reader_keys
                            if str(key).startswith("sharepoint:")]
        store = self._sharepoint() if sharepoint_units else None
        audience = self._audience()
        if store is not None and audience is not None:
            check = store.audience_check(audience)
            for unit in sharepoint_units:
                grants[unit] = principals if check(actor, unit.partition(":")[2]) \
                    else frozenset()
        return ReadPolicy(policy.tenant, grants, policy.markings)

    @property
    def tools(self) -> ot.OntologyTools:
        built = self._tools is None
        tools = super().tools
        if built:
            tools.item_gate = self._item_gate(tools.item_gate)
        return tools

    @property
    def evidence(self) -> Any:
        """``EvidenceTools`` as ``CaseHostTools`` builds them, reading through
        ``CapturedCitations``."""
        if self._evidence is None:
            evidence = super().evidence
            evidence.reader = CapturedCitations(
                self.runtime, self, mail_capture_dir=self.mail_capture_dir,
                store_lineage=evidence.reader.store_lineage, bc_books=self.bc_books)
        return self._evidence

    def _item_gate(self, runtime_gate: Callable[[Any, str], bool] | None
                   ) -> Callable[[Any, str], bool]:
        citations = RuntimeCitations(self.runtime, mail_capture_dir=self.mail_capture_dir)

        def fallback(actor: Any, handle: str) -> bool:
            return runtime_gate is not None and runtime_gate(actor, handle) is True

        def gate(actor: Any, handle: str) -> bool:
            handle = str(handle)
            if handle.startswith("sharepoint://") and self.sharepoint_capture_dir is not None:
                return self.captured(actor, handle) is not None
            if not handle.startswith("graph-mail://") or self.mail_capture_dir is None:
                return fallback(actor, handle)
            # A member of a captured zip/.msg/.eml attachment is visible iff its
            # attachment is: every check below decides on the attachment.
            handle = _member_container(handle) or handle
            opened = self._mail()
            if opened is None:
                return False
            store, audience = opened
            try:
                gated = self.runtime.employee_gate(actor)
                if not store.item_gate(audience)(gated, handle):
                    return False
            except Exception:  # noqa: BLE001 - a refused gate reads nothing
                return False
            finally:
                store.close()
            return citations.read(handle, actor).get("status") in _READ_OK
        return gate

    # ---- SharePoint: the per-item gate ----------------------------------------

    def captured(self, actor: Any, handle: str, *, text: bool = False
                 ) -> dict[str, Any] | None:
        """The captured version ``handle`` names (``SharePointCaptureStore.resolve``
        shape) when this reader may be served it now, else None (module docstring).
        A refusal of a version the capture holds (or of the reader) is recorded for this
        call in ``_refused`` as (status, reason, detail); a version the capture does not
        hold for this reader is not."""
        from integrations.capture_sharepoint import CaptureError
        store, audience = self._sharepoint(), self._audience()
        if store is None or audience is None:
            return None
        handle = str(handle)
        self._refused.pop(handle, None)
        container = _member_container(handle)
        if container is not None:
            return self._captured_member(actor, handle, container, text)
        item_refused = ("withheld", "sharepoint_item_refused",
                        "the Workbench's SharePoint capture holds no version of this file "
                        "this reader may be served now (not visible, deleted, moved or gone "
                        "from the capture's scope); it is not read live in its place")
        try:
            gated = self.runtime.employee_gate(actor)
        except Exception:  # noqa: BLE001 - a refused employee gate reads nothing
            self._refused[handle] = ("withheld", "employee_gate",
                                     "the employee gate refused this reader")
            return None
        try:
            view = store.resolve(handle, gated, audience, text=text)
            if view is None:
                return None   # not captured, or not visible to this reader at capture
            # The employee's item ACL decides on this version and on the item's latest
            # captured one: a revocation captured since hides the older versions too,
            # also where no live re-read can confirm it (no reader, budget, failure).
            if view["removed_reason"] is not None or any(
                    self.runtime._employee_acl_denial(gated, "sharepoint",
                                                      {**view["row"], "source_acl": acl})
                    for acl in (view["source_acl"], view["item_source_acl"])):
                self._refused[handle] = item_refused
                return None
            if self._projected_stale(view):
                # Checked after the reader's permissions: a reader who may not see the
                # file learns nothing from this reason.
                self._refused[handle] = (
                    "unresolvable", "capture_stale",
                    "the Workbench's index shows this file under a name or path the "
                    "capture no longer holds (moved since the index was built); it is "
                    "served again once the index is rebuilt, and it is not read live in "
                    "its place")
                return None
            decided = self._live_check(gated, view)
        except (CaptureError, sqlite3.Error, OSError, ValueError, LookupError):
            self._refused[handle] = ("unresolvable", "capture_unreadable",
                                     "the Workbench's SharePoint capture could not be read "
                                     "for this file now; it is not read live in its place")
            return None
        if decided is None:
            self._refused[handle] = item_refused
            return None
        basis, changed, failure = decided
        if basis != "live":
            if not capture_decides(view["last_seen_at"], self.clock(),
                                   self.max_capture_age):
                # Capture-time permissions too old (or undated, or dated after now) to
                # decide alone.
                why = ("this call's live re-read bound is reached" if failure == "budget"
                       else f"the live re-read failed now ({failure.partition(':')[2][:80]})"
                       if failure else "this host has no live reader for the drive")
                if failure == "budget":
                    self._over_budget.add(handle)
                self._refused[handle] = (
                    "unresolvable", "capture_too_old",
                    f"the captured file is outside the host's capture age bound and "
                    f"{why}, so its recorded permissions cannot decide; it is not read "
                    f"live in its place")
                return None
        row = view["row"]
        self._served[view["l1_handle"]] = {
            "captured_at": view["last_seen_at"], "basis": basis, "changed": changed,
            "failure": failure, "text_status": row.get("text_status"),
            "content_status": row.get("content_status"), "size": row.get("size")}
        return view

    def _captured_member(self, actor: Any, handle: str, container: str, text: bool
                         ) -> dict[str, Any] | None:
        """A member of a captured zip/.msg/.eml file (``integrations.captured_text``):
        served iff its container is served to this reader now (:meth:`captured` on the
        container: item ACL and file class, staleness, the live re-read and the capture
        age bound all decide on the container's view), then read from the container's
        pinned bytes. A refusal of the container is the member's."""
        from integrations.capture_sharepoint import CaptureError
        held = self.captured(actor, container)
        if held is None:
            if container in self._refused:
                self._refused[handle] = self._refused[container]
            return None
        try:
            view = self._sharepoint().resolve(handle, self.runtime.employee_gate(actor),
                                              self._audience(), text=text)
        except (CaptureError, sqlite3.Error, OSError, ValueError, LookupError):
            self._refused[handle] = ("unresolvable", "capture_unreadable",
                                     "the Workbench's SharePoint capture could not be read "
                                     "for this file now; it is not read live in its place")
            return None
        if view is not None:
            self._served[view["l1_handle"]] = self._served[held["l1_handle"]]
        return view

    def refusals(self) -> dict[str, str]:
        """The last tool call's refusals of captured versions the capture holds: version
        handle -> reason (``sharepoint_item_refused``, ``capture_too_old``, ...). For an
        in-process diagnosis of a reader's own answers (``operating.sharepoint_host_check``)
        and for ``CaseCaptures`` (a refused version is unresolvable, one the capture does
        not hold is read live); never part of a tool result."""
        return {handle: reason for handle, (_status, reason, _detail) in self._refused.items()}

    def _projected_stale(self, view: Mapping[str, Any]) -> bool:
        """Whether the projection shows this version under a name or path the capture
        no longer holds (moved since the projection was built): not served until the
        projection is rebuilt."""
        if self.store_path is None:
            return False
        held = {"row.name": view["row"].get("name"), "row.path": view["row"].get("path")}
        store = self.tools.store   # the generation this request reads
        with store.lock:
            rows = store.conn.execute(
                "SELECT field_path, value FROM object_values WHERE l1_handle = ? AND "
                "field_path IN ('row.name', 'row.path')", (view["l1_handle"],)).fetchall()
        for path, value in rows:
            if value is not None and value != held[path]:
                return True
        return False

    def _live_check(self, gated: Any, view: Mapping[str, Any]
                    ) -> tuple[str, bool, str | None] | None:
        """(permission basis, newer version at the source, why the capture decides), or
        None when a live re-read refuses the item (not visible to this reader, deleted,
        or moved since the capture). ``capture`` when the host has no live reader for
        the drive (why: None), this call's re-read bound is reached (``budget``) or the
        source failed transiently in this call (``failed:<detail>``)."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    RequestBudgetExhausted, SourceNotFound,
                                                    SourceUnavailable, unavailable_detail)
        drive = view["row"].get("drive_id")
        reader = (getattr(getattr(self.runtime, "live", None), "sharepoint", None)
                  or {}).get(drive)
        if reader is None:
            return ("capture", False, None)
        key = (frozenset(gated.principals), view["l1_handle"])
        if key in self._live:
            return self._live[key]
        if drive in self._down:
            return ("capture", False, "failed:" + self._down[drive])
        if not self._may_reread():
            return ("capture", False, "budget")
        self._rereads += 1
        with self.metered() as budget:
            refused = budget.refused if budget is not None else 0
            try:
                resolved = reader.resolve_citation(view["l1_handle"], gated)
            except (NotVisible, SourceNotFound, ReadOnlyGuardError, ValueError, LookupError):
                decided = None   # refused, deleted or malformed: nothing is served
            except RequestBudgetExhausted:
                decided = ("capture", False, "budget")
            except SourceUnavailable as exc:
                if getattr(exc, "http_status", None) == 410:
                    decided = None   # Gone: no longer exists at that version, not transient
                else:
                    self._down[drive] = unavailable_detail(exc) or "unclassified"
                    decided = ("capture", False, "failed:" + self._down[drive])
            else:
                record = resolved.get("record") if isinstance(resolved, Mapping) else None
                if (not isinstance(record, Mapping)
                        or resolved.get("status") not in ("current", "changed_since")
                        or self.runtime._employee_acl_denial(gated, "sharepoint", record)
                        or record.get("path") != view["row"].get("path")):
                    decided = None   # denied for this employee, or moved since the capture
                else:
                    decided = ("live", resolved["status"] != "current", None)
        if budget is not None and budget.refused > refused:
            # A request inside the re-read hit the bound (even one its reader absorbed):
            # whatever it read, e.g. part of the permissions, decides nothing.
            decided = ("capture", False, "budget")
        self._live[key] = decided
        return decided

    def _may_reread(self, gets: int = REREAD_GETS) -> bool:
        """Whether this call may start one more live read: under ``max_live_rereads``
        and, where the host's transports are known, with room for ``gets`` requests in
        this call's budget (every live request of the call counts). The budget itself
        refuses any request past it (``per_call``)."""
        if self._rereads >= self.max_live_rereads:
            return False
        return self._budget is None or self._budget.used + gets <= self._budget.limit

    def may_read_live(self, handle: str) -> bool:
        """Whether a ``sharepoint://`` citation the capture does not hold may be read
        live in this call; a live read counts as a re-read against the same bounds (none
        is counted where the host has no live reader for the drive: nothing is sent)."""
        from integrations.live_source_guard import parse_handle
        try:
            (drive, _item), _version, _extra = parse_handle(handle, "sharepoint", 2)
        except ValueError:
            return True   # refused as invalid by the reader, before any request
        if drive not in (getattr(getattr(self.runtime, "live", None), "sharepoint", None)
                         or {}):
            return True
        if not self._may_reread(LIVE_READ_GETS):
            return False
        self._rereads += 1
        return True

    # ---- SharePoint: labels -----------------------------------------------------

    def _labels(self, handle: str) -> list[dict[str, Any]]:
        """The gaps that label one served captured version (module docstring)."""
        from integrations.capture_sharepoint import (CONTENT_SKIPPED, EXCEEDS_CAP, OCR_FAILED,
                                                     OCR_UNAVAILABLE, TEXT_OCR)
        from integrations.captured_text import TEXT_EXCEEDS_BOUND, text_status_kind
        info = self._served.get(handle)
        if info is None:
            return []
        failure = info["failure"] or ""
        if info["basis"] == "live":
            basis = "its permissions were re-read live now"
        else:
            basis = "its permissions are as recorded at that capture" + (
                " (this call's live re-read bound is reached)" if failure == "budget"
                else " (the live re-read failed now)" if failure.startswith("failed:")
                else "")
        newer = "; the live re-read found a newer version at the source" \
            if info["changed"] else ""
        out = [_gap("stale" if info["changed"] else "unknown", "as_of_capture", handle,
                    f"as of capture {info['captured_at']}: served from the Workbench's "
                    f"SharePoint capture, not read live now; {basis}{newer}")]
        if failure.startswith("failed:"):
            out.append(_gap("unknown", "live_reread_failed", handle,
                            f"the live SharePoint re-read failed now "
                            f"({failure.partition(':')[2][:80]}); the captured file is "
                            "served as captured, within the capture age bound, not "
                            "confirmed against the source"))
        content = str(info["content_status"] or "")
        if content == EXCEEDS_CAP:
            size = info["size"] if isinstance(info["size"], int) else "unknown"
            out.append(_gap("not_ingested", "content_exceeds_cap", handle,
                            f"the file ({size} bytes) is over the capture's content cap: "
                            "its metadata, permissions and version are captured, its "
                            "content was skipped, so nothing in it is searched or shown"))
        elif content == CONTENT_SKIPPED:
            out.append(_gap("not_ingested", "content_not_read", handle,
                            "a metadata-only capture recorded this file's metadata, "
                            "permissions and version but never read its bytes; nothing in "
                            "it is searched or shown"))
        elif content.startswith("unavailable:"):
            out.append(_gap("not_ingested", "content_unavailable", handle,
                            "the capture could not read this file's bytes (retried by "
                            "later captures); nothing in it is searched or shown"))
        status = text_status_kind(str(info["text_status"]))
        if status == TEXT_OCR:
            out.append(_gap("unknown", "ocr_text_unconfirmed", handle,
                            "machine-read OCR text: what the scan appears to say, for a "
                            "person to confirm, never a verified value"))
        elif status in (OCR_UNAVAILABLE, OCR_FAILED):
            out.append(_gap("not_ingested", status, handle,
                            "scanned pages of this PDF were not OCR-read on the capturing "
                            "host; identifiers on them are neither searched nor shown"))
        elif status == TEXT_EXCEEDS_BOUND:
            out.append(_gap("not_ingested", status, handle,
                            "the file's bytes are captured but over the text extractor's "
                            "bound, so its own text was never read whole and is neither "
                            "searched nor shown (a zip, .msg or .eml is read member by "
                            "member)"))
        return out

    def _label(self, name: str, result: dict[str, Any], actor: Any) -> dict[str, Any]:
        """``result`` with an ``as_of_capture`` label for every captured SharePoint
        version it serves, within the tool's gap limit and its size bound (past either,
        one summary gap for them all); a result that cannot carry them is an error, never
        unlabelled. A plx.investigate page leaves ``LABEL_RESERVE_BYTES`` free for this."""
        served: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str) and node.startswith("sharepoint://"):
                served.add(node.split("#", 1)[0])

        walk(result)
        for handle in sorted(served - set(self._served)):
            self.captured(actor, handle)   # served by another path in this call
        handles = sorted(h for h in served if h in self._served)
        # Admitted to the computation (counts, totals, filters) but not listed.
        counted = sorted(set(self._served) - served)
        if not handles and not counted:
            return result
        admitted = sorted(self._served[h]["captured_at"] for h in (*handles, *counted))
        output = self.tools.contracts["tools"][name]["output"]
        shape = (output["oneOf"][0] if "oneOf" in output else output).get("properties", {})
        out = dict(result)
        meta = out.get("meta")
        if isinstance(meta, dict):
            freshness = list(meta.get("freshness") or [])
            if len(freshness) < 8 and not any(f.get("source") == "sharepoint"
                                              for f in freshness):
                freshness.append({"source": "sharepoint", "completeness": "unknown",
                                  "stale": False, "captured_at": admitted[0]})
                out["meta"] = dict(meta, freshness=freshness)
        if "gaps" not in shape:
            return self._bounded(name, out)
        cap = shape["gaps"].get("maxItems", 20)
        existing = list(out.get("gaps") or [])
        labels = [gap for handle in handles for gap in self._labels(handle)]
        if counted:
            times = sorted(self._served[h]["captured_at"] for h in counted)
            labels.append(_gap("unknown", "as_of_capture", "sharepoint",
                               f"{len(counted)} further SharePoint files from the "
                               f"Workbench's capture as of {times[0]} to {times[-1]} "
                               "entered this answer's counts, totals or filters without "
                               "being listed; they were not read live now"))
        failed = sum(1 for h in (*handles, *counted)
                     if str(self._served[h]["failure"] or "").startswith("failed:"))
        summary = _gap("unknown", "as_of_capture", "sharepoint",
                       f"{len(handles) + len(counted)} SharePoint files served or "
                       f"counted from the Workbench's capture as of {admitted[0]} to "
                       f"{admitted[-1]}, not read live now"
                       + (f"; the live re-read failed for {failed}" if failed else "")
                       + "; per-file labels beyond the gap limit or the size bound are "
                       "not listed")
        room = cap - len(existing)
        if len(labels) > room:
            labels = labels[:max(room - 1, 0)] + [summary]
            existing = existing[:cap - len(labels)]
        out["gaps"] = existing + labels
        if not self._fits(out):
            out["gaps"] = existing[:cap - 1] + [summary]
        return self._bounded(name, out)

    def _fits(self, result: Mapping[str, Any]) -> bool:
        return ot._envelope_bytes(result) <= self.tools.contracts["limits"]["max_result_bytes"]

    def _bounded(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        contracts = self.tools.contracts
        problems = validate(result, contracts["tools"][name]["output"], root=contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if not self._fits(result):
            return _error("result_too_large", "the answer and its capture labels do not "
                          "fit one response", "narrow the question or lower limit")
        return result

    # ---- SharePoint: reads and document search -----------------------------------

    def captured_read(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """``plx.read`` of a captured SharePoint version (``CitationReader.read`` shape):
        the file, or the refusal (with its reason) of a captured version the gate
        refuses; None when the capture does not hold it for this reader. Inside a tool
        call it shares that call's re-read bound."""
        if not self._in_call:
            self._new_call()
        view = self.captured(actor, handle, text=True)
        if view is None:
            return self._refusal(str(handle)) or self._page_not_held(str(handle), actor)
        row = dict(view["row"])
        text = row.pop("content_text", None)
        fields = {f"row.{name}": value for name, value in row.items()
                  if _ROW_FIELD.match(str(name))}
        out: dict[str, Any] = {
            "status": "resolved", "reason": "ok", "lineage": "sharepoint",
            "digest": view["digest"], "fields": fields, "document": True,
            "values_citation": handle, "content_citation": handle,
            "content_field": "row.content_text", "gaps": self._labels(view["l1_handle"])}
        if isinstance(text, str):
            out["content"] = text
            skipped = extraction_skipped(row)
            if skipped is not None and "page" not in view:
                out["gaps"].append(skipped_gap(handle, skipped))
        if view["superseded_at"] is not None:
            out["gaps"].append(_gap("stale", "superseded_in_capture", handle[:700],
                                    "the capture no longer holds this version as current"))
        return out

    def _refusal(self, handle: str) -> dict[str, Any] | None:
        """The ``plx.read`` refusal of a captured version ``captured`` refused in this
        call, else None."""
        refused = self._refused.get(handle)
        if refused is None:
            return None
        status, reason, detail = refused
        return {"status": status, "reason": reason,
                "gaps": [_gap("inaccessible" if status == "withheld" else "stale"
                              if reason == "capture_too_old" else "unknown",
                              reason, "sharepoint", detail)]}

    def _page_not_held(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """``plx.read`` of a page the text of a captured file this reader may be served
        (its file is served now) does not hold: past the page cap ``unresolvable`` with
        the ``skipped_by_cap`` gap, never as if the page did not exist; past the last page
        of a file whose text was read (its page count known) ``page_not_found``; else
        None (no text read: whether the page exists cannot be told). A cap whose range is
        not recorded whole takes every page not read for one past it (fail closed). The
        file's refusal is the page's: a refused reader learns nothing of its pages."""
        base, page = mn.cited_page(handle)
        view = None if page is None else self.captured(actor, base, text=True)
        if view is None:
            return None if page is None else self._refusal(base)
        skipped = extraction_skipped(view["row"])
        if skipped is not None and ("first_page" not in skipped
                                    or skipped["first_page"] <= page <= skipped["last_page"]):
            return {"status": "unresolvable", "reason": "skipped_by_cap",
                    "gaps": [*self._labels(view["l1_handle"]), skipped_gap(handle, skipped)]}
        if isinstance(view["row"].get("content_text"), str):
            return {"status": "unresolvable", "reason": "page_not_found", "gaps": []}
        return None

    def call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        with self.request(), self.per_call():
            result = super().call(name, arguments, actor)
            if name in self.registered or not isinstance(result, dict) or "error" in result \
                    or name not in self.tools.contracts["tools"] or name == "plx.read" \
                    or self.sharepoint_capture_dir is None:
                return result   # plx.read: its reader labels a captured file itself
            if name == "plx.documents" and isinstance(arguments, dict):
                result = self._captured_documents(arguments, actor, result)
            return self._label(name, result, actor)

    def _captured_documents(self, arguments: Mapping[str, Any], actor: Any,
                            result: dict[str, Any]) -> dict[str, Any]:
        """Captured SharePoint files whose text names an identifier of the query, for
        this reader, added to the first page of ``plx.documents`` (each mention cited by
        page, a lead and an external assertion with no object: nothing resolves it).
        A file the projection already lists (its name or a field names the identifier)
        gets the page-cited mentions its captured text holds that it lacks: no projected
        object takes them, so it would otherwise come back with no page cited.
        The text is matched offline, through the reader's identifier lookup
        (:meth:`_identifier_lookup`); only a matching file (re-read on its matching pages
        alone) or one over the content
        cap or the text extractor's bound, reported as unsearched, or a PDF read on its
        first pages only that does not match, reported with the pages not searched:
        ``skipped_by_cap``) goes through the per-item gate and its re-read, the matching
        files first: the unsearched ones share what is left of the call's re-read bound,
        and a file the gate drops because that bound is reached is counted in one
        ``live_read_budget`` gap, never dropped silently. A lookup cut at its bounds
        makes the answer ``scan_cap``."""
        from integrations.capture_sharepoint import CaptureError
        from integrations.captured_text import TEXT_EXCEEDS_BOUND
        query = arguments.get("query")
        if not isinstance(query, str) or arguments.get("cursor") or arguments.get("since") \
                or arguments.get("until") or arguments.get("object_id") is not None \
                or "sharepoint" not in (arguments.get("sources") or ("sharepoint",)):
            return result
        wanted = {(m["identifier_kind"], m["normalized"]) for m in mn.extract(query)
                  if m["normalized"]}
        store, audience = self._sharepoint(), self._audience()
        if not wanted or store is None or audience is None:
            return result
        try:
            gated = self.runtime.employee_gate(actor)
        except Exception:  # noqa: BLE001 - a refused employee gate reads nothing
            return result
        lookup = self._identifier_lookup(store, gated, audience)
        documents = list(result["documents"])
        listed = {doc["citation"]: at for at, doc in enumerate(documents)}
        pages: dict[int, set[int | None]] = {}
        for key in wanted:
            for at, page in lookup.entries.get(key, ()):
                pages.setdefault(at, set()).add(page)
        matching: list[tuple[str, dict[str, Any]]] = []
        for at in sorted(pages):
            handle = lookup.handles[at]
            try:
                view = store.resolve(handle, gated, audience)
            except (CaptureError, sqlite3.Error, OSError, ValueError):
                continue
            if view is None or not isinstance(view["row"].get("content_text"), str):
                continue
            document = self._mentioning(view, wanted, pages[at])
            if document is not None:
                matching.append((handle, document))
        # No match on the pages read is never absence from the pages not read.
        unsearched = [(lookup.handles[at], why) for at, why in lookup.unsearched
                      if at not in pages and lookup.handles[at] not in listed]
        found, capped, over_cap, over_bound, over_budget, cited = [], [], 0, 0, 0, 0
        not_read = 0
        for handle, document in matching:
            if self.captured(actor, handle) is None:
                over_budget += handle in self._over_budget
                continue
            if handle in listed:
                merged = _with_mentions(documents[listed[handle]], document["mentions"])
                if merged is not None:
                    documents[listed[handle]], cited = merged, cited + 1
            else:
                found.append(document)
        for handle, why in unsearched:
            if self.captured(actor, handle) is None:
                over_budget += handle in self._over_budget
            elif why == "cap":
                over_cap += 1
            elif why == "bound":
                over_bound += 1
            elif why == "not_read":
                not_read += 1
            else:
                capped.append(skipped_gap(handle, why))
        room = arguments.get("limit", DEFAULT_LIMIT) - len(documents)
        if not found and not lookup.truncated and not over_cap and not over_bound \
                and not capped and not over_budget and not not_read:
            return dict(result, documents=documents) if cited else result
        out = dict(result, documents=[*documents, *found[:max(room, 0)]])
        gaps = [g for g in result["gaps"] if not (found and g["reason"] == "no_match")]
        searched = dict(result["searched"])
        searched["scopes"] = [*searched.get("scopes", []), "capture:sharepoint"][:16]
        cut = len(found) > room or lookup.truncated
        if cut:
            gaps.append(_gap("unknown", "scan_cap", "capture:sharepoint",
                             "the captured SharePoint files matching this query exceed what "
                             "one answer holds or the scan bound; narrow the query"))
        if over_cap:
            gaps.append(_gap("not_ingested", "content_exceeds_cap", "capture:sharepoint",
                             f"{over_cap} captured SharePoint file(s) you may see are over "
                             "the capture's content cap: their metadata is captured, their "
                             "text is not, so identifiers in them were not searched"))
        if not_read:
            gaps.append(_gap("not_ingested", "content_not_read", "capture:sharepoint",
                             f"{not_read} captured SharePoint file(s) you may see were "
                             "captured metadata-only: their bytes were never read, so "
                             "identifiers in them were not searched"))
        gaps.extend(capped_gaps(capped, "capture:sharepoint", "captured SharePoint PDF(s)"))
        if over_bound:
            gaps.append(_gap("not_ingested", TEXT_EXCEEDS_BOUND, "capture:sharepoint",
                             f"{over_bound} captured SharePoint file(s) you may see are over "
                             "the text extractor's bound: their bytes are captured, their "
                             "text was never read whole, so identifiers in them were not "
                             "searched"))
        if over_budget:
            gaps.append(_gap("unknown", "live_read_budget", "capture:sharepoint",
                             f"{over_budget} captured SharePoint file(s) were left out of "
                             "this answer unread for it: this call's live re-read bound was "
                             "reached before their permissions could be re-read, and their "
                             "capture is past the host's age bound; ask again in a new call"))
        completeness = dict(result["completeness"])
        completeness["returned"] = len(out["documents"])
        if completeness.get("total") is not None:
            completeness["total"] += len(found)
        if cut:
            completeness.update(complete=False, truncated=True, reason="scan_cap")
        elif (over_cap or over_bound or capped or over_budget or not_read) \
                and not completeness.get("truncated"):
            completeness.update(complete=False, reason="partial_scope")
        return dict(out, gaps=gaps[:20], searched=searched, completeness=completeness)

    def _identifier_lookup(self, store: Any, gated: Any, audience: Any
                           ) -> "_IdentifierLookup":
        """Every current captured file this reader may see, read once: per identifier
        its text names (the one grammar, ``mn.file_text_fields`` and ``mn.extract``, as
        :meth:`_mentioning` reads it) the (file, page) entries naming it, and the files
        whose text was not searched whole. Reused while the reader, the audience and
        the capture store are unchanged (SQLite ``data_version``: another connection's
        commit makes a new one); one is held at a time. Bounded by ``LOOKUP_FILES``
        files and ``LOOKUP_ENTRIES`` entries (a cut lookup is ``truncated``); its time
        is one read of each file's text, which each call no longer repeats."""
        from integrations.capture_sharepoint import CONTENT_SKIPPED, EXCEEDS_CAP, CaptureError
        from integrations.captured_text import TEXT_EXCEEDS_BOUND
        state = store.db.execute("PRAGMA data_version").fetchone()[0]
        held = self._lookup
        if held is not None and held.reader == gated and held.audience is audience \
                and held.state == state and held.store is store:
            return held
        lookup = _IdentifierLookup(store, gated, audience, state)
        index = store.index(gated, audience, limit=LOOKUP_FILES)
        lookup.truncated, held_entries = index["truncated"], 0
        for item in index["items"]:
            handle = item["l1_handle"]
            try:
                view = store.resolve(handle, gated, audience)
            except (CaptureError, sqlite3.Error, OSError, ValueError):
                continue
            if view is None:
                continue
            row, text = view["row"], view["row"].get("content_text")
            if not isinstance(text, str):
                why = ("cap" if row.get("content_status") == EXCEEDS_CAP else
                       "not_read" if row.get("content_status") == CONTENT_SKIPPED else
                       "bound" if row.get("text_status") == TEXT_EXCEEDS_BOUND else None)
                if why is not None:
                    lookup.unsearched.append((len(lookup.handles), why))
                    lookup.handles.append(handle)
                continue
            paged = mn.is_paged_file(row)
            named: dict[tuple[str, str, int | None], None] = {}   # in text order, once
            for field in mn.file_text_fields(handle, "row.content_text", text, paged=paged):
                page = mn.cited_page(field.citation)[1] if paged else None
                for found in mn.extract(field.text):
                    if found["normalized"]:
                        named[(found["identifier_kind"], found["normalized"], page)] = None
            if held_entries + len(named) > LOOKUP_ENTRIES:
                lookup.truncated = True   # a file is held whole or not at all
                break
            at, held_entries = len(lookup.handles), held_entries + len(named)
            lookup.handles.append(handle)
            for kind, normalized, page in named:
                lookup.entries.setdefault((kind, normalized), []).append((at, page))
            skipped = extraction_skipped(row)
            if skipped is not None:
                lookup.unsearched.append((at, skipped))
        self._lookup = lookup
        return lookup

    def _mentioning(self, view: Mapping[str, Any], wanted: set[tuple[str, str]],
                    pages: set[int | None] | None = None) -> dict[str, Any] | None:
        """One captured file as a ``plx.documents`` item when its text names one of
        ``wanted`` (the one identifier grammar, page by page for a PDF), read on
        ``pages`` alone when given (``None``: an unpaged file's one field)."""
        handle, row = view["l1_handle"], view["row"]
        paged = mn.is_paged_file(row)
        fields = mn.file_text_fields(handle, "row.content_text", row["content_text"],
                                     paged=paged)
        limit = self.tools.contracts["limits"]["max_untrusted_text_chars"]
        mentions = []
        for field in fields:
            if pages is not None \
                    and (mn.cited_page(field.citation)[1] if paged else None) not in pages:
                continue
            for found in mn.extract(field.text):
                if (found["identifier_kind"], found["normalized"]) not in wanted:
                    continue
                mentions.append({
                    "identifier_kind": found["identifier_kind"],
                    "normalized": str(found["normalized"])[:200],
                    "span": ot._envelope(field.text[found["start"]:found["end"]],
                                         field.citation, field.field_path, limit),
                    "tier": "lead", "external_assertion": True})
        if not mentions:
            return None
        name, date = row.get("name"), row.get("last_modified")
        return {"citation": handle, "kind": "sharepoint_file",
                "title": (ot._envelope(name, handle, "row.name", limit)
                          if isinstance(name, str) and name else None),
                "date": date if isinstance(date, str) and _DATE.match(date) else None,
                "snippet": mentions[0]["span"], "mentions": mentions[:MAX_MENTIONS]}


class _IdentifierLookup:
    """One reader's identifier lookup over the captured SharePoint files
    (``CaptureCaseHostTools._identifier_lookup``): ``handles`` in the store's order,
    ``entries`` (identifier kind, normalized) -> [(position in ``handles``, page or None)],
    ``unsearched`` [(position, "cap" | "not_read" | "bound" | the pages not read)]."""

    def __init__(self, store: Any, reader: Any, audience: Any, state: int) -> None:
        self.store, self.reader, self.audience, self.state = store, reader, audience, state
        self.handles: list[str] = []
        self.entries: dict[tuple[str, str], list[tuple[int, int | None]]] = {}
        self.unsearched: list[tuple[int, Any]] = []
        self.truncated = False


class PageNotRead(CitationUnresolvable):
    """A citation of a page of a captured file this reader may be served that its text
    reading never read: past the page cap (``skipped_by_cap``) or past its last page.
    Public, as ``case_runtime.LiveSourceNotConfigured`` is: raised only once the file
    itself is served to the reader, it tells nothing the file's own read does not."""

    code = PAGE_NOT_READ
    public = True

    def __init__(self) -> None:
        super().__init__(PAGE_NOT_READ)


class CaseCaptures:
    """The host's mail and SharePoint captures as the case runtime resolves a citation
    (``CaseRuntime.captures``), through the gates ``plx.read`` reads them with, so a
    case finding can cite a captured document the employee just read:

    * ``sharepoint://``: ``CaptureCaseHostTools.captured`` (the capture's path keys and
      item permissions, the employee's item ACL on the version and on the item's latest
      captured version, a live permission re-read where the host has a reader for the
      drive, the capture age bound) over the projection ``plx.read`` reads
      (``store_path``: a file moved since it was built is refused ``capture_stale``, as
      ``plx.read`` refuses it). Inside one case tool call (``call``) every citation goes
      through one gate: the call's re-reads share one ``max_live_rereads`` and one
      ``max_live_gets`` bound (every live request of the call counts against it) and an
      item is re-read once per reader. Outside a call a resolve gets a gate of its own,
      whose re-reads the budget of a tool call in progress bounds (``plx.read`` reading a
      cited version's current one);
    * ``graph-mail://``: ``RuntimeCitations.mail_capture`` (the mailbox identities
      recorded at capture, the live audience, the case mail scope, the employee's item
      ACL).

    ``__call__`` returns the content-stable view the case store digests (a mail view
    digests as ``plx.read`` reports it), None when the capture holds no version of the
    handle for this reader (the live reader then decides, as for ``plx.read``), and
    raises ``CitationUnresolvable`` for a captured version the gate refuses, never read
    live in its place, and ``PageNotRead`` (``page_not_read``) for a page of a served
    file its text reading never read (past the page cap or past its last page). A
    version the capture no longer holds as current is ``changed_since`` (never
    support), as a superseded mirror row is: a message whose version was superseded by
    a newer one (``current_citation``) or that has no current version at all (deleted,
    moved or gone from the source; ``current_citation`` None). A mail attachment is the
    exception, pinned to its bytes (a message's changeKey alone is not a content
    change, as for the live reader).

    A current whole-file SharePoint version cited by its pinned bytes
    (``sharepoint://...#sha256=``) also carries its ``row``: the ``documentFields`` row
    those bytes state (``ontology.document_fields.pinned_document_row``, the one path
    ``document`` and ``capability_guards.current_reader`` serve), so the claim gate
    re-reads a figure a case states from it (``row.fields.<name>.value``) in the bytes
    the reader is served now, inside the digest the case recorded; bytes that are not
    read or not classified now give no row (no claim holds, and a recorded digest no
    longer matches). A version, page or member cited otherwise carries none."""

    MAX_DOCUMENT_ROWS = 256   # pinned extractions kept per instance (bytes are pinned)

    def __init__(self, runtime: Any, *, mail_capture_dir: str | Path | None = None,
                 sharepoint_capture_dir: str | Path | None = None,
                 store_path: str | Path | None = None, **bounds: Any) -> None:
        self.runtime = runtime
        self.mail_capture_dir = None if mail_capture_dir is None else Path(mail_capture_dir)
        self.sharepoint_capture_dir = (None if sharepoint_capture_dir is None
                                       else Path(sharepoint_capture_dir))
        self.store_path = None if store_path is None else Path(store_path)
        self.bounds = bounds   # CaptureCaseHostTools' capture age and re-read bounds
        self._calls = threading.local()   # the case tool call in progress, per thread
        # Extractions of pinned bytes, read only after the reader's gate served them.
        self._rows: dict[str, dict[str, Any] | None] = {}
        self._rows_lock = threading.Lock()

    @contextmanager
    def call(self) -> Iterator[None]:
        """One case tool call: the SharePoint citations resolved inside it share one
        gate per reader (a call has one), built on first use and closed at the end. A
        call inside a call shares the outer one."""
        calls = self._calls
        if getattr(calls, "stack", None) is not None:
            yield
            return
        with ExitStack() as stack:
            calls.stack, calls.gates = stack, {}
            try:
                yield
            finally:
                calls.stack = calls.gates = None

    @contextmanager
    def _gate(self, actor: Any) -> Iterator["CaptureCaseHostTools"]:
        """The reader's SharePoint gate of the call in progress, else one for this
        resolve. Another reader never shares a gate's decisions."""
        calls = self._calls
        stack = getattr(calls, "stack", None)
        if stack is None:
            with ExitStack() as stack:
                yield self._new_gate(actor, stack)
            return
        gate = calls.gates.get(actor)
        if gate is None:
            gate = calls.gates[actor] = self._new_gate(actor, stack)
            stack.enter_context(gate.per_call(whole_block=False))
        yield gate

    def _new_gate(self, actor: Any, stack: ExitStack) -> "CaptureCaseHostTools":
        gate = CaptureCaseHostTools(self.runtime, actor, store_path=self.store_path,
                                    sharepoint_capture_dir=self.sharepoint_capture_dir,
                                    **self.bounds)
        stack.callback(gate.close)
        return gate

    def __call__(self, handle: str, actor: Any) -> dict[str, Any] | None:
        try:
            if handle.startswith("sharepoint://"):
                return self._sharepoint(handle, actor)
            if handle.startswith("graph-mail://"):
                return self._mail(handle, actor)
        except (sqlite3.Error, OSError, ValueError, LookupError):
            # An unreadable capture serves nothing, and is not read live in its place.
            raise CitationUnresolvable("capture_citation_unresolvable") from None
        return None

    def _sharepoint(self, handle: str, actor: Any) -> dict[str, Any] | None:
        if self.sharepoint_capture_dir is None:
            return None
        from integrations.captured_text import byte_pinned
        document = None
        with self._gate(actor) as gate:
            view = gate.captured(actor, handle)
            # A page is cited only as read: the same resolve, now with the page's text.
            unread = (view is not None and view.get("page") is not None
                      and gate.captured(actor, handle, text=True) is None)
            refused = handle in gate.refusals()
            if (not refused and view is not None and view["superseded_at"] is None
                    and view.get("page") is None
                    and view["relation"] == "sharepoint.driveItems"
                    and isinstance(view["row"].get("content_sha256"), str)
                    and byte_pinned(view["l1_handle"], view["row"]["content_sha256"]) == handle):
                # The same gate (one re-read per reader and call) serves the pinned bytes;
                # pinned bytes it cannot serve leave the citation unresolvable.
                try:
                    served = self._sharepoint_file(gate, actor, handle)
                except CitationUnresolvable:
                    refused = True
                    served = None
                document = None if served is None else self._document_row(handle, served)
        if refused:
            raise CitationUnresolvable("capture_citation_unresolvable")
        if unread:
            raise PageNotRead()
        if view is None:
            return None
        superseded = view["superseded_at"] is not None
        return {"citation": handle, "source": "sharepoint", "captured": True,
                "relation": view["relation"], "l1_handle": view["l1_handle"],
                "digest": view["digest"], "content_sha256": view["row"].get("content_sha256"),
                "page": view.get("page"), "recorded_at": view["recorded_at"],
                "status": "changed_since" if superseded else "current",
                "changed_since": superseded,
                # The row the pinned bytes state now is derived from them: the digest
                # binds the bytes (content_sha256), the claim gate re-reads the row.
                **({"row": document, "derived": ["row"]} if document is not None else {})}

    def _sharepoint_file(self, gate: "CaptureCaseHostTools", actor: Any, handle: str
                         ) -> dict[str, Any] | None:
        """The pinned bytes of a current whole-file SharePoint version through ``gate``
        (``document``'s shape); None when the capture holds no version of it for this
        reader; ``CitationUnresolvable`` when the gate refuses it, a later version
        superseded it, it is a page or no file, or its bytes cannot be read."""
        from integrations.captured_text import TEXT_OCR
        refused = CitationUnresolvable("capture_citation_unresolvable")
        view = gate.captured(actor, handle, text=True)
        if view is None:
            if handle in gate.refusals():
                raise refused
            return None
        if (view["superseded_at"] is not None or view.get("page") is not None
                or view["relation"] != "sharepoint.driveItems"):
            raise refused
        row = view["row"]
        try:
            content = gate._sharepoint().blob(row["content_sha256"])
        except (sqlite3.Error, OSError, ValueError, LookupError, TypeError):
            raise refused from None
        text = row.get("content_text") if row.get("text_status") == TEXT_OCR else None
        return {"l1_handle": view["l1_handle"], "row": row, "content": content, "text": text}

    def _document_row(self, handle: str, served: Mapping[str, Any]
                      ) -> dict[str, Any] | None:
        """The ``documentFields`` row the served pinned bytes state now
        (``pinned_document_row``), None when they state none. Kept per (handle, served
        row, text): the extraction of pinned bytes is deterministic, and the gate decided
        this reader before it is looked up."""
        from ontology.document_fields import pinned_document_row
        key = hashlib.sha256(json.dumps(
            [handle, served["l1_handle"], served["row"], served["text"]], sort_keys=True,
            default=str).encode("utf-8")).hexdigest()
        with self._rows_lock:
            if key in self._rows:
                return self._rows[key]
        found = pinned_document_row(handle, served["l1_handle"], served["content"],
                                    served["row"], served["text"])
        row = found["row"] if found.get("ok") else None
        with self._rows_lock:
            if len(self._rows) >= self.MAX_DOCUMENT_ROWS:
                self._rows.pop(next(iter(self._rows)))
            self._rows[key] = row
        return row

    def _mail(self, handle: str, actor: Any) -> dict[str, Any] | None:
        if self.mail_capture_dir is None:
            return None
        # The runtime's one mail resolver (``CaseRuntime.mail_capture``), on this capture.
        return self.runtime.mail_capture(handle, actor, directory=self.mail_capture_dir)

    def document(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """The pinned bytes of a captured document version for ``actor``, through the gate
        ``__call__`` resolves it with, while that version is current: a SharePoint file
        (``sharepoint://...#sha256=``) or a mail file attachment (``#attachment=..&sha256=``).
        ``{"l1_handle", "row", "content", "text"}`` (``row``: the capture's row of the
        version, a mail attachment's with the sender provenance its direction is read
        from, ``ontology.project.mail_attachment_sender``; ``text``: the capture's OCR
        text of a scan, else None), for
        ``ontology.document_fields.pinned_document_row``; None when the capture holds no
        version of it for this reader (nothing is read live in its place);
        ``CitationUnresolvable`` when the gate refuses it, a later version superseded it
        or its bytes cannot be read."""
        from integrations.captured_text import TEXT_OCR
        refused = CitationUnresolvable("capture_citation_unresolvable")
        try:
            if handle.startswith("sharepoint://"):
                if self.sharepoint_capture_dir is None:
                    return None
                with self._gate(actor) as gate:
                    return self._sharepoint_file(gate, actor, handle)
            if handle.startswith("graph-mail://"):
                view = self._mail(handle, actor)
                if view is None:
                    return None
                if view.get("changed_since") or view.get("relation") != "graph-mail.attachment":
                    raise refused
                from integrations.capture_mail import MailCaptureStore
                from ontology.project import mail_attachment_sender
                store = MailCaptureStore(self.mail_capture_dir)
                try:
                    content = store.attachment_content(
                        handle, self.runtime.employee_gate(actor), self.runtime.live.audience)
                    found = store.db.execute("SELECT * FROM attachments WHERE l1_handle=?",
                                             (handle,)).fetchone()
                    if content is None or found is None:
                        raise refused
                    text = (store.blob(found["text_sha256"]).decode("utf-8")
                            if found["text_status"] == TEXT_OCR else None)
                    whose = mail_attachment_sender(store.db, handle)
                    if whose is None:
                        raise refused
                    row = {**view["row"], **whose}
                finally:
                    store.close()
                return {"l1_handle": handle, "row": row, "content": content, "text": text}
        except (sqlite3.Error, OSError, ValueError, LookupError):
            raise refused from None
        return None


def serving_tools(runtime: Any, actor: Any, *, store_path: str | Path | None = None,
                  mail_capture_dir: str | Path | None = None,
                  sharepoint_capture_dir: str | Path | None = None,
                  **options: Any) -> CaseHostTools:
    """The ontology host a session serves over ``runtime``: ``CaptureCaseHostTools``
    when the host holds a mail or SharePoint capture, else ``CaseHostTools``. Both serve
    the runtime's Teams capture through ``CaseHostTools`` (``case_host_policy`` and the
    runtime's item gate), once it exists. The one construction the MCP server and the
    host's ``trim`` check share, so the trim reads under the ``policy_now`` sessions are
    served with."""
    if mail_capture_dir is None and sharepoint_capture_dir is None:
        return CaseHostTools(runtime, actor, store_path=store_path, **options)
    return CaptureCaseHostTools(runtime, actor, store_path=store_path,
                                mail_capture_dir=mail_capture_dir,
                                sharepoint_capture_dir=sharepoint_capture_dir, **options)


__all__ = ["CaptureCaseHostTools", "CaseCaptures", "CapturedCitations", "PageNotRead",
           "PagedRead", "serving_tools"]
