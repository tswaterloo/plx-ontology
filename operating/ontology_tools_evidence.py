"""Generic evidence tools ``plx.identity``, ``plx.documents`` and ``plx.read``.

07-ONTOLOGY-DESIGN C.8; the frozen I/O contracts in
``operating/ontology_tool_contracts.json`` (every output is validated with
``ontology/json_contract.py`` and bounded by ``limits.max_result_bytes`` before it
leaves). The three tools reuse L8's reader and policy (``ontology_tools``) and
L9's from-T visibility SQL (``ontology_tools_traverse``): one permission rule for
every generic tool. Nothing here names an object type, identifier kind, link type or
case: anchors, identity keys, display properties and document types come from the
compiled schema and the store rows.

``plx.identity`` (R3: why and why not). For each root object the reader may see:

* ``edges``: every identity link the reader may see that touches it. An identity link
  is a stored link of an identity type (``ot.identity_link_type``, the rule every tool
  shares: ``<T>.same_record_as``, its text-match and reviewed links included). A
  ``same_record`` stored key between two types (``ItemLedgerEntry.posted_by_receipt``)
  and a person's review of another link type (an accepted reference, an allocation)
  are never an identity (ER-06).
  Each edge carries the projector's ``reason`` features (the compared keys with their
  cited values) and ``why_not`` features. A link touching a ``gold_native_staging``
  version is capped at ``lead`` (never authority). A link between two *anchor* records
  (orders, POs, AOs, invoices: schema ``anchor``) below ``verified``/``reviewed`` gets
  the why-not ``anchor_never_auto_merged``: such records are joined only by an exact
  stored key or a person's review, never by inference. Records are never merged; an
  edge is a link between two separate objects.
* ``near_misses`` (``include_near_misses``): visible records that carry the same
  normalized identifier as the root (the kind itself, its alias kinds and the kinds it
  is an alias of, each normalized by its own normalizer from the raw text) but that no
  identity link joins to it. ``failed`` lists why they are not the same record:
  ``identity_link`` (always; ``agrees`` null: no link this reader can see, a hidden one is
  neither said nor denied; cites both identifier fields), ``object_type``,
  ``identifier_kind`` (another kind's reading), ``primary_key`` (an alias such as a
  renumbered or legacy number matched: a renumbering suspicion), ``key_unique`` (the
  type's identity key is not unique), ``declared_identity_key`` (the kind is no
  identity key of the type), ``anchor_never_auto_merged``, ``lineage_authority`` and
  ``identity_scope`` (a kind unique only within a scope, as a customer PO per customer:
  false for two scope values of one system, null when either record names no scope or
  the scopes are two systems' columns; ``ot.identity_scopes``).
* Two visible records of one type sharing a key with no identity link stay two
  records: a gap ``ambiguous_identity`` names the root and the count of such
  records the reader can see; a pair the ``identity_scope`` feature keeps apart (false:
  two customers of one system) is never counted there (identity review never spans two
  customers), while a pair it cannot decide (null) still is. No winner is ever chosen; no model takes part.

``plx.documents``: the documents the reader may see (objects in mail, SharePoint or
original audience units of the projection store, re-gated per item through their L1
handle, plus the operator originals of the case runtime's ``OriginalStore`` and the
captured case originals (``operating.captured_originals``: each behind its source item's
gate; ones the SharePoint family already lists left out) that the reader may read now), around an object (documents linked to it, and documents linked to
those, e.g. a message's attachments) and/or matching a query (typed identifiers of the
query resolved to objects, then their documents; the remaining words matched against
the documents' own text values). A query that is one unlabelled token (no label or
self-delimiting shape the grammar finds, e.g. a bare number) is tried as every kind whose
grammar reads it, and as the stored same-number twins of each reading (the kind's
``same_number`` rule: a bare number matches any series), in the store and in the
originals; every kind with a record or original the reader may see is reported in one
gap (``identifier_reading``, or ``ambiguous_identifier`` when two or more readings hold
records: each reading's documents this reader may see in the requested sources are
listed and none is chosen). A labelled query
reads as today, and a bare token no visible record holds adds no reading gap. Each
document lists its identifier mentions (stored
``mention`` links, ``ontology/mentions.py``) with the exact span, the object, the tier
(``lead``) and ``external_assertion``. Originals outside the projection are scanned
with the same deterministic extractor (``ontology.mentions.extract``) and matched to
the object's visible identifiers by exact normalized key; their author is unknown, so
every such mention is an external assertion. An original read on its first pages only
(the page cap) that names none of them is a ``skipped_by_cap`` gap naming it and the
pages not read, so no match there is never absence. ``searched`` states what was searched
(R13); a source family the reader's policy does not cover in full is reported as
``withheld`` (``objects``/``properties`` null: nothing is counted) with an
``inaccessible`` gap, so a reader without the mailbox or site audience sees that the
search was partial, never the content. A document whose own ACL excludes the reader is
neither listed nor counted, with one exception: captured case originals whose file name
holds the query's words (never their text) that this reader may not open are counted per
gate kind
(``original_not_openable``: no known source ACL, or the source gate refused), never
named or quoted, and never when the people exclusion withholds them or cannot decide
(a Teams chat attachment: its sender and the chat's members must clear). A
search ``as_known_at`` a time before a store source's first captured version states
``before_first_capture`` for that source instead of a bare ``no_match``.

``plx.read``: one citation as the reader may see it now (``CitationReader``):
``api://`` rows through the audience-checked source mirror; ``original://`` originals
(file or page, OCR labelled) through the ``OriginalStore`` (audience, live source gate,
byte re-hash; an extraction read on its first pages only carries a ``skipped_by_cap`` gap,
and a page past them answers ``unresolvable`` with that gap); ``graph-mail://`` from the
versioned mail capture when the host has one, else through the GET-only live reader;
``sharepoint://`` and ``bc://`` through the GET-only live readers. ``status``:
``resolved``, ``changed_since`` (``current_citation`` names the newer version; the cited
version is returned where the source keeps it: the source mirror's capture, the mail
capture; a live SharePoint, BC or Graph mail reader holds only the current version, so
that one is returned, every value's origin is ``current_citation``, the digest is that
version's and a ``cited_version_not_retained`` gap says so), ``withheld`` (the reader
holds no grant for the citation's source scope: the relation, the mail or BC family, the
SharePoint drive; decided from configuration alone, before any read) or
``unresolvable`` (anything else: absent, not visible at item level, malformed, source
down; deliberately indistinguishable). ``digest`` is the digest the case store records
for the citation whose values are returned (``operating/cases._digest`` of
``CaseRuntime.resolve_citation``), so a finding citing it can be checked against it; a
mail-capture read has no live view, so its digest is ``cases._digest`` of the captured
version (relation, handle, recorded time, row). A ``bc://`` capture handle
(``…@<generation>-<capture-id>``) is read through the runtime's one citation resolver
(``CaseRuntime.resolve_citation``: the host's BC capture store, read-only, under the live
audience for the employee-gated reader), exactly the revision it names, as captured
(never a live read); a revision a later capture superseded answers ``changed_since`` with
its own values and ``current_citation``; the digest is the one a case records. ``page``
reads one page of an original, or of a SharePoint PDF from the host's capture, and the read
cites that page (``citation`` is the ``#page=<n>`` handle, evidence the answer gate takes);
a read of an original's whole text lists in ``pages`` the page citations of the pages the
returned text covers (``covered_pages``; a ``text_offset`` window its own) (a reader without
a capture answers ``unresolvable`` with a ``page_not_captured`` gap, never the whole
file as if it were the page); a ``graph-mail://`` attachment's page (``&page=<n>``, or
``page`` given to the reader) is read from the mail capture's text alone, that page's text
as content (past the page cap ``skipped_by_cap``, a file of which no text was read
``page_text_not_read``, past its last page ``page_not_found``; a page of a mailbox the
capture does not serve this reader, whether it holds it or not, ``withheld``
``mailbox_not_granted`` with nothing of the attachment read). A ``graph-mail://``
message the mail capture does not hold (and no live reader) answers ``unresolvable``
with a gap naming the mailbox that would hold it (``mail:<mailbox>``) and what the
capture holds of it for this reader (``mailbox_not_captured``,
``mailbox_capture_incomplete`` with its reasons such as ``forbidden``, or
``not_in_captured_folders``; ING-06).

Source text is data (``operating/injection_guard.py`` rule 2): every
string read from a source leaves inside an ``untrusted_text`` envelope with its origin
citation and field path; every field of a document citation (mail, SharePoint,
original) is enveloped, because an author wrote all of it. Typed row values of
``api://``/``bc://`` rows are plain only when they are numbers, booleans or nulls
(numbers as Decimal text). Nothing in a read is interpreted.

Evidence label of everything here: ``isolated-fixture`` / ``injected-transport`` in
tests; the live path is the host's reviewed GET-only readers (live-unverified).
"""

from __future__ import annotations

from collections import OrderedDict
import contextlib
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from ontology import identifiers
from ontology.json_contract import validate
from ontology.text_index import chunk_text, query_terms
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt
from operating.capability_guards import read_field, value_state
from operating.captured_originals import (WITHHELD_GATE_DENIED, WITHHELD_UNKNOWN_ACL,
                                          holds_words, vocabulary)
from operating.ontology_tools import (_IDENTITY_TIERS, LIVE_LOOKUP_HINT, OntologyTools,
                                      ToolError, _completeness, _error, _gap, _parse_time)
from operating.playbook_originals import extraction_skipped

TOOL_NAMES = ("plx.identity", "plx.documents", "plx.read")
# plx.read arguments only the case host serves (operating/case_tools_host.PagedRead).
PAGING_ARGUMENTS = ("text_offset", "column_offset", "line", "find")
TIER_ORDER = tt.TIER_ORDER
_TIER_RANK = {tier: index for index, tier in enumerate(TIER_ORDER)}
NEVER_AUTHORITY = tt.NEVER_AUTHORITY
# Audience-key prefixes (store_ddl docstring) of document records and their source family.
DOCUMENT_FAMILIES = {"mail": "graph_mail", "sharepoint": "sharepoint", "original": "original"}
_FAMILY_OF_SOURCE = {source: prefix for prefix, source in DOCUMENT_FAMILIES.items()}
# The document families the projection holds (an original is in the originals store): their
# value-cell spans narrow a word search (``_text_hits``), read when a host opens the store.
STORE_DOCUMENT_FAMILIES = tuple(f for f in DOCUMENT_FAMILIES if f != "original")
MAX_EDGES = 100
MAX_NEAR_MISSES = 20
MAX_GAPS = 20
MAX_MENTIONS = 50
MAX_READINGS = 8           # identifier kinds named in a bare query's reading gap
LINK_SCAN_CAP = 5000       # visible links read per root (identity, documents)
DOC_SCAN_CAP = 2000        # candidate documents considered per documents call
CANDIDATE_CAP = 200        # identifier rows read per key (near misses)
ORIGINAL_SCAN = 50         # operator originals listed per documents call
CAPPED_LISTED = 5          # capped files with no match one documents answer names one by one
DEFAULT_LIMIT = 10
DEFAULT_READ_CHARS = 8000
SNIPPET_CHARS = 300
_SCHEME_FAMILY = {"graph-mail": "mail", "sharepoint": "sharepoint", "original": "original",
                  "api": "api", "bc": "bc"}
# Why a matching captured original is not openable (``original_not_openable`` gaps).
_WITHHELD_KIND_TEXT = {
    WITHHELD_UNKNOWN_ACL: "no captured source tells who may read them: the Teams chat or "
                          "SharePoint item they came from is not in the capture, or they "
                          "name no source item",
    WITHHELD_GATE_DENIED: "the gate of the SharePoint item or Teams chat they came from "
                          "does not admit this reader now"}
# The L1 handle scheme of each document family the projection holds versions of.
_SCHEME_OF_FAMILY = {"mail": "graph-mail", "sharepoint": "sharepoint"}
_KIND_OF_SCHEME = {"graph-mail": "email", "sharepoint": "sharepoint_file",
                   "original": "original"}
# Identifier mentions (plus one per text) of original texts one process keeps.
TEXT_MENTIONS_BOUND = 100_000   # about 1 KB each
_DECIMAL = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_HANDLE = ot._HANDLE
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LINEAGE = re.compile(r"^(?:fm_bronze|sage_bronze|silver_view|gold_etl_from_fm|"
                      r"gold_etl_from_sage|gold_native_staging|mail|sharepoint|original|"
                      r"workbench|mission_control|brain|bc_[a-z0-9][a-z0-9_]{0,47})$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?$")


def _scheme(handle: Any) -> str:
    return handle.split("://", 1)[0] if isinstance(handle, str) and "://" in handle else ""


def _audience_first(alias: str, gated: str) -> str:
    """SQL: ``gated`` (terms that call the item gate) only for a row whose object
    ``alias`` is in the call's audiences (``:aud``): a CASE is evaluated in order, so a
    row outside them is dropped before the gate (a per-item permission read) is asked
    about it, whatever order the planner gives the terms of an AND."""
    return (f"(CASE WHEN {alias}.audience_key IN (SELECT value FROM json_each(:aud)) "
            f"THEN ({gated}) ELSE 0 END)")


def _family(audience_key: Any) -> str:
    return str(audience_key or "").split(":", 1)[0]


def _document_kind(handle: str) -> str | None:
    scheme = _scheme(handle)
    if scheme == "graph-mail" and "#attachment=" in handle:
        return "attachment"
    return _KIND_OF_SCHEME.get(scheme)


def _envelope(text: Any, citation: str, field_path: str | None = None,
              limit: int = 20_000, *, cut: bool = False) -> dict[str, Any] | None:
    """Source text as an ``untrusted_text`` envelope (``injection_guard`` rule 2)."""
    out = ot._envelope(text, citation, field_path, limit)
    if out is not None and cut:
        out["truncated"] = True
    return out


def _with_normalized(mention: dict[str, Any], kind: str, normalized: str, handle: str,
                     path: str | None = None) -> dict[str, Any]:
    """A mention with its ``normalized`` value typed (``ontology_tools.identifier_out``): a
    value that is no identifier of its kind is null with an ``identifier_invalid``
    ``normalized_gap`` quoting it, never bare."""
    value, gap = ot.identifier_out(kind, normalized, f"mention.{kind}", handle, path, 20_000)
    mention["normalized"] = value
    if gap is not None:
        mention["normalized_gap"] = gap
    return mention


def _json_list(text: Any) -> list:
    return tt._json_list(text)


