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
  (1-based, numbered in the whole text; a file read as one part numbered as the file
  is, from the line after its extraction's part marker) for a line-pinned question;
  ``find`` locates a short phrase in the same text (``matches``: at most
  ``MAX_FIND_HITS`` places, each its line, offset and a short snippet served as a
  window's text is, on an original its page citation), so two passages far apart are
  found without paging between them; and every read of a document's text names each
  place that text contradicts itself about what it was given (``_with_conflicts``,
  ``operating.document_conflicts``) as a ``conflicting_sources`` gap with both lines,
  whatever part of it the read returns;
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
  beside the store and shared by every process (``lookup_path``), and never extracts or
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
import concurrent.futures
import contextlib
import contextvars
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import os
from operating.file_modes import keep_shared, make_absent
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import mentions as mn
from ontology.json_contract import validate
from operating import ontology_tools as ot
from operating import ontology_tools_policy
from operating.capture_age import MAX_CAPTURE_AGE, capture_decides
from operating.cases import CitationUnresolvable
from operating.ontology_host import CaseHostTools
from operating.ontology_tools_identity import ToolError, _error, _gap
from operating.ontology_tools_policy import ReadPolicy
from operating.ontology_tools_results import _completeness
from operating.ontology_tools_evidence import (DEFAULT_LIMIT, DEFAULT_READ_CHARS,
                                               PAGING_ARGUMENTS,
                                               CitationReader, EvidenceTools,
                                               full_mentions_kept, held_mentions,
                                               kept_mentions, summarise_mentions,
                                               RuntimeCitations, capped_gaps, covered_pages,
                                               field_value, skipped_gap, wanted_field,
                                               with_pages)
from operating.playbook_originals import extraction_skipped
from operating.document_conflicts import (CONFLICT_REASON, line_of, part_line_base,
                                          scan_conflicts)
from .case_tools_host_rules import (
    _READ_OK, MAX_FIND_HITS, MAX_ATTACHMENT_FINDS, ATTACHMENT_FIND_HITS, FIND_CONTEXT_CHARS,
    _METHOD, _WORD, _SHA, _TIME, _Recording, _text_of, _ROW_FIELD, UNSEARCHED_GATE, _NAME_TOKEN,
    _NAME_PART, MAX_LIVE_REREADS, MAX_LIVE_GETS, LABEL_RESERVE_BYTES, SEARCH_TOOLS, _UNPREPARED,
    PageNotRead, _close_reader, _mail_store)
from .case_tools_host_citations import (
    _CallBound, _SEARCH_HITS, CapturedCitations, _file_priors, _placed, _with_mentions)
from .case_tools_host_index import (
    INDEX_FORMAT, INDEX_FILE, LOOKUP_FILE, LOOKUP_FORMAT, TEXT_NOT_READ, STALE_INDEX, _UNSEARCHED,
    _INDEX_ROWS_SQL, _INDEX_STATE_SQL, _LOOKUP_SCHEMA, _FACTS_CHUNK, _ENTRY_CHUNK, _prune_indexes,
    _index_row, _file_key, _state_key, _valid_entry, _load_index, _store_inode, _store_stamp,
    _LOOKUPS_LOCK, LOOKUP_DIR_PREFIX, _lookup_tmp_root, _sweep_stale_lookup_dirs, _made,
    WARM_EVERY_SECONDS, _WARMING, _WARMING_LOCK)
from .case_tools_host_mailboxes import (
    ATTACHMENT_MAILBOXES_SECONDS, _MAILBOXES, _MAILBOXES_LOCK, _MAILBOXES_READING,
    _cached_mailboxes, _keep_mailboxes, _one_scan, MAILBOXES_WAIT_SECONDS, _mailbox_list,
    _scan_mailboxes, _read_mailboxes, _refresh_mailboxes, prime_attachment_mailboxes,
    _attachment_mailboxes)
from .case_tools_host_gates import _GatesMixin
from .case_tools_host_labels import _LabelsMixin
from .case_tools_host_documents import _DocumentsMixin
from .case_tools_host_calls import _CallsMixin


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
        recording = _Recording(self.evidence.reader or CitationReader(),
                               mark=self.evidence._marked)
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
        result = self._with_conflicts(result, raw, arguments)
        if "find" in arguments:
            found = self._find(result, raw, arguments)
            if not found.get("error") and isinstance(raw.get("attachments"), list) \
                    and raw["attachments"] and "field_path" not in arguments:
                found = self._attachment_finds(found, raw["attachments"], arguments,
                                               actor, budget)
            return self._checked(found)
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

    def _with_conflicts(self, result: dict[str, Any], raw: Mapping[str, Any],
                        arguments: Mapping[str, Any]) -> dict[str, Any]:
        """``result`` with a ``CONFLICT_REASON`` gap for each place the document's whole
        text says it was given an item another of its lines says was not provided
        (``operating.document_conflicts``), whatever part of the text this read returns:
        a whole read, a window or a find. Each names both lines (numbered as ``line``
        reads them) and holds both as source text; a page read, or one field of a row, is
        not the document's text and gets none."""
        text = raw.get("content") if isinstance(raw.get("content"), str) else None
        citation = raw.get("content_citation") or arguments["citation"]
        if text is None or arguments.get("field_path") or "page" in arguments \
                or any(mn.cited_page(cited)[1] is not None
                       for cited in (citation, arguments["citation"])):
            return result
        scan = scan_conflicts(text)
        found = scan["conflicts"]
        gaps = []
        if scan["stopped_at_line"] is not None:
            gaps.append(_gap("unknown", "conflict_scan_partial", (
                f"{citation}: the conflict scan stopped at line {scan['stopped_at_line']} "
                "(its size or time bound)"), (
                "contradictions about what the text was given were looked for only before "
                "that line; read the lines after it (plx.read with text_offset, line or "
                "find) before saying the text holds none")))
        if not gaps and not found:
            return result
        spans = raw.get("content_pages")
        limit = self.limits["max_untrusted_text_chars"]
        for conflict in found:
            held, absent = conflict["held_line"], conflict["absent_line"]
            pages = [covered_pages(citation, spans, conflict[key], conflict[key] + 1)
                     if spans else [] for key in ("held_offset", "absent_offset")]
            where = " and ".join(sorted({on[0]["page_citation"] for on in pages if on}))
            gap = _gap("unknown", CONFLICT_REASON, (
                f"lines {held} and {absent} of {citation}: line {held} says the text was "
                f"built from or given an item that line {absent} says was not provided"),
                (f"the text contradicts itself about what it was given ({where or citation};"
                 " found over its whole text, however much of it this read returned): read "
                 "both lines (plx.read with line), cite both and keep this as an unknown gap "
                 f"with reason {CONFLICT_REASON} naming both lines; never pick one"))
            envelope = ot._envelope(f"line {held}: {conflict['held']}\nline {absent}: "
                                    f"{conflict['absent']}", citation, None, limit)
            if envelope is not None:
                gap["source_text"] = envelope
            gaps.append(gap)
        own = list(result.get("gaps") or [])
        return dict(result, gaps=(own + gaps)[:20])

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"]["plx.read"]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if not self._fits(result):
            result = self._fewer_listed(result)
        if ot._envelope_bytes(result) > self.limits["max_result_bytes"]:
            return _error("result_too_large", "the answer does not fit one response",
                          "page it: text_offset (with field_path for one field) or "
                          "column_offset")
        return result

    def _fewer_listed(self, result: dict[str, Any]) -> dict[str, Any]:
        """A read too large for one response only for what travels with it (a long
        thread's attachments, their find hits) keeps the text and lists as many of them
        as fit, saying how many are left out: the message is never refused whole for its
        attachment list."""
        for key in ("attachment_matches", "attachments"):
            listed = result.get(key)
            if self._fits(result) or not isinstance(listed, list) or not listed:
                continue
            total = len(listed)

            def build(count: int, key: str = key, listed: list = listed,
                      total: int = total) -> dict[str, Any]:
                out = {k: v for k, v in result.items() if k != key}
                if count:
                    out[key] = listed[:count]
                out["gaps"] = [*(result.get("gaps") or [])][:19] + [_gap(
                    "unknown", f"{key}_cut", str(result.get("citation") or key)[:700],
                    f"{count} of {total} {key.replace('_', ' ')} listed; the rest do not "
                    "fit one response: read one by its citation, or find a phrase in one")]
                return out
            low, high = 0, total - 1     # the most that fit
            while low < high:
                middle = (low + high + 1) // 2
                low, high = (middle, high) if self._fits(build(middle)) else (low, middle - 1)
            result = build(low)
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
        # A file read as one part is numbered as the file is (``part_line_base``): its
        # line 1 is the first line after the extraction's part marker.
        numbered = part_line_base(text) if key == "content" else 0
        line = arguments.get("line")
        if line is not None:
            line += numbered
        if line is None:
            offset = arguments["text_offset"]
            if offset > total:
                return _error("invalid_input", f"text_offset beyond the text ({total} chars)")
        elif line > len(starts):
            return _error("invalid_input",
                          f"line beyond the text ({len(starts) - numbered} lines)")
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
                      "first_line": line_of(starts, offset, numbered),
                      "last_line": line_of(starts, max(offset, end - 1), numbered)}
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
        numbered = part_line_base(text) if key == "content" else 0
        _head, page = mn.cited_page(citation)
        spans = raw.get("content_pages") if key == "content" and page is None else None
        limit = self.limits["max_untrusted_text_chars"]
        base = {k: v for k, v in result.items() if k not in ("content", "fields", "pages")}
        base["gaps"] = [gap for gap in base.get("gaps") or ()
                        if gap.get("reason") != "page_citations_cut"]
        hits: list[dict[str, Any]] = []
        total = 0
        for match in phrase.finditer(text):
            line = bisect_right(starts, match.start())
            if line <= numbered:
                # The extraction's display-only part marker is no line of the file: a hit
                # there has no line a read can name (it was reported as line 1).
                continue
            total += 1
            if len(hits) >= MAX_FIND_HITS:
                continue
            end_of_line = starts[line] - 1 if line < len(starts) else len(text)
            low = max(starts[line - 1], match.start() - FIND_CONTEXT_CHARS)
            high = min(max(end_of_line, match.end()), match.end() + FIND_CONTEXT_CHARS)
            on = covered_pages(citation, spans, match.start(), match.start() + 1) \
                if spans else []
            if on:   # the snippet stays on the page it is cited by
                low = max(low, on[0]["offset"])
                high = min(high, on[0]["offset"] + on[0]["chars"])
            cited = on[0]["page_citation"] if on else citation
            hit = {"offset": low, "chars": high - low, "line": line - numbered,
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

    def _attachment_finds(self, found: dict[str, Any], attachments: Sequence[Any],
                          arguments: Mapping[str, Any], actor: Any, budget: Any
                          ) -> dict[str, Any]:
        """A message's ``find`` also looks in what travels with it: the same phrase in
        each attachment whose text was read (``MAX_ATTACHMENT_FINDS``, in the listed
        order), each a ``plx.read`` find of that attachment's citation for the same
        reader, gated again (``attachment_matches``: its citation, status, digest and
        matches, the first ``ATTACHMENT_FIND_HITS`` hits of each). An attachment this
        reader may not read, or with no text, is left out; the attachments not searched
        are counted (``attachments_not_searched``)."""
        listed = [a for a in attachments if isinstance(a, Mapping)
                  and isinstance(a.get("citation"), str) and a.get("text_read")]
        out: list[dict[str, Any]] = []
        for attachment in listed[:MAX_ATTACHMENT_FINDS]:
            got = self._call({"citation": attachment["citation"],
                              "find": arguments["find"]}, actor, budget)
            matches = got.get("matches") if isinstance(got, Mapping) else None
            if got.get("status") not in _READ_OK or not isinstance(matches, Mapping):
                continue
            entry = {"citation": got.get("citation") or attachment["citation"],
                     "status": got["status"],
                     "matches": dict(matches, hits=list(matches.get("hits") or ())[
                         :ATTACHMENT_FIND_HITS])}
            if got.get("digest"):
                entry["digest"] = got["digest"]
            out.append(entry)
        if not out and len(listed) <= MAX_ATTACHMENT_FINDS:
            return found
        shown = dict(found, attachment_matches=out)
        if len(listed) > MAX_ATTACHMENT_FINDS:
            shown["attachments_not_searched"] = len(listed) - MAX_ATTACHMENT_FINDS
        while not self._fits(shown) and shown["attachment_matches"]:
            shown["attachment_matches"] = shown["attachment_matches"][:-1]
            shown["attachments_not_searched"] = shown.get("attachments_not_searched", 0) + 1
        return shown

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
# Bound of the identifier lookup plx.documents reads captured SharePoint text through
# (CaptureCaseHostTools._identifier_lookup): the current captured files it reads (the
# store's own read-back bound). Past it the lookup is cut and an answer from it says so
# (scan_cap). It holds no entries (they are queried from the shared lookup file); a
# question reads at most ``LOOKUP_MATCHES`` files whose text names its identifiers (in
# listing order), and past it the answer is cut the same way.
LOOKUP_FILES = 100_000
LOOKUP_MATCHES = 2_000


class CaptureCaseHostTools(_GatesMixin, _LabelsMixin, _DocumentsMixin, _CallsMixin, CaseHostTools):
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
        # ``warm_sharepoint=False``: no background warm-up thread (a batch reader such as
        # the proactive engine's, whose calls rarely read the file index: a warm-up
        # reading it anew holds the interpreter for minutes beside them); a call that
        # needs the index reads it on its own thread, as before.
        warm = options.pop("warm_sharepoint", True)
        super().__init__(runtime, actor, mail_capture_dir=mail_capture_dir, **options)
        self._projection = projection
        self.sharepoint_capture_dir = (None if sharepoint_capture_dir is None
                                       else Path(sharepoint_capture_dir))
        self.max_live_rereads, self.max_live_gets = max_live_rereads, max_live_gets
        self.max_capture_age = max_capture_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._policy_doubts = 0   # policies decided with an undecidable mailbox check
        if self.sharepoint_capture_dir is not None:
            self.label_reserve = LABEL_RESERVE_BYTES
            if warm:
                warm_sharepoint(self.sharepoint_capture_dir)
        self._sharepoint_store: Any = None
        # The store a documents search's SharePoint lookup reads on its own thread, beside
        # the projected search (``_search_ahead``); handed back to the call's thread after.
        self._lookup_store: Any = None
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
        for opened in (self._sharepoint_store, self._lookup_store):
            if opened is not None:
                opened.close()
        self._sharepoint_store = self._lookup_store = None
        self._lookup = None
        super().close()

    def policy_inputs(self, actor: Any) -> tuple[tuple, tuple] | None:
        """``CaseHostTools.policy_inputs`` and the mail and SharePoint capture stores'
        file states (their mailboxes, drives and recorded ACLs), and how many policies
        were decided with an undecidable mailbox check (one such is never kept)."""
        base = super().policy_inputs(actor)
        if base is None:
            return None
        values, objects = base
        return values + (*self._capture_grant_stamps(), self._policy_doubts), objects

    def policy_now(self, actor: Any) -> ReadPolicy:
        policy = super().policy_now(actor)
        if policy.unrestricted:
            # A full reader (``ontology_host._full_reader_policy``): no mailbox or drive
            # audience is read; the people exclusion is in the policy and the item gates.
            units = dict(policy.units)
            for family in ("mail", "sharepoint"):
                held = [k for k in self._reader_keys if str(k).startswith(family + ":")]
                if held:
                    units[family] = tuple(held)
            return ReadPolicy(policy.tenant, policy.audiences, policy.markings, units,
                              unrestricted=True, owner=policy.owner)
        if policy.staff:
            return self._staff_mail_policy(policy, actor)
        grants = dict(policy.audiences)
        principals = frozenset(actor.principals)
        # A family whose every held unit got a grant decision here is listed (``units``):
        # a reader granted all of them reads the whole family, one granted some is told
        # how many it lacks. A family left undecided (no capture store) is not listed.
        units = dict(policy.units)
        mail_units = [key for key in self._reader_keys if str(key).startswith("mail:")]
        # A record seen through any of several mailboxes (a person: ``people_directory``):
        # granted when one of its mailboxes would be, decided as a ``mail:`` unit is.
        composites = [key for key in self._reader_keys if str(key).startswith("mail-any:")]
        opened = self._mail() if mail_units or composites else None
        if opened is not None:
            stores, audience = opened
            with stores as store:
                check = store.audience_check(audience)
                # A mailbox unit is granted only when the mail item gate can admit its
                # messages at all (the mailbox's audience, the people exclusion, the case
                # mail scope and the employee's item ACL: ``visible_mailboxes``), so a
                # search scoped by this policy never fills its lists with messages every
                # one of which the item gate then refuses. Only ever narrower than the
                # audience check alone; the item gate still decides each message.
                try:
                    # the employee resolved once for every mailbox's check (``holding``),
                    # not once per mailbox: the same entitlement each check resolved
                    holding = getattr(self.runtime.employee_gate, "holding", None)
                    with (holding(actor) if holding is not None else
                          contextlib.nullcontext(self.runtime.employee_gate(actor))) as gated:
                        admitted = RuntimeCitations(
                            self.runtime, mail_capture_dir=self.mail_capture_dir
                        ).visible_mailboxes(store, gated, audience)
                except Exception:  # noqa: BLE001 - undecidable: no mailbox is granted
                    admitted = frozenset()
                    self._policy_doubts += 1   # transient, maybe: never kept
                for unit in mail_units:
                    mailbox = unit.partition(":")[2]
                    grants[unit] = principals if check(actor, mailbox) \
                        and mailbox in admitted else frozenset()
                units["mail"] = tuple(mail_units)
                if composites:
                    from ontology import people_directory
                    try:
                        every = tuple(b.mailbox_id for b in people_directory.mailboxes(store.db))
                    except Exception:  # noqa: BLE001 - an unreadable list decodes nothing
                        every = ()
                    for unit in composites:
                        # a key that no longer decodes (the mailbox list changed) opens
                        # nothing; each mailbox of it is decided as its own unit
                        # (the owner's full-read grant reads them all; the item gate
                        # still decides each person under the exclusion in effect now)
                        named = people_directory.decode_audience(unit, every) or ()
                        grants[unit] = principals if policy.unrestricted and admitted or any(
                            m in admitted and check(actor, m) for m in named) else frozenset()
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
        return ReadPolicy(policy.tenant, grants, policy.markings, units,
                          unrestricted=policy.unrestricted, owner=policy.owner)

    def _staff_mail_policy(self, policy: ReadPolicy, actor: Any) -> ReadPolicy:
        """A staff reader's policy (``ontology_host._staff_policy``) with the mail units
        the capture decides: the reader's own mailbox and every shared mailbox open to all
        employees (``staff_scope.shared_mailboxes``: the capture's recorded purpose, minus
        the restricted list), never one of an excluded person; a ``mail-any:`` key when one
        of its mailboxes is granted. No mailbox audience or item ACL is read per unit; the
        item gate still decides each message (participants, the exclusion)."""
        from .ontology_tools_evidence import people_excluded
        from .staff_scope import shared_mailboxes
        grants = dict(policy.audiences)
        principals = frozenset(actor.principals)
        mail_units = [k for k in self._reader_keys if str(k).startswith("mail:")]
        composites = [k for k in self._reader_keys if str(k).startswith("mail-any:")]
        guard = getattr(self.runtime, "people_exclusion", None)
        usable = guard is not None and not getattr(guard, "unavailable", True)
        if (mail_units or composites) and not usable:
            # no mailbox granted while the exclusion cannot decide: transient, never kept
            self._policy_doubts += 1
        opened = self._mail() if (mail_units or composites) and usable else None
        if opened is not None:
            stores, _audience = opened
            with stores as store:
                shared = shared_mailboxes(store.db)
                every: tuple[str, ...] = ()
                if composites:
                    from ontology import people_directory
                    try:
                        every = tuple(b.mailbox_id for b in people_directory.mailboxes(store.db))
                    except Exception:  # noqa: BLE001 - an unreadable list decodes nothing
                        every = ()
            for unit in mail_units:
                mailbox = str(unit).partition(":")[2].strip().lower()
                if mailbox in shared and not people_excluded(guard, [mailbox]):
                    grants[unit] = principals
            from ontology import people_directory
            # each key decoded once per mailbox list (a pure function of the two): a
            # staff policy decodes thousands of keys on every request (11-18 profile:
            # 6-7 s of one 3-visit staff walk)
            kept = getattr(self, "_decoded_mail_keys", None)
            if kept is None or kept[0] != every:
                kept = (every, {})
                self._decoded_mail_keys = kept
            decoded = kept[1]
            for unit in composites:
                named = decoded.get(unit)
                if named is None:
                    named = decoded[unit] = people_directory.decode_audience(unit, every) or ()
                grants[unit] = principals if any(
                    policy.may_read(actor, "mail:" + m) or (
                        str(m).lower() in shared and not people_excluded(guard, [m]))
                    for m in named) else frozenset()
        return ReadPolicy(policy.tenant, grants, policy.markings, policy.units, staff=True)

    @property
    def tools(self) -> ot.OntologyTools:
        built = self._tools is None
        tools = super().tools
        if built:
            tools.item_gate = self._item_gate(tools.item_gate)
            state = getattr(self.runtime, "state_dir", None)
            if state is not None:
                tools.item_gate_scope = ("capture-host", str(state), str(self.mail_capture_dir),
                                         str(self.sharepoint_capture_dir))
                tools.item_gate_stamp = self._exclusion_stamp
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

    # A record (plx.object) over the response bound: its longest untrusted_text values (an
    # Assembly Order packet's evidence text: 8 KB, some 21 KB as sent) are cut, longest
    # first, to each length in turn, each marked ``truncated``, until the answer fits; one
    # ``result_trimmed`` gap says so and where the whole text is. Every other value, link
    # count, conflict and gap stays. None: it does not fit even so (``result_too_large``).
    TRIM_OBJECT_TEXT_CHARS = (4000, 1000, 240)

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

    def call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        with self.request(), self.per_call(), self._employee_held(actor):
            bound = self._call_bound
            searching = bound.search
            # A search result is matched on the capture's stored, hash-checked text and
            # gated on the permissions recorded at capture: never re-read live (only
            # plx.read of a cited file re-reads it, within the call's bound).
            bound.search = searching or name in SEARCH_TOOLS
            try:
                if name != "plx.documents":
                    return self._capture_call(name, arguments, actor)
                # The store's whole mention lists stay at hand while this call adds the
                # captured files' own (``_with_mentions``).
                with full_mentions_kept():
                    return self._capture_call(name, arguments, actor)
            finally:
                bound.search = searching

    def _captured_call(self, name: str, arguments: Any, actor: Any,
                       early: Any = None, ahead: Any = None) -> dict[str, Any]:
        try:
            # plx.find fits its page to how this host sends it (``_fits``), so the page is
            # never cut here after its cursor was written (``ot.page_fits``).
            with ontology_tools_policy.page_fits(self._fits) if name == "plx.find" else nullcontext():
                result = super().call(name, arguments, actor)
        finally:
            # Taken back before anything else reads its store (whatever the search did).
            searched = _UNPREPARED
            # Not started yet (the pool busy with other readers' lookups): read on this
            # thread below rather than queue behind them.
            if ahead is not None and not ahead.cancel():
                try:
                    searched = ahead.result()
                except Exception:  # noqa: BLE001 - read on this thread below, as before
                    searched = _UNPREPARED
        prepared: Any = _UNPREPARED
        if name in self.registered or not isinstance(result, dict) or "error" in result \
                or name not in self.tools.contracts["tools"] or name == "plx.read":
            return result   # plx.read: its reader labels a captured file itself
        if name in SEARCH_TOOLS and isinstance(arguments, dict):
            result = self._use_case_documents(name, arguments, actor, result)
        if name == "plx.documents" and isinstance(arguments, dict) \
                and self.mail_capture_dir is not None:
            if searched is not _UNPREPARED and self.sharepoint_capture_dir is not None:
                prepared = searched   # read beside the projected search (_search_ahead)
            elif early is not None and self.sharepoint_capture_dir is not None \
                    and "error" not in result:
                # The reader's SharePoint lookup the capture search below reads is read
                # while the early mail scan still runs, not after it.
                prepared = self._captured_search(arguments, actor)
            result = self._newer_mail(arguments, actor, result, early)
        if self.sharepoint_capture_dir is None:
            return self._bounded(name, result) \
                if "use_case" in result or name in SEARCH_TOOLS else result
        if name == "plx.documents" and isinstance(arguments, dict):
            hits = len(result.get("documents") or [])
            result = self._captured_documents(
                arguments, actor, result,
                *(() if prepared is _UNPREPARED else (prepared,)))   # read early
            token = _SEARCH_HITS.set(hits)
            try:
                return self._label(name, result, actor)
            finally:
                _SEARCH_HITS.reset(token)
        return self._label(name, result, actor)

    def _captured_documents(self, arguments: Mapping[str, Any], actor: Any,
                            result: dict[str, Any], prepared: Any = _UNPREPARED
                            ) -> dict[str, Any]:
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
        makes the answer ``scan_cap``. New matching files are gated best first by their
        cheap prior (``_file_priors``) and only one past the room the page has left: the
        rest are never gated, the answer is ``scan_cap`` with its total unknown, and the
        files shown are placed among the projection's page by that prior (``_placed``).
        At most ``UNSEARCHED_GATE`` unsearched files are gated to be counted; past it
        every count is a lower bound ("N+")."""
        from integrations.capture_sharepoint import CaptureError
        from integrations.captured_text import TEXT_EXCEEDS_BOUND
        if prepared is _UNPREPARED:
            # Else ``prepared`` is this call's search read earlier (None: none to make).
            prepared = self._captured_search(arguments, actor)
        if prepared is None:
            return result
        query, wanted, terms, store, audience, gated, lookup = prepared
        documents = list(result["documents"])
        listed = {doc["citation"]: at for at, doc in enumerate(documents)}
        whole_mentions = held_mentions()
        pages = lookup.found(wanted)
        # Past the match bound the lookup's answer is cut (scan_cap), never an absence.
        matches_cut = len(pages) > LOOKUP_MATCHES
        if matches_cut:
            pages = {at: pages[at] for at in sorted(pages)[:LOOKUP_MATCHES]}
        matching: list[tuple[str, dict[str, Any]]] = []
        for at in sorted(pages):
            handle = lookup.handles[at]
            try:
                view = store.resolve(handle, gated, audience,
                                     people=getattr(self.runtime, "people_exclusion", None))
            except (CaptureError, sqlite3.Error, OSError, ValueError):
                continue
            if view is None or not isinstance(view["row"].get("content_text"), str):
                continue
            document = self._mentioning(view, wanted, pages[at])
            if document is not None:
                matching.append((handle, document))
        # A file whose name or folder path holds the query's words (a lot number in a
        # COA's file name, which no grammar reads as an identifier), newest first: the
        # capture's files the projection does not list yet (captured since it was built)
        # are found too. Each still passes the per-item gate below before it is listed.
        texted = {handle for handle, _document in matching}
        matching.extend(sorted(
            ((handle, self._named(handle, name, path, modified))
             for handle, name, path, modified in lookup.named(terms)
             if handle not in listed and handle not in texted),
            key=lambda pair: str(pair[1]["date"] or ""), reverse=True))
        found_handles = {handle for handle, _document in matching}
        # No match on the pages read is never absence from the pages not read.
        unsearched = [(lookup.handles[at], why) for at, why in lookup.unsearched
                      if at not in pages and lookup.handles[at] not in listed
                      and lookup.handles[at] not in found_handles]
        found, capped, over_cap, over_bound, over_budget, cited = [], [], 0, 0, 0, 0
        not_read = text_unread = warming = 0
        room = arguments.get("limit", DEFAULT_LIMIT) - len(documents)
        # The per-item gate (a permission re-read each) runs on what the page can show:
        # the files the projection already listed, then the new ones best first by
        # their cheap prior (``operating.search.authority``: a use-case document, a name
        # naming the query's identifier), one past the room left so a cut is known; the
        # rest are never gated and the answer says it was cut (``scan_cap``).
        prior = _file_priors(query)
        new = sorted((pair for pair in matching if pair[0] not in listed),
                     key=lambda pair: -prior(pair[1]))
        ungated = False
        for handle, document in [pair for pair in matching if pair[0] in listed] + new:
            if handle not in listed and len(found) > max(room, 0):
                ungated = True
                break
            if self.captured(actor, handle) is None:
                over_budget += handle in self._over_budget
                continue
            if handle in listed:
                merged = _with_mentions(documents[listed[handle]], document["mentions"],
                                        whole_mentions.get(handle),
                                        arguments.get("mention_offset", 0))
                if merged is not None:
                    documents[listed[handle]], cited = merged, cited + 1
            else:
                found.append(summarise_mentions(document, arguments.get("mention_offset", 0)))
        # The files whose text was not searched are only counted (each through the gate,
        # so a count never includes a file this reader may not see): at most
        # ``UNSEARCHED_GATE`` are checked; past it every count is a lower bound ("N+").
        unchecked = max(len(unsearched) - UNSEARCHED_GATE, 0)
        for handle, why in unsearched[:UNSEARCHED_GATE]:
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
            elif why == STALE_INDEX:
                warming += 1
            else:
                capped.append(skipped_gap(handle, why))
        if not found and not lookup.truncated and not matches_cut and not over_cap \
                and not over_bound \
                and not capped and not over_budget and not not_read and not text_unread \
                and not ungated and not unchecked and not warming:
            return dict(result, documents=documents) if cited else result
        out = dict(result, documents=_placed(documents, found[:max(room, 0)], prior))
        gaps = [g for g in result["gaps"] if not (found and g["reason"] == "no_match")]
        searched = dict(result["searched"])
        searched["scopes"] = [*searched.get("scopes", []), "capture:sharepoint"][:16]
        cut = len(found) > room or lookup.truncated or ungated or matches_cut
        more = "+" if unchecked else ""
        if cut:
            gaps.append(_gap("unknown", "scan_cap", "capture:sharepoint",
                             "the captured SharePoint files matching this query exceed what "
                             "one answer holds or the scan bound; narrow the query"))
        if over_cap:
            gaps.append(_gap("not_ingested", "content_exceeds_cap", "capture:sharepoint",
                             f"{over_cap}{more} captured SharePoint file(s) you may see are over "
                             "the capture's content cap: their metadata is captured, their "
                             "text is not, so identifiers in them were not searched"))
        if not_read:
            gaps.append(_gap("not_ingested", "content_not_read", "capture:sharepoint",
                             f"{not_read}{more} captured SharePoint file(s) you may see were "
                             "captured metadata-only: their bytes were never read, so "
                             "identifiers in them were not searched"))
        if text_unread:
            gaps.append(_gap("not_ingested", TEXT_NOT_READ, "capture:sharepoint",
                             f"{text_unread}{more} captured SharePoint file(s) you may see have "
                             "no text the capture read whole (none stored: unreadable, "
                             "unsupported or failed at capture, or scanned pages left for "
                             "a later OCR pass), and text is never extracted when a "
                             "question is asked, so identifiers in them were not searched"))
        gaps.extend(capped_gaps(capped, "capture:sharepoint", "captured SharePoint PDF(s)"))
        if over_bound:
            gaps.append(_gap("not_ingested", TEXT_EXCEEDS_BOUND, "capture:sharepoint",
                             f"{over_bound}{more} captured SharePoint file(s) you may see are over "
                             "the text extractor's bound: their bytes are captured, their "
                             "text was never read whole, so identifiers in them were not "
                             "searched"))
        if unchecked and not (over_cap or not_read or text_unread or over_bound or capped or warming):
            gaps.append(_gap("unknown", "unsearched_not_counted", "capture:sharepoint",
                             "more captured SharePoint files whose text was not searched "
                             "for identifiers were not checked or counted for this answer; "
                             "identifiers in them were not searched"))
        if warming:
            gaps.append(_gap("unknown", "index_warming", "capture:sharepoint",
                             f"{warming}{more} captured SharePoint file(s) you may see were "
                             "added or changed since the identifier index last finished and "
                             "it is being rebuilt now, so identifiers in them were not "
                             "searched; ask again shortly"))
        if over_budget:
            gaps.append(_gap("unknown", "live_read_budget", "capture:sharepoint",
                             f"{over_budget} captured SharePoint file(s) were left out of "
                             "this answer unread for it: this call's live re-read bound was "
                             "reached before their permissions could be re-read, and their "
                             "capture is past the host's age bound; ask again in a new call"))
        completeness = dict(result["completeness"])
        completeness["returned"] = len(out["documents"])
        if ungated:
            completeness["total"] = None   # files left ungated: how many are served is unknown
        elif completeness.get("total") is not None:
            completeness["total"] += len(found)
        if cut:
            completeness.update(complete=False, truncated=True, reason="scan_cap")
        elif (over_cap or over_bound or capped or over_budget or not_read or text_unread
              or unchecked or warming) and not completeness.get("truncated"):
            completeness.update(complete=False, reason="partial_scope")
        return dict(out, gaps=gaps[:20], searched=searched, completeness=completeness)

    def _identifier_lookup(self, store: Any, gated: Any, audience: Any
                           ) -> "_IdentifierLookup":
        """Every current captured file this reader may see (``store.index``), looked up in
        the store's shared lookup file (:func:`_current_lookup`: per file, the identifiers its
        stored text names by page under the one grammar, ``mn.file_text_fields`` and
        ``mn.extract``, as :meth:`_mentioning` reads it, and why its text was not searched
        whole). Nothing is extracted or OCR-read here: a file without stored text is
        unsearched (``text_not_read``), never read at query time. Reused while the
        reader, the audience and the capture store are unchanged (SQLite
        ``data_version``: another connection's commit makes a new one); one is held at a
        time, and only when its every listed file's kept entry is current. Bounded by
        ``LOOKUP_FILES`` files (a cut lookup is ``truncated``)."""
        version = store.db.execute("PRAGMA data_version").fetchone()[0]
        held = self._lookup
        same = (held is not None and held.reader == gated and held.audience is audience
                and held.store is store)
        if same and held.version == version and held.generation_current():
            return held   # no commit since
        # A commit: the lookup is the same while this reader's visible files (the
        # index's state) and the store's versions and their content (``content_stamp``)
        # are; a capture listing files again, or re-reading permissions that decide
        # nothing new for this reader, keeps it.
        index = store.index(gated, audience, limit=LOOKUP_FILES)
        stamp = getattr(store, "content_stamp", None)
        state = (index.get("state"), stamp() if callable(stamp) else version)
        if same and index.get("state") is not None and held.state == state \
                and held.generation_current():
            held.version = version
            return held
        lookup = _IdentifierLookup(store, gated, audience, state)
        lookup.version = version
        lookup.truncated = index["truncated"]
        current = True
        if index["items"]:
            shared, current = _current_lookup(store)
            lookup.shared, lookup.generation = shared, shared.generation()
            lookup.handles = [item["l1_handle"] for item in index["items"]]
            lookup.positions = {handle: at for at, handle in enumerate(lookup.handles)}
            for at, stale, why, skipped, searched in _visible_exceptions(
                    shared, index["items"]):
                if stale:
                    # Added or changed since the lookup file was last brought up (another
                    # process is bringing it up now): its text is not searched yet.
                    lookup.unsearched.append((at, STALE_INDEX))
                    lookup.not_current.add(at)
                    current = False
                elif not searched:
                    if why is not None:
                        lookup.unsearched.append((at, why))
                elif skipped is not None:
                    # One gap per file: a page-capped file names its pages not read (the
                    # more specific gap), also when its scanned pages wait for OCR too.
                    lookup.unsearched.append(
                        (at, extraction_skipped({"skipped_by_cap": skipped})))
                elif why is not None:
                    lookup.unsearched.append((at, why))   # scanned pages left for OCR
        if current:
            self._lookup = lookup   # a lookup that is not current is never kept
        else:
            lookup.version = None
        return lookup


class _IdentifierLookup:
    """One reader's identifier lookup over the captured SharePoint files
    (``CaptureCaseHostTools._identifier_lookup``): ``handles``, the files the store lists
    for the reader in its order (``positions``: handle -> position), ``unsearched``
    [(position, "cap" | "not_read" | "bound" | text_not_read | index_warming | the pages
    not read)], ``not_current`` the positions whose kept entry is not current. What a
    file's text or name names is read from the store's shared lookup file (``shared``)
    when asked (``found``, ``named``): nothing reader-independent is held here."""

    def __init__(self, store: Any, reader: Any, audience: Any, state: Any) -> None:
        # ``state``: the reader's visible files and the store's content it was made from;
        # ``version``: the store's commit counter it was last found current at.
        self.store, self.reader, self.audience, self.state = store, reader, audience, state
        self.version: Any = None
        self.shared: _SharedLookup | None = None
        self.handles: list[str] = []
        self.positions: dict[str, int] = {}
        self.unsearched: list[tuple[int, Any]] = []
        self.not_current: set[int] = set()
        self.truncated = False
        self.generation: str | None = None

    def generation_current(self) -> bool:
        """Whether the shared lookup file this lookup was made from still holds that
        build (not made anew since: a broken or foreign file replaced, another store)."""
        return self.shared is None or (self.generation is not None
                                       and self.shared.generation() == self.generation)

    def found(self, wanted: Iterable[tuple[str, str]]) -> dict[int, set[int | None]]:
        """position -> the pages (None: an unpaged file) of each listed file whose kept
        entry is current and whose stored text names one of ``wanted``."""
        pages: dict[int, set[int | None]] = {}
        if self.shared is None or not wanted:
            return pages
        for handle, page in _named_by(self.shared, wanted):
            at = self.positions.get(handle)
            if at is not None and at not in self.not_current:
                pages.setdefault(at, set()).add(page)
        return pages

    def named(self, terms: Sequence[str]) -> list[tuple[str, Any, Any, Any]]:
        """(handle, name, folder path, last modified) of each listed file whose name's
        and folder path's words hold every term (``_named_files``), in listing order."""
        if self.shared is None or not terms:
            return []
        found = [(self.positions[row[0]], row) for row in _named_files(self.shared, terms)
                 if row[0] in self.positions]
        return [row for _at, row in sorted(found, key=lambda pair: pair[0])]


def identifier_index_path(store_root: str | Path, code: str | None = None) -> Path:
    """Where older code kept the JSON identifier index of the SharePoint capture under
    ``store_root`` for index code ``code`` (``_index_code``): a new lookup file of the
    same code is seeded from it."""
    code = _index_code() if code is None else code
    stem, suffix = INDEX_FILE.rsplit(".", 1)
    return Path(store_root) / (INDEX_FILE if code is None else f"{stem}-{code[:16]}.{suffix}")


def lookup_path(store_root: str | Path, code: str | None = None) -> Path:
    """Where the shared lookup file of the SharePoint capture under ``store_root`` (the
    store's own directory, ``SharePointCaptureStore.root``) is kept: beside its database,
    named by the code version that makes it (``_lookup_code``), so processes running
    different code never write each other's file."""
    code = _lookup_code() if code is None else code
    stem, suffix = LOOKUP_FILE.rsplit(".", 1)
    return Path(store_root) / f"{stem}-{(code or 'none')[:16]}.{suffix}"


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


def _read_facts(record: Any, path: Any) -> tuple[Any, Any, Any, frozenset[str], Any]:
    """(name, folder path, last modified, name-and-path words, MIME type) of a version from
    its record and path: what the lookup keeps of it (read once per version)."""
    from integrations.captured_text import file_type
    record = record if isinstance(record, dict) else {}
    name, modified = record.get("name"), record.get("last_modified")
    return (name if isinstance(name, str) else None, path if isinstance(path, str) else None,
            modified if isinstance(modified, str) else None, _name_tokens(name, path),
            file_type(record)[0])


@functools.lru_cache(maxsize=1)
def _facts_code() -> str | None:
    import inspect
    from integrations import captured_text
    try:
        parts = [inspect.getsource(_name_tokens), _NAME_TOKEN.pattern, _NAME_PART.pattern,
                 inspect.getsource(captured_text.file_type),
                 inspect.getsource(captured_text._mime), inspect.getsource(_read_facts)]
    except (OSError, TypeError, AttributeError):
        return None
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=1)
def _lookup_code() -> str | None:
    """A digest of everything a lookup file holds is made by: its format and tables, the
    index code (``_index_code``), the facts code (``_facts_code``) and the code writing
    it (the state read, its keys, the bring-up); None when one
    cannot be read (each process then keeps a private lookup of its own)."""
    import inspect
    index, facts = _index_code(), _facts_code()
    if index is None or facts is None:
        return None
    try:
        writers = [inspect.getsource(item) for item in (
            _state_key, _store_stamp, _sync_lookup, _sync_files, _sync_entries)]
    except (OSError, TypeError):
        return None
    return hashlib.sha256("\x00".join(
        (LOOKUP_FORMAT, *_LOOKUP_SCHEMA, _INDEX_STATE_SQL, index, facts, *writers)
    ).encode("utf-8")).hexdigest()


class _SharedLookup:
    """The lookup file of one capture store in this process: thread-local read
    connections (never writing), and the one writer (``sync``) under the file's lock."""

    def __init__(self, path: Path, code: str | None, private: bool = False) -> None:
        self.path, self.code, self.private = path, code, private
        self.lock_path = Path(f"{path}.lock")
        self._local = threading.local()

    def _make(self) -> None:
        """The file and its lock file, owner-only, made when absent."""
        for target in (self.path, self.lock_path):
            # never opened when it exists: called before every read, and closing a
            # descriptor of the file dropped the SQLite locks of this process's other
            # readers and its writer (``make_absent``)
            make_absent(target, 0o600)
            keep_shared(target, 0o600)   # before SQLite makes -wal/-shm from its mode

    def reader(self) -> sqlite3.Connection:
        """This thread's read connection (``query_only``: it never writes the file), opened
        again when the file was made anew since (``_sync_lookup`` replacing a broken one)."""
        self._make()
        inode = os.stat(self.path).st_ino
        held = getattr(self._local, "conn", None)
        if held is not None and held[1] == inode:
            return held[0]
        if held is not None:
            held[0].close()
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA query_only = 1")
        self._local.conn = (conn, inode)
        return conn

    def read(self, work: Any) -> Any:
        """``work(conn)`` on this thread's read connection; a connection that reads the
        file as broken ('database disk image is malformed' while a fresh connection reads
        it whole: measured on the live capture, the thread's connection opened before the
        file was brought up) is closed and the read made once more on a fresh one."""
        try:
            return work(self.reader())
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError):
                raise
            held = getattr(self._local, "conn", None)
            if held is not None:
                held[0].close()
                self._local.conn = None
            return work(self.reader())

    def stamp(self) -> str | None:
        """The store state this file was last brought up to (None: never, or not this
        code's file)."""
        try:
            kept = dict(self.read(lambda conn: conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('format', 'code', 'stamp')"
            ).fetchall()))
        except sqlite3.Error:
            return None
        if kept.get("format") != LOOKUP_FORMAT or kept.get("code") != (self.code or ""):
            return None
        return kept.get("stamp")

    def generation(self) -> str | None:
        """The build of the file (made anew when its tables are reset), or None."""
        try:
            row = self.read(lambda conn: conn.execute(
                "SELECT value FROM meta WHERE key = 'generation'").fetchone())
        except sqlite3.Error:
            return None
        return None if row is None else row[0]

    @contextmanager
    def locked(self, wait: bool | float) -> Iterator[bool]:
        """Hold the file's write lock (across processes and threads: one descriptor per
        holder); yields False when another holder has it and ``wait`` is false, or still
        has it after ``wait`` seconds (True: wait as long as it takes)."""
        import fcntl
        self._make()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        keep_shared(self.lock_path, 0o600)
        try:
            if wait is True:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                until = time.monotonic() + float(wait or 0)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= until:
                            yield False
                            return
                        time.sleep(0.2)
            yield True
        finally:
            os.close(fd)   # releases the lock

    def sync(self, store: Any, *, wait: bool | float) -> bool:
        """Bring the file up to ``store``'s state (``_sync_lookup``). False when another
        process or thread is bringing it up now (``locked``'s ``wait``)."""
        with self.locked(wait) as held:
            if not held:
                return False
            try:
                _sync_lookup(store, self)
            except sqlite3.DatabaseError as exc:
                # A file whose pages are broken (SQLITE_CORRUPT: "database disk image is
                # malformed", found by the bring-up, not on open): made anew here, under
                # the lock, once for every holder. Kept as it was, every process read
                # into a private lookup of its own, the whole capture's text read again
                # per process (live 11-18: minutes per fresh session, every session).
                if isinstance(exc, sqlite3.OperationalError):
                    raise   # locked, I/O, interrupted: the file is left as it is
                _set_aside(self.path)
                _sync_lookup(store, self)
            return True


