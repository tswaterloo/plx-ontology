"""What the case host adds to the generic tools it lists.

The case host keeps 22 tools
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
  (1-based, numbered in the whole text) for a line-pinned question; ``find`` locates a
  short phrase in the same text (``matches``: at most ``MAX_FIND_HITS`` places, each its
  line, offset and a short snippet served as a window's text is, on an original its page
  citation), so two passages far apart are found without paging between them;
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
  a revocation, a deletion or a move applies at once). A search (``plx.documents``,
  ``plx.find``: ``SEARCH_TOOLS``) never re-reads live: it serves the stored row at any
  capture age, decided on the permissions and exclusions the capture recorded (a row
  with no read, non-empty recorded permission list is withheld), each file labelled
  with its capture time and the oldest named in ``meta.freshness``; ``plx.read`` of a
  cited file re-reads it live. The re-reads of one tool call
  are bounded by ``max_live_rereads``, shared by every gate built inside the call (a
  captured original's gate included: ``_call_bound``). Every live request of one tool call (mail live
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
  is an ``ocr_unavailable`` gap, a read of a file that holds no extracted text a
  ``text_not_extracted`` gap with the reason (never a complete read with nothing in
  it; when a captured case original the reader may open holds the same bytes with
  their text, a ``text_in_case_original`` gap cites it, and a use case listing that
  copy lists the original after it), a file over the capture's content cap a
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
  identifier over every captured file the reader may see gives the files and pages
  naming it. The lookup reads the text the capture stored, through an index of it kept
  beside the store for every later process (``file_index``), and never extracts or
  OCR-reads at query time: a file without stored text, or whose scanned pages were left
  for a later OCR pass, is a ``text_not_read`` gap. The host gates and re-reads only a
  file whose text names the identifier, or one unsearched (a capped PDF whose pages read
  do not, reported as a ``skipped_by_cap`` gap, so that no match is never absence); the
  matching files are gated first, and a file the call's re-read bound drops is counted
  in a ``live_read_budget`` gap), so a document
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
import contextlib
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import functools
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
                                               RuntimeCitations, capped_gaps, covered_pages,
                                               field_value, skipped_gap, wanted_field,
                                               with_pages)
from operating.playbook_originals import extraction_skipped

_READ_OK = ("resolved", "changed_since")
# plx.read's find: the places it lists (the contract's matches.hits bound) and the
# characters of context its snippet keeps on each side of the phrase.
MAX_FIND_HITS = 50
FIND_CONTEXT_CHARS = 80
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
                "invalid_input", "text_offset, column_offset, line and find are exclusive"))
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
        if "find" in arguments:
            return self._checked(self._find(result, raw, arguments))
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

    @staticmethod
    def _text(raw: Mapping[str, Any], arguments: Mapping[str, Any]
              ) -> tuple[str | None, str, str | None, str]:
        """(the text a window or a find reads, its citation, its field path, and whether
        it is the read's ``content`` or one of its ``fields``)."""
        wanted = arguments.get("field_path")
        if wanted is not None and wanted != raw.get("content_field"):
            fields = raw.get("fields") or {}
            value = fields.get(wanted)
            if wanted not in fields:  # a key inside a JSON column: the one field reader
                got, value = wanted_field(fields, wanted)
                value = value if got is True else None
            return (_text_of(value), raw.get("values_citation") or arguments["citation"],
                    wanted, "fields")
        text = raw.get("content") if isinstance(raw.get("content"), str) else None
        return (text, raw.get("content_citation") or arguments["citation"],
                raw.get("content_field"), "content")

    def _window(self, result: dict[str, Any], raw: Mapping[str, Any],
                arguments: Mapping[str, Any]) -> dict[str, Any]:
        text, citation, field_path, key = self._text(raw, arguments)
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
        base = {k: v for k, v in result.items() if k not in ("content", "fields", "pages")}
        # The whole read's page list is not this window's (``with_pages`` below).
        base["gaps"] = [gap for gap in base.get("gaps") or ()
                        if gap.get("reason") != "page_citations_cut"]
        spans = raw.get("content_pages") if key == "content" and page is None else None

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
        while spans:
            # The pages this window's text is on, each citable as its page; a window
            # with no room for even the gap that says they were cut is made smaller.
            end = offset + out["text_window"]["chars"]
            paged = with_pages(out, covered_pages(citation, spans, offset, end),
                               self.limits["max_result_bytes"], cut_text=False)
            if self._fits(paged) or chars <= 1:
                return paged
            chars = max(1, chars // 2)
            out = build(chars)
        return out

    def _find(self, result: dict[str, Any], raw: Mapping[str, Any],
              arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Where ``find``'s phrase is in the text a window would page (``_text``), read
        for the same reader by the same generic read: the first ``MAX_FIND_HITS`` places,
        each its line, the ``offset`` and ``chars`` of a snippet around it
        (``FIND_CONTEXT_CHARS`` each side, within its line and its page) and that snippet
        as the window's text envelope, so it is served text as a window's is (quotable,
        and kept as a served part of that text); on an original's whole text, the page it
        is on (``page_citation``, ``page_offset``). Nothing outside the snippets is
        returned; ``total_hits`` counts every place."""
        text, citation, field_path, key = self._text(raw, arguments)
        if text is None:
            return _error("no_text", "the citation (or field) holds no text to search",
                          "name a text field with field_path, or read without find")
        words = arguments["find"].split()
        if not words:
            return _error("invalid_input", "find names no word")
        phrase = re.compile(r"\s+".join(map(re.escape, words)), re.IGNORECASE)
        starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
        _head, page = mn.cited_page(citation)
        spans = raw.get("content_pages") if key == "content" and page is None else None
        limit = self.limits["max_untrusted_text_chars"]
        base = {k: v for k, v in result.items() if k not in ("content", "fields", "pages")}
        base["gaps"] = [gap for gap in base.get("gaps") or ()
                        if gap.get("reason") != "page_citations_cut"]
        hits: list[dict[str, Any]] = []
        total = 0
        for match in phrase.finditer(text):
            total += 1
            if len(hits) >= MAX_FIND_HITS:
                continue
            line = bisect_right(starts, match.start())
            end_of_line = starts[line] - 1 if line < len(starts) else len(text)
            low = max(starts[line - 1], match.start() - FIND_CONTEXT_CHARS)
            high = min(max(end_of_line, match.end()), match.end() + FIND_CONTEXT_CHARS)
            on = covered_pages(citation, spans, match.start(), match.start() + 1) \
                if spans else []
            if on:   # the snippet stays on the page it is cited by
                low = max(low, on[0]["offset"])
                high = min(high, on[0]["offset"] + on[0]["chars"])
            cited = on[0]["page_citation"] if on else citation
            hit = {"offset": low, "chars": high - low, "line": line,
                   "text": ot._envelope(text[low:high], cited, None if on else field_path,
                                        limit)}
            if on:
                hit.update(page=on[0]["page"], page_citation=cited,
                           page_offset=low - on[0]["offset"])
            hits.append(hit)
        window = {"hits": hits, "total_hits": total, "total_chars": len(text),
                  "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        if field_path and ot._FIELD_PATH.match(field_path):
            window["field_path"] = field_path
        while True:
            out = dict(base, matches=window, completeness=_completeness(
                len(window["hits"]), total, len(window["hits"]) < total, "find_hits_cut",
                None))
            if self._fits(out) or not window["hits"]:
                return out
            window = dict(window, hits=window["hits"][:-1])

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


# The search tools: their results are never re-read live (``_CallBound.search``), and a
# query naming a use case gets that use case's documents (``_use_case_documents``).
SEARCH_TOOLS = frozenset({"plx.documents", "plx.find"})
# The gaps a use-case block brings (:meth:`CaptureCaseHostTools._use_case_documents`):
# kept over every other gap when an answer is trimmed to fit.
USE_CASE_GAPS = frozenset({"use_case_documents_withheld", "use_case_documents_not_searched",
                           "use_case_document_not_held", "unknown_use_case",
                           "use_case_catalog_unreadable"})
USE_CASE_NOTE = ("Answer a question about this use case from these documents, never from "
                 "another use case's map; read a cited map whole (page with text_offset "
                 "until complete) before saying something is absent from it.")


# Statuses of a permission list the capture read (``LiveSourceAudience.visible_all``).
_RECORDED_ACL = ("read", "derived")


class _CallBound:
    """The live re-read bound of one tool call, shared by every SharePoint gate built
    inside it (``CaptureCaseHostTools._call_bound``): the re-reads and live reads started
    against ``limit``, the request budget over the host's transports (None when none is
    known), and the re-read answers (per reader principals and version), the reads a
    reader's permissions decided and the drives whose source failed in the call."""

    def __init__(self, limit: int, budget: Any) -> None:
        self.limit, self.budget, self.rereads = limit, budget, 0
        self.live: dict[tuple, Any] = {}
        self.live_decided: set[tuple] = set()
        self.down: dict[str, str] = {}
        # True while a search tool runs (SEARCH_TOOLS): no live re-read is started.
        self.search = False
        # Per-call decisions of the search mail gate (``_mail_search_gate``).
        self.memo: dict[Any, Any] = {}


# The bound of the tool call in progress on this context (``CaptureCaseHostTools.per_call``).
# How many of a plx.documents page's documents are the search's own (its cursor pages
# them): the captured SharePoint files :meth:`CaptureCaseHostTools._captured_documents`
# appends after them are on no page. None: all of them.
_SEARCH_HITS: ContextVar[int | None] = ContextVar("plx_capture_search_hits", default=None)
_ACTIVE_CALL: ContextVar[_CallBound | None] = ContextVar("plx_capture_call_bound",
                                                         default=None)


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
                 clock: Callable[[], datetime] | None = None,
                 projection: Callable[[], Any] | None = None, **options: Any) -> None:
        """``projection``: the reader of the projection file ``store_path`` names now
        (``CaseCaptures.projection``), for the moved-file check of a gate that serves no
        tool call of its own."""
        # The use-case catalog (``operating.use_case_catalog``; None: the default file).
        self.use_cases = options.pop("use_cases", None)
        super().__init__(runtime, actor, mail_capture_dir=mail_capture_dir, **options)
        self._projection = projection
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
        # Handles whose refusal was decided (the reader's item ACL or audience refused
        # a version the capture holds): ``refusal_kind``; never part of a tool result.
        self._decided: set[str] = set()
        # The live re-read state of this gate's own calls (``_call_bound``).
        self._bound = _CallBound(self.max_live_rereads, None)
        self._lookup: _IdentifierLookup | None = None

    def close(self) -> None:
        if self._sharepoint_store is not None:
            self._sharepoint_store.close()
        self._sharepoint_store = None
        self._lookup = None
        super().close()

    def _new_call(self, shared: "_CallBound | None" = None) -> None:
        """Fresh per-call state; the live re-read bound is ``shared`` (the tool call in
        progress on this context, whose bound every gate built inside it draws from),
        else a new one of this host's own bounds."""
        from integrations.live_source_guard import GuardedTransport, RequestBudget
        self._served, self._refused = {}, {}
        self._over_budget, self._decided = set(), set()
        if shared is not None:
            self._bound = shared
            return
        live = getattr(self.runtime, "live", None)
        transports = [t for t in (getattr(live, "graph_transport", None),
                                  getattr(live, "bc_transport", None))
                      if isinstance(t, GuardedTransport)]
        self._bound = _CallBound(self.max_live_rereads, RequestBudget(
            transports, self.max_live_gets) if transports else None)

    @property
    def _call_bound(self) -> "_CallBound":
        """The live re-read bound this gate draws from now: its own call's while it serves
        one (shared with the tool call it was opened in, if any), else the tool call in
        progress on this context (a gate a resolve builds inside another host's call:
        ``CaseCaptures._gate``), else its own. So every gate built inside one tool call
        shares the call's one ``max_live_rereads`` and ``max_live_gets`` bound and its
        re-read answers, and a nested gate never starts a fresh budget."""
        if not self._in_call:
            active = _ACTIVE_CALL.get()
            if active is not None:
                return active
        return self._bound

    @contextmanager
    def per_call(self, *, whole_block: bool = True) -> Iterator[Any]:
        """One tool call: fresh per-call state (the mail capture's mailbox visibility
        decided once, ``capture_mail.visibility_scope``), and this call's request budget
        enforced on every live request inside the block. A call inside a call shares the outer
        call's state and budget. ``whole_block=False`` (``CaseCaptures.call``): the
        budget is enforced on this gate's own live reads only, so a case tool's other
        live reads are not refused by it, though every request of the block still
        counts against it."""
        if self._in_call:
            with self.metered() as budget:
                yield budget
            return
        from integrations.capture_mail import visibility_scope
        outer = _ACTIVE_CALL.get()
        self._new_call(outer)
        self._in_call = True
        token = _ACTIVE_CALL.set(self._bound) if outer is None else None
        try:
            with visibility_scope(), \
                    (self.metered() if whole_block else nullcontext()) as budget:
                yield budget
        finally:
            self._in_call = False
            if token is not None:
                _ACTIVE_CALL.reset(token)

    @contextmanager
    def metered(self) -> Iterator[Any]:
        """This call's request budget, enforced on the host's Graph and BC transports
        inside the block (None, and nothing enforced, when no transport is known)."""
        budget = self._call_bound.budget
        if budget is None:
            yield None
            return
        with budget.active() as active:
            yield active

    def _audience(self) -> Any:
        return getattr(getattr(self.runtime, "live", None), "audience", None)

    def _mail(self) -> tuple[Any, Any] | None:
        """(a context of the mail capture store, the live audience), or None: the case
        runtime's kept read-only store (``CaseRuntime.mail_reader``), else one opened and
        closed for this use."""
        from integrations.capture_mail import store_exists
        audience = self._audience()
        if self.mail_capture_dir is None or audience is None \
                or not store_exists(Path(self.mail_capture_dir)):
            return None
        return _mail_store(self.runtime, Path(self.mail_capture_dir)), audience

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
        # A family whose every held unit got a grant decision here is listed (``units``):
        # a reader granted all of them reads the whole family, one granted some is told
        # how many it lacks. A family left undecided (no capture store) is not listed.
        units = dict(policy.units)
        mail_units = [key for key in self._reader_keys if str(key).startswith("mail:")]
        opened = self._mail() if mail_units else None
        if opened is not None:
            stores, audience = opened
            with stores as store:
                check = store.audience_check(audience)
                for unit in mail_units:
                    grants[unit] = principals if check(actor, unit.partition(":")[2]) \
                        else frozenset()
                units["mail"] = tuple(mail_units)
        sharepoint_units = [key for key in self._reader_keys
                            if str(key).startswith("sharepoint:")]
        store = self._sharepoint() if sharepoint_units else None
        audience = self._audience()
        if store is not None and audience is not None:
            check = store.audience_check(audience)
            for unit in sharepoint_units:
                grants[unit] = principals if check(actor, unit.partition(":")[2]) \
                    else frozenset()
            units["sharepoint"] = tuple(sharepoint_units)
        return ReadPolicy(policy.tenant, grants, policy.markings, units)

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
            bound = self._call_bound
            if bound.search:
                # This host's decisions in this call (the bound is shared by every gate
                # built inside the call, of any host).
                memo = bound.memo.setdefault(("host", id(self)), {"host": self})
                if "opened" not in memo:   # one store check per search call
                    opened = self._mail()
                    memo["opened"] = None if opened is None else opened[1]
                audience = memo["opened"]
                if audience is None:
                    return False
                return self._mail_search_gate(
                    citations, lambda: _mail_store(self.runtime, Path(self.mail_capture_dir)),
                    audience, actor, handle, memo)
            opened = self._mail()
            if opened is None:
                return False
            stores, audience = opened
            try:
                with stores as store:
                    gated = self.runtime.employee_gate(actor)
                    if not store.item_gate(audience)(gated, handle):
                        return False
            except Exception:  # noqa: BLE001 - a refused gate reads nothing
                return False
            return citations.read(handle, actor).get("status") in _READ_OK
        return gate

    def _mail_search_gate(self, citations: Any, stores: Callable[[], Any], audience: Any, actor: Any,
                          handle: str, memo: dict) -> bool:
        """The mail item gate for a search candidate (``_CallBound.search``): the same
        decision as the capture's item gate followed by ``RuntimeCitations.mail_capture``
        (both read the one mail capture store with the live audience: the mailbox
        audience and owner ACL recorded at capture, the case mail scope, the employee's
        item ACL, the people exclusion over the mailbox and the sender), made on the
        capture's rows alone: never read live, no message body or attachment text
        decoded, and the reader's gate, its visible mailboxes and each mailbox's decision
        made once per call (``memo``). A page citation is decided by the full capture
        resolve (only its text tells whether the page exists)."""
        from integrations.graph_evidence import is_mail_attachment_citation
        from ontology.mentions import cited_page
        try:
            key = ("gated", actor)
            if key not in memo:
                memo[key] = self.runtime.employee_gate(actor)
            gated = memo[key]
            if citations._exclusion_refusal() or not audience.may_read_any(gated, "mail:"):
                return False
            if cited_page(handle)[1] is not None:
                with stores() as store:
                    if not store.item_gate(audience)(gated, handle):
                        return False
                return citations.mail_capture(handle, gated, audience) is not None
            with stores() as store:
                message = handle
                if "#" in handle and is_mail_attachment_citation(handle):
                    found = store.db.execute("SELECT message_handle FROM attachments "
                                             "WHERE l1_handle=?", (handle,)).fetchone()
                    if found is None:
                        return False
                    message = found[0]
                seen = ("visible", str(store.path), gated, id(audience))
                if seen not in memo:
                    memo[seen] = (audience, frozenset(store._visible(gated, audience)))
                row = store._latest(message)
                if row is None or row["mailbox_id"] not in memo[seen][1]:
                    return False
                mailbox = row["mailbox_id"]
                decided = ("mailbox", str(store.path), mailbox, gated)
                if decided not in memo:
                    memo[decided] = citations._visible_mailbox(
                        store, mailbox, gated, audience) is not None
                if not memo[decided]:
                    return False
                record = json.loads(row["record"])
                return not citations._sent_by_excluded(
                    store, [record.get("from"), record.get("sender")])
        except Exception:  # noqa: BLE001 - a refused gate or unreadable capture admits nothing
            return False

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
        self._decided.discard(handle)
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
                # Not captured, or not visible to this reader at capture: decided only
                # when the capture holds this exact version as the item's current one.
                if _current_version(store, handle):
                    self._decided.add(handle)
                return None
            # The employee's item ACL decides on this version and on the item's latest
            # captured one: a revocation captured since hides the older versions too,
            # also where no live re-read can confirm it (no reader, budget, failure).
            removed = view["removed_reason"] is not None
            denied = not removed and any(
                self.runtime._employee_acl_denial(gated, "sharepoint",
                                                  {**view["row"], "source_acl": acl})
                for acl in (view["source_acl"], view["item_source_acl"]))
            if removed or denied:
                self._refused[handle] = item_refused
                if denied:
                    self._decided.add(handle)
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
            if (frozenset(gated.principals), view["l1_handle"]) \
                    in self._call_bound.live_decided:
                self._decided.add(handle)
            return None
        basis, changed, failure = decided
        if failure == "search":
            # A search serves the stored row at any capture age, decided on the
            # permissions the capture recorded (as the projection decides every other
            # answer), labelled with its capture time. A row whose capture holds no read,
            # non-empty permission list (this version's or the item's latest) is
            # withheld at any age: an empty recorded list decides nothing.
            if not all(isinstance(acl, Mapping) and acl.get("status") in _RECORDED_ACL
                       and isinstance(acl.get("identities"), list) and acl["identities"]
                       for acl in (view["source_acl"], view["item_source_acl"])):
                self._refused[handle] = (
                    "withheld", "capture_permissions_unknown",
                    "the Workbench's capture holds no permission list for this file, so "
                    "a search cannot decide who may see it; it is not listed")
                return None
        elif basis != "live":
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
            if container in self._decided:
                self._decided.add(handle)
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

    def refusal_kind(self, handle: str) -> str:
        """Of the last :meth:`captured` of ``handle`` that served nothing: ``REFUSED``
        when this reader's permissions decided it (the item ACL or the capture's
        audience refused a version the capture holds as current, or a live re-read said
        not visible or denied), else ``FAILED`` (an employee gate that refused, a store
        that could not be read, a stale index, outside a search a capture too old to
        decide (a search decides on the capture at any age), in a search a stored row
        with no recorded permission list (``capture_permissions_unknown``), a removed
        item, no capture, a live 404 or 410, a live permission list not read in full
        such as on a 429, 5xx or timeout: ``graph_read.undecided_not_visible``). For
        ``CaseCaptures.gate_decision`` only; never in a result."""
        from operating.captured_originals import FAILED, REFUSED
        return REFUSED if str(handle) in self._decided else FAILED

    def _projected_stale(self, view: Mapping[str, Any]) -> bool:
        """Whether the projection shows this version under a name or path the capture
        no longer holds (moved since the projection was built): not served until the
        projection is rebuilt."""
        if self.store_path is None:
            return False
        held = {"row.name": view["row"].get("name"), "row.path": view["row"].get("path")}
        # The generation this request reads: its own tools' when it serves calls, else the
        # projection file as it is now. Never a whole tool table per gate: building one
        # reads every audience key and BC unit of the store (1.3 GB on 2.5M objects), and
        # an originals search makes a gate per original it checks.
        store = (self._projection() if self._tools is None and self._projection is not None
                 else None) or self.tools.store
        with store.lock:
            # Through the version index (object_versions by l1_handle, then
            # value_sources by object and handle): ``object_values`` alone has no index
            # on l1_handle, and this runs once per file the item gate decides.
            rows = store.conn.execute(
                "SELECT field_path, value FROM object_values WHERE object_id IN "
                "(SELECT object_id FROM object_versions WHERE l1_handle = ?) AND "
                "l1_handle = ? AND field_path IN ('row.name', 'row.path')",
                (view["l1_handle"], view["l1_handle"])).fetchall()
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
        if self._call_bound.search:
            return ("capture", False, "search")   # a search never re-reads live
        if reader is None:
            return ("capture", False, None)
        key = (frozenset(gated.principals), view["l1_handle"])
        bound = self._call_bound
        if key in bound.live:
            return bound.live[key]
        if drive in bound.down:
            return ("capture", False, "failed:" + bound.down[drive])
        if not self._may_reread():
            return ("capture", False, "budget")
        bound.rereads += 1
        with self.metered() as budget:
            refused = budget.refused if budget is not None else 0
            try:
                resolved = reader.resolve_citation(view["l1_handle"], gated)
            except NotVisible as exc:
                decided = None   # refused: nothing is served
                if getattr(exc, "undecided", None) is None:
                    # Decided by the reader's permissions; a 404 (deleted) or a
                    # permission list not read in full (a transient failure) is not.
                    bound.live_decided.add(key)
            except (SourceNotFound, ReadOnlyGuardError, ValueError, LookupError):
                decided = None   # deleted or malformed: nothing is served
            except RequestBudgetExhausted:
                decided = ("capture", False, "budget")
            except SourceUnavailable as exc:
                if getattr(exc, "http_status", None) == 410:
                    decided = None   # Gone: no longer exists at that version, not transient
                else:
                    bound.down[drive] = unavailable_detail(exc) or "unclassified"
                    decided = ("capture", False, "failed:" + bound.down[drive])
            else:
                record = resolved.get("record") if isinstance(resolved, Mapping) else None
                if (not isinstance(record, Mapping)
                        or resolved.get("status") not in ("current", "changed_since")):
                    decided = None   # no current record
                elif self.runtime._employee_acl_denial(gated, "sharepoint", record):
                    decided = None   # denied for this employee
                    bound.live_decided.add(key)
                elif record.get("path") != view["row"].get("path"):
                    decided = None   # moved since the capture
                else:
                    decided = ("live", resolved["status"] != "current", None)
        if budget is not None and budget.refused > refused:
            # A request inside the re-read hit the bound (even one its reader absorbed):
            # whatever it read, e.g. part of the permissions, decides nothing.
            decided = ("capture", False, "budget")
            bound.live_decided.discard(key)
        bound.live[key] = decided
        return decided

    def _may_reread(self, gets: int = REREAD_GETS) -> bool:
        """Whether this call may start one more live read: under ``max_live_rereads``
        and, where the host's transports are known, with room for ``gets`` requests in
        this call's budget (every live request of the call counts). The budget itself
        refuses any request past it (``per_call``)."""
        bound = self._call_bound
        if bound.rereads >= bound.limit:
            return False
        return bound.budget is None or bound.budget.used + gets <= bound.budget.limit

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
        self._call_bound.rereads += 1
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
                " (a search result is not re-read live; plx.read of it re-reads them)"
                if failure == "search"
                else " (this call's live re-read bound is reached)" if failure == "budget"
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
            # Nothing captured served: a use-case block alone is still held to the bound.
            return self._bounded(name, result) if "use_case" in result else result
        admitted = sorted(self._served[h]["captured_at"] for h in (*handles, *counted))
        output = self.tools.contracts["tools"][name]["output"]
        shape = (output["oneOf"][0] if "oneOf" in output else output).get("properties", {})
        out = dict(result)
        meta = out.get("meta")
        if isinstance(meta, dict):
            freshness = list(meta.get("freshness") or [])
            # A search serves captures of any age on their recorded permissions; past the
            # host's age bound the oldest one says so (no gap, not incomplete).
            old = self._call_bound.search and not capture_decides(
                admitted[0], self.clock(), self.max_capture_age)
            note = (f"host captures as of {admitted[0]}, permissions as of capture; "
                    "plx.read re-checks live")
            at = next((i for i, f in enumerate(freshness) if f.get("source") == "sharepoint"),
                      None)
            if at is None and len(freshness) < 8:
                entry = {"source": "sharepoint", "completeness": "unknown", "stale": False,
                         "captured_at": admitted[0]}
                if old:
                    entry["label"] = note
                freshness.append(entry)
                out["meta"] = dict(meta, freshness=freshness)
            elif at is not None and old and "label" not in freshness[at]:
                freshness[at] = dict(freshness[at], label=note)
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
        if not self._fits(result) and self._hits_key(result) is not None:
            result = self._trimmed(name, result, _SEARCH_HITS.get())
            problems = validate(result, contracts["tools"][name]["output"], root=contracts,
                                limit=5)
            if problems:
                return _error("contract_violation", ("trimmed output failed the contract: "
                                                     + "; ".join(problems))[:600])
        if not self._fits(result):
            return _error("result_too_large", "the answer and its capture labels do not "
                          "fit one response", "narrow the question or lower limit")
        return result

    # A search answer over the response bound (plx.documents' documents, plx.find's
    # candidates) is trimmed until it fits, each step only while the answer is still too
    # large: the mentions each listed document carries, then its snippet and title
    # length, then whole hits from the end of the page (its next cursor moved back over
    # them, so nothing is skipped), then the use-case block's titles and second entry
    # (its citations and counts stay: a use case is never dropped for ordinary hits),
    # then gaps from the end (the use-case gaps last). One ``result_trimmed`` gap, never
    # itself trimmed, names every step taken; the search's own total stays. What cannot
    # fit even so is the ``result_too_large`` error.
    TRIM_MENTIONS = (5, 0)
    TRIM_TEXT_CHARS = (400, 120)

    @staticmethod
    def _hits_key(result: Mapping[str, Any]) -> str | None:
        for key in ("documents", "candidates"):
            if isinstance(result.get(key), list):
                return key
        return None

    def _trimmed(self, name: str, result: Mapping[str, Any], paged: int | None = None
                 ) -> dict[str, Any]:
        """``paged``: how many of the hits, from the first, the search's cursor pages
        (None: all); the rest (captured files a host appended) are on no other page."""
        out = json.loads(json.dumps(result))   # never edit the caller's answer
        key = self._hits_key(out)
        noun = "documents" if key == "documents" else "candidates"
        hits: list[dict[str, Any]] = out[key]
        total = len(hits)
        paged = total if paged is None else max(0, min(paged, total))
        cursor = (out.get("completeness") or {}).get("next_cursor")
        # plx.find keeps its status's own candidates: a found answer its one, an ambiguous
        # one two (never a lone apparent winner); a document list may empty.
        floor = 0 if key == "documents" else {"found": 1, "ambiguous": 2}.get(
            str(out.get("status")), 0)
        steps: list[str] = []
        output = self.tools.contracts["tools"][name]["output"]
        shape = (output["oneOf"][0] if "oneOf" in output else output).get("properties", {})
        cap = shape.get("gaps", {}).get("maxItems", 20)

        def attempt(*extra: str) -> dict[str, Any] | None:
            gaps = [g for g in out.get("gaps") or [] if g.get("reason") != "result_trimmed"]
            note = _gap("unknown", "result_trimmed", name, (
                "this answer was over the response bound and was trimmed to fit: "
                + "; ".join([*steps, *extra]) + ". plx.read a listed document for its "
                "full text and mentions")[:600])
            trial = dict(out, gaps=gaps[:cap - 1] + [note])
            return trial if self._fits(trial) else None

        def counts() -> tuple[int, int]:
            """(search hits, appended captured files) dropped so far."""
            searched = max(0, paged - len(hits))
            return searched, total - len(hits) - searched

        def dropped() -> str:
            searched, captured = counts()
            parts = []
            if searched:
                head = (f"the last {searched} of {paged}" if hits else f"all {paged}") \
                    + f" {noun} the search paged left out"
                if out["completeness"]["next_cursor"] is not None:
                    head += "; completeness.next_cursor continues from the first left out"
                elif cursor is None:
                    head += (f"; ask again with limit {max(len(hits), 1)} to page through "
                             "them")
                else:
                    head += "; no next page can be offered"
                parts.append(head)
            if captured:
                parts.append(f"{captured} more captured SharePoint file(s) matching this "
                             "query left out; they are on no page: narrow the question to "
                             "see them")
            return "; ".join(parts)

        def drop_one() -> None:
            hits.pop()
            searched, _captured = counts()
            # The page's cursor moves back over the search's own hits dropped here (a
            # resumable offset cursor), else none is offered and the note says how to
            # page; dropped captured files are on no page: the page is partial.
            out["completeness"] = dict(
                out.get("completeness") or {"total": None}, complete=False, truncated=True,
                reason="result_too_large", returned=len(hits),
                next_cursor=(ot._rewound_cursor(cursor, searched) if searched
                             else cursor))

        for keep in self.TRIM_MENTIONS:
            cut = 0
            for doc in hits:
                if len(doc.get("mentions") or []) > keep:
                    doc["mentions"], cut = doc["mentions"][:keep], cut + 1
            if cut:
                steps.append(f"mentions kept to {keep} on {cut} document(s)")
                if (answer := attempt()) is not None:
                    return answer
        for chars in self.TRIM_TEXT_CHARS:
            cut = 0
            for doc in hits:
                for field in ("snippet", "title"):
                    text = doc.get(field)
                    if isinstance(text, dict) and len(text.get("untrusted_text", "")) > chars:
                        doc[field] = dict(text, untrusted_text=text["untrusted_text"][:chars],
                                          truncated=True)
                        cut += 1
            if cut:
                steps.append(f"{cut} snippet(s) or title(s) cut to {chars} characters")
                if (answer := attempt()) is not None:
                    return answer
        while len(hits) > floor:
            drop_one()
            if (answer := attempt(dropped())) is not None:
                return answer
        if total > len(hits):
            steps.append(dropped())
        block = out.get("use_case")
        entries = [] if not isinstance(block, dict) else [block, *(block.get("also") or [])]
        if any(d.get("title") for entry in entries for d in entry.get("documents") or []):
            for entry in entries:
                for doc in entry.get("documents") or []:
                    doc["title"] = None
            steps.append("the use cases' document titles left out (their citations and "
                         "counts stay)")
            if (answer := attempt()) is not None:
                return answer
        also = [entry for entry in entries[1:] if entry.get("documents")]
        if also:
            for entry in also:
                entry["documents"] = []
            steps.append("the second use case's documents left out (its id and withheld "
                         "count stay): ask about it by name to list them")
            if (answer := attempt()) is not None:
                return answer
        # Last, the gaps from the end, a use case's own gaps after every other (the
        # trimming note itself always stays); an answer that does not fit even so is
        # returned as it was, and fails as too large.
        gaps = [g for g in out.get("gaps") or [] if g.get("reason") != "result_trimmed"]
        gaps = ([g for g in gaps if g.get("reason") in USE_CASE_GAPS]
                + [g for g in gaps if g.get("reason") not in USE_CASE_GAPS])
        for keep in range(min(len(gaps), cap - 1) - 1, -1, -1):
            out["gaps"] = gaps[:keep]
            if (answer := attempt(f"{len(gaps) - keep} further gap(s) left out")) is not None:
                return answer
        return dict(result)

    # ---- SharePoint: reads and document search -----------------------------------

    def captured_read(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """``plx.read`` of a captured SharePoint version (``CitationReader.read`` shape):
        the file, or the refusal (with its reason) of a captured version the gate
        refuses; None when the capture does not hold it for this reader. Inside a tool
        call it shares that call's re-read bound."""
        if not self._in_call:
            self._new_call(_ACTIVE_CALL.get())
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
            "content_field": "row.content_text", "gaps": self._labels(view["l1_handle"]),
            # Why a file read holds no text (``text_not_extracted``): a member's own
            # status, else the capture's (its content status when its bytes were not read).
            "text_status": str(row.get("text_status") or row.get("content_status")
                               or "text_not_read")}
        if isinstance(text, str):
            out["content"] = text
            skipped = extraction_skipped(row)
            if skipped is not None and "page" not in view:
                out["gaps"].append(skipped_gap(handle, skipped))
        if not (isinstance(text, str) and text.strip()) and "page" not in view:
            original = self._text_original(actor, view)
            if original is not None:
                out["gaps"].append(_gap(
                    "not_ingested", "text_in_case_original", original,
                    "the SharePoint capture holds no text of this version; the captured "
                    "case original cited here holds the same bytes (SHA-256) with their "
                    "extracted text: read it (and its members) for what this file says"))
        if view["superseded_at"] is not None:
            out["gaps"].append(_gap("stale", "superseded_in_capture", handle[:700],
                                    "the capture no longer holds this version as current"))
        return out

    def _text_original(self, actor: Any, view: Mapping[str, Any]) -> str | None:
        """The ``original://`` citation of the captured original holding the text of the
        captured SharePoint version ``view`` (served to this reader) when the capture
        holds that version without usable text, and this reader may open the original
        (``CapturedOriginals.text_original_of``); else None. A container member: None."""
        from integrations import capture_sharepoint as cs
        originals = getattr(self.runtime, "captured_originals", None)
        row = view.get("row") or {}
        handle = str(view.get("l1_handle") or "")
        if originals is None or _member_container(handle) is not None:
            return None
        try:
            (drive_id, item_id), _etag, _extra = cs.parse_handle(handle, cs.SCHEME, 2)
        except ValueError:
            return None
        return originals.text_original_of(drive_id, item_id, row.get("content_sha256"),
                                          actor)

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
            bound = self._call_bound
            searching = bound.search
            # A search result is matched on the capture's stored, hash-checked text and
            # gated on the permissions recorded at capture: never re-read live (only
            # plx.read of a cited file re-reads it, within the call's bound).
            bound.search = searching or name in SEARCH_TOOLS
            try:
                return self._capture_call(name, arguments, actor)
            finally:
                bound.search = searching

    def _capture_call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        result = super().call(name, arguments, actor)
        if name in self.registered or not isinstance(result, dict) or "error" in result \
                or name not in self.tools.contracts["tools"] or name == "plx.read":
            return result   # plx.read: its reader labels a captured file itself
        if name in SEARCH_TOOLS and isinstance(arguments, dict):
            result = self._use_case_documents(name, arguments, actor, result)
        if self.sharepoint_capture_dir is None:
            return self._bounded(name, result) if "use_case" in result else result
        if name == "plx.documents" and isinstance(arguments, dict):
            hits = len(result.get("documents") or [])
            result = self._captured_documents(arguments, actor, result)
            token = _SEARCH_HITS.set(hits)
            try:
                return self._label(name, result, actor)
            finally:
                _SEARCH_HITS.reset(token)
        return self._label(name, result, actor)

    def _use_case_documents(self, name: str, arguments: Mapping[str, Any], actor: Any,
                            result: dict[str, Any]) -> dict[str, Any]:
        """``result`` with the source documents of each use case the query names
        (``operating.use_case_catalog``) for this reader, listed in ``use_case`` alone:
        they are never search hits, so the search's page, counts and ``no_match`` gap
        stay as the search produced them. Only the source kinds the call searches
        (``sources``: ``sharepoint``, ``original``) are resolved; those held in a source
        the call does not search are counted per source in a
        ``use_case_documents_not_searched`` gap (never named, never resolved), so a
        narrowed search is never told the catalog lists none. Each goes through
        its source's gate (a SharePoint item through :meth:`captured`, a case original
        through the runtime's captured-original gate), and those not served to this
        reader are counted, never named. A document the use case needs that no source
        holds is a named ``use_case_document_not_held`` gap, so a question about the use
        case is never answered from another use case's map. Only a first page (no
        cursor) is resolved. A query saying "use case" that names none in the catalog
        is an ``unknown_use_case`` gap; an unreadable catalog a
        ``use_case_catalog_unreadable`` gap."""
        from operating import use_case_catalog as ucc
        query = arguments.get("query")
        if not isinstance(query, str) or arguments.get("cursor"):
            return result
        kinds = {"sharepoint": "sharepoint_file", "original": "original"}
        wanted = {kinds[source] for source in (arguments.get("sources") or kinds)
                  if source in kinds}
        if not wanted:
            return result   # a mail-only search: no use-case documents
        gaps: list[dict[str, Any]] = []
        try:
            catalog = self.use_cases if self.use_cases is not None else ucc.load()
            cases, unknown = catalog.named(query)
        except ucc.CatalogError:
            cases, unknown = [], False
            if "use case" in " ".join(ucc.words(query)):
                gaps.append(_gap("unknown", "use_case_catalog_unreadable", "use_case_catalog",
                                 "the Workbench's use-case catalog could not be read; this "
                                 "answer matched documents by their words only"))
        if unknown:
            gaps.append(_gap("unknown", "unknown_use_case",
                             f"query:{' '.join(query.split())}"[:700],
                             "the query names a use case the Workbench's catalog does not "
                             "list; documents were matched by their words only, and none of "
                             "them is known to belong to that use case"))
        if not cases and not gaps:
            return result
        blocks = []
        for case in cases:
            listed, withheld, unsearched = self._use_case_served(case, actor, wanted)
            block = {"id": case.id, "title": case.title,
                     "documents": [{"citation": d["citation"], "kind": d["kind"],
                                    "role": d["role"], "title": d["title"]}
                                   for d in listed],
                     "withheld": withheld,
                     "note": USE_CASE_NOTE}
            blocks.append(block)
            if withheld:
                gaps.append(_gap("inaccessible", "use_case_documents_withheld",
                                 f"use_case:{case.id}",
                                 f"{withheld} source document(s) of this use case are not "
                                 "served to you (not held by the Workbench, or not openable "
                                 "with your permissions); they are not named"))
            if unsearched:
                held_in = ", ".join(f"{count} in {source}"
                                    for source, count in sorted(unsearched.items()))
                gaps.append(_gap("unknown", "use_case_documents_not_searched",
                                 f"use_case:{case.id}",
                                 f"{sum(unsearched.values())} source document(s) of this use "
                                 f"case are held in a source this search did not include "
                                 f"({held_in}); search again with those sources (or with no "
                                 "sources) to list the ones you may open"))
            for missing in case.gaps:
                gaps.append(_gap("not_ingested", "use_case_document_not_held",
                                 f"use_case:{case.id}",
                                 f"{missing['document']}: {missing['detail']}. This use "
                                 "case's own document is not held, and another use case's "
                                 "map does not stand in for it; records and files held "
                                 "elsewhere (SharePoint files, mail, Portal or ERP records, "
                                 "task boards) may still answer the question"))
            if not case.documents and not case.gaps:
                gaps.append(_gap("not_ingested", "use_case_document_not_held",
                                 f"use_case:{case.id}",
                                 "the Workbench's catalog lists no source document for this "
                                 "use case"))
        out = dict(result)
        if blocks:
            out["use_case"] = blocks[0] if len(blocks) == 1 else blocks[0] | {
                "also": [{"id": b["id"], "title": b["title"], "documents": b["documents"],
                          "withheld": b["withheld"]} for b in blocks[1:]]}
        output = self.tools.contracts["tools"][name]["output"]
        shape = (output["oneOf"][0] if "oneOf" in output else output).get("properties", {})
        cap = shape.get("gaps", {}).get("maxItems", 20)
        # The use case's documents are never search hits: they are listed in
        # ``use_case`` alone, and the search's own page, counts and no_match gap stay
        # exactly as the search produced them.
        # Too large with the block, the search's own hits are trimmed to fit
        # (:meth:`_trimmed`), never the block dropped.
        out["gaps"] = (gaps + list(out.get("gaps") or []))[:cap]
        return out

    def _use_case_served(self, case: Any, actor: Any, kinds: set[str]
                         ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """(the documents of ``case`` of ``kinds`` served to this reader now, in catalog
        order, each {citation, kind, role, title}; how many of them are not; how many
        of its documents are of a kind not in ``kinds``, by ``sources`` name: never
        resolved, so never gated, listed or withheld here)."""
        from operating.captured_originals import SCHEME as ORIGINAL
        from operating import use_case_catalog as ucc
        listed: list[dict[str, Any]] = []
        withheld = 0
        unsearched: dict[str, set[Any]] = {}   # each document once, however often named
        store = self._sharepoint()
        originals = getattr(self.runtime, "captured_originals", None)
        # Originals listed beside a textless SharePoint copy, within the list's bound.
        room = ucc.MAX_DOCUMENTS - len(case.documents)
        for doc in case.documents:
            if ("sharepoint_file" if "sharepoint" in doc else "original") not in kinds:
                source = "sharepoint" if "sharepoint" in doc else "original"
                unsearched.setdefault(source, set()).add(
                    doc.get("original", doc.get("sharepoint")))
                continue   # a source this call does not search: counted, not resolved
            if any(d.get("_key") == doc.get("original", doc.get("sharepoint"))
                   or "original" in doc and d["citation"] == ORIGINAL + doc["original"]
                   for d in listed):
                continue   # named twice (or listed with its textless copy): listed once
            if "sharepoint" in doc:
                try:
                    row = None if store is None else store.current(*doc["sharepoint"])
                except (sqlite3.Error, OSError, ValueError):
                    row = None
                view = None if row is None or row.get("removed_reason") is not None \
                    else self.captured(actor, row["l1_handle"])
                if view is None:
                    withheld += 1
                    continue
                handle, kind = view["l1_handle"], "sharepoint_file"
                name = view["row"].get("name")
                # A copy held without text: the case original holding its text (same
                # bytes, same item gate) is listed after it, while the list has room.
                original = None if "original" not in kinds or room <= 0 \
                    else self._text_original(actor, view)
                if original is not None and all(d["citation"] != original for d in listed):
                    room -= 1
                    entry = originals.entries().get(original[len(ORIGINAL):])
                    listed.append(self._use_case_document(doc, handle, kind, name))
                    handle, kind = original, "original"
                    name = None if entry is None else entry.receipt.get("name")
            else:
                handle, kind = ORIGINAL + doc["original"], "original"
                try:
                    opened = originals is not None \
                        and originals.refusal_kind(handle, actor) is None
                    entry = originals.entries().get(doc["original"]) if opened else None
                except Exception:  # noqa: BLE001 - a failing gate serves nothing
                    entry = None
                if entry is None:
                    withheld += 1
                    continue
                name = entry.receipt.get("name")
            listed.append(self._use_case_document(doc, handle, kind, name))
        return listed, withheld, {source: len(keys) for source, keys in unsearched.items()}

    @staticmethod
    def _use_case_document(doc: Mapping[str, Any], handle: str, kind: str, name: Any
                           ) -> dict[str, Any]:
        return {"_key": doc.get("original", doc.get("sharepoint")),
                "citation": handle, "kind": kind, "role": doc["role"],
                "title": None if not isinstance(name, str) or not name
                else {"untrusted_text": name[:400], "origin": {"citation": handle}}}

    def _captured_documents(self, arguments: Mapping[str, Any], actor: Any,
                            result: dict[str, Any]) -> dict[str, Any]:
        """Captured SharePoint files whose text names an identifier of the query, for
        this reader, added to the first page of ``plx.documents`` (each mention cited by
        page, a lead and an external assertion with no object: nothing resolves it).
        A file the projection already lists (its name or a field names the identifier)
        gets the page-cited mentions its captured text holds that it lacks: no projected
        object takes them, so it would otherwise come back with no page cited.
        The text is matched offline, through the reader's identifier lookup
        (:meth:`_identifier_lookup`, from the text the capture stored); only a matching
        file (read as ``plx.read`` serves it, on its matching pages alone) or one
        unsearched (over the content cap or the text extractor's bound, metadata-only,
        or without stored text: ``text_not_read``), or a PDF read on its first pages
        only that does not match, reported with the pages not searched:
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
        not_read = text_unread = 0
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
            elif why == TEXT_NOT_READ:
                text_unread += 1
            else:
                capped.append(skipped_gap(handle, why))
        room = arguments.get("limit", DEFAULT_LIMIT) - len(documents)
        if not found and not lookup.truncated and not over_cap and not over_bound \
                and not capped and not over_budget and not not_read and not text_unread:
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
        if text_unread:
            gaps.append(_gap("not_ingested", TEXT_NOT_READ, "capture:sharepoint",
                             f"{text_unread} captured SharePoint file(s) you may see have "
                             "no text the capture read whole (none stored: unreadable, "
                             "unsupported or failed at capture, or scanned pages left for "
                             "a later OCR pass), and text is never extracted when a "
                             "question is asked, so identifiers in them were not searched"))
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
        elif (over_cap or over_bound or capped or over_budget or not_read or text_unread) \
                and not completeness.get("truncated"):
            completeness.update(complete=False, reason="partial_scope")
        return dict(out, gaps=gaps[:20], searched=searched, completeness=completeness)

    def _identifier_lookup(self, store: Any, gated: Any, audience: Any
                           ) -> "_IdentifierLookup":
        """Every current captured file this reader may see (``store.index``), looked up in
        the store's identifier index (:func:`file_index`: per file, the identifiers its
        stored text names by page under the one grammar, ``mn.file_text_fields`` and
        ``mn.extract``, as :meth:`_mentioning` reads it, and why its text was not searched
        whole). Nothing is extracted or OCR-read here: a file without stored text is
        unsearched (``text_not_read``), never read at query time. Reused while the
        reader, the audience and the capture store are unchanged (SQLite
        ``data_version``: another connection's commit makes a new one); one is held at a
        time. Bounded by ``LOOKUP_FILES`` files and ``LOOKUP_ENTRIES`` entries (a cut
        lookup is ``truncated``)."""
        state = store.db.execute("PRAGMA data_version").fetchone()[0]
        held = self._lookup
        if held is not None and held.reader == gated and held.audience is audience \
                and held.state == state and held.store is store:
            return held
        lookup = _IdentifierLookup(store, gated, audience, state)
        index = store.index(gated, audience, limit=LOOKUP_FILES)
        files = file_index(store) if index["items"] else {}
        lookup.truncated, held_entries = index["truncated"], 0
        for item in index["items"]:
            handle = item["l1_handle"]
            entry = files.get(handle)
            if entry is None:
                continue   # no longer current: the index read the store after the listing
            named, why = entry["named"], entry["why"]
            if named is None:
                if why is not None:
                    lookup.unsearched.append((len(lookup.handles), why))
                    lookup.handles.append(handle)
                continue
            if held_entries + len(named) > LOOKUP_ENTRIES:
                lookup.truncated = True   # a file is held whole or not at all
                break
            at, held_entries = len(lookup.handles), held_entries + len(named)
            lookup.handles.append(handle)
            for kind, normalized, page in named:
                lookup.entries.setdefault((kind, normalized), []).append((at, page))
            skipped = (extraction_skipped({"skipped_by_cap": entry["skipped"]})
                       if entry["skipped"] is not None else None)
            # One gap per file: a page-capped file names its pages not read (the more
            # specific gap), also when its scanned pages wait for OCR too.
            if skipped is not None:
                lookup.unsearched.append((at, skipped))
            elif why is not None:
                lookup.unsearched.append((at, why))   # scanned pages left for OCR
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


# ---------------------------------------------------------------- the identifier index
# What each current captured SharePoint file's stored text names, for every reader's
# lookup (``CaptureCaseHostTools._identifier_lookup``). Reader-independent: a reader's lookup takes the
# entries of the files ``store.index`` lists for it. Each entry is keyed by the file
# version's identity (its MIME type, name, content and text status and SHA-256s: what
# its stored text and its paging are read from) and the whole index by a digest of the
# code that makes it (``_index_code``), so an entry is never used for other text or
# another grammar. It is kept beside the store (owner-only, written whole or not at all)
# for every later process: a new MCP session reads it instead of the captured text, and
# only the files captured since are read (from their stored text) and added.
INDEX_FORMAT = "plx-identifier-index/v1"
INDEX_FILE = ".identifier-index.json"
INDEX_MAX_BYTES = 256 << 20
# Why a file's text is not searched whole (``_IdentifierLookup.unsearched``).
TEXT_NOT_READ = "text_not_read"
_UNSEARCHED = ("cap", "not_read", "bound", TEXT_NOT_READ)
_INDEX_ROWS_SQL = ("SELECT l1_handle, record, content_status, content_sha256, text_status, "
                   "text_sha256 FROM items WHERE superseded_at IS NULL")
_INDEXES: dict[str, tuple[str, dict[str, dict[str, Any]]]] = {}   # per index file
_INDEXES_LOCK = threading.Lock()


def identifier_index_path(store_root: str | Path) -> Path:
    """Where the identifier index of the SharePoint capture under ``store_root`` (the
    store's own directory, ``SharePointCaptureStore.root``) is kept: beside its
    database."""
    return Path(store_root) / INDEX_FILE


def _index_row(row: Mapping[str, Any]) -> dict[str, Any]:
    record = json.loads(row["record"])
    return {**(record if isinstance(record, dict) else {}),
            **{key: row[key] for key in ("content_status", "content_sha256", "text_status",
                                         "text_sha256")}}


def _file_key(row: Mapping[str, Any]) -> str:
    from integrations.captured_text import file_type
    mime, name = file_type(row)
    return hashlib.sha256(json.dumps(
        [mime, name, row.get("content_status"), row.get("content_sha256"),
         row.get("text_status"), row.get("text_sha256")]).encode("utf-8")).hexdigest()


def _file_entry(blobs: Path, handle: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """One file's index entry: ``named``, the (kind, normalized, page) its stored text
    names in text order, once each (None: its text is not searched), ``why`` its text
    is not searched whole (``_UNSEARCHED``; for a file whose scanned pages were left for
    a later OCR pass, its stored text layer is searched and ``why`` is
    ``text_not_read``), and ``skipped`` the pages past the page bound it does not hold.
    Read from the stored text alone (``captured_text.stored_text``): no extraction, no
    OCR. A file the capture read and found textless (``ocr_no_text``, an unpaged
    ``no_text``) is searched and names nothing; any other file without stored text
    (unreadable, unsupported, failed, an OCR failure, bytes not captured, a text blob
    missing or altered) is ``text_not_read``."""
    from integrations.capture_sharepoint import CONTENT_SKIPPED, EXCEEDS_CAP
    from integrations.captured_text import (OCR_NO_TEXT, SCANS_PENDING, TEXT_EXCEEDS_BOUND,
                                            stored_text)
    content, status = row.get("content_status"), row.get("text_status")
    why = ("cap" if content == EXCEEDS_CAP else "not_read" if content == CONTENT_SKIPPED
           else "bound" if status == TEXT_EXCEEDS_BOUND else None)
    if why is not None:
        return {"named": None, "why": why, "skipped": None}
    stored = stored_text(blobs, row)
    if stored.text is None:
        textless = status == OCR_NO_TEXT or (status == "no_text" and not stored.paged)
        return {"named": [] if textless else None,
                "why": None if textless else TEXT_NOT_READ, "skipped": None}
    named: dict[tuple[str, str, int | None], None] = {}
    for field in mn.file_text_fields(handle, "row.content_text", stored.text,
                                     paged=stored.paged):
        page = mn.cited_page(field.citation)[1] if stored.paged else None
        for found in mn.extract(field.text):
            if found["normalized"]:
                named[(found["identifier_kind"], str(found["normalized"]), page)] = None
    return {"named": [list(key) for key in named],
            "why": TEXT_NOT_READ if status in SCANS_PENDING else None,
            "skipped": stored.skipped_by_cap}


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"named", "why", "skipped"}:
        return False
    named, why, skipped = entry["named"], entry["why"], entry["skipped"]
    if why is not None and why not in _UNSEARCHED:
        return False
    if skipped is not None and not isinstance(skipped, dict):
        return False
    return named is None or (isinstance(named, list) and all(
        isinstance(item, list) and len(item) == 3 and isinstance(item[0], str)
        and isinstance(item[1], str) and (item[2] is None or (
            isinstance(item[2], int) and not isinstance(item[2], bool)))
        for item in named))


def _index_inputs() -> list[str]:
    """Everything an index entry is made from, as text: the grammar (``ontology.identifiers``
    and its schema, ``ontology.mentions`` with its paging, ``ontology.units``' unit
    words), the stored-text reader (all of ``integrations.captured_text``: text blobs,
    dropped readings and their decoding), the page bound and page count of a capped PDF
    (``document_understanding.MAX_PAGES``, ``pdf_page_count`` and its readers, pypdf's
    version), the capture's content statuses and this module's entry code. Raises
    ``OSError`` or ``TypeError`` when a source cannot be read."""
    import inspect
    from integrations import capture_sharepoint, captured_text, document_understanding as du
    from ontology import identifiers, units
    try:
        from importlib.metadata import version
        pypdf = version("pypdf")
    except Exception:  # noqa: BLE001 - no pypdf: page counts come from poppler alone
        pypdf = "none"
    return [INDEX_FORMAT, json.dumps(_UNSEARCHED), _INDEX_ROWS_SQL,
            *(Path(module.__file__).read_text(encoding="utf-8")
              for module in (identifiers, mn, units, captured_text)),
            identifiers.SCHEMA_PATH.read_text(encoding="utf-8"),
            json.dumps([du.MAX_PAGES, pypdf, capture_sharepoint.CONTENT_SKIPPED,
                        capture_sharepoint.EXCEEDS_CAP, mn.PAGE_BREAK]),
            *(inspect.getsource(item) for item in (
                mn.text_pages, mn.is_paged_file, mn.file_text_fields, mn.cited_page,
                captured_text.stored_text, captured_text.read_blob,
                captured_text.read_dropped, captured_text.decode_reading,
                captured_text.reading_key, captured_text.ocr_digest,
                du.pdf_page_count, du._pypdf_open, du._poppler_page_count,
                _index_row, _file_key, _file_entry, _valid_entry))]


@functools.lru_cache(maxsize=1)
def _index_code() -> str | None:
    """A digest of :func:`_index_inputs`: an index kept by other code is never used; None
    when a source cannot be read (no index is kept then: every process reads the stored
    text anew)."""
    try:
        parts = _index_inputs()
    except (OSError, TypeError):
        return None
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _load_index(path: Path, code: str) -> dict[str, dict[str, Any]] | None:
    """The entries kept in ``path`` by this code, else None (absent, foreign, too
    large, malformed: nothing of it is used)."""
    try:
        if path.stat().st_size > INDEX_MAX_BYTES:
            return None
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(kept, dict) or kept.get("format") != INDEX_FORMAT \
            or kept.get("code") != code or not isinstance(kept.get("files"), dict):
        return None
    files = kept["files"]
    if not all(isinstance(key, str) and _valid_entry(entry) for key, entry in files.items()):
        return None
    return files


def _save_index(path: Path, code: str, files: Mapping[str, Any]) -> None:
    """Keep ``files`` in ``path``, owner-only, written whole or not at all (a temporary
    file renamed over it); a directory this process may not write keeps nothing."""
    import os
    import secrets
    text = json.dumps({"format": INDEX_FORMAT, "code": code, "files": dict(files)},
                      sort_keys=True, separators=(",", ":"))
    if len(text) > INDEX_MAX_BYTES:
        return
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def file_index(store: Any) -> dict[str, dict[str, Any]]:
    """Every current captured file version of ``store`` (by version handle) with its
    identifier index entry (:func:`_file_entry`), for every reader: no visibility is
    decided here, and a reader's lookup takes only the files the store lists for it.
    Entries come from this process's copy, else the file kept beside the store, else
    the file's stored text (then the index is kept again, holding the current files
    only)."""
    code = _index_code()
    path = identifier_index_path(store.root)
    rows = {row["l1_handle"]: _index_row(row)
            for row in store.db.execute(_INDEX_ROWS_SQL).fetchall()}
    keys = {handle: _file_key(row) for handle, row in rows.items()}
    with _INDEXES_LOCK:
        held = _INDEXES.get(str(path))
    entries = held[1] if held is not None and code is not None and held[0] == code else None
    if entries is None or not set(keys.values()) <= set(entries):
        loaded = None if code is None else _load_index(path, code)
        entries = {**(loaded or {}), **(entries or {})}
    missing = {key: handle for handle, key in keys.items() if key not in entries}
    if missing:
        entries = dict(entries)
        for key, handle in missing.items():
            entries[key] = _file_entry(store.blobs, handle, rows[handle])
        entries = {key: entries[key] for key in set(keys.values())}
        if code is not None:
            _save_index(path, code, entries)
    if code is not None:
        with _INDEXES_LOCK:
            _INDEXES[str(path)] = (code, entries)
    return {handle: entries[key] for handle, key in keys.items()}


class PageNotRead(CitationUnresolvable):
    """A citation of a page of a captured file this reader may be served that its text
    reading never read: past the page cap (``skipped_by_cap``) or past its last page.
    Public, as ``case_runtime.LiveSourceNotConfigured`` is: raised only once the file
    itself is served to the reader, it tells nothing the file's own read does not."""

    code = PAGE_NOT_READ
    public = True

    def __init__(self) -> None:
        super().__init__(PAGE_NOT_READ)


def _close_reader(reader: ot.StoreReader) -> None:
    """Close ``reader``'s connection once no gate's query is running on it (its lock)."""
    with reader.lock:
        reader.conn.close()


def _mail_store(runtime: Any, directory: Path) -> Any:
    """A context of the mail capture store under ``directory``: the runtime's kept
    read-only one (``CaseRuntime.mail_reader``), else one opened and closed for this use."""
    from integrations.capture_mail import MailCaptureStore
    reader = getattr(runtime, "mail_reader", None)
    if callable(reader):
        return reader(directory)
    return contextlib.closing(MailCaptureStore(directory))


def _current_version(store: Any, handle: str) -> bool:
    """Whether the SharePoint capture holds ``handle`` (a plain version handle, no page,
    pin or member) as its item's current, not removed, version: a reader refused it was
    refused a version that exists. Any doubt: False."""
    from integrations import capture_sharepoint as cs
    try:
        (drive_id, item_id), _etag, extra = cs.parse_handle(handle, cs.SCHEME, 2)
        if extra:
            return False
        current = store.current(drive_id, item_id)
    except Exception:  # noqa: BLE001 - undecided: not a refusal
        return False
    return (isinstance(current, Mapping) and current.get("l1_handle") == handle
            and current.get("removed_reason") is None)


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
      item is re-read once per reader. Outside a case tool call a resolve gets a gate of
      its own; inside a host tool call in progress (``plx.documents`` gating captured
      originals, ``plx.read`` reading a cited version's current one) every such gate
      draws from that call's one re-read and request bound and shares its re-read
      answers (``CaptureCaseHostTools._call_bound``);
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
        self._projection_reader: tuple[Any, ot.StoreReader] | None = None
        self._projection_lock = threading.Lock()

    def projection(self) -> ot.StoreReader | None:
        """One reader of the projection file ``store_path`` names now, shared by the gates
        (``CaptureCaseHostTools._projected_stale``): reopened when a newer generation is
        published over the path (another inode); None without a projection."""
        from operating.ontology_host import _file_identity
        if self.store_path is None:
            return None
        identity = _file_identity(self.store_path)
        if identity is None:
            return None
        with self._projection_lock:
            held = self._projection_reader
            if held is None or held[0] != identity:
                if held is not None:
                    _close_reader(held[1])
                held = self._projection_reader = (identity,
                                                  ot.StoreReader.open(self.store_path))
            return held[1]

    def close(self) -> None:
        """Close the shared projection reader (the case runtime's ``close`` and a host's
        generation swap call it): an older generation's file is not held open, its disk
        space kept, until the next resolve. The next resolve opens the file anew."""
        with self._projection_lock:
            held, self._projection_reader = self._projection_reader, None
        if held is not None:
            _close_reader(held[1])

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
                                    projection=self.projection, **self.bounds)
        stack.callback(gate.close)
        return gate

    def __call__(self, handle: str, actor: Any) -> dict[str, Any] | None:
        return self._resolve(handle, actor, None)

    def gate_decision(self, handle: str, actor: Any) -> bool | str:
        """The captured-original gate's decision on a SharePoint version
        (``CaseRuntime._captured_original_gate``): True when ``__call__`` serves it as
        the current version, else ``REFUSED`` when this reader's permissions refused a
        version the capture holds (``CaptureCaseHostTools.refusal_kind``) or
        ``FAILED`` for anything else (no capture, an unreadable store, a stale index, a
        superseded version, any error). The same resolve ``__call__`` makes: the kind
        rides beside it, so what a reader is answered never changes."""
        from operating.captured_originals import FAILED, REFUSED
        why: dict[str, str] = {}
        try:
            view = self._resolve(handle, actor, why)
        except Exception:  # noqa: BLE001 - a refusing or failing gate denies
            return REFUSED if why.get("kind") == REFUSED else FAILED
        if isinstance(view, Mapping) and view.get("status") == "current":
            return True
        return REFUSED if why.get("kind") == REFUSED else FAILED

    def _resolve(self, handle: str, actor: Any, why: dict[str, str] | None
                 ) -> dict[str, Any] | None:
        try:
            if handle.startswith("sharepoint://"):
                return self._sharepoint(handle, actor, why)
            if handle.startswith("graph-mail://"):
                return self._mail(handle, actor)
        except (sqlite3.Error, OSError, ValueError, LookupError):
            # An unreadable capture serves nothing, and is not read live in its place.
            if why is not None:
                why.pop("kind", None)
            raise CitationUnresolvable("capture_citation_unresolvable") from None
        return None

    def _sharepoint(self, handle: str, actor: Any, why: dict[str, str] | None = None
                    ) -> dict[str, Any] | None:
        """``why`` (``gate_decision``): gets ``kind``, the gate's refusal kind of a
        version it served nothing of."""
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
            if view is None and why is not None:
                why["kind"] = gate.refusal_kind(handle)
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

    def served_row(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """The row ``plx.read`` serves ``actor`` for a SharePoint version, page or member
        now: the record's fields (``row.<name>``: name, path, size ...) and its captured
        text (``content_text``: the file's, the page's or the member's), through the gate
        ``__call__`` resolves it with (one per reader and call); None when that gate
        serves none or the version is no longer current. For the claim gate's rows only
        (``ontology_tools_investigate.citation_rows``), never a digested view."""
        if self.sharepoint_capture_dir is None or not str(handle).startswith("sharepoint://"):
            return None
        try:
            with self._gate(actor) as gate:
                view = gate.captured(actor, handle, text=True)
        except (sqlite3.Error, OSError, ValueError, LookupError):
            return None
        if view is None or view.get("superseded_at") is not None \
                or not isinstance(view.get("row"), Mapping):
            return None
        return {name: value for name, value in view["row"].items()
                if _ROW_FIELD.match(str(name))}

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
        reading = None
        try:
            store = gate._sharepoint()
            if store.original_dropped(row["content_sha256"]):
                # An original the capture dropped once its text was read: the reading it
                # kept of those bytes (the same reading, so the same row).
                content, reading = None, store.kept_reading(view["l1_handle"])
                if reading is None:
                    raise refused
            else:
                content = store.blob(row["content_sha256"])
        except (sqlite3.Error, OSError, ValueError, LookupError, TypeError):
            raise refused from None
        text = row.get("content_text") if row.get("text_status") == TEXT_OCR else None
        return {"l1_handle": view["l1_handle"], "row": row, "content": content, "text": text,
                "reading": reading}

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
                                    served["row"], served["text"],
                                    reading=served.get("reading"))
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
                from ontology.project import mail_attachment_sender
                with _mail_store(self.runtime, Path(self.mail_capture_dir)) as store:
                    reader = self.runtime.employee_gate(actor)
                    audience = self.runtime.live.audience
                    content = store.attachment_content(handle, reader, audience)
                    # An original the capture dropped once its text was read: the reading
                    # it kept of those bytes (the same reading, so the same row).
                    reading = (store.attachment_reading(handle, reader, audience)
                               if content is None else None)
                    found = store.db.execute("SELECT * FROM attachments WHERE l1_handle=?",
                                             (handle,)).fetchone()
                    if (content is None and reading is None) or found is None:
                        raise refused
                    text = (store.blob(found["text_sha256"]).decode("utf-8")
                            if found["text_status"] == TEXT_OCR else None)
                    whose = mail_attachment_sender(store.db, handle)
                    if whose is None:
                        raise refused
                    row = {**view["row"], **whose}
                return {"l1_handle": handle, "row": row, "content": content, "text": text,
                        "reading": reading}
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