def _strings(value: Any) -> list[str]:
    """Every string inside a captured value (a sender, a membership), shape-agnostic."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def people_excluded(guard: Any, identities: Iterable[Any]) -> bool:
    """Whether a captured record naming ``identities`` is withheld from every reader now
    by the people exclusion (``integrations.people_exclusion``). A capture keeps what it
    read before a person was excluded, so the guard is re-checked whenever a mail or
    Teams capture is served. An object id is decided always; an address only by a bound
    guard (offline nothing else says whose it is). A missing or unavailable guard
    withholds every record (fail closed)."""
    from integrations.people_exclusion import PeopleExclusion, is_object_id
    if not isinstance(guard, PeopleExclusion) or guard.unavailable:
        return True
    values = {v.strip().lower() for v in _strings(list(identities)) if v.strip()}
    return guard.excluded(v for v in values if guard.bound or is_object_id(v))


# --------------------------------------------------------------------------- citation reader

class CitationReader:
    """Source access ``plx.read`` and ``plx.documents`` need from a case runtime.

    Every method decides for the session ``actor`` now; nothing is cached here.
    ``read(handle, actor)`` returns a plain dict: ``status`` (``resolved``,
    ``changed_since``, ``withheld``, ``unresolvable``), ``reason``, ``lineage``,
    ``digest``, ``fields`` (raw row values by field path) with ``values_citation`` (the
    citation they were read at; default the handle), ``content`` (source text) with
    ``content_citation``/``content_field``, ``content_pin`` (a live mail body: the
    body-pinned citation of exactly that content), ``document`` (every field is source
    text), ``current_citation``, ``text_source`` and ``gaps``."""

    def read(self, handle: str, actor: Actor, *, page: int | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def originals(self, actor: Actor, limit: int) -> list[dict[str, Any]]:
        return []

    def original_text(self, handle: str, actor: Actor) -> dict[str, Any] | None:
        return None

    def captured_originals(self, actor: Actor, words: Sequence[str], limit: int
                           ) -> dict[str, Any]:
        """The capture store's case originals ``actor`` may read now
        (``operating.captured_originals.CapturedOriginals.search``): ``found`` (those
        holding ``words`` in the file name or stored text first, ``match`` naming which),
        ``cut``, ``withheld`` (gate kind -> count of matching originals this reader may not
        open, never their names; None: not counted) and ``refused`` (the store refused
        this reader or could not be read: nothing searched)."""
        return {"found": [], "cut": False, "withheld": None, "refused": False}

    def field_text(self, handle: str, field_path: str, actor: Actor) -> str | None:
        return None

    def batch(self) -> contextlib.AbstractContextManager[Any]:
        """A block of reads that may share per-call gate state (``RuntimeCitations``);
        the decisions inside it are those made outside it."""
        return contextlib.nullcontext()


class RuntimeCitations(CitationReader):
    """``CitationReader`` over a ``CaseRuntime`` (``operating/case_runtime.py``) and,
    when present, the versioned mail capture under ``mail_capture_dir``
    (``integrations/capture_mail.MailCaptureStore``, read offline for readers its
    recorded mailbox identities and the live audience admit, narrowed to the case mail
    scope). ``store_lineage(handle)`` (optional) names the projection's lineage for a
    handle (used for ``bc://`` records; else the company book of
    ``integrations/bc_company_books.json``)."""

    def __init__(self, runtime: Any, *, mail_capture_dir: str | Path | None = None,
                 store_lineage: Callable[[str], str | None] | None = None,
                 bc_books: Mapping[str, str] | None = None) -> None:
        self.runtime = runtime
        self.mail_capture_dir = None if mail_capture_dir is None else Path(mail_capture_dir)
        self.store_lineage = store_lineage
        self._bc_books = None if bc_books is None else {str(k).lower(): v
                                                         for k, v in bc_books.items()}

    def batch(self) -> contextlib.AbstractContextManager[Any]:
        """The runtime's case-tool-call block of its captures (``CaseCaptures.call``):
        the SharePoint gates of captured originals checked inside it are one gate per
        reader, its capture store opened once, instead of a gate and a store per
        original. Each original is still decided by the same gate on its own version,
        within the tool call's one live re-read bound (shared either way)."""
        call = getattr(getattr(self.runtime, "captures", None), "call", None)
        return call() if callable(call) else contextlib.nullcontext()

    # ---- helpers -----------------------------------------------------------

    def _people(self) -> Any:
        return getattr(self.runtime, "people_exclusion", None)

    def _mail_store(self) -> Any:
        """A context of the mail capture store: the runtime's kept read-only one
        (``CaseRuntime.mail_reader``), else the store opened and closed for this use."""
        from integrations.capture_mail import MailCaptureStore
        reader = getattr(self.runtime, "mail_reader", None)
        if callable(reader):
            return reader(self.mail_capture_dir)
        return contextlib.closing(MailCaptureStore(self.mail_capture_dir))

    def _gated(self, actor: Actor) -> Actor | None:
        try:
            return self.runtime.employee_gate(actor)
        except Exception:  # noqa: BLE001 - a refused employee gate reads nothing
            return None

    @staticmethod
    def _out(status: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "reason": reason, **extra}

    def _case_digest(self, handle: str, actor: Actor) -> str | None:
        """The digest the case store records for this citation (cases._digest of the
        runtime's content-stable view)."""
        from .cases import _digest
        try:
            return _digest(self.runtime.resolve_citation(handle, actor))
        except Exception:  # noqa: BLE001 - no stable view: no digest
            return None

    # ---- read --------------------------------------------------------------

    def read(self, handle: str, actor: Actor, *, page: int | None = None) -> dict[str, Any]:
        scheme = _scheme(handle)
        if scheme == "api":
            return self._read_api(handle, actor)
        if scheme == "original":
            return self._read_original(handle, actor, page)
        if scheme == "graph-mail":
            return self._read_mail(handle, actor, page)
        if scheme == "sharepoint" and page is not None:
            # A page is read from captured text only (a host holding the capture).
            return self._out("unresolvable", "page_not_captured", gaps=[_gap(
                "not_ingested", "page_not_captured", handle[:700],
                "a page of a SharePoint file is read from the Workbench's capture; this "
                "host holds no captured text of that page for this reader")])
        if scheme in ("sharepoint", "bc", "sharepoint-list"):
            return self._read_live(handle, actor, scheme)
        if scheme == "mc":
            # A Mission Control record: read from the host's MC capture only, through the
            # runtime's per-reader resolve (``_read_capture``).
            return self._read_capture(handle, actor)
        return self._out("unresolvable", "citation_scheme_not_supported")

    def _read_api(self, handle: str, actor: Actor) -> dict[str, Any]:
        from .employee_permissions import EmployeeAccessRefused
        try:
            result = self.runtime.mirror.resolve_citation(handle, actor)
        except EmployeeAccessRefused:
            return self._out("withheld", "employee_gate")
        if not result.get("ok"):
            if result.get("error") == "not_visible":
                # The relation named in the handle is outside the reader's audience:
                # a configuration fact about a handle the caller already holds.
                return self._out("withheld", "relation_not_granted")
            return self._out("unresolvable", str(result.get("error") or "unresolvable"))
        view: dict[str, Any] = {"citation": handle, "relation": result["relation"],
                                "key": result["key"],
                                "capture_id": result["capture"]["capture_id"]}
        fields: dict[str, Any] = {}
        if "row" in result:
            view["row"] = result["row"]
            for name, value in result["row"].items():
                fields[f"row.{name}"] = value
        else:
            view["rows"] = result["rows"]
            for index, row in enumerate(result["rows"]):
                for name, value in row.items():
                    fields[f"rows.{index}.{name}"] = value
        from .cases import _digest
        out = self._out("resolved", "ok", lineage=result.get("lineage"),
                        digest=_digest(view), fields=fields, document=False)
        gaps = []
        if result.get("changed_since"):
            if result.get("current_citation"):
                out.update(status="changed_since", current_citation=result["current_citation"])
            else:
                gaps.append(_gap("stale", "superseded_without_current", handle[:700],
                                 "a later capture replaced this row and no current row "
                                 "is held; the cited content is shown"))
        if result.get("missing_from_source_since"):
            gaps.append(_gap("absent_in_source", "missing_from_source", handle[:700],
                             "the source no longer returns this row (key scan); not "
                             "proof it was deleted"))
        if result.get("deleted_in_source"):
            gaps.append(_gap("absent_in_source", "deleted_in_source", handle[:700],
                             "the source marks this row deleted (its own soft-delete "
                             f"column) since {str(result['deleted_in_source'])[:40]}; the "
                             "cited content is shown, it is not live"))
        if "rows" in result:
            gaps.append(_gap("unknown", "key_not_unique", handle[:700],
                             "the cited key names several rows; each is shown"))
        out["gaps"] = gaps
        return out

    def _read_original(self, handle: str, actor: Actor, page: int | None) -> dict[str, Any]:
        target = handle
        if page is not None:
            if "page=" in handle:
                return self._out("unresolvable", "page_already_in_citation")
            target = f"{handle}&page={page}" if "#" in handle else f"{handle}#page={page}"
        from .employee_permissions import EmployeeAccessRefused
        try:
            view = self.runtime.read_original(target, actor)
        except EmployeeAccessRefused:
            return self._out("withheld", "employee_gate")
        if not view.get("ok"):
            # An original outside the reader's audience reads exactly like a missing one.
            return self._skipped_page(target, actor) or self._out(
                "unresolvable", "original_not_resolvable")
        digest = self._case_digest(target, actor)
        fields = {"row.filename": view.get("filename"), "row.sha256": view.get("sha256"),
                  "row.media_type": view.get("media_type"), "row.bytes": view.get("bytes")}
        extraction = view.get("extraction") or {}
        fields["row.page_count"] = extraction.get("page_count")
        out = self._out("resolved", "ok", lineage="original", digest=digest, fields=fields,
                        document=True, content_citation=target, gaps=[],
                        text_status=_extraction_status(extraction))
        if "page" in view:
            out["content"] = view.get("text") or ""
            out["text_source"] = view.get("text_source")
        else:
            try:
                whole = self.runtime.originals.text_document(target, actor)
            except Exception:  # noqa: BLE001 - text unavailable: fields only
                whole = None
            if whole is not None:
                out["content"] = whole["text"]
                # The page spans of that text: plx.read names the pages it returns.
                out["content_pages"] = list(whole.get("pages") or [])
                if whole.get("ocr_pages"):
                    out["text_source"] = "ocr"
            skipped = extraction_skipped(extraction)
            if skipped is not None and "content" in out:
                out["gaps"].append(skipped_gap(target, skipped))
        if out.get("text_source") == "ocr":
            out["gaps"].append(_gap("unknown", "ocr_text_unconfirmed", target[:700],
                                    "machine-read OCR text: what the scan appears to say, "
                                    "for a person to confirm, never a verified value"))
        if digest is None:
            return self._out("unresolvable", "original_not_resolvable")
        return out

    def _skipped_page(self, target: str, actor: Actor) -> dict[str, Any] | None:
        """The answer for a page citation past the page cap of an original this reader
        may read (its file citation resolves): the ``skipped_by_cap`` gap, never a silent
        unresolvable; else None."""
        from ontology.mentions import cited_page
        base, page = cited_page(target)
        if page is None:
            return None
        try:
            view = self.runtime.read_original(base, actor)
        except Exception:  # noqa: BLE001 - the file is not readable either: nothing to say
            return None
        extraction = (view.get("extraction") or {}) if view.get("ok") else {}
        skipped = extraction_skipped(extraction) if extraction else None
        if skipped is None or page <= (extraction.get("page_count") or 0) or (
                "first_page" in skipped
                and not skipped["first_page"] <= page <= skipped["last_page"]):
            return None
        return self._out("unresolvable", "skipped_by_cap",
                         gaps=[skipped_gap(target, skipped)])

    def _live(self) -> Any:
        return getattr(self.runtime, "live", None)

    def _read_mail(self, handle: str, actor: Actor, page: int | None = None
                   ) -> dict[str, Any]:
        from ontology.mentions import cited_page, page_citation
        if page is not None:
            # One page of an attachment: the citation of that page (``&page=<n>``).
            if cited_page(handle)[1] is not None:
                return self._out("unresolvable", "page_already_in_citation")
            handle = page_citation(handle, page)
        live = self._live()
        audience = getattr(live, "audience", None)
        if audience is None:
            return self._out("unresolvable", "source_not_configured",
                             gaps=[_gap("not_ingested", "source_not_configured",
                                        "graph_mail", "this host has no mail audience")])
        gated = self._gated(actor)
        if gated is None or not audience.may_read_any(gated, "mail:"):
            return self._out("withheld", "mail_not_granted")
        refusal = self._exclusion_refusal()
        if refusal and self.mail_capture_dir is not None:
            from integrations.capture_mail import store_exists
            if store_exists(self.mail_capture_dir):
                # No usable people guard: the capture serves nothing, and says why.
                return self._out("withheld", refusal, gaps=[_gap(
                    "inaccessible", refusal, "graph_mail",
                    "the people exclusion cannot decide on this host, so no captured "
                    "message is served")])
        captured = self._read_mail_capture(handle, gated, audience)
        if captured is not None:
            return captured
        if cited_page(handle)[1] is not None:
            if not self._mailbox_served(handle, gated, audience):
                # Decided on the mailbox alone: nothing of the attachment or page is read.
                return self._out("withheld", "mailbox_not_granted", gaps=[_gap(
                    "inaccessible", "mailbox_not_granted", "graph_mail",
                    "the Workbench's mail capture serves this reader no mailbox this "
                    "citation names (not granted to this reader, or not held for it)")])
            # A page is read from captured text only, never the whole file as the page.
            return self._out("unresolvable", "page_not_captured", gaps=[_gap(
                "not_ingested", "page_not_captured", handle[:700],
                "a page of a mail attachment is read from the Workbench's mail capture; "
                "this host holds no captured text of that page for this reader")])
        if getattr(live, "mail", None) is None:
            return self._out("unresolvable", "not_captured",
                             gaps=[self._mail_capture_gap(handle, gated, audience)])
        return self._read_live(handle, actor, "graph-mail")

    def _mailbox_admits(self, gated: Actor, mailbox: str, row: Mapping[str, Any]) -> bool:
        """The case mail scope and the employee's item ACL over a captured mailbox row
        (identities and owner ACL recorded at capture): the same decision the live
        path makes (CaseRuntime._employee_acl_denial)."""
        scope = getattr(self.runtime, "mail_scope", None)
        if scope is not None and not scope.allows(
                [*json.loads(row["identities"]), row["upn"], mailbox]):
            return False
        return not self.runtime._employee_acl_denial(
            gated, "graph-mail", {"source_acl": json.loads(row["source_acl"])})

    def _visible_mailbox(self, store: Any, mailbox: str, gated: Actor, audience: Any
                         ) -> dict[str, Any] | None:
        """The captured row of ``mailbox`` when the capture serves it to this reader (the
        audience over the identities and owner ACL recorded at capture, the people
        exclusion over the owner's identities, then ``_mailbox_admits``), else None: one
        it does not hold reads the same."""
        row = store._visible(gated, audience).get(mailbox)
        if row is None or people_excluded(self._people(), [
                mailbox, row["upn"], *json.loads(row["identities"])]):
            return None
        return row if self._mailbox_admits(gated, mailbox, row) else None

    def visible_mailboxes(self, store: Any, gated: Actor, audience: Any) -> frozenset[str]:
        """Every mailbox ``_visible_mailbox`` serves this reader, decided with one read
        of the capture's visible rows: the mailboxes whose messages the mail item gate
        can admit at all (a message still passes its own checks). Nothing when the people
        exclusion cannot decide (no captured message is served then)."""
        if self._exclusion_refusal():
            return frozenset()
        guard = self._people()
        return frozenset(
            mailbox for mailbox, row in store._visible(gated, audience).items()
            if not people_excluded(guard, [mailbox, row["upn"],
                                           *json.loads(row["identities"])])
            and self._mailbox_admits(gated, mailbox, row))

    def _exclusion_refusal(self) -> str | None:
        """The people guard's unavailability code (``CaseRuntime.exclusion_refusal``)."""
        from integrations.people_exclusion import PeopleExclusion
        refusal = getattr(self.runtime, "exclusion_refusal", None)
        if callable(refusal):
            return refusal()
        guard = self._people()
        return (guard.unavailable if isinstance(guard, PeopleExclusion)
                else "excluded_people_oids_absent")

    def _address_guard(self) -> Any:
        """A guard that decides addresses (``CaseRuntime.address_exclusion``), or None."""
        bound = getattr(self.runtime, "address_exclusion", None)
        return bound() if callable(bound) else None

    def _sent_by_excluded(self, store: Any, senders: Sequence[Any]) -> bool:
        """Whether a captured message's sender is, or may be, an excluded person now
        (then nobody is served it). An object id is decided by the guard; an address by
        the object ids the capture recorded beside it for a mailbox it holds, else by a
        guard bound to the directory (``_address_guard``). Offline an address no captured
        mailbox names cannot be resolved to a person: one in a tenant domain (the case
        mail scope's internal domains and every domain of a captured mailbox's
        addresses) may be an excluded colleague's and is withheld (fail closed); one in
        no tenant domain is not a member's. A sender that is neither is withheld."""
        from integrations.people_exclusion import PeopleExclusion, is_object_id
        guard = self._people()
        if not isinstance(guard, PeopleExclusion) or guard.unavailable:
            return True
        values = {v.strip().lower() for v in _strings(list(senders)) if v.strip()}
        ids = {v for v in values if is_object_id(v)}
        addresses = values - ids
        if any(a.count("@") != 1 or not a.rsplit("@", 1)[1] for a in addresses):
            return True
        domains = {d.lower() for d in getattr(getattr(self.runtime, "mail_scope", None),
                                              "internal_domains", ()) if isinstance(d, str)}
        unresolved = set(addresses)
        if addresses:
            for mailbox, text in store.db.execute("SELECT mailbox_id, identities FROM mailboxes"):
                identities = {str(i).strip().lower() for i in [mailbox, *json.loads(text)]
                              if isinstance(i, str) and i.strip()}
                domains |= {i.rsplit("@", 1)[1] for i in identities if i.count("@") == 1}
                owner = {i for i in identities if is_object_id(i)}
                if owner and addresses & identities:
                    ids |= owner
                    unresolved -= identities
        if guard.excluded(ids):
            return True
        if not addresses:
            return False
        bound = self._address_guard()
        if bound is not None:
            return bound.excluded(addresses)
        return any(a.rsplit("@", 1)[1] in domains for a in unresolved)

    def _mailbox_served(self, handle: str, gated: Actor, audience: Any) -> bool:
        """Whether this host's mail capture serves this reader the mailbox ``handle``
        names (True when the host holds no mail capture: nothing is withheld by it)."""
        from integrations.capture_mail import store_exists
        from integrations.live_source_guard import parse_handle
        if self.mail_capture_dir is None or not store_exists(self.mail_capture_dir):
            return True
        try:
            (mailbox, _message), _version, _extra = parse_handle(handle, "graph-mail", 2)
        except ValueError:
            return False
        with self._mail_store() as store:
            return self._visible_mailbox(store, mailbox.lower(), gated, audience) is not None

    def _mail_capture_gap(self, handle: str, gated: Actor, audience: Any) -> dict[str, Any]:
        """ING-06: a cited message this host holds no capture of names the source that
        would hold it (``mail:<mailbox>``) and what the capture holds of that mailbox
        for this reader; a mailbox withheld from the reader reads as not captured."""
        from integrations.capture_mail import store_exists
        from integrations.live_source_guard import parse_handle
        if self.mail_capture_dir is None or not store_exists(self.mail_capture_dir):
            return _gap("not_ingested", "source_not_configured", "graph_mail",
                        "no live mail reader and no mail capture on this host")
        try:
            (mailbox, _message), _version, _extra = parse_handle(handle, "graph-mail", 2)
        except ValueError:
            return _gap("unknown", "citation_handle_invalid", "graph_mail")
        with self._mail_store() as store:
            coverage = (store.mailbox_coverage(mailbox, gated, audience)
                        if self._visible_mailbox(store, mailbox.lower(), gated, audience)
                        else {"source": f"mail:{mailbox.lower()}", "status": "not_captured",
                              "reasons": []})
        searched = {"sources": ["graph_mail"], "scopes": [coverage["source"]]}
        if coverage["status"] == "not_captured":
            return _gap("not_ingested", "mailbox_not_captured", coverage["source"],
                        "this mailbox would hold the message; this reader holds no capture "
                        "of it (excluded from the capture scope, not reached yet, not "
                        "openable, or not granted to this reader)", searched)
        if coverage["status"] == "incomplete":
            return _gap("not_ingested", "mailbox_capture_incomplete", coverage["source"],
                        "this mailbox would hold the message; its capture is incomplete ("
                        + ", ".join(coverage["reasons"] or ["no folder captured"]) + ")",
                        searched)
        return _gap("not_ingested", "not_in_captured_folders", coverage["source"],
                    "this mailbox is captured complete for its scope folders and window; the "
                    "message is outside them or gone from the source", searched)

    def mail_capture(self, handle: str, gated: Actor, audience: Any, *, text: bool = False
                     ) -> tuple[dict[str, Any], str | None] | None:
        """``(captured, current)`` for a ``graph-mail://`` version the mail capture holds
        for the employee-gated reader (the mailbox identities recorded at capture, the
        live audience, the case mail scope and the employee's item ACL decide), else
        None. ``captured``: the row as the case store digests it; ``current``: the
        capture's current version of a superseded message (else None). ``text``: an
        attachment's row carries its text (``content_text``; a cited page's only, and
        None when the attachment has no such page: ``MailCaptureStore.resolve``). A
        version of an excluded person's mailbox, or one an excluded person sent, is None
        for every reader, whenever it was captured (``people_excluded``)."""
        if self.mail_capture_dir is None:
            return None
        from integrations.capture_mail import store_exists
        if not store_exists(self.mail_capture_dir):
            return None
        with self._mail_store() as store:
            resolved = store.resolve(handle, gated, audience, text=text)
            if resolved is None:
                return None
            found = store.db.execute("SELECT mailbox_id, message_id, record FROM messages "
                                     "WHERE l1_handle=? LIMIT 1",
                                     (resolved.get("message_handle") or handle,)).fetchone()
            if found is None:
                return None
            mailbox, message_id, record = found[0], found[1], json.loads(found[2])
            # The mailbox identities recorded at capture, as the reader may see them.
            if self._visible_mailbox(store, mailbox, gated, audience) is None \
                    or self._sent_by_excluded(store, [record.get("from"), record.get("sender")]):
                return None
            current = None
            if resolved.get("superseded_at") and resolved["relation"] == "graph-mail.message":
                latest = store.current_version(mailbox, message_id)
                current = latest.get("l1_handle") if latest else None
        return ({"citation": handle, "relation": resolved["relation"],
                 "recorded_at": resolved["recorded_at"], "row": resolved["row"],
                 "superseded_at": resolved.get("superseded_at")}, current)

    def _read_mail_capture(self, handle: str, gated: Actor, audience: Any
                           ) -> dict[str, Any] | None:
        """A captured message or attachment; a cited page of an attachment (or of a
        container member) with that page's captured text as its content, digested as the
        case store digests the page citation (``CaseRuntime.mail_capture``)."""
        from ontology.mentions import cited_page
        base, page = cited_page(handle)
        found = self.mail_capture(handle, gated, audience)
        if found is None:
            return None if page is None else self._mail_page_unread(base, page, gated,
                                                                    audience)
        resolved, current = found
        from .cases import _digest
        record = dict(resolved["row"])
        body = record.pop("body_text", None)
        fields = {f"row.{name}": value for name, value in record.items()}
        digest = _digest({key: resolved[key]
                          for key in ("citation", "relation", "recorded_at", "row")})
        out = self._out("resolved", "ok", lineage="mail", digest=digest, fields=fields,
                        document=True, content_citation=handle, content_field="row.body_text",
                        gaps=[])
        if isinstance(body, str):
            out["content"] = body
        if page is None and resolved["relation"] in _MAIL_FILES:
            # An attachment (or a member of one) read whole: its text, as a page read
            # gives one page of it; its text status says why there is none.
            whole = self.mail_capture(handle, gated, audience, text=True)
            row = {} if whole is None else whole[0]["row"]
            text = row.get("content_text")
            out.update(content_field="row.content_text",
                       text_status=str(row.get("text_status") or "text_not_read"))
            if isinstance(text, str):
                out["content"] = text
                skipped = extraction_skipped(row)
                if skipped is not None:
                    out["gaps"].append(skipped_gap(handle, skipped))
        if page is not None:
            paged = self.mail_capture(handle, gated, audience, text=True)
            text = None if paged is None else paged[0]["row"].get("content_text")
            if not isinstance(text, str):
                return self._mail_page_unread(base, page, gated, audience) or self._out(
                    "unresolvable", "page_not_found")
            out.update(content=text, content_field="row.content_text")
        if current and current != handle:
            out.update(status="changed_since", current_citation=current)
        elif resolved.get("superseded_at"):
            out["gaps"].append(_gap("stale", "superseded_in_capture", handle[:700],
                                    "the capture no longer holds this version as current"))
        return out

    def _mail_page_unread(self, base: str, page: int, gated: Actor, audience: Any
                          ) -> dict[str, Any] | None:
        """The answer for page ``page`` of a captured attachment (or member) ``base``
        this reader may read whose captured text holds no such page: ``skipped_by_cap``
        past the page cap and ``page_text_not_read`` when no text of it was read (a scan
        the capture could not OCR), both typed unknowns, never a page that does not
        exist; else ``page_not_found``. None when ``base`` is not readable."""
        from ontology.mentions import page_citation
        whole = self.mail_capture(base, gated, audience, text=True)
        if whole is None:
            return None
        if whole[0]["relation"] == "graph-mail.message":
            return self._out("unresolvable", "page_not_found")   # a message has no pages
        row, cited = whole[0]["row"], page_citation(base, page)
        skipped = extraction_skipped(row)
        if skipped is not None and ("first_page" not in skipped
                                    or skipped["first_page"] <= page <= skipped["last_page"]):
            return self._out("unresolvable", "skipped_by_cap",
                             gaps=[skipped_gap(cited, skipped)])
        if not isinstance(row.get("content_text"), str):
            return self._out("unresolvable", "page_text_not_read", gaps=[_gap(
                "not_ingested", "page_text_not_read", cited[:700],
                "no text of this attachment was read (a scan the capture could not OCR "
                "or a file it does not read as text): what the page says, and whether "
                "it exists, is unknown")])
        return self._out("unresolvable", "page_not_found")

    def missing_page(self, handle: str, gated: Actor, audience: Any) -> bool:
        """Whether ``handle`` cites a page of a captured attachment (or member) this
        reader may read that its captured text shows does not exist (``plx.read``:
        ``page_not_found``); False for a page it holds, for a page whose existence the
        capture cannot tell (past the page cap, no text read) and for anything else."""
        from ontology.mentions import cited_page
        base, page = cited_page(handle)
        if page is None or self.mail_capture(handle, gated, audience, text=True) is not None:
            return False
        unread = self._mail_page_unread(base, page, gated, audience)
        return unread is not None and unread["reason"] == "page_not_found"

    def _read_live(self, handle: str, actor: Actor, scheme: str) -> dict[str, Any]:
        from integrations.capture_store import is_capture_handle
        from integrations.live_source_guard import parse_handle
        live = self._live()
        audience = getattr(live, "audience", None)
        if live is None or audience is None:
            return self._out("unresolvable", "source_not_configured",
                             gaps=[_gap("not_ingested", "source_not_configured",
                                        scheme, "this host has no reader for this source")])
        gated = self._gated(actor)
        prefix = {"graph-mail": "mail:", "bc": "bc:", "sharepoint-list": "sharepoint:list:"
                  }.get(scheme)
        if scheme == "sharepoint":
            try:
                (drive, _item), _version, _extra = parse_handle(handle, "sharepoint", 2)
            except ValueError:
                return self._out("unresolvable", "citation_handle_invalid")
            prefix = f"sharepoint:{drive}:"
        if gated is None or not audience.may_read_any(gated, prefix):
            return self._out("withheld", "source_scope_not_granted")
        if scheme == "sharepoint-list" or scheme == "bc" and is_capture_handle(handle):
            return self._read_capture(handle, actor)
        result = self.runtime.read_live(handle, actor)
        if not result.get("ok"):
            if result.get("error") == "source_not_configured":
                return self._out("unresolvable", "source_not_configured",
                                 gaps=[_gap("not_ingested", "source_not_configured", scheme,
                                            "this host has no reader for this source")])
            return self._out("unresolvable", "live_citation_unresolvable")
        gaps: list[dict[str, Any]] = []
        read_handle = handle
        if result.get("changed_since"):
            # The GET-only live readers return only the source's current version; the
            # cited one cannot be read back. The current version is returned under its
            # own citation (values' origin and digest), never labelled with the old one.
            current = result.get("current_citation")
            if not isinstance(current, str) or not current:
                return self._out("unresolvable", "live_citation_changed")
            result = self.runtime.read_live(current, actor)
            if not result.get("ok") or result.get("changed_since"):
                return self._out("unresolvable", "live_citation_changed")
            read_handle = current
            gaps.append(_gap("stale", "cited_version_not_retained", handle[:700],
                             "the source keeps only its current version; the values shown "
                             "were read now at current_citation, not at the cited version"))
        record = dict(result.get("record") or {})
        record.pop("source_acl_status", None)
        fields = {f"row.{name}": value for name, value in record.items()
                  if _FIELD.match(str(name))}
        digest = self._current_digest(read_handle, actor)
        if digest is None:
            return self._out("unresolvable", "live_citation_unresolvable")
        out = self._out("resolved", "ok", fields=fields, document=scheme != "bc",
                        digest=digest, values_citation=read_handle,
                        content_citation=read_handle, gaps=gaps)
        if read_handle != handle:
            out.update(status="changed_since", current_citation=read_handle)
        if scheme == "graph-mail":
            out["lineage"] = "mail"
            out["content_field"] = "row.body_text"
            if "#attachment=" not in read_handle:
                snapshot = self._live_body(read_handle, actor)
                if snapshot is not None:
                    # One read: the body shown and its pin are of the same snapshot.
                    out["content"], out["content_pin"] = snapshot
            else:
                out["text_status"] = LIVE_METADATA_ONLY
        elif scheme == "sharepoint":
            # A live read is the file's metadata: its bytes are read from the capture.
            out.update(lineage="sharepoint", text_status=LIVE_METADATA_ONLY)
        else:
            out["lineage"] = self._bc_lineage(read_handle)
            if out["lineage"] is None:
                return self._out("unresolvable", "bc_company_unknown",
                                 gaps=[_gap("unknown", "bc_company_unknown", handle[:700],
                                            "the record's BC company maps to no book")])
        return out

    def _read_capture(self, handle: str, actor: Actor) -> dict[str, Any]:
        """A BC or SharePoint list item capture handle through ``CaseRuntime
        .resolve_citation`` (one read: the values shown and the digest are of the same view;
        a list item also passes its own captured permissions there). A list item's column
        values are its ``row.fields.<column>`` fields."""
        from .case_runtime import LiveSourceNotConfigured
        from .cases import _digest
        try:
            view = self.runtime.resolve_citation(handle, actor)
        except LiveSourceNotConfigured:
            return self._out("unresolvable", "source_not_configured",
                             gaps=[_gap("not_ingested", "source_not_configured", _scheme(handle),
                                        "this host has no reader for this source")])
        except Exception:  # noqa: BLE001 - hidden, absent and uncaptured read alike
            return self._out("unresolvable", "capture_citation_unresolvable")
        fields = {f"row.{name}": value for name, value in view["row"].items()
                  if _FIELD.match(str(name))}
        columns = fields.pop("row.fields", None) if handle.startswith("sharepoint-list://") \
            else None
        if isinstance(columns, Mapping):
            fields.update({f"row.fields.{name}": value for name, value in columns.items()
                           if _FIELD.match(str(name))})
        out = self._out("resolved", "ok", lineage=view["lineage"], digest=_digest(view),
                        fields=fields, document=False, content_citation=handle, gaps=[])
        if view["changed_since"]:
            if view.get("current_citation"):
                out.update(status="changed_since", current_citation=view["current_citation"])
            else:
                out["gaps"].append(_gap("stale", "superseded_without_current", handle[:700],
                                        "a later capture replaced this revision and no "
                                        "current one is held; the cited revision is shown"))
        return out

    def _current_digest(self, handle: str, actor: Actor) -> str | None:
        """The case-store digest of a live citation that is still current. A version
        that changed between the record read and this one has no digest (its view is
        a changed-since marker, not the values just read)."""
        from .cases import _digest
        try:
            view = self.runtime.resolve_citation(handle, actor)
        except Exception:  # noqa: BLE001 - no stable view: no digest
            return None
        return _digest(view) if view.get("status") == "current" else None

    def _live_body(self, handle: str, actor: Actor) -> tuple[str, str] | None:
        """The message body and its body-pinned citation, from one read."""
        from integrations.live_source_guard import parse_handle
        try:
            (mailbox, message), _version, _extra = parse_handle(handle, "graph-mail", 2)
            message_read = self.runtime.mail_message(actor, mailbox, message)
        except Exception:  # noqa: BLE001 - body unavailable: metadata only
            return None
        if message_read.get("metadata_citation") != handle.split("#", 1)[0]:
            return None  # another version than the one cited: never shown as it
        body, pinned = message_read.get("body_text"), message_read.get("citation")
        if not isinstance(body, str) or not isinstance(pinned, str):
            return None
        return body, pinned

    def _bc_lineage(self, handle: str) -> str | None:
        if self.store_lineage is not None:
            found = self.store_lineage(handle)
            if found:
                return found
        from integrations.live_source_guard import parse_handle
        try:
            (environment, company, _set, _record), _etag, _extra = parse_handle(handle,
                                                                                "bc", 4)
        except ValueError:
            return None
        company = company.lower()
        books = self._bc_books
        if books is None:
            try:
                from integrations.bc_read import load_company_books
                books = {str(k).lower(): v for k, v in
                         load_company_books().get(environment.lower(), {}).items()}
            except Exception:  # noqa: BLE001 - no books: no lineage
                books = {}
        book = books.get(company)
        return f"bc_{book}" if isinstance(book, str) and book else None

    # ---- originals (plx.documents) --------------------------------------------

    def originals(self, actor: Actor, limit: int) -> list[dict[str, Any]]:
        try:
            return list(self.runtime.originals.list_visible(actor, limit=limit))
        except Exception:  # noqa: BLE001 - the store refused this reader: nothing listed
            return []

    def original_text(self, handle: str, actor: Actor) -> dict[str, Any] | None:
        try:
            return self.runtime.originals.text_document(handle, actor)
        except Exception:  # noqa: BLE001 - unreadable for this reader now
            return None

    def captured_originals(self, actor: Actor, words: Sequence[str], limit: int
                           ) -> dict[str, Any]:
        captured = getattr(self.runtime, "captured_originals", None)
        if captured is None:
            return super().captured_originals(actor, words, limit)
        try:
            found = captured.search(actor, limit, words=list(words))
        except Exception:  # noqa: BLE001 - the store refused this reader: nothing listed
            return {"found": [], "cut": False, "withheld": None, "refused": True}
        return {**found, "refused": False}

    def field_text(self, handle: str, field_path: str, actor: Actor) -> str | None:
        """One text field of a captured mail record (``row.<field>``) for the reader."""
        if _scheme(handle) != "graph-mail" or not field_path.startswith("row."):
            return None
        read = self.read(handle, actor)
        if read.get("status") not in ("resolved", "changed_since"):
            return None
        if field_path == read.get("content_field"):
            return read.get("content") if isinstance(read.get("content"), str) else None
        value = (read.get("fields") or {}).get(field_path)
        return value if isinstance(value, str) else None