# How long a question waits for another process's first build of the lookup file: well
# inside a search's own time limit (the Ask's plx.search: 40 s). A first build takes
# minutes on the live capture; past this wait the question lists what the store holds and
# says each file's text is not searched yet (``STALE_INDEX``), never runs out its limit.
FIRST_BUILD_WAIT_SECONDS = 5.0
_LOOKUPS: dict[tuple[str, str | None, bool], _SharedLookup] = {}
_PRIVATE_DIRS: list[str] = []


def _remove_private_dirs() -> None:
    import shutil
    for directory in _PRIVATE_DIRS:
        shutil.rmtree(directory, ignore_errors=True)


def _on_terminate(signum: int, frame: Any) -> None:
    _remove_private_dirs()
    raise SystemExit(128 + signum)


def _private_lookup(store: Any) -> _SharedLookup:
    """A lookup file of this process alone (in a private temporary directory removed at
    exit): for a store without a directory, code whose sources cannot be read, or a
    shared file this process cannot write."""
    import atexit
    import shutil
    import tempfile
    key = (str(getattr(store, "path", "") or id(store)), None, True)
    with _LOOKUPS_LOCK:
        held = _LOOKUPS.get(key)
        if held is None:
            root = _lookup_tmp_root()
            if not _PRIVATE_DIRS:
                _sweep_stale_lookup_dirs([root, tempfile.gettempdir()])
                atexit.register(_remove_private_dirs)
                # a SIGTERM (a timeout, a stop) ends the process without atexit: remove
                # the directory first, where nothing else handles the signal
                import signal
                if threading.current_thread() is threading.main_thread() and \
                        signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
                    signal.signal(signal.SIGTERM, _on_terminate)
            directory = tempfile.mkdtemp(prefix=f"{LOOKUP_DIR_PREFIX}{os.getpid()}-",
                                         dir=root)
            _PRIVATE_DIRS.append(directory)
            held = _LOOKUPS[key] = _SharedLookup(Path(directory) / LOOKUP_FILE, None,
                                                 private=True)
        return held


