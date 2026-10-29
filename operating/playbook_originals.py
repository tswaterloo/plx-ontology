"""Integrity and RECORDED document kind of the ``original://`` documents a case cites.

A playbook role that means "the freight quote" is filled only by an original whose kind a
**recorded source** states for that exact original. Text never decides a kind: a title, a
number or a phrase cannot tell a document from a reference line on another document (five
review rounds of keyword rules failed on exactly that), so text is used only to *contradict*.

``check_case_originals`` is called by ``operating.playbook_eval.evaluate`` with the
evaluator's own read-only reader, so every read runs with the evaluating session's permission
(the store's audience check and byte re-hash on each read). A refused read (rate limit, tool
error, source outage) is never a check result: the reader raises and the evaluation is
incomplete (``playbook_eval``).

1. **Integrity (every playbook).** Every ``original://`` handle the case cited is re-read
   through ``plx.source_citation``; the content-stable view the case digested at citation time
   is rebuilt from the response and its digest must equal every digest the case recorded. A
   failed read, an unrecorded digest, a view that cannot be rebuilt exactly or a different
   digest is a gap. Citations shown to this reader without a handle may be originals: the gap
   ``cited_evidence_withheld_from_reader`` (a count, no handle). When the playbook declares no
   original role nothing else is read.
2. **Recorded kind.** Two sources, nothing else:

   a. **SharePoint document-type columns** of the item the original's bytes were read from
      (provenance ``sharepoint-drive``), read through the GET-only listItem read
      (``integrations.graph_read.SharePointReader.list_item_fields``) for the evaluating actor
      (``document_fields``). Only ``KIND_COLUMNS`` are read; the value tables
      (``operating.portal_kind_columns``) mirror the Portal exactly (``PORTAL_SOURCES``) and
      map a value to a kind only where the Portal
      category names that document. The column must describe the same item (drive, item) and
      the same content (the attach-time eTag, or the attach-time content tag ``cTag``, which a
      column edit does not change; hashes must agree where both sides carry them), else
      ``document_type_item_changed``. An item this reader may not see:
      ``document_type_not_visible``. A host without the drive's reader: the column is
      ``not_read`` (a report limit, never a fill).
   b. **A Workbench confirmation** (``operating.kind_confirmation``): a person answered the
      ``plx.document_fields`` form (MCP elicitation, or a console form) for exactly
      this original in their own client, choosing its Workbench document kind and/or its
      Portal-taxonomy columns. ``kind_confirmations(original_id)`` (``KindConfirmations``)
      returns that confirmation only while the original still reads, for the evaluating
      reader, as the same bytes; it counts only when it names this original and its SHA-256.
      A confirmed document kind establishes that kind; confirmed ``KIND_COLUMNS`` read through
      the same Portal tables as a live column. Its source label is
      ``workbench_confirmation``, never ``sharepoint_column``. A case finding never confirms a
      kind: the former finding statement ("Confirmed document kind: ...") was copyable by any
      agent and is retired (it is now ordinary text with no effect).

   No recorded kind: ``role_unverifiable``. Two recorded kinds (e.g. a live column and a
   confirmation that disagree), a column value of another document class, or a column value
   the Portal taxonomy does not define:
   ``document_kind_ambiguous``.
3. **Contradiction scan (text only contradicts).** An original with one recorded kind is read
   whole within one per-evaluation page budget (``PAGE_READ_BUDGET``); text that cannot be read
   whole (an extraction with ``skipped_by_cap``, or one stored before that field with only its
   ``pdf_pages_skipped_by_cap`` warning, included: its pages are not the whole file) is
   ``document_text_incomplete``. A *title line* of another document class (a line that
   is the title, optionally followed by its own number: "PROFORMA INVOICE No. PI-1", "INVOICE
   No. SWC024999", a line that is a PI number) makes it ``document_kind_ambiguous``. Reference
   lines ("Re: your PURCHASE ORDER 5447", "Assembly Order: AO-26005B", "Against PI-...") are not
   titles and neither contradict nor support. Nothing in the text ever supports a kind.
4. **Roles.** ``ROLE_DOCUMENTS`` names the kind each ``original`` role means. A role is filled
   only by a cited original whose recorded kind is that kind, with no contradiction. A required
   role left unfilled is ``role_not_filled``; a role naming no known kind is
   ``role_document_undeclared``; an original whose recorded kind no role declares is
   ``original_fills_no_role``.

Agent identities: whole-document reads never go to an agent identity
(``OriginalStore.read_pdf``); when the evaluating principals are all agent principals no
original is read and each is ``original_not_visible_to_agent``.

A recorded kind says what the document is, not that it is this transaction's document
(identity stays the evaluator's ``originals_identity_not_checked`` limit). A Workbench
confirmation requires a session holding one employee principal (a host-configured label, not
PLX authentication) whose MCP client declared elicitation; no case tool or finding records one.
The Workbench cannot prove that a person answered the form: hosts must declare elicitation only
for clients that show the form to a person (``operating.kind_confirmation``). The stored report
shows which
source filled a role (``kind_sources``), never the confirming principal
(``public_original_checks``).
Evidence label: isolated-fixture / injected-transport; no live listItem was read for a kind
(``devloop/live-checks/graph.sharepoint_listitem.json`` covers the read itself).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping

from integrations.live_source_guard import NotVisible
from ontology.mentions import WITHHELD_PAGES_SKIPPED

from .cases import _digest as _case_citation_digest
from .entitlements import _AGENT_PREFIXES  # "agents are never principals" (one rule)
# The document kinds and the Portal's document-type columns (re-exported names).
from .portal_kind_columns import (KIND_COLUMNS, KINDS, PORTAL_COMMIT,  # noqa: F401
                                  PORTAL_SOURCES, column_reading, recorded_columns)

# Playbook original role name -> the document kind it means
# (the retired packaging draft playbook's roles, customer_po_enrichment.json po_evidence =
# the customer's PO document).
ROLE_DOCUMENTS: dict[str, str] = {
    **{kind: kind for kind in KINDS},
    "vendor_pi": "proforma_invoice",
    "shipping_documents": "shipping_document",
    "po_evidence": "customer_purchase_order",
    "ao_original": "ao_scan",
    "quality_record": "qc_record",
}
MAX_PAGES = 200  # the extraction contract's page cap
MAX_DOCUMENT_CHARS = 1_000_000
MAX_TEXT_CALLS = 50  # text slices per page
PAGE_READ_BUDGET = 40  # plx.source_citation calls per evaluation for contradiction scans

# --------------------------------------------------------------------------- gap kinds
NOT_VISIBLE = "original_not_visible"
NOT_VISIBLE_TO_AGENT = "original_not_visible_to_agent"
NOT_RESOLVED = "citation_not_resolved"
NOT_PINNED = "recorded_digest_missing"
NOT_REBUILT = "recorded_view_not_reconstructable"
CHANGED = "original_changed_since_cited"
UNVERIFIABLE = "role_unverifiable"
AMBIGUOUS = "document_kind_ambiguous"
TYPE_NOT_VISIBLE = "document_type_not_visible"
TYPE_ITEM_CHANGED = "document_type_item_changed"
FILLS_NO_ROLE = "original_fills_no_role"
ROLE_NOT_FILLED = "role_not_filled"
ROLE_UNDECLARED = "role_document_undeclared"
TEXT_INCOMPLETE = "document_text_incomplete"
WITHHELD = "cited_evidence_withheld_from_reader"
TYPE_NOT_READ = "sharepoint_document_type_not_read"  # a limit, not a gap
HUMAN_ONLY = ("A person confirms the kind through plx.document_fields (a form sent "
              "to an employee session's MCP client; the Workbench cannot prove who answers, "
              "so hosts offer it only to clients that show it to a person); an agent never "
              "confirms, and a case finding never counts as a confirmation.")
WORKBENCH_CONFIRMATION = "workbench_confirmation"  # kind source label (operating.kind_confirmation)

_HANDLE = re.compile(r"^original://(orig-[0-9a-f]{32})"
                     r"(?:#(?:extraction=([2-9]|[1-9][0-9])(?:&page=([1-9][0-9]{0,3}))?"
                     r"|page=([1-9][0-9]{0,3})))?$")
# Keys plx.source_citation adds to the store's resolve view (case_runtime.read_original,
# OriginalStore.read, mcp_server._fit_original_views and _original_page_result).
_FILE_ENVELOPE = frozenset({"ok", "status", "changed_since", "note", "page_citations",
                            "extractions", "latest_extraction_version",
                            "page_citations_omitted", "page_citation_pattern"})
_PAGE_ENVELOPE = frozenset({"ok", "status", "changed_since", "note", "page_text_sha256",
                            "page_text_chars", "text_offset", "text_chars", "next_text_offset",
                            "text_complete"})

Read = Callable[[str, dict[str, Any]], Mapping[str, Any]]
# (drive_id, item_id) -> SharePointReader.list_item_fields result for the evaluating actor,
# None when this host has no reader for the drive; raises NotVisible when the actor may not
# see the item (any other exception is the caller's: playbook_eval makes it incomplete).
DocumentFields = Callable[[str, str], "Mapping[str, Any] | None"]
# original id -> the current (not superseded) Workbench confirmation record of that original
# (``operating.kind_confirmation.confirmed_kinds``: a store lookup, no source read), or None.
# It is bound here to the evaluator's own file-citation read (``confirmation_binds``).
KindConfirmationsRead = Callable[[str], "Mapping[str, Any] | None"]
BINDING_KEYS = ("source", "drive_id", "item_id", "etag", "ctag")


def confirmation_binds(record: Mapping[str, Any], view: Mapping[str, Any]) -> bool:
    """Does a confirmation record describe the original ``view`` (a file-citation read for the
    current reader): the same original id, the same SHA-256 and the same source item
    (source, drive, item, eTag, cTag)? The one binding rule of the evaluator and
    ``KindConfirmations.status``."""
    then = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    now = view.get("provenance") if isinstance(view.get("provenance"), Mapping) else {}
    return (record.get("original_id") == view.get("original_id")
            and record.get("original_sha256") == view.get("sha256")
            and all(now.get(key) == then.get(key) for key in BINDING_KEYS))



def is_agent_principal(principal: str) -> bool:
    return principal == "*" or principal.strip().lower().startswith(_AGENT_PREFIXES)


def is_agent_only(principals: Iterable[str] | None) -> bool:
    """True when ``principals`` is given and holds no employee principal (only ``agent:``/
    ``sp_`` ones or ``*``): such a reader never gets an original read (read_pdf's rule)."""
    if principals is None:
        return False
    return not {p for p in principals if not is_agent_principal(str(p))}


# --------------------------------------------------------------------------- text contradiction

_LETTER_SPACED = re.compile(r"(?<!\S)(?:[A-Za-z] ){2,}[A-Za-z](?!\S)")
_DECORATION = " \t*|=~_"
# A title line: the title, optionally followed by its own number ("No. X", "# X", " X" with a
# digit). A colon right after the title makes it a label ("Assembly Order: AO-1"), not a title.
_TRAIL = r"(?:\s*#\s*\S*\d\S*|\s+(?:no\.?|number|nr\.?|n°)\s*[:.#]?\s*\S*\d\S*|\s+\S*\d\S*)?"
_TITLES: tuple[tuple[str, frozenset[str]], ...] = (
    (r"pro\s*-?\s*forma(?:\s+invoice)?", frozenset({"proforma_invoice"})),
    (r"(?:freight|rate|shipping)\s+(?:quotation|quote)|freight\s+rate\s+(?:quotation|sheet)",
     frozenset({"freight_quote"})),
    (r"quotation|quote", frozenset({"freight_quote", "proforma_invoice"})),
    (r"order\s+(?:confirmation|acknowledge?ment)", frozenset({"proforma_invoice"})),
    (r"purchase\s+order|p\.\s*o\.(?:\s+number)?",
     frozenset({"purchase_order_pdf", "customer_purchase_order"})),
    (r"commercial\s+invoice|bill\s+of\s+lading|packing\s+list", frozenset({"shipping_document"})),
    # a bare (final / tax) invoice: not a proforma; a commercial invoice may be titled INVOICE
    (r"(?:tax\s+|final\s+)?invoice", frozenset({"shipping_document"})),
    (r"assembly\s+order", frozenset({"ao_scan"})),
    (r"certificate\s+of\s+analysis|(?:final\s+)?inspection\s+(?:sheet|report|record)"
     r"|q\.?\s*c\.?\s+(?:record|report|release|sign-?off)|quality\s+control(?:\s+(?:report|record))?",
     frozenset({"qc_record"})),
)
_TITLE_LINES = tuple((re.compile(f"(?:{title}){_TRAIL}", re.I), kinds) for title, kinds in _TITLES)
# A line that is a PI number (the document's own number printed as its own line).
_PI_LINE = re.compile(r"(?:p\s*/\s*i|pi)(?:\s*(?:no\.?|#|number))?\s*[:.#-]?\s*[a-z]{0,5}-?\d{4,}",
                      re.I)


def _clean(text: str | None) -> str:
    """Text as scanned: NFKC, format characters (zero-width and the like, category Cf)
    removed, and letter-spaced OCR headers ("P R O F O R M A") joined."""
    text = "".join(char for char in unicodedata.normalize("NFKC", text or "")
                   if unicodedata.category(char) != "Cf")
    return _LETTER_SPACED.sub(lambda match: match.group(0).replace(" ", ""), text)


def title_lines(text: str | None) -> list[dict[str, Any]]:
    """Every title line of ``text``: {line, kinds consistent with it}."""
    found = []
    for raw in _clean(text).splitlines():
        line = " ".join(raw.strip(_DECORATION).split())
        if not line or len(line) > 120:
            continue
        if _PI_LINE.fullmatch(line):
            found.append({"line": line, "kinds": ["proforma_invoice"]})
            continue
        for pattern, kinds in _TITLE_LINES:
            if pattern.fullmatch(line):
                found.append({"line": line, "kinds": sorted(kinds)})
                break
    return found


def contradictions(kind: str, text: str | None) -> list[dict[str, Any]]:
    """Title lines of ``text`` that belong to another document class than ``kind``."""
    return [item for item in title_lines(text) if kind not in item["kinds"]][:5]


# --------------------------------------------------------------------------- reading

def _file_handle(handle: str | None) -> str | None:
    """The file citation of the same extraction version as ``handle``."""
    match = _HANDLE.match(handle or "")
    if match is None:
        return None
    original_id, version = match.group(1), match.group(2)
    return f"original://{original_id}" + (f"#extraction={version}" if version else "")


def _page_of(handle: str | None) -> int | None:
    match = _HANDLE.match(handle or "")
    page = match and (match.group(3) or match.group(4))
    return int(page) if page else None


def _has_omitted_warnings(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "warnings_omitted" in value or any(_has_omitted_warnings(item)
                                                  for item in value.values())
    if isinstance(value, list):
        return any(_has_omitted_warnings(item) for item in value)
    return False


class _Pages:
    """Page text reads of one evaluation: cached, and bounded by one call budget."""

    def __init__(self, read: Read, budget: int) -> None:
        self.read, self.budget, self.calls = read, budget, 0
        self.texts: dict[str, str] = {}

    def _call(self, arguments: dict[str, Any], budgeted: bool) -> Mapping[str, Any] | None:
        if budgeted:
            if self.calls >= self.budget:
                return None
            self.calls += 1
        result = self.read("plx.source_citation", arguments)  # a refused read raises
        return result if isinstance(result, Mapping) else None

    def text(self, handle: str, first: Mapping[str, Any] | None = None, *,
             budgeted: bool = True) -> str | None:
        """The whole text of a page citation, reassembled from its slices and checked against
        ``page_text_sha256``; None when it cannot be read whole (or the budget is spent)."""
        if handle in self.texts:
            return self.texts[handle]
        result = first if first is not None else self._call({"citation": handle}, budgeted)
        if result is None:
            return None
        parts, head = [], result
        for _ in range(MAX_TEXT_CALLS):
            if not (result.get("ok") and isinstance(result.get("text"), str)):
                return None
            parts.append(result["text"])
            offset = result.get("next_text_offset")
            if offset is None:
                break
            result = self._call({"citation": handle, "text_offset": offset}, budgeted)
            if result is None:
                return None
        else:
            return None
        text = "".join(parts)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != head.get("page_text_sha256"):
            return None
        self.texts[handle] = text
        return text


def _resolve_view(pages: _Pages, handle: str, result: Mapping[str, Any]
                  ) -> dict[str, Any] | None:
    """The store's content-stable view of ``handle`` (what the case digested), rebuilt from the
    read tool's response, or None when it cannot be rebuilt exactly."""
    if _has_omitted_warnings(result):
        return None
    if "page" in result:
        text = pages.text(handle, result, budgeted=False)  # a cited handle: always re-read
        if text is None:
            return None
        return {**{k: v for k, v in result.items() if k not in _PAGE_ENVELOPE}, "text": text}
    return {k: v for k, v in result.items() if k not in _FILE_ENVELOPE}


def page_view(read: Read, handle: str, result: Mapping[str, Any],
              pins: set[Any]) -> dict[str, Any] | None:
    """The store's view of one cited ORIGINAL PAGE as the evaluator re-read it (whole text
    reassembled and hashed), or None unless it is rebuilt exactly and equals every digest the
    case recorded for ``handle``. Claim checks score page-level claims on this view only."""
    if _page_of(handle) is None or not result.get("ok") or not pins or None in pins:
        return None
    view = _resolve_view(_Pages(read, 0), handle, result)
    if view is None or "page" not in view:
        return None
    if any(pin != _case_citation_digest(view) for pin in pins):
        return None
    return view


def extraction_skipped(extraction: Mapping[str, Any]) -> dict[str, Any] | None:
    """The pages an original's extraction left unread past the page cap, else None: its
    ``skipped_by_cap``, or for an extraction stored before that field the range of its
    ``pdf_pages_skipped_by_cap:<first>-<last>`` warning (``{"cap": "pages"}`` alone when
    the range does not parse), so an old capped read is never taken for the whole file."""
    skipped = extraction.get("skipped_by_cap")
    if skipped is not None:
        whole = isinstance(skipped, Mapping) and all(
            isinstance(skipped.get(key), int) for key in ("first_page", "last_page"))
        return dict(skipped) if whole else {"cap": "pages"}
    prefix = WITHHELD_PAGES_SKIPPED + ":"
    for warning in extraction.get("warnings") or ():
        if isinstance(warning, str) and warning.startswith(prefix):
            first, _, last = warning[len(prefix):].partition("-")
            if all(n.isascii() and n.isdigit() for n in (first, last)) \
                    and 0 < int(first) <= int(last):
                return {"first_page": int(first), "last_page": int(last), "cap": "pages",
                        "limit": int(first) - 1}
            return {"cap": "pages"}
    return None


def _whole_text(pages: _Pages, view: Mapping[str, Any]) -> tuple[str | None, str]:
    """(every page's text, why not) of an original's file citation view: None with the
    reason when it cannot be read whole."""
    extraction = view.get("extraction") or {}
    warnings = [str(item) for item in extraction.get("warnings") or []]
    if any("unavailable" in item for item in warnings):
        return None, "the extraction reports an unavailable part"
    skipped = extraction_skipped(extraction)
    if skipped is not None:
        return None, ("pages past the extraction's page cap were not read"
                      + (f" (pages {skipped['first_page']}-{skipped['last_page']})"
                         if "first_page" in skipped else ""))
    page_list = view.get("page_citations")
    if not isinstance(page_list, list):
        count, pattern = extraction.get("page_count"), view.get("page_citation_pattern")
        if not isinstance(count, int) or not isinstance(pattern, str):
            return None, "the page list is not readable"
        page_list = [pattern.replace("<n>", str(n)) for n in range(1, count + 1)]
    if not page_list:
        return None, "the extraction has no text pages"
    if len(page_list) > MAX_PAGES:
        return None, f"more than {MAX_PAGES} pages"
    ocr_pages = set(extraction.get("ocr_pages") or [])
    texts, total = [], 0
    for page in page_list:
        text = pages.text(page)
        if text is None:
            return None, ("the per-evaluation page read budget is spent"
                          if pages.calls >= pages.budget else f"{page} cannot be read whole")
        if not text.strip():
            number = _page_of(page)
            return None, (f"page {number} has no text"
                          + ("" if number in ocr_pages else " and was not OCR'd"))
        total += len(text)
        if total > MAX_DOCUMENT_CHARS:
            return None, f"more than {MAX_DOCUMENT_CHARS} characters"
        texts.append(text)
    return "\n".join(texts), ""


def _same_content(provenance: Mapping[str, Any], read: Mapping[str, Any]) -> bool:
    """The listItem read describes the bytes the original was read from: the attach-time
    eTag, or the attach-time content tag (a column edit changes the eTag, not the cTag) with
    no disagreeing content hash."""
    snapshot = read.get("snapshot") if isinstance(read.get("snapshot"), Mapping) else {}
    if provenance.get("etag") and snapshot.get("drive_etag") == provenance.get("etag"):
        return True
    ctag = provenance.get("ctag")
    if not (isinstance(ctag, str) and ctag and read.get("ctag") == ctag):
        return False
    then = provenance.get("source_hashes") or {}
    now = read.get("source_hashes") or {}
    shared = set(then) & set(now) if isinstance(then, Mapping) and isinstance(now, Mapping) \
        else set()
    return all(str(then[key]).lower() == str(now[key]).lower() for key in shared)


def _column_source(view: Mapping[str, Any], fields: DocumentFields | None
                   ) -> dict[str, Any]:
    """The SharePoint document-type columns of the item an original was read from."""
    provenance = view.get("provenance") if isinstance(view.get("provenance"), Mapping) else None
    if provenance is None or provenance.get("source") != "sharepoint-drive":
        return {"status": "no_source_item"}
    drive, item = provenance.get("drive_id"), provenance.get("item_id")
    where = {"drive_id": drive, "item_id": item}
    if fields is None or not isinstance(drive, str) or not isinstance(item, str):
        return {"status": "not_read", **where}
    try:
        read = fields(drive, item)
    except NotVisible:
        return {"status": "not_visible", **where}
    if read is None:
        return {"status": "not_read", **where}
    if read.get("drive_id") != drive or read.get("item_id") != item:
        return {"status": "item_changed", **where, "why": "the read names another item"}
    if not _same_content(provenance, read):
        return {"status": "item_changed", **where,
                "why": "the item's content changed since the original's bytes were read"}
    snapshot = read.get("snapshot") if isinstance(read.get("snapshot"), Mapping) else {}
    return {"status": "read", **where, "drive_etag": snapshot.get("drive_etag"),
            "source_api": snapshot.get("source_api"),
            "columns": recorded_columns(snapshot.get("fields"))}


# --------------------------------------------------------------------------- the check

def check_case_originals(entries: Iterable[Mapping[str, Any]],
                         roles: Iterable[Mapping[str, Any]],
                         read: Read,
                         citation: Callable[[str], Mapping[str, Any]],
                         pins: Callable[[str], set[Any]],
                         principals: Iterable[str] | None = None,
                         withheld: int = 0,
                         findings: Iterable[Mapping[str, Any]] = (),
                         page_budget: int = PAGE_READ_BUDGET,
                         document_fields: DocumentFields | None = None,
                         kind_confirmations: KindConfirmationsRead | None = None
                         ) -> dict[str, Any]:
    """Check the case's cited originals against the playbook's original roles (module doc).

    ``entries`` are the case's manifest entries, ``findings`` its supported findings (accepted
    for the caller's signature and never read: a finding does not confirm a kind),
    ``kind_confirmations`` the evaluating reader's Workbench confirmations
    (``KindConfirmationsRead``; None: not consulted), ``citation(handle)`` the evaluator's cached
    ``plx.source_citation`` read, ``read`` its read-only tool caller (raises on a refused read),
    ``pins(handle)`` the digests the case recorded and ``document_fields`` the evaluating
    actor's SharePoint listItem read (``DocumentFields``). ``withheld`` counts citations shown
    without a handle."""
    cited = [entry for entry in entries if str(entry.get("handle", "")).startswith("original://")]
    role_specs = [{"role": item["role"], "required": item.get("required", True),
                   "document": ROLE_DOCUMENTS.get(item["role"])}
                  for item in roles if item.get("relation") == "original"]
    agent = is_agent_only(principals)
    pages = _Pages(read, page_budget)
    gaps: list[dict[str, Any]] = []
    limits: set[str] = set()
    documents: dict[str, dict[str, Any]] = {}

    def gap(kind: str, detail: str, **where: Any) -> None:
        gaps.append({"gap": kind, **where, "detail": detail})

    # 1. integrity of every cited handle
    for entry in sorted(cited, key=lambda item: item["handle"]):
        handle, file_handle = entry["handle"], _file_handle(entry["handle"])
        status, sha = "ok", None
        if file_handle is None or entry.get("status") != "resolved":
            status = NOT_RESOLVED
        elif agent:
            status = NOT_VISIBLE_TO_AGENT
        else:
            result = citation(handle)
            recorded = pins(handle)
            if not result.get("ok"):
                status = NOT_VISIBLE
            elif not recorded or None in recorded:
                status = NOT_PINNED
            else:
                view = _resolve_view(pages, handle, result)
                if view is None:
                    status = NOT_REBUILT
                elif any(pin != _case_citation_digest(view) for pin in recorded):
                    status = CHANGED
                else:
                    sha = view.get("sha256")
        if status != "ok":
            gap(status, "the case's citation could not be re-read as recorded", handle=handle)
        if file_handle is not None:
            doc = documents.setdefault(file_handle, {
                "handle": file_handle, "cited_handles": [], "status": "ok", "filename": None,
                "sha256": None, "source": None, "document": None, "kind_sources": [],
                "sharepoint_columns": None, "workbench_confirmation": None,
                "contradictions": [],
                "_sha": set()})
            doc["cited_handles"].append(handle)
            if sha:
                doc["_sha"].add(sha)
            if status != "ok" and doc["status"] == "ok":
                doc["status"] = status

    # 2.-3. recorded kinds and the contradiction scan, only when the playbook has original roles
    for file_handle in sorted(documents):
        doc = documents[file_handle]
        cited_sha = doc.pop("_sha")
        if doc["status"] != "ok" or not role_specs:
            continue
        result = citation(file_handle)
        if not result.get("ok"):
            doc["status"] = NOT_VISIBLE
            gap(NOT_VISIBLE, "the original's file citation is not readable", handle=file_handle)
            continue
        provenance = result.get("provenance") if isinstance(result.get("provenance"),
                                                            Mapping) else {}
        doc.update(filename=result.get("filename"), sha256=result.get("sha256"),
                   source=provenance.get("source") or "operator-inbox")
        if cited_sha != {doc["sha256"]}:
            doc["status"] = CHANGED  # the file read is not the document the case cited
            gap(CHANGED, "the original's file citation names other bytes than the cited view",
                handle=file_handle)
            continue
        column = _column_source(result, document_fields)
        doc["sharepoint_columns"] = column
        confirmation = _confirmation_source(_HANDLE.match(file_handle).group(1), result,
                                            kind_confirmations)
        doc["workbench_confirmation"] = confirmation
        sources: list[dict[str, Any]] = list(confirmation.get("sources") or [])
        problem: tuple[str, str] | None = None
        if column["status"] == "not_read":
            limits.add(TYPE_NOT_READ)
        elif column["status"] == "not_visible":
            problem = (TYPE_NOT_VISIBLE, "the SharePoint item's document-type columns are not "
                       "visible to this reader")
        elif column["status"] == "item_changed":
            problem = (TYPE_ITEM_CHANGED, f"the SharePoint document type was not read for "
                       f"these bytes: {column['why']}")
        columns = column.get("columns") or {}
        for name, value in sorted(columns.items()):
            if value["reads_as"] == "establishes":
                sources.append({"source": "sharepoint_column", "kind": value["kinds"][0],
                                "column": name, "value": value["value"]})
        doc["kind_sources"] = sources
        # every recorded column, live or confirmed, may contradict the kind
        recorded = [("SharePoint item", name, value) for name, value in sorted(columns.items())]
        recorded += [("Workbench confirmation", name, value) for name, value in
                     sorted((confirmation.get("columns") or {}).items())]
        kinds = sorted({item["kind"] for item in sources})
        if problem is None and not kinds:
            problem = (UNVERIFIABLE, "no recorded document kind for this original: "
                       + _missing(column) + "; and " + _unconfirmed(confirmation) + ". "
                       + HUMAN_ONLY)
        elif problem is None and len(kinds) > 1:
            problem = (AMBIGUOUS, "recorded sources name several kinds: " + ", ".join(kinds))
        if problem is None:
            kind = kinds[0]
            against = [f"{where} {name}={value['value']!r} ({value['reads_as']})"
                       for where, name, value in recorded
                       if value["reads_as"] in ("another_document", "unrecognised")
                       or (value["reads_as"] == "consistent" and kind not in value["kinds"])]
            if against:
                problem = (AMBIGUOUS, f"recorded {kind}, but " + ", ".join(against))
        if problem is None:
            text, why = _whole_text(pages, result)
            if text is None:
                problem = (TEXT_INCOMPLETE, f"the whole text could not be read ({why}): no "
                           "contradiction scan, so the recorded kind is not checked")
            else:
                doc["contradictions"] = contradictions(kind, text)
                if doc["contradictions"]:
                    problem = (AMBIGUOUS, f"recorded {kind}, but the text has a title line of "
                               "another document class: " + "; ".join(
                                   repr(item["line"]) for item in doc["contradictions"]))
        if problem:
            doc["status"] = problem[0]
            gap(problem[0], problem[1], handle=file_handle)
        else:
            doc["document"] = kind

    # 4. playbook original roles
    role_rows = []
    kinds_declared = {spec["document"] for spec in role_specs if spec["document"]}
    for spec in role_specs:
        filled = sorted(handle for handle, doc in documents.items()
                        if doc["status"] == "ok" and doc["document"] == spec["document"])
        status = ("filled" if filled else
                  ROLE_UNDECLARED if spec["document"] is None else
                  ROLE_NOT_FILLED if spec["required"] else "not_cited")
        role_rows.append({**spec, "filled_by": filled, "status": status})
        if status == ROLE_UNDECLARED and (spec["required"] or documents):
            gap(ROLE_UNDECLARED, "the playbook role names no known document kind",
                role=spec["role"])
        elif status == ROLE_NOT_FILLED:
            gap(ROLE_NOT_FILLED, "required original role has no cited original whose recorded "
                f"kind is {spec['document']}", role=spec["role"])
    if role_specs:
        for handle, doc in sorted(documents.items()):
            if doc["status"] == "ok" and doc["document"] not in kinds_declared:
                doc["status"] = FILLS_NO_ROLE
                gap(FILLS_NO_ROLE, f"recorded as {doc['document']}, which no original role of "
                    "the playbook declares", handle=handle)
    if withheld:
        gap(WITHHELD, f"{withheld} citation(s) of the case are withheld from this reader or "
            "unresolved; any of them may be an original that could not be checked",
            count=withheld)
    state = ("not_read_agent_identity" if agent else
             "checked" if role_specs else
             "integrity_only" if documents or withheld else "none_expected")
    return {"status": state, "roles": role_rows,
            "originals": [documents[handle] for handle in sorted(documents)],
            "kind_columns": list(KIND_COLUMNS), "page_reads": pages.calls,
            "page_read_budget": page_budget, "limits": sorted(limits),
            "gaps": gaps, "ok": not gaps}


def _confirmation_source(original_id: str, view: Mapping[str, Any],
                         kind_confirmations: KindConfirmationsRead | None) -> dict[str, Any]:
    """The Workbench confirmation of one original as kind sources (``workbench_confirmation``).

    ``view`` is the evaluator's own file-citation read of the original for this reader (no
    second read: a refused read already raised there). The confirmation counts only when it
    binds that view (``confirmation_binds``); its document kind establishes that kind and its
    ``KIND_COLUMNS`` read through the Portal tables like a live column."""
    if kind_confirmations is None:
        return {"status": "not_read"}
    record = kind_confirmations(original_id)
    if not isinstance(record, Mapping):
        return {"status": "none"}
    if record.get("original_id") != original_id or not confirmation_binds(record, view):
        return {"status": "not_this_original"}
    note = {"confirmation": record.get("id"), "confirmed_by": record.get("confirmed_by"),
            "at": record.get("confirmed_at"), "channel": record.get("channel")}
    sources: list[dict[str, Any]] = []
    kind = record.get("document_kind")
    if kind in KINDS:  # the store accepts only KINDS; anything else names no kind
        sources.append({"source": WORKBENCH_CONFIRMATION, "kind": kind, **note})
    fields = record.get("confirmed_fields")
    columns = recorded_columns(fields if isinstance(fields, Mapping) else None)
    for name, value in sorted(columns.items()):
        if value["reads_as"] == "establishes":
            sources.append({"source": WORKBENCH_CONFIRMATION, "kind": value["kinds"][0],
                            "column": name, "value": value["value"], **note})
    return {"status": "confirmed", **note, "document_kind": kind, "columns": columns,
            "sources": sources}


def _unconfirmed(confirmation: Mapping[str, Any]) -> str:
    return {"not_read": "Workbench confirmations were not consulted by this evaluation",
            "not_this_original": "the Workbench confirmation names other bytes",
            "confirmed": "the Workbench confirmation names no playbook kind"}.get(
                str(confirmation.get("status")), "no Workbench confirmation")


def _missing(column: Mapping[str, Any]) -> str:
    """What a reader needs to know is missing for a column-recorded kind."""
    status = column.get("status")
    if status == "no_source_item":
        return "an operator-supplied original has no SharePoint document-type column"
    if status == "not_read":
        return "the SharePoint document-type columns were not read (no reader on this host)"
    shown = ", ".join(f"{name}={value['value']!r}" for name, value in
                      sorted((column.get("columns") or {}).items())) or "no value"
    return (f"the SharePoint item's document-type columns ({', '.join(KIND_COLUMNS)}) name no "
            f"playbook kind ({shown})")


def public_original_checks(checks: Mapping[str, Any]) -> dict[str, Any]:
    """What a stored report may show every case-audience reader: statuses, kinds, counts and
    gap codes. No handle, file name, hash, SharePoint id, column value, title line, detail text
    or confirming principal: those were read under the evaluator's permission."""
    return {
        "status": checks["status"], "ok": checks["ok"], "kind_columns": checks["kind_columns"],
        "page_reads": checks["page_reads"], "page_read_budget": checks["page_read_budget"],
        "limits": list(checks["limits"]),
        "roles": [{"role": row["role"], "required": row["required"],
                   "document": row["document"], "status": row["status"],
                   "filled": len(row["filled_by"])} for row in checks["roles"]],
        "originals": [{"status": doc["status"], "document": doc["document"],
                       "kind_sources": sorted({item["source"] for item in doc["kind_sources"]})}
                      for doc in checks["originals"]],
        "gaps": [{"gap": item["gap"], **({"role": item["role"]} if "role" in item else {}),
                  **({"count": item["count"]} if "count" in item else {})}
                 for item in checks["gaps"]]}


def role_fill(checks: Mapping[str, Any]) -> tuple[dict[str, set[str]], bool]:
    """({original role: cited handles of the originals that fill it}, whether some cited or
    withheld original has no settled kind, so it may be of any kind)."""
    cited = {doc["handle"]: set(doc["cited_handles"]) for doc in checks["originals"]}
    fill = {row["role"]: {h for handle in row["filled_by"] for h in cited.get(handle, ())}
            for row in checks["roles"]}
    unsure = any(doc["status"] not in ("ok", FILLS_NO_ROLE) for doc in checks["originals"]) \
        or any(gap["gap"] == WITHHELD for gap in checks["gaps"])
    return fill, unsure


def runtime_document_fields(runtime: Any, actor: Any) -> DocumentFields:
    """``DocumentFields`` of a case runtime for one actor: the runtime's own employee gate,
    the drive's GET-only reader (``list_item_fields``, audience and item ACL checked before the
    listItem GET) and, for an employee, the item ACL intersected with the employee as on an
    original read. None for a drive this host has no reader for."""
    def read(drive_id: str, item_id: str) -> Mapping[str, Any] | None:
        gated = runtime.employee_gate(actor)  # refused actors raise here, at read time
        live = runtime.live
        reader = None if live is None else live.sharepoint.get(drive_id)
        if reader is None:
            return None
        result = reader.list_item_fields(gated, item_id, field_names=KIND_COLUMNS)
        if not runtime._employee_acl_allows(gated, "sharepoint", result):
            raise NotVisible()
        return result
    return read


__all__ = ["DocumentFields", "KINDS", "KIND_COLUMNS", "PAGE_READ_BUDGET", "PORTAL_SOURCES",
           "ROLE_DOCUMENTS", "check_case_originals", "column_reading",
           "contradictions", "is_agent_only", "page_view", "public_original_checks", "recorded_columns",
           "role_fill",
           "runtime_document_fields", "title_lines", "WORKBENCH_CONFIRMATION",
           "KindConfirmationsRead", "confirmation_binds"]