# --------------------------------------------------------------------------- tools

class EvidenceTools:
    """``plx.identity``, ``plx.documents`` and ``plx.read`` over one projection store.

    ``base``: the host's L8 ``OntologyTools`` (store, policy, item gate, schema,
    grammar, contracts, clock); ``traverse``: its L9 ``TraverseTools`` (built when not
    given), whose from-T visibility rules this module applies; ``reader``: a
    ``CitationReader`` for ``plx.read`` and operator originals (without one,
    ``plx.read`` answers ``unresolvable`` with a ``source_not_configured`` gap)."""

    def __init__(self, base: OntologyTools, *, traverse: tt.TraverseTools | None = None,
                 reader: CitationReader | None = None) -> None:
        self.base = base
        self.traverse = traverse if traverse is not None else tt.TraverseTools(base)
        self.reader = reader
        self.store = base.store
        self.contracts = base.contracts
        self.limits = self.contracts["limits"]

    @property
    def schema(self):  # noqa: ANN201 - L8 SchemaView or None
        return self.base.schema

    # ------------------------------------------------------------------ entry

    def call(self, name: str, arguments: Any, actor: Actor) -> dict[str, Any]:
        """Run one tool for ``actor``; the result always satisfies the contract."""
        if name not in TOOL_NAMES:
            raise KeyError(name)
        contract = self.contracts["tools"][name]
        if not isinstance(arguments, dict):
            return self._checked(name, _error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(name, _error("invalid_input", "; ".join(problems)))
        if name == "plx.read" and set(arguments) & set(PAGING_ARGUMENTS):
            # Paging is the case host's (operating/case_tools_host.PagedRead): never
            # silently ignored here.
            return self._checked(name, _error(
                "paging_not_served", "text_offset, column_offset, line and find are served by "
                "the case host's plx.read", "read without them, or through the case host"))
        try:
            if name == "plx.read":
                result = self._read(arguments, actor)
            else:
                handler = {"plx.identity": self._identity,
                           "plx.documents": self._documents}[name]
                result = handler(arguments, actor)
        except ToolError as exc:
            result = _error(exc.code, exc.message, exc.hint)
            try:
                result["meta"] = self.base._meta(self.base._store_meta(), self.base.clock(), ())
            except ToolError:
                pass
        except sqlite3.Error as exc:
            result = _error("store_unavailable",
                            f"the projection store could not be read ({type(exc).__name__})",
                            "rebuild or reopen the projection store")
        return self._checked(name, result)

    def _checked(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"][name]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if ot._envelope_bytes(result) > self.limits["max_result_bytes"]:
            return _error("result_too_large", "the answer does not fit one response",
                          "narrow the request (fewer objects, a smaller limit or max_chars)")
        return result

    def _with_gate(self, actor: Actor, body: Callable[[str], Any]) -> Any:
        """Run ``body(gate)`` holding the store lock with this call's SQL item gate."""
        gate = "plx_ev_gate_" + secrets.token_hex(8)
        with self.store.lock:
            self.store.conn.create_function(gate, 1, self.traverse._gate_function(actor),
                                            deterministic=True)
            try:
                return body(gate)
            finally:
                self.store.conn.create_function(gate, 1, None)

    def _visible_or_raise(self, scope: Any, object_id: str, actor: Actor) -> None:
        if not self.traverse._visible_ids(scope, [object_id]):
            if self.base.sets.live_id(object_id, actor):
                raise ToolError("not_in_projection",
                                "the object came from a live lookup, not the projection store",
                                LIVE_LOOKUP_HINT)
            raise ToolError("object_not_found", "no such object is visible to this reader",
                            "resolve the identifier with plx.find first")

    def _type_spec(self, object_type: str) -> Mapping[str, Any]:
        return ((self.schema.object_types.get(object_type) if self.schema else None) or {})

    def _identity_link_types(self) -> list[str]:
        return sorted(self.schema.identity_link_types()) if self.schema else []

    def _visible_links(self, scope: Any, object_ids: Sequence[str], extra: str = "",
                       params: Mapping[str, Any] | None = None) -> list[sqlite3.Row]:
        """Links touching ``object_ids`` the reader may see at T (live, markings held,
        every evidence handle passes the item gate, both ends visible)."""
        gate = scope.gate
        rows = self.store.conn.execute(
            f"""SELECT l.*, fo.object_type AS from_type, xo.object_type AS to_type,
                       fo.audience_key AS from_audience, xo.audience_key AS to_audience
                FROM links l JOIN objects fo ON fo.object_id = l.from_object
                JOIN objects xo ON xo.object_id = l.to_object
                WHERE (l.from_object IN (SELECT value FROM json_each(:ids))
                       OR l.to_object IN (SELECT value FROM json_each(:ids)))
                AND {tt._live('l')} AND l.markings IN (SELECT value FROM json_each(:marks))
                AND NOT EXISTS (SELECT 1 FROM json_each(l.evidence_handles) h
                                WHERE NOT {gate}(h.value))
                AND {tt._visible_object('fo', gate)} AND {tt._visible_object('xo', gate)}
                {extra}
                ORDER BY l.link_id LIMIT :cap""",
            scope.params(ids=json.dumps(sorted(set(object_ids))), cap=LINK_SCAN_CAP + 1,
                         **dict(params or {}))).fetchall()
        ends = {r["from_object"] for r in rows} | {r["to_object"] for r in rows}
        strict = self.traverse._visible_ids(scope, ends)
        return [r for r in rows if r["from_object"] in strict and r["to_object"] in strict]

    def _lineages(self, scope: Any, ids: Iterable[str]) -> dict[str, str]:
        return {oid: row["lineage"] for oid, row in
                self.traverse._versions_at(scope, sorted(set(ids))).items()}

    def _effective_tier(self, row: Mapping[str, Any], lineages: Mapping[str, str]) -> str:
        return tt._effective_tier({"tier": row["tier"],
                                   "from_lineage": lineages.get(row["from_object"]),
                                   "to_lineage": lineages.get(row["to_object"])})

    # ------------------------------------------------------------------ plx.identity

    def _identity(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        return self._with_gate(actor, lambda gate: self._identity_in(arguments, actor, gate))

    def _identity_in(self, arguments: Mapping[str, Any], actor: Actor, gate: str
                     ) -> dict[str, Any]:
        scope = self.traverse._scope(actor, arguments, gate)
        roots = list(dict.fromkeys(arguments["object_ids"]))
        visible = self.traverse._visible_ids(scope, roots)
        gaps: list[dict[str, Any]] = []
        if not visible:
            if len(roots) == 1:
                self._visible_or_raise(scope, roots[0], actor)
            raise ToolError("object_not_found", "none of these objects is visible to this reader",
                            "resolve the identifiers with plx.find first")
        for root in roots:
            if root not in visible:
                # Absent, outside the audience, marked or not yet captured: one answer.
                gaps.append(_gap("unknown", "object_not_found", root,
                                 "not visible to this reader; nothing is said about it"))
        roots = [r for r in roots if r in visible]
        itypes = self._identity_link_types()
        rows = self._visible_links(
            scope, roots, "AND l.link_type IN (SELECT value FROM json_each(:itypes))",
            {"itypes": json.dumps(itypes)})
        truncated = len(rows) > LINK_SCAN_CAP
        rows = rows[:LINK_SCAN_CAP]
        lineages = self._lineages(scope, [r["from_object"] for r in rows]
                                  + [r["to_object"] for r in rows] + roots)
        l8_scope = self.traverse._l8_scope(scope)
        edges: list[dict[str, Any]] = []
        joined: set[tuple[str, str]] = set()
        sources: set[str | None] = set()
        for row in rows:
            tier = self._effective_tier(row, lineages)
            edge = tt._edge(row, tier)
            if edge is None:
                gaps.append(_gap("unknown", "malformed_link", row["link_id"][:700],
                                 "a stored link could not be shown in the contract shape"))
                continue
            from_anchor = bool(self._type_spec(row["from_type"]).get("anchor"))
            to_anchor = bool(self._type_spec(row["to_type"]).get("anchor"))
            if from_anchor and to_anchor and tier not in _IDENTITY_TIERS:
                edge["why_not"] = (edge["why_not"] + [
                    {"feature": "anchor_never_auto_merged", "agrees": False}])[:16]
            edge = self.base._trim_citations(l8_scope, edge, {edge["from"], edge["to"]})
            edges.append(edge)
            joined.add((row["from_object"], row["to_object"]))
            joined.add((row["to_object"], row["from_object"]))
            sources |= {ot._source_of(h) for h in _json_list(row["evidence_handles"])
                        if isinstance(h, str)}
        edges.sort(key=lambda e: (_TIER_RANK[e["tier"]], e["link_id"]))
        if len(edges) > MAX_EDGES:
            edges, truncated = edges[:MAX_EDGES], True

        near, near_gaps, near_cut = self._near_misses(scope, roots, joined, lineages)
        gaps.extend(near_gaps)
        if near_cut:
            truncated = True
        if not arguments.get("include_near_misses"):
            near = []
        for row in self.traverse._versions_at(scope, roots).values():
            sources.add(ot._source_of(row["l1_handle"], row["lineage"]))
        meta, fresh_gaps = self.traverse._meta(scope, sources)
        gaps.extend(fresh_gaps)
        result: dict[str, Any] = {
            "edges": edges, "near_misses": near[:MAX_NEAR_MISSES],
            "gaps": gaps[:MAX_GAPS],
            "completeness": _completeness(len(edges), None if truncated else len(edges),
                                          truncated, "result_cap" if truncated else None,
                                          None),
            "meta": meta}
        return self._fit(result, "edges")

    def _root_identifiers(self, scope: Any, roots: Sequence[str]) -> list[sqlite3.Row]:
        return self.store.conn.execute(
            f"""SELECT i.*, o.object_type, v.digest FROM identifiers i
                JOIN objects o ON o.object_id = i.object_id
                JOIN object_versions v ON v.object_id = i.object_id
                     AND v.l1_handle = i.l1_handle
                WHERE i.object_id IN (SELECT value FROM json_each(:ids)) AND {tt._live('i')}
                AND i.markings IN (SELECT value FROM json_each(:marks))
                AND {scope.gate}(i.l1_handle)
                ORDER BY i.object_id, i.identifier_kind, i.normalized, i.l1_handle,
                         i.field_path""",
            scope.params(ids=json.dumps(sorted(roots)))).fetchall()

    def _search_keys(self, kind: str, normalized: str, raw: str) -> list[tuple[str, str]]:
        """(kind, normalized) pairs one identifier is searched under: its own kind, its
        alias kinds and the kinds it is an alias of, each normalized by its own
        normalizer from the raw text (as ``ontology/mentions.py`` and plx.find do)."""
        if self.base.grammar is None:
            return [(kind, normalized)]
        return identifiers.search_keys(kind, normalized, raw, self.base.grammar)

    def _identity_key(self, object_type: str, kind: str) -> Mapping[str, Any] | None:
        for key in self._type_spec(object_type).get("identity_keys", ()) or ():
            if key.get("identifier_kind") == kind:
                return key
        return None

    def _near_misses(self, scope: Any, roots: Sequence[str], joined: set[tuple[str, str]],
                     lineages: MutableMapping[str, str]
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """Records sharing a key with a root that no identity link joins to it, with
        the features that keep them apart. Computed for every call (the ambiguity gap
        needs it); shown when the caller asks.

        Records of one type sharing a copy identity (a kind whose grammar declares
        ``copies``: one message in several mailboxes, one file stored twice) whose content
        fields this reader may read agree (the carrier's ``copy_content`` as stored
        properties; a digest is content itself) are one copy group: a ``copies`` gap, not
        ``ambiguous_identity``. A field unread or different keeps the pair ambiguous (a
        forged Message-ID is no copy). Each copy stays its own record; none is chosen."""
        gate = scope.gate
        found: dict[tuple[str, str], dict[str, Any]] = {}
        cut = False
        for ident in self._root_identifiers(scope, roots):
            root = ident["object_id"]
            for kind, value in self._search_keys(ident["identifier_kind"], ident["normalized"],
                                                 ident["raw"]):
                rows = self.store.conn.execute(
                    f"""SELECT i.*, o.object_type, v.digest FROM identifiers i
                        JOIN objects o ON o.object_id = i.object_id
                        JOIN object_versions v ON v.object_id = i.object_id
                             AND v.l1_handle = i.l1_handle
                        WHERE i.identifier_kind = :kind AND i.normalized = :value
                        AND i.object_id <> :root AND {tt._live('i')}
                        AND i.markings IN (SELECT value FROM json_each(:marks))
                        AND {gate}(i.l1_handle) AND {tt._visible_object('o', gate)}
                        ORDER BY i.object_id, i.l1_handle, i.field_path LIMIT :cap""",
                    scope.params(kind=kind, value=value, root=root,
                                 cap=CANDIDATE_CAP + 1)).fetchall()
                if len(rows) > CANDIDATE_CAP:
                    rows, cut = rows[:CANDIDATE_CAP], True
                for row in rows:
                    other = row["object_id"]
                    if (root, other) in joined:
                        continue  # an identity link joins them: an edge, not a near miss
                    entry = found.setdefault((root, other), {"root_ident": ident, "rows": []})
                    entry["rows"].append(row)
        candidates = sorted({other for _, other in found})
        strict = self.traverse._visible_ids(scope, candidates)
        idents = {(r["object_id"], r["l1_handle"], r["field_path"]): r
                  for entry in found.values() for r in (entry["root_ident"], *entry["rows"])}
        scopes = ot.identity_scopes(
            self.store, self.base.grammar, self.base._binding_relations, scope.held,
            [(r["object_id"], r["l1_handle"], r["field_path"], r["identifier_kind"], r["raw"])
             for r in idents.values()])
        lineages.update(self._lineages(scope, [c for c in candidates if c not in lineages]))
        refs = {ref["object_id"]: ref for ref in self.traverse._object_refs(
            scope, sorted(strict))}
        out: list[dict[str, Any]] = []
        same_type: dict[str, set[str]] = {}
        copy_kinds = {kind for kind in identifiers.kinds()
                      if identifiers.kind_spec(kind).get("copies")}
        copy_pairs: dict[tuple[str, str], str] = {}
        for (root, other), entry in sorted(found.items()):
            if other not in strict or other not in refs:
                continue
            ident, rows = entry["root_ident"], entry["rows"]
            root_type = ident["object_type"]
            row = sorted(rows, key=lambda r: (r["identifier_kind"] != ident["identifier_kind"],
                                              r["role"] != "primary", r["l1_handle"]))[0]
            citations = [c for c in (_cite(ident), _cite(row)) if c is not None]
            # Only the links this reader may see were looked for: a link hidden from it is
            # not said to be absent (and not revealed), so the feature is unknown.
            failed: list[dict[str, Any]] = [{"feature": "identity_link", "agrees": None}]
            if citations:
                failed[0]["citations"] = citations[:4]
            key = self._identity_key(root_type, ident["identifier_kind"])
            # A key unique only within its binding (an FM id, per FM table) names no
            # record of another binding.
            scoped = (key is not None and key.get("scope") == "within_binding"
                      and root.split(":", 2)[1] != other.split(":", 2)[1])
            apart = _scope_apart(scopes, ident, row)
            if row["object_type"] != root_type:
                failed.append({"feature": "object_type", "agrees": False})
            elif (row["identifier_kind"] == ident["identifier_kind"] and not scoped
                  and (apart is None or apart["agrees"] is not False)):
                # Same type, same key kind and value, no link: an ambiguous identity,
                # unless the kind names copies (checked below on their content).
                same_type.setdefault(root, set()).add(other)
                if ident["identifier_kind"] in copy_kinds:
                    copy_pairs.setdefault((root, other), ident["identifier_kind"])
            if row["identifier_kind"] != ident["identifier_kind"]:
                failed.append({"feature": "identifier_kind", "agrees": False})
            if "alias" in (row["role"], ident["role"]):
                failed.append({"feature": "primary_key", "agrees": False})
            if scoped:
                failed.append({"feature": "key_scope_within_binding", "agrees": False})
            if apart is not None:
                failed.append(apart)
            if key is None:
                failed.append({"feature": "declared_identity_key", "agrees": False})
            elif not key.get("unique"):
                failed.append({"feature": "key_unique", "agrees": False})
            if (row["object_type"] == root_type
                    and self._type_spec(root_type).get("anchor")):
                failed.append({"feature": "anchor_never_auto_merged", "agrees": False})
            if {lineages.get(root), lineages.get(other)} & NEVER_AUTHORITY:
                failed.append({"feature": "lineage_authority", "agrees": False})
            out.append({"object": refs[other], "failed": failed[:16], "_root": root})
        content: dict[str, dict[str, Any]] = {}
        if copy_pairs:
            for value in self.store.readable_values(
                    self.traverse._l8_scope(scope),
                    sorted({oid for pair in copy_pairs for oid in pair})):
                if self.base._item_allowed(scope.actor, value["l1_handle"]):
                    content.setdefault(value["object_id"], {}).setdefault(
                        value["property"], value["value"])
        copies: dict[str, set[str]] = {}
        for (root, other), kind in copy_pairs.items():
            fields = {name for carrier in identifiers.carriers(kind)
                      for name in carrier.get("copy_content") or ()}
            names = [prop["name"] for prop in self._type_spec(
                root.split(":", 1)[0]).get("properties", ())
                if any(source.get("path") in fields for source in prop.get("sources", ()))]
            if fields and not names:
                continue   # nothing to compare the sender-written key with
            mine, theirs = content.get(root, {}), content.get(other, {})
            if all(mine.get(name) is not None and mine.get(name) == theirs.get(name)
                   for name in names):
                copies.setdefault(root, set()).add(other)
                same_type[root].discard(other)
        gaps = []
        for root in sorted(copies):
            gaps.append(_gap("unknown", "copies", root,
                             f"{len(copies[root]) + 1} records of this type visible to this "
                             "reader are copies of one message or file (one copy group: the "
                             "same copy identity and content); each stays its own record "
                             "with its own citation and permission, none is chosen, and "
                             "evidence they carry counts once"))
        for root in sorted(r for r in same_type if same_type[r]):
            count = len(same_type[root])
            gaps.append(_gap("unknown", "ambiguous_identity", root,
                             f"{count + 1} records of this type visible to this reader share "
                             "one identifier (same kind and value) and no identity link "
                             "visible to this reader joins them; they stay "
                             "separate records and none is chosen"))
        out.sort(key=lambda n: (n["_root"], len(n["failed"]), n["object"]["object_id"]))
        if len(out) > MAX_NEAR_MISSES:
            cut = True
        return [{k: v for k, v in n.items() if k != "_root"} for n in out], gaps, cut

    def _fit(self, result: dict[str, Any], list_key: str,
             cursor_after: Callable[[int], str] | None = None) -> dict[str, Any]:
        """Shrink the main list until the answer fits one response (marked truncated).
        A paged list (``cursor_after``) keeps at least one item and its next cursor
        resumes right after the items actually returned, so nothing shrunk away is
        skipped. Kept documents still too large for one response (a few heavily
        mentioned files: about 1 KB per mention) keep fewer of their mentions (``MENTION_TRIM``
        each, then none), said by one ``mentions_trimmed`` gap: the page is never refused
        for its mentions (plx.read of the document has them all). What does not fit even
        so is refused by ``_checked``."""
        limit = self.limits["max_result_bytes"]
        if ot._envelope_bytes(result) <= limit:
            return result
        items = list(result[list_key])
        floor = 1 if cursor_after is not None else 0
        while len(items) > floor and ot._envelope_bytes(result) > limit:
            items = items[:max(len(items) // 2, floor)]
            result[list_key] = items
            previous = result["completeness"]
            result["completeness"] = _completeness(
                len(items), previous.get("total"), True, "result_size",
                cursor_after(len(items)) if cursor_after is not None
                else previous.get("next_cursor"))
        for keep in self.MENTION_TRIM:
            if ot._envelope_bytes(result) <= limit:
                break
            cut = [i for i, item in enumerate(items)
                   if isinstance(item, dict) and len(item.get("mentions") or []) > keep]
            if not cut:
                continue
            items = [dict(item, mentions=item["mentions"][:keep]) if i in cut else item
                     for i, item in enumerate(items)]
            gaps = [g for g in result.get("gaps") or [] if g.get("reason") != "mentions_trimmed"]
            note = _gap("unknown", "mentions_trimmed", "plx.documents",
                        f"the listed documents' mentions were cut to {keep} each to fit one "
                        "response; plx.read a document for all of its mentions")
            result = dict(result, **{list_key: items}, gaps=gaps[:MAX_GAPS - 1] + [note])
        return result

    MENTION_TRIM = (5, 0)

    # ------------------------------------------------------------------ plx.documents

    def _documents(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        from operating.search import plug
        with plug.call_state() as search_state:
            query = arguments.get("query")
            if isinstance(query, str) and plug.enabled() and "as_known_at" not in arguments:
                # Hybrid retrieval runs before the store lock (``_with_gate``) is taken;
                # its objects are gated under it with every other id.
                plug.prefetch(self.traverse.policy, actor, self.base._item_allowed, query)
            return self._documents_paged(arguments, actor, search_state)

    def _documents_paged(self, arguments: Mapping[str, Any], actor: Actor,
                         search_state: Mapping[str, Any]) -> dict[str, Any]:
        from operating.search import plug
        fingerprint = ot._fingerprint(arguments, actor)
        offset, pinned, cursor_mode = 0, None, None
        if arguments.get("cursor"):
            offset, at = ot._read_cursor(arguments["cursor"], fingerprint)
            cursor_mode = ot._cursor_mode(arguments["cursor"])
            pinned = (at, False)
        sources = list(arguments.get("sources") or ("graph_mail", "sharepoint", "original"))
        since = _parse_time(arguments["since"]) if arguments.get("since") else None
        until = (_parse_time(arguments["until"], end_of_day=True)
                 if arguments.get("until") else None)
        if (arguments.get("since") and since is None) or (arguments.get("until")
                                                          and until is None):
            raise ToolError("invalid_input", "since/until is not a valid date or timestamp")
        state = self._with_gate(actor, lambda gate: self._documents_in(
            arguments, actor, gate, sources, since, until, pinned))
        documents, gaps, scope_info = state
        # A page continues only the ranking it was cut from: a change of search mode or
        # order mid-pagination is an error, never an empty "complete" page.
        mode = search_state["mode"]
        if arguments.get("cursor"):
            plug.check_cursor_mode(cursor_mode, mode)
        fallback = plug.fallback_gap(f"query:{' '.join(str(arguments.get('query')).split())}")
        if fallback is not None:
            gaps.insert(0, fallback)
        if "original" in sources:
            originals, original_gaps = self._original_documents(
                arguments, actor, scope_info, since, until)
            seen = {d["citation"] for d in documents}
            documents.extend(d for d in originals if d["citation"] not in seen)
            gaps.extend(original_gaps)
        if scope_info["bare"] is not None and scope_info["bare"]["readings"]:
            gaps.insert(0, _reading_gap(arguments["query"], scope_info["bare"]["readings"],
                                         sources))
        documents.sort(key=_document_order)
        limit = arguments.get("limit", DEFAULT_LIMIT)
        total = len(documents)
        page = documents[offset:offset + limit]
        more = offset + limit < total
        cursor = (ot._write_cursor(offset + limit, scope_info["at"], fingerprint, mode)
                  if more else None)
        stored = [doc for doc in page if "_object" in doc]
        if stored:
            # Bounded: mention spans (some re-read through the citation reader) are
            # resolved for the returned page only, at the same time as the listing.
            gaps.extend(self._with_gate(actor, lambda gate: self._page_mentions(
                arguments, actor, gate, stored,
                (scope_info["at"], scope_info["explicit_at"]))))
        for doc in page:
            doc.pop("_order", None)
            doc.pop("_rank", None)
            doc.pop("_object", None)
        searched: dict[str, Any] = {"sources": sources[:8], "scopes": scope_info["scopes"][:16]}
        if arguments.get("since"):
            searched["since"] = arguments["since"]
        if arguments.get("until"):
            searched["until"] = arguments["until"]
        if arguments.get("query"):
            searched["queries"] = [arguments["query"]]
        early = self._before_first_capture(sources, scope_info) \
            if scope_info["explicit_at"] else []
        gaps.extend(early)
        searched_store = [s for s in sources if s != "original"]
        if not page and offset == 0 and not (
                searched_store and len(early) == len(searched_store)):
            gaps.append(_gap("unknown", "no_match",
                             (f"query:{' '.join(arguments['query'].split())}"
                              if arguments.get("query") else str(arguments.get("object_id"))
                              )[:700],
                             "no visible document matched; documents never prove absence",
                             searched))
        partial = scope_info["withheld"] or any(
            g["state"] in ("inaccessible", "not_ingested") for g in gaps)
        completeness = _completeness(len(page), total, more, "page" if more else None, cursor)
        if partial and not more:
            completeness.update(complete=False, reason="partial_scope")
        result: dict[str, Any] = {"documents": page, "searched": searched,
                                  "gaps": gaps[:MAX_GAPS], "completeness": completeness,
                                  "meta": scope_info["meta"]}
        if scope_info["withheld"]:
            result["withheld"] = {"properties": None, "objects": None, "reason": "audience"}
        return self._fit(result, "documents", lambda returned: ot._write_cursor(
            offset + returned, scope_info["at"], fingerprint, mode))

    def _before_first_capture(self, sources: Sequence[str], info: Mapping[str, Any]
                              ) -> list[dict[str, Any]]:
        """A ``before_first_capture`` gap per searched store source whose first captured
        version was recorded after ``as_known_at``: nothing of it was known that early, so
        a search at that time finds nothing in it (never proof of absence). The first
        capture is a store fact (one scan per store state, the same for every reader)."""
        out = []
        for source in sources:
            family = _FAMILY_OF_SOURCE.get(source)
            scheme = _SCHEME_OF_FAMILY.get(family or "")
            if scheme is None:
                continue

            def read(scheme: str = scheme) -> str | None:
                row = self.store.conn.execute(
                    "SELECT MIN(plx_l9_time(recorded_at)) FROM object_versions "
                    "WHERE l1_handle >= ? AND l1_handle < ?",
                    (f"{scheme}://", f"{scheme}:/0")).fetchone()
                return row[0] if row else None

            with self.store.lock:
                first = self.traverse._store_cached(("first_capture", scheme), read)
            moment = _parse_time(first) if isinstance(first, str) else None
            if moment is None or moment <= info["at"]:
                continue
            out.append(_gap("not_ingested", "before_first_capture", f"source:{source}",
                            f"as_known_at is earlier than this source's first capture "
                            f"({moment.date().isoformat()}); nothing captured from it is "
                            "that old, so it was not known then", {"sources": [source]}))
        return out

    def _documents_in(self, arguments: Mapping[str, Any], actor: Actor, gate: str,
                      sources: Sequence[str], since: dt.datetime | None,
                      until: dt.datetime | None, pinned: Any
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        scope = self.traverse._scope(actor, arguments, gate, pinned)
        families = sorted(_FAMILY_OF_SOURCE[s] for s in sources)
        store_families = [f for f in families if f != "original"]
        gaps: list[dict[str, Any]] = []
        withheld = False
        policy = self.traverse.policy
        for family in families:
            if family == "original":
                continue  # operator originals are gated per item, never per family
            if not policy.covers_family(actor, family):
                withheld = True
                lacking = policy.ungranted_units(actor, family)
                gaps.append(_gap("inaccessible", "audience_not_granted",
                                 f"source:{DOCUMENT_FAMILIES[family]}",
                                 "this reader's grants do not cover every unit of this "
                                 "source; units outside them were not searched"
                                 if lacking is None else
                                 f"{lacking} captured unit(s) of this source (a mailbox, a "
                                 "library or a folder) are outside this reader's grants; "
                                 "they were not searched",
                                 {"sources": [DOCUMENT_FAMILIES[family]]}))
        gaps.extend(self._binding_gaps(sources))
        root = arguments.get("object_id")
        candidate: set[str] | None = None
        snippets: dict[str, dict[str, Any]] = {}
        scan_cut = False
        root_keys: list[tuple[str, str]] = []
        if root is not None:
            self._visible_or_raise(scope, root, actor)
            candidate, cut = self._linked_documents(scope, [root], store_families)
            scan_cut |= cut
            root_keys = [(r["identifier_kind"], r["normalized"])
                         for r in self._root_identifiers(scope, [root])]
        query_keys: list[tuple[str, str]] = []
        bare: dict[str, Any] | None = None
        ranks: dict[str, int] = {}
        if arguments.get("query"):
            by_query, query_keys, bare, cut, relaxed = self._query_documents(
                scope, arguments["query"], store_families, snippets, ranks)
            scan_cut |= cut
            if relaxed:
                gaps.append(_gap(
                    "unknown", "partial_word_match",
                    f"query:{' '.join(arguments['query'].split())}"[:700],
                    "no visible document holds every word of the query; the documents "
                    "listed hold every word but one of these, which they were not "
                    f"required to hold: {', '.join(relaxed)}. Read them before relying "
                    "on any of them"))
            candidate = by_query if candidate is None else candidate & by_query
        ids = sorted(candidate or ())
        visible = self.traverse._visible_ids(scope, ids)
        versions = self.traverse._versions_at(scope, sorted(visible))
        displays = self.traverse._displays(scope, sorted(versions))
        dates = self._document_dates(scope, sorted(versions))
        documents: list[dict[str, Any]] = []
        undated = 0
        for oid in sorted(versions):
            version = versions[oid]
            handle = version["l1_handle"]
            kind = _document_kind(handle)
            if kind is None or _FAMILY_OF_SOURCE.get(ot._source_of(handle) or "") not in families:
                continue
            date = dates.get(oid)
            moment = _parse_time(date) if date else None
            if since or until:
                if moment is None:
                    undated += 1
                    continue
                if (since and moment < since) or (until and moment > until):
                    continue
            # Mentions are added for the returned page only (_page_mentions).
            doc: dict[str, Any] = {"citation": handle, "kind": kind,
                                   "title": displays.get(oid), "date": date,
                                   "snippet": snippets.get(oid), "mentions": [],
                                   "_order": (moment, handle), "_object": oid}
            if oid in ranks:
                doc["_rank"] = ranks[oid]
            markings = sorted(set(_json_list(version["markings"])))
            if markings:
                doc["markings"] = markings
            documents.append(doc)
        if undated:
            gaps.append(_gap("unknown", "undated_excluded", "documents",
                             f"{undated} matching document(s) carry no date and were left "
                             "out of the since/until window"))
        if scan_cut:
            gaps.append(_gap("unknown", "scan_cap", "documents",
                             "the candidate scan reached its cap; narrow the query"))
        meta, fresh_gaps = self.traverse._meta(scope, sources)
        gaps.extend(fresh_gaps)
        scopes = [f"projection:{DOCUMENT_FAMILIES[f]}" for f in families if f != "original"]
        if "original" in families:
            scopes.append("originals_store")
        info = {"at": scope.at, "explicit_at": scope.explicit_at, "withheld": withheld,
                "meta": meta, "scopes": scopes, "root_keys": root_keys,
                "query_keys": query_keys, "bare": bare}
        return documents, gaps, info

    def _page_mentions(self, arguments: Mapping[str, Any], actor: Actor, gate: str,
                       page: Sequence[dict[str, Any]], pinned: tuple[dt.datetime, bool]
                       ) -> list[dict[str, Any]]:
        """Fill ``mentions`` (and a missing snippet) of the page's store documents."""
        scope = self.traverse._scope(actor, arguments, gate, pinned)
        gaps: list[dict[str, Any]] = []
        for doc in page:
            mentions, mention_gaps = self._mentions(scope, doc["_object"], actor)
            gaps.extend(mention_gaps)
            doc["mentions"] = mentions[:MAX_MENTIONS]
            if doc["snippet"] is None:
                doc["snippet"] = self._mention_snippet(scope, doc["_object"], mentions)
        return gaps

    def _binding_gaps(self, sources: Sequence[str]) -> list[dict[str, Any]]:
        """A document type whose binding is only partly served (e.g. no capture feeds
        it yet) is named, so 'nothing found' never reads as 'nothing exists'."""
        out = []
        if not self.schema:
            return out
        for tid, spec in sorted(self.schema.object_types.items()):
            if "Document" not in spec.get("interfaces", ()):
                continue
            state = (spec.get("availability") or {}).get("state")
            bound = {b.get("source") for b in spec.get("bindings", ())}
            if state in ("partial", "not_served") and bound & set(sources):
                out.append(_gap("not_ingested", "binding_partial", tid,
                                (spec.get("availability") or {}).get("reason")))
        return out

    def _linked_documents(self, scope: Any, ids: Sequence[str], families: Sequence[str]
                          ) -> tuple[set[str], bool]:
        """Document objects linked to ``ids``, and documents linked to those (a
        message's attachments), as the reader may see them."""
        out: set[str] = set()
        cut = False
        frontier = list(ids)
        for _hop in range(2):
            rows = self._visible_links(scope, frontier)
            if len(rows) > LINK_SCAN_CAP:
                rows, cut = rows[:LINK_SCAN_CAP], True
            found = set()
            for row in rows:
                for oid, audience in ((row["from_object"], row["from_audience"]),
                                      (row["to_object"], row["to_audience"])):
                    if oid not in frontier and _family(audience) in families:
                        found.add(oid)
            new = found - out
            out |= found
            if not new:
                break
            frontier = sorted(new)
        if len(out) > DOC_SCAN_CAP:
            out, cut = set(sorted(out)[:DOC_SCAN_CAP]), True
        return out, cut

    def _query_documents(self, scope: Any, query: str, families: Sequence[str],
                         snippets: dict[str, dict[str, Any]],
                         ranks: dict[str, int] | None = None
                         ) -> tuple[set[str], list[tuple[str, str]], dict[str, Any] | None,
                                    bool]:
        """(document ids, keys searched, the bare-token readings or None, scan capped).
        A bare token (``_bare``) is also searched as the same-number twins of each of
        its readings; the readings are the (kind, stored value) of every identifier row
        found, so a kind counts only where a record the reader may see holds it.
        With ``PLX_SEARCH=hybrid`` a prose query also takes hybrid search's objects
        (``operating/search``: meaning, OR-matched words, typos), their relevance order
        in ``ranks``; they pass the same visibility and item gate as every other id
        here (``_documents_in``) and get no snippet from search (search text is
        normalised, not a quote: ``_page_mentions`` reads one from the source)."""
        keys = [k for k in ot._query_keys(self.base.grammar, query, None)
                if not k.secondary and k.kind]
        out: set[str] = set()
        cut = False
        pairs = [(k.kind, k.normalized) for k in keys]
        bare: dict[str, Any] | None = None
        token = self._bare(query) if pairs else None
        if token is not None:
            for key in ot._query_keys(self.base.grammar, token, None):
                if (key.kind, key.normalized) not in pairs and key.kind:
                    pairs.append((key.kind, key.normalized))
            bare = {"keys": list(pairs), "readings": {}}
            twins, twin_cut = self._twin_pairs(scope, pairs)
            pairs.extend(twins)
            cut |= twin_cut
        if pairs:
            rows = self.store.conn.execute(
                # The pairs drive the identifier index (kind, normalized): a correlated
                # EXISTS over them scans every identifier row and gates it first.
                f"""SELECT DISTINCT i.object_id, o.audience_key, i.identifier_kind,
                    i.normalized FROM json_each(:pairs) p
                    JOIN identifiers i ON i.identifier_kind = json_extract(p.value, '$[0]')
                    AND i.normalized = json_extract(p.value, '$[1]')
                    JOIN objects o ON o.object_id = i.object_id
                    WHERE {tt._live('i')} AND i.markings IN (SELECT value FROM json_each(:marks))
                    AND {scope.gate}(i.l1_handle) AND {tt._visible_object('o', scope.gate)}
                    ORDER BY i.object_id LIMIT :cap""",
                scope.params(pairs=json.dumps(pairs), cap=DOC_SCAN_CAP + 1)).fetchall()
            if len(rows) > DOC_SCAN_CAP:
                rows, cut = rows[:DOC_SCAN_CAP], True
            objects = list(dict.fromkeys(r["object_id"] for r in rows))
            out |= {r["object_id"] for r in rows if _family(r["audience_key"]) in families}
            if bare is not None:
                for r in rows:
                    bare["readings"].setdefault(r["identifier_kind"], set()).add(r["normalized"])
            linked, more = self._linked_documents(scope, objects, families) if objects \
                else (set(), False)
            out |= linked
            cut |= more
        words = _content_words(ot._text_tokens(query, keys, limit=None))[:6]
        tokens = words or ([query.strip()] if not pairs else [])
        # Words, never identifiers: a query naming an identifier (a label or a
        # self-delimiting shape) is answered by it, not by the words of a text.
        prose = bool(words) and not self._names_identifier(query)
        relaxed: list[str] = []
        if tokens:
            matched: set[str] | None = None
            for token in tokens:
                hits = self._text_hits(scope, token, families, snippets)
                matched = hits if matched is None else matched & hits
            out |= matched or set()
            text_hits: set[str] = set()
            if prose:
                text_hits, text_cut = self._document_text_hits(scope, words, families,
                                                               snippets)
                out |= text_hits
                cut |= text_cut
            hybrid = self._hybrid_documents(scope, query) if prose else []
            for rank, object_id in enumerate(hybrid, start=1):
                out.add(object_id)
                if ranks is not None:
                    ranks.setdefault(object_id, rank)
            if (not matched and not text_hits and not hybrid and prose
                    and len(words) >= RELAX_MIN_WORDS):
                # No document holds every word: the documents whose text holds all but
                # one, each word left out in turn, are leads, and the answer says which
                # words no listed document was required to hold.
                for index, word in enumerate(words):
                    hits, more = self._document_text_hits(
                        scope, words[:index] + words[index + 1:], families, snippets)
                    cut |= more
                    if hits:
                        out |= hits
                        relaxed.append(word)
        return out, pairs, bare, cut, relaxed

    def _hybrid_documents(self, scope: Any, query: str) -> list[str]:
        """Object ids hybrid search ranks for ``query``, best first (``PLX_SEARCH=hybrid``
        and a read about now; else none). The caller gates every one."""
        from operating.search import plug
        if not plug.enabled() or not plug.is_now(scope.at, self.base.clock()):
            return []
        hits = plug.ranked(self.traverse.policy, scope.actor, self.base._item_allowed,
                           query).hits
        return [h["object_id"] for h in hits][:DOC_SCAN_CAP]

    def _document_text_hits(self, scope: Any, tokens: Sequence[str], families: Sequence[str],
                            snippets: dict[str, dict[str, Any]]) -> tuple[set[str], bool]:
        """Documents whose captured text (the word index ``plx.find`` searches,
        ``ontology/text_index.py``: a file's, a message body's or an attachment page's)
        holds every word of ``tokens``, and whether the scan was cut. The same rows a
        value match reads: the object readable at T, the field's version live at T and
        its markings held, the item gate on that version (all in SQL, before the cap).
        A query the index cannot read is a cut scan (nothing is claimed absent). The
        snippet quotes the matched text, cited to the field (and page) it was read from."""
        keys = [key for key in scope.audience_keys if _family(key) in families]
        if not keys:
            return set(), False   # no audience of these families: no chunk could be kept
        terms = query_terms(tokens)
        if not terms:
            # A word the index splits or drops: the text pass cannot run (the value pass
            # still did); nothing is claimed about the text.
            return set(), False
        # A word matches its prefix (expire: expired); a term with a digit only itself.
        words = " AND ".join('"' + term.replace('"', '""') + '"'
                             + ("" if any(c.isdigit() for c in term) else "*")
                             for term in terms)
        readable = _audience_first("o", (
            f"{tt._live('v')} AND fd.markings IN (SELECT value FROM json_each(:marks)) "
            f"AND {scope.gate}(v.l1_handle) AND {tt._visible_object('o', scope.gate)}"))
        try:
            rows = self.store.conn.execute(
                f"""SELECT fd.object_id, fd.citation, fd.field_path, t.body, o.audience_key
                    FROM chunk_text f JOIN text_chunks t ON t.chunk_id = f.rowid
                    JOIN text_fields fd ON fd.field_id = t.field_id
                    JOIN objects o ON o.object_id = fd.object_id
                    JOIN object_versions v ON v.l1_handle = fd.version_handle
                    AND v.object_id = fd.object_id
                    WHERE chunk_text MATCH :words AND {readable}
                    ORDER BY f.rowid LIMIT :cap""",
                # The families' audiences only: another family's chunk never takes a
                # slot of the cap nor pays for the item gate.
                scope.params(words=words, cap=DOC_SCAN_CAP + 1, aud=json.dumps(keys))
            ).fetchall()
        except sqlite3.OperationalError:
            return set(), True   # an index this store lacks, or a query it cannot read
        cut = len(rows) > DOC_SCAN_CAP
        out: set[str] = set()
        for row in rows[:DOC_SCAN_CAP]:
            if _family(row["audience_key"]) not in families:
                continue
            out.add(row["object_id"])
            if row["object_id"] not in snippets:
                snippets[row["object_id"]] = _window(
                    chunk_text(row["body"]), tokens[0], row["citation"],
                    row["field_path"])
        return out, cut

    def _names_identifier(self, query: str) -> bool:
        """Whether the grammar finds an identifier mention in ``query`` (a grammar that
        refuses the text reads it as one)."""
        try:
            return bool(self.base.grammar.find_identifiers(query))
        except Exception:  # noqa: BLE001 - a grammar refusing the text reads it as labelled
            return True

    def _bare(self, query: str) -> str | None:
        """The query's one unlabelled token (trailing sentence punctuation aside, as the
        mention scanner strips it from values), or None: a query with whitespace, or in
        which the grammar finds an identifier mention (a label or a self-delimiting
        shape), is no bare token."""
        token = query.strip().rstrip(".,;:?!")
        if not token or any(char.isspace() for char in token):
            return None
        try:
            return None if self.base.grammar.find_identifiers(query) else token
        except Exception:  # noqa: BLE001 - a grammar refusing the text reads it as labelled
            return None

    def _twin_pairs(self, scope: Any, pairs: Sequence[tuple[str, str]]
                    ) -> tuple[list[tuple[str, str]], bool]:
        """Stored values the reader may see that hold the same number as a reading, and
        whether a range was cut by its cap. The lookup is plx.find's (``_lead_matches``):
        ``same_number_prefixes`` bound the index range ``StoreReader.identifiers_by_prefix``
        reads, ``ontology.resolve.SameNumberIndex`` decides which rows are twins; the rows
        then pass these from-T visibility and the item gate, as every row here does.
        A range holding more than ``LEAD_SCAN_CAP`` rows is cut: the rows past the cap
        are never read, so whether one is a twin is unknown and the cut is reported."""
        from ontology.resolve import SameNumberIndex

        prefixes_of = getattr(self.base.grammar, "same_number_prefixes", None)
        out: list[tuple[str, str]] = []
        cut = False
        if prefixes_of is None:
            return out, cut
        l8 = self.traverse._l8_scope(scope)
        for kind, value in pairs:
            try:
                prefixes = prefixes_of(kind, value)
            except Exception:  # noqa: BLE001 - a kind without the rule has no twin
                prefixes = ()
            index, rows = SameNumberIndex(kind), {}
            try:
                for prefix in prefixes:
                    found = self.store.identifiers_by_prefix(l8, (kind,), prefix)
                    cut = cut or len(found) > ot.LEAD_SCAN_CAP
                    for row in found[:ot.LEAD_SCAN_CAP]:
                        ref = (row["object_id"], row["l1_handle"], row["field_path"])
                        rows[ref] = row
                        index.add(ref, row["normalized"], row["property"])
                twins = [rows[ref] for ref in index.twins(value)]
            except Exception:  # noqa: BLE001 - a value the rule cannot read has no twin
                twins = []
            visible = self.traverse._visible_ids(scope, [row["object_id"] for row in twins])
            for row in twins:
                pair = (kind, row["normalized"])
                if (row["object_id"] in visible and pair not in out and pair not in pairs
                        and self.base._item_allowed(scope.actor, row["l1_handle"])):
                    out.append(pair)
        return out, cut

    @staticmethod
    def _same(same: Callable[[str, Any, Any], bool], kind: str, first: Any, second: Any
              ) -> bool:
        try:
            return bool(same(kind, first, second))
        except Exception:  # noqa: BLE001 - a value the rule cannot read is no twin
            return False

    def _cell_spans(self, families: Sequence[str]) -> list[tuple[int, int]] | None:
        """The value-cell rowid spans of ``families`` (``TraverseTools.cell_span``; a family
        with no cell has none), or None when they cannot be read (search unnarrowed)."""
        try:
            with self.store.lock:
                spans = [self.traverse.cell_span(f) for f in sorted(set(families))]
        except sqlite3.Error:
            return None
        return [span for span in spans if span is not None]

    def _text_hits(self, scope: Any, token: str, families: Sequence[str],
                   snippets: dict[str, dict[str, Any]]) -> set[str]:
        """Objects of ``families`` holding a text or identifier value that contains
        ``token``, read as every value row is (live at T, held, gated). The rows are
        read in the families' audiences only, so another family's row never takes a
        slot of the cap nor pays for the item gate. A token that is one indexed word is
        looked up in the value word index (``value_text``, as ``plx.find`` reads it: the
        word or, without a digit, a word it starts) and then held to the substring
        match, so the index only narrows; any other token (or a store without the index)
        is the substring scan alone."""
        keys = [key for key in scope.audience_keys if _family(key) in families]
        if not keys:
            return set()   # no audience of these families: no row could be kept
        pattern = "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params = scope.params(pattern=pattern, cap=DOC_SCAN_CAP, aud=json.dumps(keys))
        visible = _audience_first("o", f"{tt._visible_object('o', scope.gate)} "
                                       f"AND {tt._visible_value('v', scope.gate)}")
        terms = query_terms([token])
        rows = None
        if terms:
            term = terms[0]
            params["words"] = ('"' + term.replace('"', '""') + '"'
                               + ("" if any(c.isdigit() for c in term) else "*"))
            # A common word matches cells of every source; each match costs a value,
            # version and object read (cold: a disk read each) before its audience drops
            # it. The families' cell spans drop those first, on the index's rowid alone.
            spans = self._cell_spans(families)
            if spans == []:
                return set()   # these families hold no value cell: nothing can match
            narrowed = "" if spans is None else " AND (" + " OR ".join(
                f"f.rowid BETWEEN :lo{i} AND :hi{i}" for i in range(len(spans))) + ")"
            for i, (low, high) in enumerate(spans or ()):
                params[f"lo{i}"], params[f"hi{i}"] = low, high
            try:
                rows = self.store.conn.execute(
                    f"""SELECT v.object_id, v.value, v.l1_handle, v.field_path, o.audience_key
                        FROM value_text f JOIN object_values_numbered v ON v.row_no = f.rowid
                        JOIN objects o ON o.object_id = v.object_id
                        WHERE value_text MATCH :words{narrowed}
                        AND v.value_type IN ('text', 'identifier') AND v.value LIKE :pattern
                        ESCAPE '\\' AND {visible}
                        ORDER BY f.rowid LIMIT :cap""", params).fetchall()
            except sqlite3.OperationalError:
                rows = None   # a store without the value word index: the scan below
        if rows is None:
            rows = self.store.conn.execute(
                f"""SELECT v.object_id, v.value, v.l1_handle, v.field_path, o.audience_key
                    FROM object_values v JOIN objects o ON o.object_id = v.object_id
                    WHERE v.value_type IN ('text', 'identifier') AND v.value LIKE :pattern
                    ESCAPE '\\' AND {visible}
                    ORDER BY v.object_id, v.property LIMIT :cap""", params).fetchall()
        out = set()
        for row in rows:
            if _family(row["audience_key"]) not in families:
                continue
            out.add(row["object_id"])
            if row["object_id"] not in snippets:
                snippets[row["object_id"]] = _window(row["value"], token, row["l1_handle"],
                                                     row["field_path"])
        return out

    def _document_dates(self, scope: Any, ids: Sequence[str]) -> dict[str, str]:
        """The document's date: its first visible timestamp/date value in the type's
        declared property order."""
        if not ids:
            return {}
        rows = self.store.conn.execute(
            f"""SELECT v.object_id, v.property, v.value, o.object_type FROM object_values v
                JOIN objects o ON o.object_id = v.object_id
                WHERE v.object_id IN (SELECT value FROM json_each(:ids))
                AND v.value_type IN ('timestamp', 'date') AND v.value IS NOT NULL
                AND {tt._visible_value('v', scope.gate)}""",
            scope.params(ids=json.dumps(sorted(ids)))).fetchall()
        best: dict[str, tuple[int, str, str]] = {}
        for row in rows:
            if not _TIME.match(row["value"] or ""):
                continue
            order = [p.get("name") for p in self._type_spec(row["object_type"]).get(
                "properties", ())]
            rank = order.index(row["property"]) if row["property"] in order else len(order)
            current = best.get(row["object_id"])
            if current is None or (rank, row["property"]) < current[:2]:
                best[row["object_id"]] = (rank, row["property"], row["value"])
        return {oid: value for oid, (_, _, value) in best.items()}

    def _mentions(self, scope: Any, doc: str, actor: Actor
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The document's stored mention links the reader may see, in the contract's
        mention shape, each span re-read from the cited text."""
        rows = [r for r in self._visible_links(scope, [doc], "AND l.binding_kind = 'mention'")
                if r["from_object"] == doc]
        lineages = self._lineages(scope, [r["to_object"] for r in rows] + [doc])
        texts: dict[tuple[str, str], str | None] = {}
        out: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        unreadable = 0
        for row in rows:
            feature = next((f for f in _json_list(row["reason"]) if isinstance(f, dict)
                            and f.get("feature") == "identifier_mention"), None)
            if feature is None:
                continue
            cites = [c for c in feature.get("citations", ()) if isinstance(c, dict)]
            span = feature.get("span")
            if not cites or not (isinstance(span, list) and len(span) == 2
                                 and all(isinstance(x, int) for x in span)):
                continue
            handle, path = cites[0].get("l1_handle"), cites[0].get("field_path")
            if not isinstance(handle, str) or not isinstance(path, str):
                continue
            if (handle, path) not in texts:
                texts[(handle, path)] = self._field_text(scope, doc, handle, path, actor)
            text = texts[(handle, path)]
            start, end = span
            if text is None or not 0 <= start < end <= len(text):
                unreadable += 1
                continue
            key = (row["to_object"], handle, path, start, end)
            if key in seen:
                continue
            seen.add(key)
            external = any(isinstance(f, dict) and f.get("feature") == "external_assertion"
                           for f in _json_list(row["why_not"]))
            normalized = str(feature.get("normalized") or text[start:end])[:200]
            kind = str(feature.get("identifier_kind") or "")
            if not re.match(r"^[a-z][a-z0-9_]{0,63}$", kind) or not normalized:
                continue
            mention = {"identifier_kind": kind, "span": _envelope(text[start:end], handle, path),
                       "object_id": row["to_object"],
                       "tier": self._effective_tier(row, lineages),
                       "external_assertion": external,
                       "_order": (start, row["to_object"])}
            out.append(_with_normalized(mention, kind, normalized, handle, path))
        out.sort(key=lambda m: m.pop("_order"))
        if unreadable:
            gaps.append(_gap("unknown", "mention_span_unreadable", doc,
                             f"{unreadable} mention(s) could not be re-read from the cited "
                             "text for this reader and are not shown"))
        return out, gaps

    def _field_text(self, scope: Any, doc: str, handle: str, path: str,
                    actor: Actor) -> str | None:
        row = self.store.conn.execute(
            f"""SELECT v.value FROM object_values v WHERE v.object_id = :doc
                AND v.l1_handle = :handle AND v.field_path = :path
                AND v.value_type IN ('text', 'identifier')
                AND {tt._visible_value('v', scope.gate)} LIMIT 1""",
            scope.params(doc=doc, handle=handle, path=path)).fetchone()
        if row is not None and isinstance(row["value"], str):
            return row["value"]
        if self.reader is None or scope.explicit_at:
            return None
        return self.reader.field_text(handle, path, actor)

    def _mention_snippet(self, scope: Any, doc: str, mentions: Sequence[Mapping[str, Any]]
                         ) -> dict[str, Any] | None:
        if not mentions:
            return None
        span = mentions[0]["span"]
        origin = span["origin"]
        return _envelope(span["untrusted_text"], origin["citation"], origin.get("field_path"))

    def _original_documents(self, arguments: Mapping[str, Any], actor: Actor,
                            info: Mapping[str, Any], since: dt.datetime | None,
                            until: dt.datetime | None
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Operator originals (``OriginalStore``) and captured case originals the reader may
        read now, matched by exact identifier keys in their extracted text, or by the
        query's words in their file name or extracted text (a captured original's stored
        reading, never its bytes; text only of originals the reader may open). Captured
        originals whose file name matches and that the reader may not open are one
        ``original_not_openable`` gap per gate kind: a count, never a name."""
        gaps: list[dict[str, Any]] = []
        if self.reader is None:
            gaps.append(_gap("not_ingested", "source_not_configured", "source:original",
                             "this host passed no originals reader", {"sources": ["original"]}))
            return [], gaps
        if info["explicit_at"]:
            gaps.append(_gap("unknown", "originals_not_versioned", "source:original",
                             "operator originals are read as they are now, not as known at "
                             "an earlier time; they were not searched",
                             {"sources": ["original"]}))
            return [], gaps
        if since or until:
            gaps.append(_gap("unknown", "undated_excluded", "source:original",
                             "operator originals carry no document date; they were left "
                             "out of the since/until window", {"sources": ["original"]}))
            return [], gaps
        with self.reader.batch():
            return self._original_documents_in(arguments, actor, info, gaps)

    def _original_documents_in(self, arguments: Mapping[str, Any], actor: Actor,
                               info: Mapping[str, Any], gaps: list[dict[str, Any]]
                               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        listed = self.reader.originals(actor, ORIGINAL_SCAN)
        if len(listed) >= ORIGINAL_SCAN:
            gaps.append(_gap("unknown", "scan_cap", "source:original",
                             f"only the newest {ORIGINAL_SCAN} readable originals were "
                             "scanned", {"sources": ["original"]}))
        query = arguments.get("query")
        root = arguments.get("object_id")
        capped: list[dict[str, Any]] = []
        wanted: set[tuple[str, str]] = set()
        for kind, value in list(info["root_keys"]) + list(info["query_keys"]):
            wanted.add((kind, value))
        tokens = ([t.casefold() for t in ot._text_tokens(query, [])] if query else [])
        words = _content_words(tokens) if tokens else []
        # The capture store's case originals, each behind its source item's gate, those
        # holding the query's words in their name or stored extracted text first.
        captured = self.reader.captured_originals(actor, words, ORIGINAL_SCAN)
        if captured.get("cut"):
            gaps.append(_gap("unknown", "scan_cap", "source:original",
                             "only the first captured case originals (file names matching "
                             "the query first) were gated and scanned",
                             {"sources": ["original"]}))
        if captured.get("refused"):
            gaps.append(_gap("inaccessible", "originals_store_refused", "source:original",
                             "the captured originals store refused this reader or could not "
                             "be read; none of its originals were searched",
                             {"sources": ["original"]}))
        for kind, count in sorted((captured.get("withheld") or {}).items()):
            if count:
                gaps.append(_gap(
                    "inaccessible", "original_not_openable", f"source:original:{kind}",
                    f"{count}{'+' if captured.get('cut') else ''} captured original(s) "
                    "match this query by file name but this reader cannot open them ("
                    + _WITHHELD_KIND_TEXT.get(kind, kind) + "); their names and text are "
                    "withheld", {"sources": ["original"]}))
        listed = list(listed) + list(captured.get("found") or [])
        bare = info["bare"]
        query_wanted = set(info["query_keys"])
        out = []
        for summary in listed:
            citation = summary.get("citation")
            if not isinstance(citation, str) or not _HANDLE.match(citation):
                continue
            filename = str(summary.get("filename") or "")
            name_hit = bool(tokens) and all(t in filename.casefold() for t in tokens)
            # A captured original's search already matched its stored text; an operator
            # original's text is read here (its store keeps no separate reading).
            captured_match = summary.get("match", False)
            needs_text = bool(wanted) or (bool(query) and not name_hit and (
                captured_match is False or captured_match == "text"))
            text_view = self.reader.original_text(citation, actor) if needs_text else None
            text_hit = captured_match == "text" if captured_match is not False else (
                text_view is not None and holds_words(
                    words, filename, vocabulary(str(text_view.get("text") or ""))))
            mentions: list[dict[str, Any]] = []
            if text_view is not None and wanted:
                text = text_view.get("text") or ""
                pages = text_view.get("pages") or []
                for found in _text_mentions(text):
                    pair = (found["identifier_kind"], found["normalized"])
                    if found["normalized"] is None:
                        continue
                    read = bare is not None and (
                        self._key_matches(pair, found, query_wanted)
                        or self._twin_of(pair, bare["keys"]))
                    if not (read or self._key_matches(pair, found, wanted)):
                        continue
                    if read:
                        bare["readings"].setdefault(pair[0], set()).add(pair[1])
                    page_handle = _page_citation(citation, pages, found["start"])
                    mention: dict[str, Any] = _with_normalized({
                        "identifier_kind": found["identifier_kind"],
                        "span": _envelope(text[found["start"]:found["end"]], page_handle),
                        "tier": "lead", "external_assertion": True},
                        found["identifier_kind"], str(found["normalized"])[:200], page_handle)
                    if root is not None:
                        mention["object_id"] = root
                    mentions.append(mention)
            if wanted and not mentions and text_view is not None:
                # No match on the pages read is never absence from the pages not read.
                skipped = extraction_skipped(
                    (text_view.get("view") or {}).get("extraction") or {})
                if skipped is not None:
                    capped.append(skipped_gap(citation, skipped))
            by_root = root is None or bool(mentions)
            by_query = True
            if query:
                by_query = bool(mentions) if not tokens else (
                    bool(mentions) or name_hit or text_hit)
            if not (by_root and by_query):
                continue
            snippet = mentions[0]["span"] if mentions else None
            if snippet is None and text_hit and not name_hit and text_view is not None:
                snippet = _text_snippet(citation, text_view, words, filename)
            title = _envelope(filename, citation) if filename else None
            out.append({"citation": citation, "kind": "original", "title": title,
                        "date": None, "snippet": snippet,
                        "mentions": mentions[:MAX_MENTIONS], "_order": (None, citation)})
        return out, gaps + capped_gaps(capped, "source:original", "original(s)")

    def _twin_of(self, pair: tuple[str, str], readings: Sequence[tuple[str, str]]) -> bool:
        same = getattr(self.base.grammar, "same_number", None)
        return same is not None and any(
            kind == pair[0] and self._same(same, kind, value, pair[1])
            for kind, value in readings)

    def _key_matches(self, pair: tuple[str, str], found: Mapping[str, Any],
                     wanted: set[tuple[str, str]]) -> bool:
        if pair in wanted:
            return True
        return any(key in wanted for key in self._search_keys(
            pair[0], pair[1], str(found.get("text") or "")))

    # ------------------------------------------------------------------ plx.read

    def read_unbounded(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        """``plx.read`` of already validated ``arguments`` (no paging arguments) before
        the output contract check and the size bound, for a host that pages the result
        itself (``operating/case_tools_host.PagedRead``). Raises ``ToolError`` and
        ``sqlite3.Error`` like the tool body."""
        return self._read(arguments, actor)

    def _read(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        handle = arguments["citation"]
        scheme = _scheme(handle)
        page = arguments.get("page")
        if page is not None and scheme not in ("original", "sharepoint"):
            raise ToolError("page_not_applicable",
                            "page applies to original:// and sharepoint:// citations only",
                            "omit page, or read an original:// or sharepoint:// citation")
        max_chars = arguments.get("max_chars", DEFAULT_READ_CHARS)
        source = ot._source_of(handle)
        meta = self.base._meta(self.base._store_meta(), self.base.clock(),
                               {source} if source else ())
        base: dict[str, Any] = {"citation": handle, "gaps": [], "meta": meta}
        if self.reader is None:
            return dict(base, status="unresolvable",
                        gaps=[_gap("not_ingested", "source_not_configured", handle[:700],
                                   "this host passed no citation reader")],
                        completeness={"complete": False, "truncated": False, "returned": 0,
                                      "reason": "source_not_configured"})
        read = self.reader.read(handle, actor, page=page)
        status = read.get("status")
        gaps = list(read.get("gaps") or [])
        if status not in ("resolved", "changed_since"):
            status = "withheld" if status == "withheld" else "unresolvable"
            return dict(base, status=status, gaps=gaps[:MAX_GAPS],
                        completeness={"complete": False, "truncated": False, "returned": 0,
                                      "reason": status})
        lineage = read.get("lineage")
        digest = read.get("digest")
        if not (isinstance(lineage, str) and _LINEAGE.match(lineage)) \
                or not (isinstance(digest, str) and _DIGEST.match(digest)):
            gaps.append(_gap("unknown", "lineage_or_digest_unknown", handle[:700],
                             "the record resolved but its lineage or digest is not known; "
                             "it is not shown"))
            return dict(base, status="unresolvable", gaps=gaps[:MAX_GAPS],
                        completeness={"complete": False, "truncated": False, "returned": 0,
                                      "reason": "lineage_unknown"})
        document = bool(read.get("document"))
        limit = self.limits["max_untrusted_text_chars"]
        fields: dict[str, Any] = {}
        wanted = arguments.get("field_path")
        # The citation the values were read at (a live changed_since: the current one).
        origin = read.get("values_citation") or handle
        for path, value in (read.get("fields") or {}).items():
            if wanted is not None and path != wanted and not path.startswith(wanted + "."):
                continue
            fields[path] = field_value(value, origin, path, document, limit)
        if wanted is not None and wanted not in fields and wanted != read.get("content_field"):
            # The one field reader: absent only when the field was read and is empty or
            # missing; a column the reader does not mirror, or JSON text that does not
            # parse, is unknown (a trimmed field is never reported absent).
            got, value = wanted_field(read.get("fields") or {}, wanted)
            if got is True:
                fields[wanted] = field_value(value, origin, wanted, document, limit)
            elif value_state(got, value)["state"] == "absent":
                gaps.append(_gap("absent_in_source", "no_value", wanted,
                                 "the cited record has no such field"))
            else:
                gaps.append(_gap("unknown", got, wanted,
                                 "this reader cannot read that field of the cited record; "
                                 "absence from the source is not established"))
        result = dict(base, status=status, digest=digest, lineage=lineage)
        if read.get("current_citation") and status == "changed_since":
            result["current_citation"] = read["current_citation"]
        truncated = False
        content = read.get("content")
        if isinstance(content, str) and (wanted is None or wanted == read.get("content_field")):
            cut = len(content) > max_chars
            truncated = cut
            result["content"] = _envelope(content[:max_chars],
                                          read.get("content_citation") or handle,
                                          read.get("content_field"), limit, cut=cut)
        unread = text_not_extracted(read, handle, wanted)
        if unread is not None:
            # A file whose text was not extracted: never served as an empty text, and
            # never a complete read (what it says is unread, not absent).
            result.pop("content", None)
            gaps.insert(0, unread)
        if len(fields) > 400:
            fields, truncated = dict(list(fields.items())[:400]), True
        if fields or "content" not in result:
            result["fields"] = fields
        result["gaps"] = gaps[:MAX_GAPS]
        result["completeness"] = _completeness(len(fields) + ("content" in result), None,
                                               truncated, "max_chars" if truncated else None,
                                               None)
        if unread is not None and not truncated:
            result["completeness"].update(complete=False, reason=TEXT_NOT_EXTRACTED)
        if page is not None and status == "resolved":
            # A page read cites that page: the citation it returns is the page's
            # (``#page=<n>``, the one the answer gate checks the page text of).
            from ontology.mentions import cited_page
            cited = read.get("content_citation")
            if isinstance(cited, str) and cited_page(cited)[1] == page:
                result["citation"] = cited
        if ot._envelope_bytes(result) > self.limits["max_result_bytes"] and "content" in result:
            text = result["content"]["untrusted_text"]
            while text and ot._envelope_bytes(result) > self.limits["max_result_bytes"]:
                text = text[:len(text) // 2]
                result["content"] = dict(result["content"], untrusted_text=text,
                                         truncated=True)
            result["completeness"] = _completeness(result["completeness"]["returned"], None,
                                                   True, "result_size", None)
        spans = read.get("content_pages") if "content" in result else None
        if spans:
            result = with_pages(result, covered_pages(
                read.get("content_citation") or handle, spans, 0,
                len(result["content"]["untrusted_text"])), self.limits["max_result_bytes"])
        return result


# --------------------------------------------------------------------------- pure helpers

TEXT_NOT_EXTRACTED = "text_not_extracted"
# A live read of a file: its metadata, read now; its text is read from a capture only.
LIVE_METADATA_ONLY = "live_metadata_only"
# The mail capture relations of a file (whose text is extracted), not of a message body.
_MAIL_FILES = ("graph-mail.attachment", "graph-mail.containerMember")


def _extraction_status(extraction: Mapping[str, Any]) -> str:
    """An original's extraction status as a reason: ``status`` with its first warning
    (``unsupported:unsupported_mime_type:image/heic``), ``unknown`` when unrecorded."""
    status = str(extraction.get("status") or "unknown")
    warnings = [str(w) for w in extraction.get("warnings") or () if w]
    return f"{status}:{warnings[0]}" if status != "extracted" and warnings else status


def text_not_extracted(read: Mapping[str, Any], handle: str, wanted: str | None
                       ) -> dict[str, Any] | None:
    """The ``text_not_extracted`` gap of a reader's file read (``read``: it states the
    file's ``text_status``) that holds no text (none, or only white space) where its text
    was asked for (no ``field_path``, or the content field), else None. The reason is the
    reader's status: ``unsupported`` (a type no extractor reads), ``ocr_pending``,
    ``exceeds_bound``, ``failed`` and so on. A reader that states no ``text_status`` (a
    record, a message body) is never a file whose text was not extracted."""
    status = read.get("text_status")
    content = read.get("content")
    if not read.get("document") or not isinstance(status, str) or (
            wanted is not None and wanted != read.get("content_field")) or (
            isinstance(content, str) and content.strip()):
        return None
    if status.startswith("extracted"):   # read, yet nothing of it (or of this page) is text
        status = "no_text"
    return _gap("not_ingested", TEXT_NOT_EXTRACTED, handle[:700],
                f"no text was extracted from this file ({status[:120]}): what it says is "
                "unread, not absent; its fields are its metadata only")


def skipped_gap(subject: str, skipped: Mapping[str, Any]) -> dict[str, Any]:
    """The typed partial-text gap of a reading capped by pages (``skipped_by_cap``:
    ``first_page``, ``last_page``, ``limit``)."""
    first, last = skipped.get("first_page"), skipped.get("last_page")
    pages = f"pages {first}-{last}" if first is not None else "pages past the page cap"
    read = f"pages 1-{first - 1}" if isinstance(first, int) else "its first pages"
    return _gap("not_ingested", "skipped_by_cap", subject[:700],
                f"{pages} of this file are past the extraction's page cap and were not "
                f"read: its text is {read} only, never the whole file, and nothing on "
                "the pages not read is searched or shown")


def capped_gaps(capped: Sequence[dict[str, Any]], scope: str, what: str
                ) -> list[dict[str, Any]]:
    """The ``skipped_by_cap`` gaps of a search's files read on their first pages only that
    did not match: the first ``CAPPED_LISTED`` by name, the rest counted in one gap."""
    out = list(capped[:CAPPED_LISTED])
    if len(capped) > CAPPED_LISTED:
        out.append(_gap("not_ingested", "skipped_by_cap", scope,
                        f"{len(capped) - CAPPED_LISTED} further {what} you may see were "
                        "read on their first pages only (the page cap): identifiers on "
                        "their pages not read were not searched"))
    return out


def field_value(value: Any, handle: str, path: str, document: bool, limit: int) -> Any:
    """A row value in the plx.read ``fields`` shape. Numbers, booleans and nulls of a
    structured row stay typed (numbers as Decimal text); every string, every nested
    value and every field of a document citation is an untrusted_text envelope."""
    field_path = path if ot._FIELD_PATH.match(path) else None
    if value is None or isinstance(value, bool) and not document:
        return value
    if document:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True,
                                                               default=str)
        return _envelope(text, handle, field_path, limit)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            text = format(Decimal(repr(value)).normalize(), "f")
        except InvalidOperation:
            return _envelope(repr(value), handle, field_path, limit)
        return text if _DECIMAL.match(text) else _envelope(repr(value), handle,
                                                           field_path, limit)
    if isinstance(value, str):
        return _envelope(value, handle, field_path, limit)
    return _envelope(json.dumps(value, sort_keys=True, default=str), handle, field_path,
                     limit)


def wanted_field(fields: Mapping[str, Any], path: str) -> tuple[bool | str, Any]:
    """``capability_guards.read_field`` of ``path`` (``row.col`` or ``row.col.key``, also
    ``key.`` and ``rows.N.``) over the row the reader returned, whose columns are the
    ``fields`` under the same prefix."""
    parts = path.split(".")
    prefix = ".".join(parts[:2 if parts[0] == "rows" else 1]) + "."
    row = {name[len(prefix):]: value for name, value in fields.items()
           if name.startswith(prefix) and "." not in name[len(prefix):]}
    column, *keys = path[len(prefix):].split(".")
    got, value = read_field(row, f"{column}.{keys[0]}" if keys else column)
    for key in keys[1:]:
        if got is not True:
            break
        if not isinstance(value, Mapping):
            return "json_not_object", None
        if key not in value:
            return False, None
        value = value[key]
    return got, value


def _reading_gap(query: str, readings: Mapping[str, set[str]], sources: Sequence[str]
                 ) -> dict[str, Any]:
    """The kinds a bare query was read as, each with how many distinct stored values the
    reader may see hold it (the values themselves are in the documents' typed mentions;
    a free-format kind's value is source text, never put in a gap bare). Two or more
    readings are ``ambiguous_identifier``: none is chosen. A reading counts where a record
    the reader may see holds it, so its documents may be hidden from the reader or lie
    outside the requested sources: the gap says only what was listed."""
    kinds = sorted(readings)
    named = ", ".join(f"{kind} ({len(readings[kind])} value"
                      f"{'' if len(readings[kind]) == 1 else 's'})"
                      for kind in kinds[:MAX_READINGS])
    if len(kinds) > MAX_READINGS:
        named += f" and {len(kinds) - MAX_READINGS} more kind(s)"
    count = sum(len(values) for values in readings.values())
    subject = f"query:{' '.join(query.split())}"[:700]
    if count == 1:
        return _gap("unknown", "identifier_reading", subject,
                    f"the unlabelled query was read as {named}, the only identifier "
                    "reading that a record or original visible to this reader holds; a "
                    "label names the kind")
    return _gap("unknown", "ambiguous_identifier", subject,
                f"the unlabelled query reads as {count} identifiers visible to this reader: "
                f"{named}; listed are each reading's documents this reader may see in the "
                f"requested sources ({', '.join(sources[:8])}), and none is chosen; "
                "a label names the kind")


def _scope_apart(scopes: Mapping[tuple[str, str, str], tuple[Any, str, Any]],
                 root: Mapping[str, Any], other: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``identity_scope`` feature keeping two records that share a value of a kind
    unique only within a scope apart (``ot.identity_scopes``: a customer PO per customer),
    or None when neither kind is scoped or both name one identity. ``agrees`` false: two
    scope values of one scope space (two customers of one system); null: either names no
    identity (no scope value, field or a withheld one) or the scopes are two systems'
    columns, never compared. Only a pair kept apart (false) is never an
    ``ambiguous_identity``: identity review never spans two customers; a pair whose
    customers are unknown or not comparable stays a question for a person."""
    mine = scopes.get((root["object_id"], root["l1_handle"], root["field_path"]))
    theirs = scopes.get((other["object_id"], other["l1_handle"], other["field_path"]))
    if mine is None and theirs is None:
        return None
    left, right = (mine or (None,))[0], (theirs or (None,))[0]
    if left is not None and left == right:
        return None
    same_space = left is not None and right is not None and left[0] == right[0]
    return {"feature": ot.SCOPE_FEATURE, "agrees": False if same_space else None}


def _cite(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not (_HANDLE.match(row["l1_handle"] or "") and ot._FIELD_PATH.match(row["field_path"] or "")
            and _DIGEST.match(row["digest"] or "") and _LINEAGE.match(row["lineage"] or "")):
        return None
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}


# Words a document search never requires a document to hold: question and function words
# a natural-language query carries ("which rules does the proposal put to Vince").
STOP_WORDS = frozenset({
    "about", "after", "and", "any", "are", "before", "but", "can", "could", "did", "does",
    "for", "from", "get", "had", "has", "have", "her", "his", "how", "into", "its", "our",
    "put", "say", "says", "she", "should", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "was", "were", "what", "when", "where",
    "which", "who", "whom", "whose", "why", "will", "with", "would", "you", "your"})
# A query of at least this many words that no document holds whole is searched again with
# each word left out in turn (``partial_word_match``).
RELAX_MIN_WORDS = 3


def _text_mentions(text: Any) -> Sequence[Mapping[str, Any]]:
    """``ontology.mentions.extract(text)``, remembered by the text's SHA-256 and the
    identifier grammar it ran with. A pure function of the text: a documents search
    reads the same originals' texts on every call, and the extraction was most of its
    cost. Nothing about a reader is in the key or the result (each call still opens
    the text through its own gate first). An extraction that raises is not remembered
    and raises to the caller as before. Least recently used out first past
    ``TEXT_MENTIONS_BOUND`` mentions."""
    global _MENTIONS_HELD
    from ontology.mentions import extract
    if not isinstance(text, str) or not text:
        return extract(text)
    key = (id(identifiers.schema()), len(text),
           hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest())
    with _MENTIONS_LOCK:
        found = _MENTIONS.get(key)
        if found is not None:
            _MENTIONS.move_to_end(key)
            return found
    found = tuple(extract(text))
    with _MENTIONS_LOCK:
        if key not in _MENTIONS:
            _MENTIONS[key] = found
            _MENTIONS_HELD += 1 + len(found)
        while _MENTIONS_HELD > TEXT_MENTIONS_BOUND and len(_MENTIONS) > 1:
            _MENTIONS_HELD -= 1 + len(_MENTIONS.popitem(last=False)[1])
    return found


_MENTIONS: OrderedDict[tuple, tuple] = OrderedDict()
_MENTIONS_LOCK = threading.Lock()
_MENTIONS_HELD = 0


def _content_words(tokens: Sequence[str]) -> list[str]:
    """The query words a document must hold: ``tokens`` without ``STOP_WORDS`` (all of them
    when every word is one), each once."""
    kept = [t for t in tokens if t.casefold() not in STOP_WORDS]
    return list(dict.fromkeys(kept or tokens))


def _window(text: Any, token: str, handle: str, field_path: str) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    at = text.casefold().find(token.casefold())
    start = max(0, at - SNIPPET_CHARS // 2) if at >= 0 else 0
    piece = text[start:start + SNIPPET_CHARS]
    return _envelope(piece, handle, field_path, cut=len(piece) < len(text))


def _text_snippet(citation: str, text_view: Mapping[str, Any], words: Sequence[str],
                  filename: str) -> dict[str, Any] | None:
    """The window of an original's extracted text around its first query word the file
    name does not hold, cited to the page holding it."""
    text = str(text_view.get("text") or "")
    folded, name = text.casefold(), filename.casefold()
    for word in words:
        if word in name:
            continue
        found = re.search(r"(?<![^\W_])" + re.escape(word), folded)
        if found is None:
            continue
        start = max(0, found.start() - SNIPPET_CHARS // 2)
        piece = text[start:start + SNIPPET_CHARS]
        handle = _page_citation(citation, text_view.get("pages") or [], found.start())
        return _envelope(piece, handle, cut=len(piece) < len(text))
    return None


def covered_pages(citation: str, pages: Sequence[Any], start: int, end: int
                  ) -> list[dict[str, Any]]:
    """The pages of an extraction's text (``pages``: ``page``, ``start``, ``end`` offsets
    into it) that the returned slice ``[start, end)`` of it overlaps, each with its page
    citation (``#page=<n>``, ``&page=<n>`` after an extraction pin): ``page``,
    ``page_citation``, and the page's own ``offset`` and ``chars`` in the whole text. A
    page with no text, or a span that is not a well-formed one, is never listed (no
    citation of a page whose text was not returned)."""
    out: list[dict[str, Any]] = []
    sep = "&" if "#" in citation else "#"
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        number, first, last = page.get("page"), page.get("start"), page.get("end")
        if not all(isinstance(v, int) and not isinstance(v, bool)
                   for v in (number, first, last)) or number < 1 or not 0 <= first < last:
            continue
        if first < end and start < last:
            out.append({"page": number, "page_citation": f"{citation}{sep}page={number}",
                        "offset": first, "chars": last - first})
    return out


def with_pages(result: dict[str, Any], pages: list[dict[str, Any]], max_bytes: int,
               *, cut_text: bool = True) -> dict[str, Any]:
    """``result`` with ``pages`` (``covered_pages``) added, as many as fit ``max_bytes``
    after the text, which keeps its place. When not all fit, the first that do are listed
    and a ``page_citations_cut`` gap names the first page left out, never a silent cut:
    with a full gap list it is merged into the last gap (that gap is kept); when even
    the gap with no page listed does not fit, the text is cut to make room for it
    (``cut_text``; else that answer is returned too large, for the caller to shrink)."""
    if not pages:
        return result
    whole = dict(result, pages=pages)
    if ot._envelope_bytes(whole) <= max_bytes:
        return whole

    def fitted(count: int) -> dict[str, Any]:
        first = pages[count]
        detail = (f"the page citations from page {first['page']} on did not fit this "
                  "answer; read the original with page, or with text_offset past the "
                  "pages listed")
        gaps = list(result.get("gaps") or [])
        if len(gaps) < MAX_GAPS:
            gaps.append(_gap("unknown", "page_citations_cut",
                             first["page_citation"][:700], detail))
        else:
            # A full list: the last gap stays, and says the cut too.
            kept = dict(gaps[MAX_GAPS - 1])
            also = f"; also, {detail}"
            kept["detail"] = str(kept.get("detail") or "")[:600 - len(also)] + also
            gaps = gaps[:MAX_GAPS - 1] + [kept]
        out = dict(result, gaps=gaps)
        if count:
            out["pages"] = pages[:count]
        return out

    bare = fitted(0)
    if ot._envelope_bytes(bare) > max_bytes and not cut_text:
        return bare
    if ot._envelope_bytes(bare) > max_bytes:
        # No room even for the gap: cut the text (said truncated) until the gap fits.
        content = result.get("content")
        text = content.get("untrusted_text") if isinstance(content, dict) else None
        while isinstance(text, str) and text and ot._envelope_bytes(bare) > max_bytes:
            text = text[:max(0, len(text) - 512)]
            bare = dict(bare, content=dict(content, untrusted_text=text, truncated=True))
        if "content" in bare:
            bare["completeness"] = _completeness(
                bare["completeness"]["returned"], None, True, "result_size", None)
        return bare
    low, high = 0, len(pages) - 1          # the largest count that fits with its gap
    while low < high:
        mid = (low + high + 1) // 2
        if ot._envelope_bytes(fitted(mid)) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return fitted(low)


def _page_citation(citation: str, pages: Sequence[Mapping[str, Any]], offset: int) -> str:
    """The page citation of the page holding ``offset`` in an extraction's text."""
    for page in pages:
        if isinstance(page, Mapping) and page.get("start", 0) <= offset < page.get("end", 0):
            sep = "&" if "#" in citation else "#"
            return f"{citation}{sep}page={page.get('page')}"
    return citation


def _document_order(doc: Mapping[str, Any]) -> tuple:
    """Hybrid search's relevance order first (``_rank``, PLX_SEARCH=hybrid), then the
    newest first; without a rank every document keeps the date order."""
    moment, handle = doc.get("_order") or (None, doc["citation"])
    stamp = -moment.timestamp() if moment is not None else float("inf")
    return (doc.get("_rank", float("inf")), stamp, handle)


# --------------------------------------------------------------------------- host wiring

def tool_specs(contracts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """MCP tool listings (name, description, inputSchema) for these tools."""
    contracts = contracts or ot.load_contracts()
    return [{"name": name, "description": contracts["tools"][name]["description"],
             "inputSchema": ot._inline_defs(contracts["tools"][name]["input"], contracts),
             "annotations": {"readOnlyHint": True}}
            for name in TOOL_NAMES]


def register(tool_table: MutableMapping[str, dict[str, Any]], tools: EvidenceTools) -> None:
    """Add the three tools to a case host's tool table (as L8's ``register``)."""
    for spec in tool_specs(tools.contracts):
        name = spec["name"]
        if name in tool_table:
            raise ValueError(f"tool already registered: {name}")
        tool_table[name] = dict(spec, handler=(lambda arguments, actor, _n=name:
                                               tools.call(_n, arguments, actor)))


__all__ = ["CitationReader", "EvidenceTools", "PAGING_ARGUMENTS", "RuntimeCitations",
           "TOOL_NAMES", "field_value", "register", "tool_specs", "wanted_field"]