def _shared_lookup(store: Any) -> _SharedLookup:
    """The lookup file of ``store`` for this code: beside the store, shared by every
    process (``lookup_path``), else this process's own (``_private_lookup``)."""
    root, code = getattr(store, "root", None), _lookup_code()
    if not isinstance(root, (str, Path)) or code is None or not Path(root).is_dir():
        return _private_lookup(store)
    path = lookup_path(root, code)
    key = (str(path), code, False)
    with _LOOKUPS_LOCK:
        held = _LOOKUPS.get(key)
        if held is None:
            held = _LOOKUPS[key] = _SharedLookup(path, code)
        return held


def _current_lookup(store: Any) -> tuple[_SharedLookup, bool]:
    """(lookup, current) for a question: the store's lookup file brought up to its state
    now, or, while another holder brings it up, as last committed (``current`` False: a
    file added or changed since is not searched yet, ``STALE_INDEX``). A file never
    brought up is waited for. A shared file this process cannot read or write is
    replaced by a private one (read anew from the stored text)."""
    lookup = _shared_lookup(store)
    try:
        return lookup, _bring_up(store, lookup)
    except (sqlite3.Error, OSError):
        if lookup.private:
            raise
    private = _private_lookup(store)
    return private, _bring_up(store, private)


def _bring_up(store: Any, lookup: _SharedLookup) -> bool:
    stamp = _store_stamp(store)
    kept = lookup.stamp()
    if kept == stamp:
        return True
    # A file never brought up is waited for, at most FIRST_BUILD_WAIT_SECONDS: past it the
    # question's listed files are all not searched yet (``STALE_INDEX``).
    if lookup.sync(store, wait=FIRST_BUILD_WAIT_SECONDS if kept is None else False):
        return lookup.stamp() == stamp
    return False


def _sync_lookup(store: Any, lookup: _SharedLookup) -> None:
    """Bring ``lookup``'s file up to ``store``'s current files (under its lock): the
    versions no longer current are dropped, each new version's record is read once
    (``_read_facts``), each version whose content or text state changed is keyed again
    (``_state_key``), and each entry key no file has yet is read from the stored text
    (``_file_entry``; from the JSON index older code of the same index code kept, when
    it holds it). One transaction: a reader sees the file as it was before or after the
    whole bring-up, never a file keyed anew without its entry. The store state recorded
    is the one read first: one read meanwhile is a newer state the next bring-up reads.

    Committed in steps, so a bring-up cut short keeps what it read: the files (their
    names, tokens and state keys), then each ``_ENTRY_CHUNK`` of entries, then the stamp.
    A reader decides per file (``_visible_exceptions``: a file whose entry is absent or
    kept for another state is not searched yet, ``STALE_INDEX``), so a file keyed before
    its entry is written is said not searched, never searched as empty; the stamp (the
    whole file current) is written last. Live 11-18: a first build of the whole capture
    in one transaction took 11-25 min, longer than the short-lived Ask hosts that held its
    lock lived; each was rolled back with its host and the file was never made, so every
    new session's SharePoint search waited for it past its time limit."""
    import secrets
    stamp, inode = _store_stamp(store), _store_inode(store)
    conn = _lookup_writer(lookup)
    conn.isolation_level = None   # the transaction is this function's own
    try:
        kept = dict(conn.execute("SELECT key, value FROM meta"))
        if kept.get("stamp") == stamp and kept.get("inode") == str(inode) \
                and kept.get("format") == LOOKUP_FORMAT and kept.get("code") == (lookup.code or ""):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            if kept.get("inode") != str(inode) or kept.get("format") != LOOKUP_FORMAT \
                    or kept.get("code") != (lookup.code or ""):
                # another store made anew, or a file another code wrote: nothing kept is
                # used (a version id or entry key of another store names another file); a
                # new generation tells every held lookup its file was made anew
                for table in ("meta", "files", "entries", "named", "tokens"):
                    conn.execute(f"DELETE FROM {table}")
                conn.executemany("INSERT INTO meta VALUES (?, ?)", (
                    ("format", LOOKUP_FORMAT), ("code", lookup.code or ""),
                    ("inode", str(inode)), ("generation", secrets.token_hex(8))))
            _sync_files(store, conn)
            conn.execute("COMMIT")
            _sync_entries(store, conn, lookup)   # a transaction per chunk
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM entries WHERE key NOT IN (SELECT key FROM files "
                         "WHERE key IS NOT NULL)")
            conn.execute("DELETE FROM named WHERE key NOT IN (SELECT key FROM entries)")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)", (stamp,))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:   # SQLite may have rolled it back itself (I/O)
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    if not lookup.private:
        _prune_indexes(lookup.path)


def _set_aside(path: Path) -> None:
    """A broken lookup file (and its -wal/-shm) removed, so the next bring-up makes it
    anew; its rows are a cache of the stored text (nothing of it is lost)."""
    for item in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            item.unlink()
        except FileNotFoundError:
            pass


def _lookup_writer(lookup: _SharedLookup) -> sqlite3.Connection:
    """A write connection to ``lookup``'s file (under its lock) with its tables; a file
    that is not a readable lookup database is made anew (nothing of it is used)."""
    for attempt in (0, 1):
        lookup._make()
        conn = sqlite3.connect(lookup.path, timeout=60)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            for ddl in _LOOKUP_SCHEMA:
                conn.execute(ddl)
            conn.execute("SELECT key, value FROM meta LIMIT 1").fetchall()
            return conn
        except sqlite3.DatabaseError as exc:
            conn.close()
            if attempt or isinstance(exc, sqlite3.OperationalError):
                raise   # locked, I/O, unopenable: the file is left as it is
            _set_aside(lookup.path)
    raise AssertionError("unreachable")


def _sync_files(store: Any, conn: sqlite3.Connection) -> None:
    """The store's current versions into ``files`` (in the caller's transaction)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS cur (vid INTEGER PRIMARY KEY, handle TEXT,"
                 " cs TEXT, csha TEXT, ts TEXT, tsha TEXT, rec TEXT)")
    conn.execute("DELETE FROM temp.cur")
    cursor = store.db.execute(_INDEX_STATE_SQL)
    while True:
        rows = cursor.fetchmany(_FACTS_CHUNK)
        if not rows:
            break
        conn.executemany("INSERT OR REPLACE INTO temp.cur VALUES (?,?,?,?,?,?,?)",
                         [tuple(row) for row in rows])
    # Gone, or the id now names another version (a store made anew): its facts go.
    conn.execute("DELETE FROM tokens WHERE vid IN (SELECT f.vid FROM files f LEFT JOIN "
                 "temp.cur c ON c.vid = f.vid WHERE c.vid IS NULL OR c.handle IS NOT f.handle)")
    conn.execute("DELETE FROM files WHERE vid IN (SELECT f.vid FROM files f LEFT JOIN "
                 "temp.cur c ON c.vid = f.vid WHERE c.vid IS NULL OR c.handle IS NOT f.handle)")
    new = [row[0] for row in conn.execute(
        "SELECT vid FROM temp.cur WHERE vid NOT IN (SELECT vid FROM files) ORDER BY vid")]
    for at in range(0, len(new), _FACTS_CHUNK):
        chunk = new[at:at + _FACTS_CHUNK]
        files, tokens = [], []
        for row in store.db.execute(
                "SELECT version_id, path, record, l1_handle FROM items WHERE version_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(chunk),)):
            try:
                record = json.loads(row["record"])
            except (TypeError, ValueError):
                record = {}
            name, path, modified, words, mime = _read_facts(record, row["path"])
            files.append((row["version_id"], row["l1_handle"], name, path, modified, mime))
            tokens.extend((word, row["version_id"]) for word in words)
        conn.executemany("INSERT OR REPLACE INTO files (vid, handle, name, path, modified, "
                         "mime) VALUES (?,?,?,?,?,?)", files)
        conn.executemany("INSERT OR IGNORE INTO tokens VALUES (?, ?)", tokens)
    # Each version whose state is new or changed is keyed by its state now.
    changed = conn.execute(
        "SELECT c.vid, c.cs, c.csha, c.ts, c.tsha, c.rec, f.name, f.mime FROM temp.cur c "
        "JOIN files f ON f.vid = c.vid WHERE f.key IS NULL OR f.cs IS NOT c.cs OR "
        "f.csha IS NOT c.csha OR f.ts IS NOT c.ts OR f.tsha IS NOT c.tsha "
        "OR f.rec IS NOT c.rec").fetchall()
    conn.executemany(
        "UPDATE files SET cs = ?, csha = ?, ts = ?, tsha = ?, rec = ?, key = ? WHERE vid = ?",
        [(cs, csha, ts, tsha, rec, _state_key(
            (name, None, None, None, mime),
            {"content_status": cs, "content_sha256": csha, "text_status": ts,
             "text_sha256": tsha}), vid)
         for vid, cs, csha, ts, tsha, rec, name, mime in changed])
    conn.execute("DELETE FROM temp.cur")


def _sync_entries(store: Any, conn: sqlite3.Connection, lookup: _SharedLookup) -> None:
    """An entry for every file key ``files`` holds without one (in the caller's
    transaction), read per ``_ENTRY_CHUNK`` files: from older code's JSON index of the same index code when it holds
    the key (checked as that code checked it: ``_load_index``), else from the stored
    text as read now (the file's key is its state as read now, if it changed since)."""
    missing = conn.execute(
        "SELECT handle, key FROM files WHERE key NOT IN (SELECT key FROM entries) "
        "ORDER BY vid").fetchall()
    if not missing:
        return
    seeded: dict[str, Any] = {}
    code = _index_code()
    if not lookup.private and code is not None:
        root = lookup.path.parent
        seeded = _load_index(identifier_index_path(root, code), code) or {}
    pool = _entry_pool(len(missing))
    try:
        _sync_entry_chunks(store, conn, missing, seeded, pool)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)


# A bring-up reading this many files' text or more reads them in worker processes (the
# mention reading is CPU-bound: one thread read the whole capture in tens of minutes, past
# the life of the host that started it, so its one transaction was rolled back and the
# next host began again: live 11-18). Spawned, never forked (the host runs threads).
ENTRY_POOL_MIN_FILES = 200
ENTRY_POOL_MAX_WORKERS = 32


def _spawn_safe_main() -> bool:
    """Whether a spawned worker starts without running this process's own work again.
    Spawn makes each worker import the parent's ``__main__``: a module run with ``-m``
    (every host: ``operating.host``, ``operator_api``) is imported as ``__mp_main__``,
    so its main guard holds; ``-c`` and an interactive session have no file to import. A
    script run by path may have no guard: each worker would run it again, here a
    bring-up waiting on the lock its own parent holds, for ever (live 11-18, an unguarded
    builder script: 16 workers parked on the lock, the parent waiting on them)."""
    import sys
    main = sys.modules.get("__main__")
    if main is None or getattr(getattr(main, "__spec__", None), "name", None):
        return True
    return getattr(main, "__file__", None) is None


def _entry_pool(files: int) -> Any:
    """A process pool for ``files`` entries to read (None: read here: few files, one
    core, or a ``__main__`` a spawned worker would run again, ``_spawn_safe_main``)."""
    if files < ENTRY_POOL_MIN_FILES or not _spawn_safe_main():
        return None
    import multiprocessing
    workers = min(ENTRY_POOL_MAX_WORKERS, os.cpu_count() or 1)   # every core: one run per deploy
    if workers < 2:
        return None
    try:
        from ontology import parent_watch
        # a host killed mid-build (a short-lived Ask host) takes its readers with it
        return concurrent.futures.ProcessPoolExecutor(
            workers, mp_context=multiprocessing.get_context("spawn"),
            **parent_watch.pool_init())
    except (OSError, ValueError):
        return None


def _sync_entry_chunks(store: Any, conn: sqlite3.Connection, missing: Sequence[Any],
                       seeded: Mapping[str, Any], pool: Any) -> None:
    for at in range(0, len(missing), _ENTRY_CHUNK):
        chunk = missing[at:at + _ENTRY_CHUNK]
        made: dict[str, dict[str, Any]] = {}
        rekeyed: list[tuple[str, str, str, str, str, str]] = []
        unread = [handle for handle, key in chunk if key not in seeded]
        rows = {}
        if unread:
            rows = {row["l1_handle"]: row for row in store.db.execute(
                _INDEX_ROWS_SQL + " AND l1_handle IN (SELECT value FROM json_each(?))",
                (json.dumps(unread),)).fetchall()}
        for handle, key in chunk:
            if key in seeded:
                made.setdefault(key, seeded[key])
                continue
            row = rows.get(handle)
            if row is None:
                continue   # superseded since: the next bring-up drops it
            entry_row = _index_row(row)
            now_key = _file_key(entry_row)
            if now_key != key:
                rekeyed.append((row["content_status"], row["content_sha256"],
                                row["text_status"], row["text_sha256"], now_key, handle))
            if now_key not in made:
                made[now_key] = (handle, entry_row)
        reads = {key: v for key, v in made.items() if isinstance(v, tuple)}
        if reads:
            keys = list(reads)
            if pool is not None:
                got = pool.map(_file_entry, [store.blobs] * len(keys),
                               [reads[k][0] for k in keys], [reads[k][1] for k in keys],
                               chunksize=max(1, len(keys) // 64))
            else:
                got = (_file_entry(store.blobs, *reads[k]) for k in keys)
            made.update(zip(keys, got))
        # each chunk its own transaction (``_sync_lookup``): kept if the bring-up is cut
        conn.execute("BEGIN IMMEDIATE")
        # A changed state keys the file anew; its version's ``rec`` is cleared, so a
        # lookup made before the next bring-up takes it as not current.
        conn.executemany("UPDATE files SET cs = ?, csha = ?, ts = ?, tsha = ?, key = ?, "
                         "rec = NULL WHERE handle = ?", rekeyed)
        for key, entry in made.items():
            if conn.execute("SELECT 1 FROM entries WHERE key = ?", (key,)).fetchone():
                continue
            conn.execute("INSERT INTO entries VALUES (?, ?, ?, ?)", (
                key, entry["named"] is not None, entry["why"],
                None if entry["skipped"] is None else json.dumps(entry["skipped"])))
            conn.executemany("INSERT INTO named VALUES (?, ?, ?, ?, ?)", [
                (key, kind, normalized, page, at)
                for at, (kind, normalized, page) in enumerate(entry["named"] or ())])
        conn.execute("COMMIT")


def _visible_exceptions(lookup: _SharedLookup, items: Sequence[Mapping[str, Any]]
                        ) -> list[tuple[int, bool, Any, Any, bool]]:
    """(position, not current, why, pages skipped, searched) of each listed file whose
    lookup entry is not current (absent, or kept for another content hash or capture
    time than the listing's: ``STALE_INDEX``) or whose text was not searched whole, in
    listing order; every other listed file's entry is current and searched whole."""
    unread = [(at, True, None, None, False) for at in range(len(items))]
    listed = json.dumps([[item.get("l1_handle"), item.get("content_sha256"),
                          item.get("recorded_at")] for item in items])

    def work(conn: sqlite3.Connection) -> Any:
        if not _made(conn):
            return None
        return conn.execute(
            "SELECT CAST(j.key AS INTEGER), (f.vid IS NULL OR e.key IS NULL OR "
            "f.csha IS NOT json_extract(j.value, '$[1]') OR "
            "f.rec IS NOT json_extract(j.value, '$[2]')) AS stale, e.why, e.skipped, "
            "e.searched FROM json_each(?) j LEFT JOIN files f ON f.handle = "
            "json_extract(j.value, '$[0]') LEFT JOIN entries e ON e.key = f.key "
            "WHERE stale OR e.why IS NOT NULL OR e.skipped IS NOT NULL OR NOT e.searched "
            "ORDER BY 1", (listed,)).fetchall()
    try:
        rows = lookup.read(work)
    except sqlite3.Error:
        # unreadable now even on a fresh connection: every file's text is not searched
        # yet (``STALE_INDEX``), the search answers from the rest, never fails whole
        return unread
    if rows is None:
        return unread
    return [(at, bool(stale), why, None if skipped is None else json.loads(skipped),
             bool(searched)) for at, stale, why, skipped, searched in rows]


def _named_by(lookup: _SharedLookup, wanted: Iterable[tuple[str, str]]
              ) -> list[tuple[str, int | None]]:
    """(handle, page) of each current file whose stored text names one of ``wanted``."""
    def work(conn: sqlite3.Connection) -> list[tuple[str, int | None]]:
        out: list[tuple[str, int | None]] = []
        if not _made(conn):
            return out
        for kind, normalized in sorted(wanted):
            out.extend(conn.execute(
                "SELECT f.handle, n.page FROM named n JOIN files f ON f.key = n.key "
                "WHERE n.kind = ? AND n.normalized = ?", (kind, str(normalized))))
        return out
    try:
        return lookup.read(work)
    except sqlite3.Error:
        return []   # unreadable now: no file is found by what it names


def _named_files(lookup: _SharedLookup, terms: Sequence[str]
                 ) -> list[tuple[str, Any, Any, Any]]:
    """(handle, name, path, last modified) of each current file whose name's and folder
    path's words hold every term: a term with a digit as a whole word (lot 240306 is not
    1240306), a word as a word's start (spec: specification)."""
    if not terms:
        return []
    try:
        return lookup.read(lambda conn: _named_files_in(conn, terms))
    except sqlite3.Error:
        return []   # unreadable now: no file is found by its name


def _named_files_in(conn: sqlite3.Connection, terms: Sequence[str]
                    ) -> list[tuple[str, Any, Any, Any]]:
    if not _made(conn):
        return []
    held: set[int] | None = None
    for term in terms:
        if any(c.isdigit() for c in term):
            found = {row[0] for row in conn.execute(
                "SELECT vid FROM tokens WHERE token = ?", (term,))}
        else:
            found = {row[0] for row in conn.execute(
                "SELECT vid FROM tokens WHERE token >= ? AND token < ?",
                (term, term + chr(0x10FFFF)))}
        held = found if held is None else held & found
        if not held:
            return []
    out: list[tuple[str, Any, Any, Any]] = []
    vids = sorted(held or ())
    for at in range(0, len(vids), _FACTS_CHUNK):
        out.extend(conn.execute(
            "SELECT handle, name, path, modified FROM files WHERE vid IN "
            "(SELECT value FROM json_each(?))", (json.dumps(vids[at:at + _FACTS_CHUNK]),)))
    return out


def warm_sharepoint(capture_dir: str | Path, *, every: float | None = WARM_EVERY_SECONDS,
                    stop: threading.Event | None = None) -> threading.Thread | None:
    """Start (once per directory and process) the daemon thread bringing the lookup file of
    the SharePoint capture under ``capture_dir`` up to the store's state now, then every
    ``every`` seconds (None: once). Reader-independent and visibility-free: nothing it
    keeps is shown to anyone; a reader's lookup still takes only the files the store lists
    for that reader. Any failure ends the thread quietly (the question then brings it up
    itself, as before). The thread, or None when the store does not exist or one already
    runs."""
    from integrations.capture_sharepoint import SharePointCaptureStore, store_exists
    key = str(Path(capture_dir).expanduser())
    if not store_exists(Path(key)):
        return None

    def run() -> None:
        try:
            # the document converter a question's Freightcom full-text check may need
            # (a file without stored text is read at query time): imported here, off the
            # question's path (live 11-18: ~19% of a cold SharePoint search's time)
            from integrations import doc_convert
            doc_convert.installed_backend()
        except Exception:  # noqa: BLE001 - not installed: the question reads without it
            pass
        try:
            store = SharePointCaptureStore.open_read_only(Path(key))
            try:
                while store_exists(Path(key)):   # a capture removed ends it
                    lookup = _shared_lookup(store)
                    if lookup.stamp() != _store_stamp(store):
                        lookup.sync(store, wait=True)
                    if every is None or (stop.wait(every) if stop is not None
                                         else time.sleep(every)):
                        return
            finally:
                close = getattr(store, "close", None)
                if callable(close):
                    close()
        except Exception:  # noqa: BLE001 - a warm-up never fails a host
            return
        finally:
            with _WARMING_LOCK:
                if _WARMING.get(key) is threading.current_thread():
                    _WARMING.pop(key, None)
    with _WARMING_LOCK:
        if key in _WARMING:
            return None
        thread = _WARMING[key] = threading.Thread(target=run, name="sharepoint-warm",
                                                  daemon=True)
    thread.start()
    return thread


def _name_tokens(*texts: Any) -> frozenset[str]:
    """The folded words of a file's name and path: each letters-and-digits run and its
    letter and digit runs (``LOT240306`` holds ``lot240306``, ``lot`` and ``240306``)."""
    out: set[str] = set()
    for text in texts:
        if isinstance(text, str):
            for word in _NAME_TOKEN.findall(text.casefold()):
                out.add(word)
                out.update(_NAME_PART.findall(word))
    return frozenset(out)


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
                    whose = mail_attachment_sender(
                        store.db, handle, mailboxes=_attachment_mailboxes(store))
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
