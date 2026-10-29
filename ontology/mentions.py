"""Identifier mentions projected from cited document captures (lane L16).

10-DECISION 3 and 7, 09-CRITIQUE R3 and R6, 07-ONTOLOGY-DESIGN C principle 5 and
C.2 (``Document mentions any object``). A *mention* is a span of captured
document text that names an identifier. It is machine-extracted, never a fact,
and it reaches an object only as a candidate of the identity layer.

Input
-----
* :class:`Document`: one captured document version with its L1 handle, the text
  fields to scan (each with its own citation and field path) and the sender class.
  :func:`mail_documents` reads them from the versioned mail capture
  (``integrations/capture_mail.MailCaptureStore``), for one reader and per that
  reader's audience. ``ontology.project.SharePointCaptures.documents`` gives captured
  SharePoint files (name and extracted text) as the same ``Document``,
  ``ontology.project_mc.McCaptures.documents`` Mission Control tasks, approval gates and
  knowledge entries; operator-original text plugs in the same way. This module has no source reader.

What it decides
---------------
* **Extraction** (:func:`extract`): only the one identifier grammar
  (``ontology/identifiers.find_identifiers`` over ``ontology/schema/identifiers.json``)
  turns untrusted text into typed values. Each mention keeps the exact span of the
  value as written, its label, the profile that found it and the normalisation
  applied (normaliser, raw -> normalised, blocking key). Text is data: nothing in
  it is read as an instruction, and no model is involved.
  A value whose shape is one kind's but whose label assigns another kind
  (``Customer PO: PO100234`` in a supplier's acknowledgement), or a number another
  kind's labelled form also reads (``Customer PO: 5280`` holds ``PO: 5280``), gives
  the label reading plus a secondary reading flagged ``label_kind_conflict``; the
  secondary resolves like any mention but only ever as a lead with that why_not.
* **Quoted history** (:func:`quoted_ranges`): a span inside reply/forward history
  (``>`` lines, divider lines in any language, ``FW:``-style subject lines, "On ...
  wrote:", Outlook ``From:``/``Sent:`` blocks, also flattened to one line) is flagged
  ``quoted_history``. Quoted text was written by someone else, so it is always an
  external assertion, even in an internal sender's message.
* **External assertions**: a mention whose text an internal sender did not write
  (external or unknown sender, or quoted history) is an ``external_assertion``. A
  sender is internal only with DMARC pass for its internal domain (:func:`sender_class`).
  By itself it can never confirm a payment, receipt or quantity fact, and no
  lead-tier mention confirms one either (:func:`confirmation_support`; R6 #4).
* **Copies** (``Document.copy_key``, from ``ontology.identifiers.copy_key``): one message
  delivered to several mailboxes (one ``internet_message_id`` with equal headers and
  body preview: a forged or reused Message-ID on another message groups nothing) and one file
  attached, stored or packed in a container (one ``content_sha256``) are copies. Each
  copy stays its own document with its own citation and permission; its links, read
  and stored alike, name its ``copy_group`` and :func:`confirmation_support` counts the
  evidence of one copy group once, listing every copy's links and citations. A document
  without a valid copy key is a group of its own.
* **Resolution** (:class:`FindResolver`): every parsed mention goes through the
  identity layer, ``plx.find`` with the mention's identifier kind (its alias kinds
  included), as the reading actor. Each visible candidate becomes one mention link
  (``Document.mentions``, binding ``mention``). Several candidates stay several
  links with ``resolution: ambiguous`` and no winner; one candidate is still a
  candidate (``single_candidate``), never a merge. A read-time link is a ``lead``.
  Every link carries ``reason`` (the agreeing features with their citations) and
  ``why_not`` (what keeps it a lead).
* **Corroboration** (:data:`MENTION_FEATURES`, :func:`corroborate_links`): a stored
  link of a link type whose schema binding allows ``corroborated``
  (``ontology/schema.py`` ``MENTION_LINK_RULE``: ``Email.mentions_<type>``) rises to
  ``corroborated`` only when two or more independent features agree (07 C.2) and no
  veto holds: the mention itself, the sender's domain at a distinctive domain of a party
  the target links to (``party_domain``), and an internal sender's own mention of the
  same target in the same conversation (``petra_thread``); vetoes are quoted history, a
  label/kind conflict, a message naming several objects of the target's type and a
  message dated before the target. One rule for every target type (the retired
  ``operating/po_mail_links.py`` per-scan supplier-domain, PO-date and Petra-thread
  checks are these features). A reader is shown a corroborated link at its tier only
  when it may read every record the corroborating features cite
  (:func:`corroboration_handles`, :func:`reader_view`), whatever else the link cites.

Stored links (:func:`store_mention_links`, decision 2026-10-02-mail-and-mentions-store
#2): a ``build_store`` ``post_project`` step writes every mention as ``lead`` links of
the declared ``Email.mentions_<type>`` link types into the same store generation (then
raises those the features corroborate, :func:`corroborate_links`), so
``plx.object`` and ``plx.around`` reach them; candidates come from the generation's
identifier index and, when the build passes them, the reviewed aliases of the review
store (``ontology.reviews.BuildAliases``: a supplier number a person confirmed as a PO's
alias), and every reader is re-gated on read (both ends and every cited handle
visible). :func:`project` stays the per-reader read-time view.

Output (:func:`project`): ``plx-mentions/v1`` with links, unresolved mentions
(``unparsed``, ``not_found``, ``resolver_error``) and value-free counts. The result
is deterministic for the same captures, store and reader (``digest``).
:func:`documents_mentions` renders links in the ``plx.documents`` mention shape.

Stdlib only. Imports nothing from the legacy ontology store and nothing from
``operating`` (the resolver is passed in).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from ontology import identifiers as ids
from ontology.store import held_stretches

FORMAT = "plx-mentions/v1"
LINK_TYPE = "Document.mentions"
BINDING_KIND = "mention"
TIER = "lead"
SENDER_CLASSES = ("internal", "external", "unknown")
DOCUMENT_KINDS = ("email", "attachment", "sharepoint_file", "container_member", "original",
                  "mc_task", "mc_approval", "knowledge_entry", "teams_message")
# Why a producer withheld a document's text (``Document.text_withheld``): one known form,
# ``pdf_pages_skipped_by_cap:<first>-<last>``.
WITHHELD_PAGES_SKIPPED = "pdf_pages_skipped_by_cap"
# Facts an external assertion can never confirm by itself (09-CRITIQUE R6 #4).
EXTERNAL_UNCONFIRMABLE = ("payment", "receipt", "quantity")
# Tiers at which internal evidence may confirm a payment/receipt/quantity fact.
CONFIRMING_TIERS = ("verified", "reviewed", "corroborated")
RESOLUTIONS = ("single_candidate", "ambiguous")
UNRESOLVED = ("unparsed", "not_found", "resolver_error")
FIND_PAGE = 50          # plx.find limit maximum
MAX_FIND_PAGES = 4      # candidates followed per mention (200); more is truncated
SPAN_CHARS = 200        # characters of a span shown as untrusted text


def extractor_version() -> str:
    """Extractor id plus the digest of the grammar file it ran with."""
    digest = sha256(ids.SCHEMA_PATH.read_bytes()).hexdigest()[:16]
    return f"ontology.mentions/1+identifiers@{digest}"


# --------------------------------------------------------------------------- quoted history

# The start of quoted reply/forward history in a plain-text body (LF or CRLF, as
# Exchange sends it): any divider line, in any language: a short text between dash
# runs ("-----Original Message-----", "---------- Weitergeleitete Nachricht ---------")
# or a run of underscores (Outlook's HTML reply rule flattened to text); a forward
# introduction ("Begin forwarded message:" and its translations); a forwarded or
# replied subject line ("FW:", "Fwd:", "WG:", "TR:", "RV:", "ENC:", "VS:", "Doorst:" ...);
# an "On ... wrote:" attribution line (English, German, French, Italian, Spanish); or an
# Outlook header block: a From line followed within three lines by a Sent/Date line, or
# both on one line (an HTML header block flattened to text). Plus every '>' line. What
# none of these marks stays unquoted and is still only a lead; a false mark only loses
# recall (quoted text is an external assertion), never identity.
_SEPARATOR = (r"-{2,}[^\S\r\n]*[^\s\-][^\r\n]{0,60}?[^\S\r\n]*-{2,}|_{8,}|"
              r"(?:Begin forwarded message|Anfang der weitergeleiteten Nachricht|"
              r"D(?:é|e)but du message r(?:é|e)exp(?:é|e)di(?:é|e)|"
              r"Inizio messaggio inoltrato|Comienzo del mensaje reenviado|"
              r"In(?:í|i)cio da mensagem reencaminhada|Begin doorgestuurd bericht)[ \t]*:|"
              r"(?:FWD?|WG|TR|RV|ENC|VS|VB|PD|Doorst|Wtg|I)[ \t]*:[ \t]*\S[^\r\n]*")
# An attribution may wrap over several lines without '>' (no blank line inside, at
# most 300 characters). Own text that starts "On ..." and reaches "... wrote:" reads
# as quoted: recall is lost, never identity.
_WRAP = r"(?:[^\r\n]|\r?\n(?![ \t]*\r?\n)){1,300}?"
_ATTRIBUTION = (rf"(?:On {_WRAP} wrote|Am {_WRAP} schrieb(?: {_WRAP})?|"
                rf"Le {_WRAP} a (?:é|e)crit|Il {_WRAP} ha scritto|El {_WRAP} escribi(?:ó|o))"
                r"[ \t]*:")
_HEADER_FROM = r"(?:From|Von|De|Da|Van|Fr(?:å|a)n|Fra|Od|Lähettäjä)"
_HEADER_SENT = (r"(?:Sent|Date|Gesendet|Datum|Envoy(?:é|e)|Inviato|Enviado|Fecha|Data|"
                r"Verzonden|Skickat|Sendt|Wys(?:ł|l)ano|L(?:ä|a)hetetty)")
_HISTORY_START = re.compile(
    rf"^[ \t]*(?P<marker>{_SEPARATOR}|{_ATTRIBUTION})[ \t]*\r?$"
    rf"|^[ \t]*{_HEADER_FROM}:[^\n]*\n(?:[^\n]*\n){{0,3}}?[ \t]*{_HEADER_SENT}:"
    rf"|{_HEADER_FROM}:[^\r\n]{{1,400}}?\b{_HEADER_SENT}:",
    re.IGNORECASE | re.MULTILINE)
_QUOTED_LINE = re.compile(r"^[ \t]*>[^\n]*", re.MULTILINE)
# A bottom-posted reply: an attribution line, then only '>' lines (blank lines
# allowed), then a blank line and the sender's own non-'>' text.
_BOTTOM_POST = re.compile(r"[ \t]*\r?\n(?:[ \t]*>[^\n]*\n|[ \t]*\r?\n)*?[ \t]*\r?\n"
                          r"(?=[ \t]*[^>\s])")


def quoted_ranges(text: Any) -> list[tuple[int, int]]:
    """Character ranges of a text that are quoted history, not the sender's own text.

    Conservative: everything from the first history marker to the end is quoted,
    with one exception that can be recognised reliably. When the marker is an
    attribution line ("On ... wrote:") immediately followed by '>' lines and the
    '>' block is followed by a blank line and non-'>' text, that text is a
    bottom-posted reply: the history ends with the '>' block and the rest is
    scanned again. Any other layout stays quoted."""
    if not isinstance(text, str) or not text:
        return []
    ranges = [(m.start(), m.end()) for m in _QUOTED_LINE.finditer(text)]
    offset = 0
    while offset < len(text):
        start = _HISTORY_START.search(text, offset)
        if start is None:
            break
        end = len(text)
        marker = start.group("marker")
        if marker and re.match(_ATTRIBUTION, marker.strip(), re.IGNORECASE):
            after = start.end()
            first = re.match(r"[ \t]*\r?\n[ \t]*>", text[after:])
            block = _BOTTOM_POST.match(text, after) if first else None
            if block is not None:
                end = block.end()
        ranges.append((start.start(), end))
        if end >= len(text):
            break
        offset = end
    return ranges


class QuotedRanges:
    """The quoted-history ranges of one text, merged and sorted once, so each
    lookup is a binary search (a long thread with thousands of '>' lines stays
    linear overall)."""

    def __init__(self, text: Any = None, *,
                 ranges: Iterable[tuple[int, int]] | None = None) -> None:
        merged: list[list[int]] = []
        for start, end in sorted(quoted_ranges(text) if ranges is None else ranges):
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self._starts = [start for start, _end in merged]
        self._ends = [end for _start, end in merged]

    def overlaps(self, start: int, end: int) -> bool:
        """Whether [start, end) shares a character with a quoted range."""
        if end <= start:
            end = start + 1
        index = bisect_right(self._starts, end - 1) - 1
        return index >= 0 and self._ends[index] > start

    def contains(self, position: int) -> bool:
        return self.overlaps(position, position + 1)

    def __bool__(self) -> bool:
        return bool(self._starts)


# --------------------------------------------------------------------------- extraction

def extract(text: Any, *, kinds: Iterable[str] | None = None,
            quoted: bool = True) -> list[dict[str, Any]]:
    """Identifier mentions in one untrusted text, in text order.

    Each mention: ``identifier_kind``, ``object_type`` (the kind's), ``start``/``end``
    (the value as written), ``label_start``, ``text``, ``label``, ``form``,
    ``profile``, ``normalized`` (None when the value does not parse as its kind:
    a literal mention that resolves to nothing), ``normalisation``,
    ``label_kind_conflict`` and, with ``quoted``, ``quoted_history`` (the label or
    the value overlaps quoted history).

    When a label assigns a value to one kind but the value has another kind's
    shape (``Customer PO: PO100234`` in a supplier's acknowledgement names a Petra
    PO; ``Customer PO: 5280`` also reads as ``PO: 5280``), the label reading comes
    first and the other kind's reading follows it with
    ``label_kind_conflict: True`` and ``secondary_of`` (the label's kind): a lead
    reading, never the reading. ``kinds`` filters both readings by their own
    kind."""
    if not isinstance(text, str) or not text:
        return []
    wanted = None if kinds is None else frozenset(kinds)
    history = QuotedRanges(text) if quoted else None
    out = []
    for found in ids.find_identifiers(text):
        readings = [(found, None)] + [(other, found) for other in found.secondary]
        for reading, primary in readings:
            if wanted is not None and reading.kind not in wanted:
                continue
            label_start = primary.label_start if primary is not None else reading.label_start
            mention = _mention(reading, label_start)
            if primary is not None:
                mention["secondary_of"] = primary.kind
                mention["label"] = primary.label
                mention["normalisation"]["label"] = primary.label
            if history is not None:
                mention["quoted_history"] = history.overlaps(label_start, reading.end)
            out.append(mention)
    return out


def _mention(found: Any, label_start: int) -> dict[str, Any]:
    spec = ids.kind_spec(found.kind)
    try:
        key = ids.blocking_key(found.kind, found.text)
    except (KeyError, ValueError):
        key = None
    return {
        "identifier_kind": found.kind, "object_type": spec.get("object_type"),
        "start": found.start, "end": found.end, "label_start": label_start,
        "text": found.text, "label": found.label, "form": found.form,
        "profile": found.profile, "confidence": found.confidence,
        "normalized": found.normalized,
        "label_kind_conflict": bool(found.label_kind_conflict),
        "normalisation": {"normalizer": spec.get("normalizer"), "raw": found.text,
                          "label": found.label, "normalized": found.normalized,
                          "blocking_key": key, "profile": found.profile,
                          "form": found.form}}


# --------------------------------------------------------------------------- documents

@dataclass(frozen=True)
class TextField:
    """One scanned text of a document: the L1 handle and field path that pin it."""
    citation: str
    field_path: str
    text: str


@dataclass(frozen=True)
class Document:
    """One captured document version (a mail message, a SharePoint file ...)."""
    citation: str
    kind: str
    fields: tuple[TextField, ...]
    sender_class: str = "unknown"
    digest: str | None = None
    recorded_at: str | None = None
    superseded_at: str | None = None
    date: str | None = None
    # (kind, normalised value[, content digest]) of the content this version holds: equal
    # keys are copies (``ontology.identifiers.copy_key``); None when the source states none.
    copy_key: tuple[str, ...] | None = None
    # Why the file's text is not among ``fields`` although it was read (a producer with
    # no gap channel withholds a partial reading: ``pdf_pages_skipped_by_cap:<first>-
    # <last>``, devloop/DECISIONS/pdf-cap-readers-contract.md); None otherwise.
    text_withheld: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in DOCUMENT_KINDS:
            raise ValueError(f"document_kind_invalid:{self.kind!r}")
        if self.copy_key is not None and not (
                isinstance(self.copy_key, tuple) and len(self.copy_key) in (2, 3)
                and all(isinstance(part, str) and part for part in self.copy_key)):
            raise ValueError("document_copy_key_invalid")
        if self.sender_class not in SENDER_CLASSES:
            raise ValueError(f"sender_class_invalid:{self.sender_class!r}")
        if self.text_withheld is not None and not _withheld_valid(self.text_withheld):
            raise ValueError(f"text_withheld_invalid:{self.text_withheld!r}")
        if not isinstance(self.citation, str) or "://" not in self.citation:
            raise ValueError("document_citation_invalid")


# A paged document's text (a PDF's pages as the SharePoint capture stores them) is its
# pages joined by form feeds; each page is cited ``<handle>#page=<n>`` (``&page=`` after
# another fragment), so a mention found on a page cites that page and its offsets are
# the page's. Unpaged text is one field under the document's own handle.
PAGE_BREAK = "\f"


def page_citation(citation: str, page: int) -> str:
    return f"{citation}{'&' if '#' in citation else '#'}page={page}"


def cited_page(citation: str) -> tuple[str, int | None]:
    """(the citation without its trailing ``page=<n>``, n), or (citation, None)."""
    for marker in ("#page=", "&page="):
        head, found, number = citation.rpartition(marker)
        if found and number.isascii() and number.isdigit() and len(number) <= 4 \
                and not number.startswith("0"):
            return head, int(number)
    return citation, None


def text_pages(text: str) -> list[str]:
    return text.split(PAGE_BREAK)


def is_paged_file(record: Mapping[str, Any]) -> bool:
    """A captured file whose text is stored by page: a PDF, by MIME type or name (the
    test ``integrations.document_understanding.extract_document`` applies)."""
    mime = str(record.get("mime_type") or "").split(";", 1)[0].strip().lower()
    name = str(record.get("name") or "").lower()
    return mime == "application/pdf" or (mime in ("", "application/octet-stream")
                                         and name.endswith(".pdf"))


def file_text_fields(citation: str, field_path: str, text: str, *, paged: bool
                     ) -> tuple[TextField, ...]:
    """The scanned fields of a file's text: one per non-blank page of a paged file
    (cited by page), else the whole text under ``citation``."""
    if not paged:
        return (TextField(citation, field_path, text),)
    return tuple(TextField(page_citation(citation, number), field_path, page)
                 for number, page in enumerate(text_pages(text), 1) if page.strip())


def sender_class(address: Any, internal_domains: Iterable[str],
                 authentication_results: Any = None) -> str:
    """``internal`` only when the address's domain is one of ``internal_domains``
    (exact, case-insensitive) and the receiving server's authentication shows DMARC
    pass for that same domain (``dmarc_pass``): a From address alone is what any
    sender may write. An internal-domain address without that proof is ``unknown``;
    any other address is ``external``; no address is ``unknown``."""
    if not isinstance(address, str) or address.count("@") != 1:
        return "unknown"
    domain = address.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return "unknown"
    internal = {d.strip().lower() for d in internal_domains if isinstance(d, str)}
    if domain not in internal:
        return "external"
    return "internal" if dmarc_pass(authentication_results, domain) else "unknown"


def dmarc_pass(authentication_results: Any, domain: str) -> bool:
    """Whether the topmost ``Authentication-Results`` header (the one the receiving
    server prepended; any lower one may be the sender's own) says DMARC ``pass`` for
    ``header.from`` equal to ``domain``, with no other DMARC result in it.
    ``authentication_results``: those header values in message order, as the mail
    capture records them (``integrations.graph_read``); anything else is no proof."""
    if not isinstance(authentication_results, (list, tuple)) or not authentication_results:
        return False
    top = authentication_results[0]
    if not isinstance(top, str):
        return False
    results, domains = set(), set()
    for token in top.replace(";", " ").split():
        name, equals, value = token.partition("=")
        value = value.strip('"').lower()
        if equals and name.lower() == "dmarc":
            results.add(value)
        elif equals and name.lower() == "header.from":
            domains.add(value.rstrip("."))
    return results == {"pass"} and domains == {domain.lower()}


def _withheld_valid(value: Any) -> bool:
    """``value`` is ``pdf_pages_skipped_by_cap:<first>-<last>`` (positive page numbers)."""
    if not isinstance(value, str):
        return False
    reason, _, pages = value.partition(":")
    numbers = pages.split("-")
    return (reason == WITHHELD_PAGES_SKIPPED and len(numbers) == 2 and
            all(n.isascii() and n.isdigit() and int(n) > 0 for n in numbers))


def document_mentions(document: Document) -> list[dict[str, Any]]:
    """Every mention in every field of one document, with its citation, field path
    and whether it is an external assertion."""
    out = []
    for field in document.fields:
        for mention in extract(field.text):
            mention.update(citation=field.citation, field_path=field.field_path,
                           sender_class=document.sender_class,
                           external_assertion=(document.sender_class != "internal"
                                               or mention["quoted_history"]))
            out.append(mention)
    return out


def mail_documents(store: Any, actor: Any, audience: Any, *,
                   internal_domains: Iterable[str] = (), as_known_at: str | None = None,
                   max_messages: int = 5000) -> tuple[list[Document], dict[str, Any]]:
    """Captured mail versions the reader may see, as :class:`Document` values, plus
    what was read (``messages_total``, ``documents``, ``truncated``, the capture's
    ``complete`` flag for the reader).

    ``store`` is a ``MailCaptureStore``: listing and resolving both decide the
    reader's mailbox audience again (a withheld mailbox gives nothing). Fields: the
    subject, the body text (else the preview when no body was captured) and each
    captured attachment's name under its own attachment handle."""
    domains = tuple(internal_domains)
    documents: list[Document] = []
    offset, total = 0, 0
    while len(documents) < max_messages:
        page = store.messages(actor, audience, as_known_at=as_known_at,
                              limit=min(1000, max_messages - len(documents)), offset=offset)
        total = page["total"]
        items = page["items"]
        if not items:
            break
        offset += len(items)
        for view in items:
            document = _mail_document(store, actor, audience, view, domains)
            if document is not None:
                documents.append(document)
        if offset >= total:
            break
    completeness = store.completeness(actor, audience)
    read = {"messages_total": total, "documents": len(documents),
            "truncated": offset < total,
            "capture_complete": bool(completeness.get("complete"))}
    return documents, read


def _mail_document(store: Any, actor: Any, audience: Any, view: Mapping[str, Any],
                   domains: Sequence[str]) -> Document | None:
    handle = view.get("l1_handle")
    resolved = store.resolve(handle, actor, audience)
    if resolved is None:
        return None
    row = resolved["row"]
    fields = []
    for name in ("subject",):
        if isinstance(row.get(name), str):
            fields.append(TextField(handle, f"row.{name}", row[name]))
    if isinstance(row.get("body_text"), str):
        fields.append(TextField(handle, "row.body_text", row["body_text"]))
    elif isinstance(row.get("body_preview"), str):
        fields.append(TextField(handle, "row.body_preview", row["body_preview"]))
    for attachment in view.get("attachments") or []:
        citation = attachment.get("l1_handle")
        name = (attachment.get("name") or {}).get("untrusted_text")
        if not citation or not isinstance(name, str):
            continue  # the message citation does not pin attachment names
        found = store.resolve(citation, actor, audience)
        if found is not None and isinstance(found["row"].get("name"), str):
            fields.append(TextField(citation, "row.name", found["row"]["name"]))
    return Document(handle, "email", tuple(fields),
                    sender_class=sender_class(row.get("from"), domains,
                                              row.get("authentication_results")),
                    digest=resolved.get("digest"), recorded_at=resolved.get("recorded_at"),
                    superseded_at=resolved.get("superseded_at"),
                    date=row.get("received_at") or row.get("sent_at"),
                    copy_key=ids.copy_key("mail", "messages", row))


# --------------------------------------------------------------------------- resolution

class Resolver(Protocol):
    def candidates(self, kind: str, normalized: str) -> dict[str, Any]:
        """``{"status": found|ambiguous|not_found|error, "candidates": [...],
        "truncated": bool, "code"?: str}``; each candidate is a ``plx.find``
        candidate (object, matched, reason, tier, rank)."""


class FindResolver:
    """Mentions resolve through the identity layer: ``plx.find`` for one reader.

    ``tools`` is anything with ``call(name, arguments, actor)`` answering the
    ``plx.find`` contract (``operating.ontology_tools.OntologyTools``). The query is
    the mention's normalised value with its identifier kind, so the typed index
    (that kind and its alias kinds) is searched and display text never is. Answers
    are memoised per (kind, value) for one projection run."""

    def __init__(self, tools: Any, actor: Any, *, as_known_at: str | None = None) -> None:
        self.tools = tools
        self.actor = actor
        self.as_known_at = as_known_at
        self._memo: dict[tuple[str, str], dict[str, Any]] = {}

    def candidates(self, kind: str, normalized: str) -> dict[str, Any]:
        key = (kind, normalized)
        if key not in self._memo:
            self._memo[key] = self._find(kind, normalized)
        return self._memo[key]

    def _find(self, kind: str, normalized: str) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": normalized[:200], "identifier_kind": kind,
                                     "limit": FIND_PAGE}
        if self.as_known_at is not None:
            arguments["as_known_at"] = self.as_known_at
        candidates: list[dict[str, Any]] = []
        status = "not_found"
        truncated = False
        for _page in range(MAX_FIND_PAGES):
            answer = self.tools.call("plx.find", arguments, self.actor)
            if "error" in answer:
                return {"status": "error", "candidates": [], "truncated": False,
                        "code": str(answer["error"].get("code", "error"))}
            status = answer["status"] if not candidates else status
            seen = {c["object"]["object_id"] for c in candidates}
            candidates.extend(c for c in answer.get("candidates", ())
                              if c["object"]["object_id"] not in seen)
            completeness = answer.get("completeness") or {}
            cursor = completeness.get("next_cursor")
            truncated = bool(completeness.get("truncated"))
            if not cursor:
                break
            arguments = dict(arguments, cursor=cursor)
        if len(candidates) >= 2:
            status = "ambiguous"
        return {"status": status, "candidates": candidates, "truncated": truncated}


# --------------------------------------------------------------------------- projection

def _envelope(text: str, citation: str, field_path: str, sender: str) -> dict[str, Any]:
    out: dict[str, Any] = {"untrusted_text": text[:SPAN_CHARS],
                           "origin": {"citation": citation, "field_path": field_path,
                                      "sender_class": sender}}
    if len(text) > SPAN_CHARS:
        out["truncated"] = True
    return out


def _link_id(*parts: Any) -> str:
    return "mention:" + sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:32]


def copy_group(document: Document) -> str | None:
    """The value-free id of a document's copy group (its ``copy_key`` hashed: the key's
    text is source-written and never shown), or None for a document without one."""
    if document.copy_key is None:
        return None
    return "copy:" + sha256(json.dumps(list(document.copy_key)).encode()).hexdigest()[:32]


def _mention_view(document: Document, mention: Mapping[str, Any]) -> dict[str, Any]:
    return {"identifier_kind": mention["identifier_kind"],
            "normalized": mention["normalized"],
            "span": _envelope(mention["text"], mention["citation"], mention["field_path"],
                              document.sender_class),
            "start": mention["start"], "end": mention["end"],
            "label_start": mention["label_start"], "label": mention["label"],
            "form": mention["form"], "profile": mention["profile"],
            "citation": mention["citation"], "field_path": mention["field_path"]}


def _why_not(mention: Mapping[str, Any], resolution: str, count: int,
             truncated: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"feature": "uncorroborated",
                                  "detail": "mention links stay lead until audited "
                                            "feature weights exist"}]
    if resolution == "ambiguous":
        out.append({"feature": "ambiguous", "candidates": count})
    if truncated:
        out.append({"feature": "candidates_truncated", "shown": count})
    if mention["quoted_history"]:
        out.append({"feature": "quoted_history"})
    if mention.get("label_kind_conflict"):
        out.append({"feature": "label_kind_conflict",
                    "label_kind": mention.get("secondary_of")})
    if mention["external_assertion"]:
        out.append({"feature": "external_assertion",
                    "cannot_confirm": list(EXTERNAL_UNCONFIRMABLE)})
    return out


def _link(document: Document, mention: Mapping[str, Any], candidate: Mapping[str, Any],
          resolution: str, count: int, truncated: bool, extractor: str) -> dict[str, Any]:
    target = candidate["object"]
    matched = candidate.get("matched") or {}
    raw = matched.get("raw") or {}
    matched_citation = (raw.get("origin") or {}).get("citation")
    span = [mention["start"], mention["end"]]
    reason = [
        {"feature": "identifier_mention", "identifier_kind": mention["identifier_kind"],
         "normalized": mention["normalized"], "citation": mention["citation"],
         "field_path": mention["field_path"], "span": span,
         "normalizer": mention["normalisation"]["normalizer"]},
        {"feature": "identity_candidate", "via": "plx.find",
         "matched_kind": matched.get("identifier_kind"),
         "matched_normalized": matched.get("normalized"),
         "property": matched.get("property"), "binding": matched.get("binding"),
         "role": matched.get("role"), "find_reason": candidate.get("reason"),
         "find_tier": candidate.get("tier"), "citation": matched_citation}]
    evidence = [document.citation]
    if mention["citation"] != document.citation:
        evidence.append(mention["citation"])
    if matched_citation and matched_citation not in evidence:
        evidence.append(matched_citation)
    return {
        "link_id": _link_id(document.citation, mention["citation"], mention["field_path"],
                            span, target["object_id"]),
        "link_type": LINK_TYPE, "binding_kind": BINDING_KIND, "tier": TIER,
        "from": {"citation": document.citation, "document_kind": document.kind,
                 "copy_group": copy_group(document)},
        "to": {"object_id": target["object_id"], "object_type": target.get("object_type")},
        "resolution": resolution, "candidate_rank": candidate.get("rank"),
        "candidate_count": count,
        "mention": _mention_view(document, mention),
        "normalisation": dict(mention["normalisation"]),
        "sender_class": document.sender_class,
        "quoted_history": mention["quoted_history"],
        "label_kind_conflict": bool(mention.get("label_kind_conflict")),
        "secondary_of": mention.get("secondary_of"),
        "external_assertion": mention["external_assertion"],
        "cannot_confirm": (list(EXTERNAL_UNCONFIRMABLE) if mention["external_assertion"]
                           else []),
        "reason": reason,
        "why_not": _why_not(mention, resolution, count, truncated),
        "evidence_handles": evidence,
        "extractor": extractor,
        "recorded_at": document.recorded_at, "superseded_at": document.superseded_at}


def _new_counts() -> dict[str, Any]:
    return {"documents": 0, "fields": 0, "mentions": 0, "mentions_by_kind": {},
            "quoted_history": 0, "external_assertions": 0, "label_kind_conflicts": 0,
            "links": 0,
            "resolution": {name: 0 for name in RESOLUTIONS + UNRESOLVED},
            "candidates_truncated": 0, "documents_text_withheld": 0}


def project(documents: Iterable[Document], resolver: Resolver | None) -> dict[str, Any]:
    """Mention links for every mention of every document (see the module text).

    ``resolver`` None projects mentions without resolution (each parsed mention is
    then ``not_found`` with reason ``no_resolver``)."""
    extractor = extractor_version()
    counts = _new_counts()
    links: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for document in documents:
        counts["documents"] += 1
        counts["fields"] += len(document.fields)
        counts["documents_text_withheld"] += document.text_withheld is not None
        for mention in document_mentions(document):
            if mention["label_kind_conflict"]:
                counts["label_kind_conflicts"] += 1
            else:
                counts["mentions"] += 1
                by_kind = counts["mentions_by_kind"]
                kind = mention["identifier_kind"]
                by_kind[kind] = by_kind.get(kind, 0) + 1
                counts["quoted_history"] += bool(mention["quoted_history"])
                counts["external_assertions"] += bool(mention["external_assertion"])
            entry = {"document": document.citation,
                     "mention": _mention_view(document, mention),
                     "normalisation": dict(mention["normalisation"]),
                     "external_assertion": mention["external_assertion"],
                     "quoted_history": mention["quoted_history"],
                     "label_kind_conflict": mention["label_kind_conflict"]}
            # ``search`` says whether the identity layer was asked: a reader-scoped
            # "no visible match" never says whether hidden records match.
            if not mention["normalized"]:
                counts["resolution"]["unparsed"] += 1
                unresolved.append(dict(entry, status="unparsed", search="not_searched"))
                continue
            if resolver is None:
                counts["resolution"]["not_found"] += 1
                unresolved.append(dict(entry, status="not_found", search="not_searched",
                                       reason="no_resolver"))
                continue
            answer = resolver.candidates(mention["identifier_kind"], mention["normalized"])
            if answer["status"] == "error":
                counts["resolution"]["resolver_error"] += 1
                unresolved.append(dict(entry, status="resolver_error", search="search_failed",
                                       reason=answer.get("code")))
                continue
            candidates = list(answer.get("candidates") or ())
            if not candidates:
                counts["resolution"]["not_found"] += 1
                unresolved.append(dict(entry, status="not_found",
                                       search="searched_no_visible_match"))
                continue
            resolution = "ambiguous" if len(candidates) > 1 else "single_candidate"
            counts["resolution"][resolution] += 1
            truncated = bool(answer.get("truncated"))
            counts["candidates_truncated"] += truncated
            for candidate in candidates:
                links.append(_link(document, mention, candidate, resolution,
                                   len(candidates), truncated, extractor))
    counts["links"] = len(links)
    result = {"format": FORMAT, "extractor": extractor, "links": links,
              "unresolved": unresolved, "counts": counts}
    result["digest"] = sha256(json.dumps({"links": links, "unresolved": unresolved},
                                         sort_keys=True).encode()).hexdigest()
    return result


# --------------------------------------------------------------------------- stored links

STORE_LINK_BATCH = 500


def mention_link_types(link_types: Mapping[str, Mapping[str, Any]]
                       ) -> dict[tuple[str, str], str]:
    """``(document type, target type) -> link type id`` of every declared link type
    with a ``mention`` binding (``ontology/schema/domains/documents.json``:
    ``Email.mentions_<type>``). A mention of a type with no declared link is not
    stored (counted ``undeclared_target``)."""
    out: dict[tuple[str, str], str] = {}
    for link_id in sorted(link_types):
        link = link_types[link_id]
        if any(b.get("kind") == BINDING_KIND for b in link.get("bindings", ())):
            out.setdefault((link["from"], link["to"]), link_id)
    return out


def _overlap(a: tuple[str, str | None], b: tuple[str, str | None]
             ) -> tuple[str, str | None] | None:
    start = max(a[0], b[0])
    ends = [end for end in (a[1], b[1]) if end is not None]
    end = min(ends) if ends else None
    return None if end is not None and end <= start else (start, end)


def _marks(*texts: str) -> str:
    return json.dumps(sorted({m for t in texts for m in json.loads(t or "[]")}),
                      separators=(",", ":"))


def _query(conn: Any, sql: str, params: Sequence[Any]) -> Any:
    """A cursor whose rows are ``sqlite3.Row`` (the builder's connection is not changed)."""
    import sqlite3

    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, tuple(params))


# The removal stretches of the record of version row ``o`` (JSON [[since, until], ...]).
REMOVALS_COLUMN = ("(SELECT json_group_array(json_array(rm.since, rm.until)) "
                   "FROM object_removals rm WHERE rm.object_id = o.object_id) AS removals")
VERSION_BY_HANDLE_SQL = (
    "SELECT v.object_id, o.object_type, v.digest, v.lineage, v.markings, v.recorded_at, "
    f"v.superseded_at, {REMOVALS_COLUMN} FROM object_versions v JOIN objects o "
    "ON o.object_id = v.object_id WHERE v.l1_handle = ?")


def held_spans(version: Any) -> list[tuple[str, str | None]]:
    """The [start, end) stretches of a document version while its source holds it: from
    its capture to its supersession, less every removal of the record (a deleted mail,
    a soft-deleted row; ``removals``: :data:`REMOVALS_COLUMN`), so the links it states
    end at a removal and start again on a return."""
    removed = [(since, until) for since, until in json.loads(version["removals"] or "[]")]
    return held_stretches(version["recorded_at"], version["superseded_at"], removed)


def _store_version(conn: Any, handle: str) -> Any:
    return _query(conn, VERSION_BY_HANDLE_SQL, (handle,)).fetchone()


def _cited_version(conn: Any, citation: str) -> Any:
    """The projected version a field citation reads: its own handle, else (a page of a
    paged file) the handle without the page."""
    found = _store_version(conn, citation)
    base, page = cited_page(citation)
    if found is None and page is not None:
        found = _store_version(conn, base)
    return found


def _store_candidates(conn: Any, mention: Mapping[str, Any], exclude: str,
                      aliases: Any = None) -> list[Any]:
    """Identifier-index rows that answer one mention: its kind and the kind's alias
    kinds, each normalised by its own normaliser (as ``plx.find`` searches), then the
    rows ``aliases`` (``ontology.reviews.BuildAliases``) gives for the same keys, over
    the time no index row of the same object answers it (:func:`beside_index`)."""
    keys: list[tuple[str, str]] = []
    for kind in (mention["identifier_kind"], *ids.alias_kinds(mention["identifier_kind"])):
        try:
            value = (mention["normalized"] if kind == mention["identifier_kind"]
                     else ids.normalize(kind, mention["text"]))
        except (KeyError, ValueError):
            value = None
        if isinstance(value, str) and value and (kind, value) not in keys:
            keys.append((kind, value))
    rows: list[Any] = []
    reviewed: list[Any] = []
    for kind, value in keys:
        rows += _query(
            conn, "SELECT i.object_id, o.object_type, i.identifier_kind, i.normalized, i.property, "
            "i.role, i.l1_handle, i.field_path, i.lineage, i.recorded_at, i.superseded_at, "
            "v.digest, v.markings FROM identifiers i JOIN objects o ON o.object_id = i.object_id "
            "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
            "WHERE i.identifier_kind = ? AND i.normalized = ? AND i.object_id <> ? "
            "ORDER BY i.object_id, i.recorded_at, i.l1_handle, i.field_path",
            (kind, value, exclude)).fetchall()
        if aliases is not None:
            reviewed += [row for row in aliases.rows(conn, kind, value)
                         if row["object_id"] != exclude]
    return rows + beside_index(reviewed, rows)


def mention_candidates(conn: Any, mention: Mapping[str, Any], source: Any,
                       aliases: Any = None,
                       spans: Sequence[tuple[str, str | None]] | None = None
                       ) -> list[tuple[Any, tuple[str, str | None], int]]:
    """The candidates the link writer links one mention of the document version
    ``source`` (a ``VERSION_BY_HANDLE_SQL`` row) to: each identifier-index or
    reviewed-alias row (:func:`_store_candidates`, ``aliases``) whose stretch overlaps a
    stretch the document's source holds it (``held_spans``, or ``spans``), with that
    window and the held stretch's index. The one resolver of written links: a reader
    asking whether a mention is answered asks it."""
    spans = held_spans(source) if spans is None else spans
    out = []
    for row in _store_candidates(conn, mention, source["object_id"], aliases):
        for held, span in enumerate(spans):
            window = _overlap(span, (row["recorded_at"], row["superseded_at"]))
            if window is not None:
                out.append((row, window, held))
    return out


def beside_index(reviewed: Sequence[Any], index: Sequence[Any]) -> list[dict[str, Any]]:
    """The reviewed-alias rows ``reviewed`` cut to the time no identifier-index row of
    ``index`` names the same object: where both match one object, the record's own
    identifier answers and the alias adds no second link. An alias stretch the index
    covers in its middle leaves two pieces; the later carries ``piece`` 1 (a link id
    names it)."""
    covered: dict[str, list[tuple[str, str | None]]] = {}
    for row in index:
        covered.setdefault(row["object_id"], []).append(
            (row["recorded_at"], row["superseded_at"]))
    out = []
    for row in reviewed:
        pieces = [(row["recorded_at"], row["superseded_at"])]
        for cover in covered.get(row["object_id"], ()):
            left = []
            for start, end in pieces:
                if _overlap((start, end), cover) is None:
                    left.append((start, end))
                    continue
                if start < cover[0]:
                    left.append((start, cover[0]))
                if cover[1] is not None and (end is None or cover[1] < end):
                    left.append((cover[1], end))
            pieces = left
        out += [dict(row, recorded_at=start, superseded_at=end, **({"piece": n} if n else {}))
                for n, (start, end) in enumerate(sorted(pieces))]
    return out


def candidate_piece(row: Any) -> str:
    """The link id suffix of a later piece of a reviewed-alias row (:func:`beside_index`)."""
    return f"~{row['piece']}" if "piece" in row.keys() else ""


def candidate_via(row: Any) -> tuple[dict[str, Any], list[str]]:
    """How a candidate row was found (``via``, and the review a reviewed alias comes
    from) and the evidence handles it cites beyond its own ``l1_handle``."""
    if "review_id" in row.keys():
        return ({"via": "reviewed_alias", "review_id": row["review_id"]},
                list(row["evidence_handles"]))
    return {"via": "identifier_index"}, []


def store_mention_links(builder: Any, projector: Any, *,
                        documents: Iterable[Document] | None = None,
                        aliases: Any = None) -> dict[str, Any]:
    """Write ``Document.mentions`` links into the generation being built (a
    ``build_store`` ``post_project`` step), so ``plx.object`` / ``plx.around`` reach
    them from either end.

    Documents: ``documents``, else every capture source of the build that offers
    ``documents()`` (``ontology.project.MailCaptures``, ``SharePointCaptures``).
    Candidates come from the generation's own identifier index and ``aliases``
    (``ontology.reviews.BuildAliases``; a link from a reviewed alias cites every handle
    the review cites) (actor-free; every reader is re-gated on read:
    both ends and every cited handle must be visible to it). Each candidate whose
    identifier version overlaps the document version in time (while its source holds
    it: ``held_span``) is one ``lead`` link of the declared
    ``<Document type>.mentions_<type>`` link type, valid over the overlap. Several
    candidates stay several links sharing one ``mention_group`` in their reason, which
    also names the document's value-free ``copy_group`` (:func:`copy_group`; null for a
    document that is a copy of nothing), so a reader of stored links counts copies once.
    Nothing actor-free about ambiguity is stored (no ``ambiguous``
    why-not, no candidate count): whether a mention is ambiguous is decided per
    reader at read time, from the sibling links of its group that reader can see
    (``operating/ontology_tools_traverse.py``), so a reader is never told that
    candidates it cannot see exist. Quoted history, external senders and label/kind
    conflicts are why-nots, as in :func:`project`.

    A supplier's own number that answers nothing (a kind whose ``reference_alias``
    names the target type, ``ontology/identifiers.py``) in a document version whose
    mention link is corroborated to one target becomes an alias lead on that link
    (:func:`propose_reference_aliases`); it joins nothing until a person accepts it.

    Memory stays bounded by the batch and one document: documents stream from their
    sources, and handle lookups go through a temporary index on
    ``object_versions(l1_handle)`` that is dropped before the step returns (it is
    not part of the store DDL or its content digest)."""
    conn = builder.connection
    declared = mention_link_types(getattr(projector, "links", {}) or {})
    if documents is None:
        documents = _source_documents(projector)
    conn.execute(f"CREATE INDEX IF NOT EXISTS {HANDLE_INDEX} ON object_versions(l1_handle)")
    unanswered: dict[str, list[dict[str, Any]]] = {}
    try:
        report = _store_links(builder, conn, declared, documents, aliases, unanswered)
        raised = corroborate_links(conn, corroborable_link_types(
            getattr(projector, "links", {}) or {}))
        report["counts"].update(propose_reference_aliases(conn, unanswered, aliases))
    finally:
        conn.execute(f"DROP INDEX IF EXISTS {HANDLE_INDEX}")
    report["counts"]["corroborated"] = raised
    report["link_tiers"] = {TIER: report["counts"]["links"] - raised, CORROBORATED: raised}
    return report


HANDLE_INDEX = "plx_mentions_version_by_handle"


def _source_documents(projector: Any) -> Iterator[Document]:
    for name in sorted(getattr(projector, "sources", {}) or {}):
        produce = getattr(projector.sources[name], "documents", None)
        if callable(produce):
            yield from produce()


def _store_links(builder: Any, conn: Any, declared: Mapping[tuple[str, str], str],
                 documents: Iterable[Document], aliases: Any,
                 unanswered: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    extractor = extractor_version()
    counts = {"documents": 0, "documents_not_projected": 0, "mentions": 0,
              "unparsed": 0, "not_found": 0, "field_not_projected": 0,
              "undeclared_target": 0, "ambiguous_mentions": 0, "links": 0,
              "reviewed_alias_links": 0, "reviewed_alias_links_open": 0,
              "documents_text_withheld": 0}
    rows: list[dict[str, Any]] = []
    for document in documents:
        seen: set[str] = set()   # one document's link ids (secondary readings repeat)
        source = _store_version(conn, document.citation)
        if source is None:
            counts["documents_not_projected"] += 1
            continue
        counts["documents"] += 1
        counts["documents_text_withheld"] += document.text_withheld is not None
        spans_of_doc = held_spans(source)
        fields: dict[str, Any] = {document.citation: source}
        for mention in document_mentions(document):
            counts["mentions"] += 1
            if not mention["normalized"]:
                counts["unparsed"] += 1
                continue
            cited = fields.get(mention["citation"])
            if mention["citation"] not in fields:
                cited = fields[mention["citation"]] = _cited_version(conn, mention["citation"])
            if cited is None:
                counts["field_not_projected"] += 1   # e.g. an attachment not projected
                continue
            candidates = mention_candidates(conn, mention, source, aliases, spans_of_doc)
            targets = sorted({row["object_id"] for row, _, _ in candidates})
            # A supplier's number nothing answers now (none, or only a closed stretch: an
            # alias since rejected) may be an alias lead.
            if (all(window[1] is not None for _, window, _ in candidates)
                    and ids.reference_alias_of(mention["identifier_kind"])
                    and not (mention["quoted_history"] or mention.get("label_kind_conflict"))):
                unanswered.setdefault(document.citation, []).append(
                    _reference_mention(mention, cited))
            if not targets:
                counts["not_found"] += 1
                continue
            counts["ambiguous_mentions"] += len(targets) > 1   # operator count only
            span = [mention["start"], mention["end"]]
            group = _link_id(document.citation, mention["citation"], mention["field_path"],
                             span)[len("mention:"):]
            kept: set[tuple[str, str, int, str]] = set()
            for row, window, held in candidates:
                key = (row["object_id"], row["l1_handle"], held, candidate_piece(row))
                if key in kept:
                    continue   # one link per target version, whichever property matched
                kept.add(key)
                link_type = declared.get((source["object_type"], row["object_type"]))
                if link_type is None:
                    counts["undeclared_target"] += 1
                    continue
                link_id = _link_id(document.citation, mention["citation"],
                                   mention["field_path"], span, row["object_id"],
                                   row["l1_handle"]) + (f"#{held}" if held else "") \
                    + candidate_piece(row)
                if link_id in seen:
                    continue
                seen.add(link_id)
                counts["reviewed_alias_links"] += "review_id" in row.keys()
                counts["reviewed_alias_links_open"] += ("review_id" in row.keys()
                                                        and window[1] is None)
                rows.append(_store_link(document, mention, source, cited, row, window,
                                        link_type, link_id, group, extractor))
                if len(rows) >= STORE_LINK_BATCH:
                    counts["links"] += builder.add("links", rows)
                    rows = []
    if rows:
        counts["links"] += builder.add("links", rows)
    return {"format": FORMAT, "extractor": extractor, "tier": TIER, "counts": counts,
            "link_tiers": {TIER: counts["links"]}}


def _store_link(document: Document, mention: Mapping[str, Any], source: Any, cited: Any,
                row: Any, window: tuple[str, str | None], link_type: str, link_id: str,
                group: str, extractor: str) -> dict[str, Any]:
    mention_cite = {"l1_handle": mention["citation"], "field_path": mention["field_path"],
                    "digest": cited["digest"], "lineage": cited["lineage"]}
    target_cite = {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                   "digest": row["digest"], "lineage": row["lineage"]}
    via, cited_handles = candidate_via(row)
    reason = [
        {"feature": "identifier_mention", "agrees": True, "citations": [mention_cite],
         "identifier_kind": mention["identifier_kind"], "normalized": mention["normalized"],
         "span": [mention["start"], mention["end"]], "mention_group": group,
         "copy_group": copy_group(document), "sender_class": document.sender_class,
         "normalizer": mention["normalisation"]["normalizer"], "extractor": extractor},
        {"feature": "identity_candidate", "agrees": True, "citations": [target_cite],
         **via, "matched_kind": row["identifier_kind"],
         "matched_normalized": row["normalized"], "property": row["property"],
         "role": row["role"]}]
    why_not: list[dict[str, Any]] = [
        {"feature": "uncorroborated", "agrees": None, "detail": UNCORROBORATED}]
    if mention["quoted_history"]:
        why_not.append({"feature": "quoted_history", "agrees": False})
    if mention.get("label_kind_conflict"):
        why_not.append({"feature": "label_kind_conflict", "agrees": False,
                        "label_kind": mention.get("secondary_of")})
    if mention["external_assertion"]:
        why_not.append({"feature": "external_assertion", "agrees": False,
                        "cannot_confirm": list(EXTERNAL_UNCONFIRMABLE)})
    evidence = list(dict.fromkeys([document.citation, mention["citation"], row["l1_handle"],
                                   *cited_handles]))
    return {"link_id": link_id, "link_type": link_type, "from_object": source["object_id"],
            "to_object": row["object_id"], "tier": TIER, "binding_kind": BINDING_KIND,
            "link_contract": None,
            "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
            "why_not": json.dumps(why_not, sort_keys=True, separators=(",", ":")),
            "evidence_handles": json.dumps(evidence, separators=(",", ":")),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": _marks(source["markings"], cited["markings"], row["markings"]),
            "recorded_at": window[0], "superseded_at": window[1]}


# --------------------------------------------------------------------------- corroboration

CORROBORATED = "corroborated"
CORROBORATION_MIN = 2   # independent agreeing features (07 C.2)
UNCORROBORATED = ("fewer than two independent features agree, or a veto holds "
                  "(ontology/mentions.py MENTION_FEATURES)")
# The mention features. ``agrees``: counts towards corroboration; ``veto``: keeps the link a
# lead whatever agrees. ``property``: the schema property the feature reads (on the
# document, the target or the party). One rule for every target type: no supplier, order
# or case is named here.
MENTION_FEATURES: Mapping[str, Mapping[str, Any]] = {
    "identifier_mention": {
        "role": "agrees",
        "detail": "the document's text names the target's identifier (one grammar, one "
                  "index): the mention itself"},
    "party_domain": {
        "role": "agrees", "property": "website", "document_property": "sender",
        "detail": "the sender's domain is at or under the website domain of a party the "
                  "target links to at tier verified, and that domain is distinctive: no "
                  "shared mail provider or marketplace, held by no other object"},
    "petra_thread": {
        "role": "agrees", "document_property": "conversation_id",
        "detail": "a message of the same conversation whose sender is internal (DMARC "
                  "pass) names the same target in its own text; never for an internal "
                  "sender's own link (Petra does not corroborate Petra)"},
    "message_predates_target": {
        "role": "veto", "property": "order_date",
        "document_property": ("received_at", "sent_at"),
        "detail": "the message is dated before the target's date"},
    "several_targets": {
        "role": "veto",
        "detail": "the document version names more than one object of the target's type "
                  "(several candidates of one mention, or several mentions), unless every "
                  "two of them are one record's twins (same_record_twin)"},
    "same_record_twin": {
        "role": "fold",
        "detail": "two targets of one document version are one record: a same_record link "
                  "at tier verified or reviewed joins them over the link's whole time, or "
                  "each matched the mention exactly on the stored field a verified link "
                  "binding joins (ontology/schema/identifiers.json link_bindings, e.g. a "
                  "Portal order's stored FM PO number and the FM order's own number); "
                  "never a number shape alone"},
    "quoted_history": {"role": "veto", "detail": "the span is quoted history"},
    "label_kind_conflict": {"role": "veto",
                            "detail": "the label assigns another identifier kind"},
}

# Shared mail providers: free mail, ISP mail and marketplace hosts. Any address
# at or under one of these is not distinctive for one party. Two rules:
# ``SHARED_MAIL_DOMAINS`` matches the domain and its subdomains (``vip.163.com``
# under ``163.com``); ``SHARED_MAIL_BRANDS`` matches the registrable label under
# any public suffix (``yahoo.ca``, ``hotmail.co.uk``, ``outlook.com.au``).
SHARED_MAIL_DOMAINS = frozenset({
    # global free mail
    "gmail.com", "googlemail.com", "ymail.com", "rocketmail.com", "aol.com", "aim.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "pm.me", "protonmail.com",
    "protonmail.ch", "tutanota.com", "tuta.io", "zoho.com", "zohomail.com", "mail.com",
    "email.com", "usa.com", "inbox.com", "fastmail.com", "fastmail.fm", "hushmail.com",
    "mail.ru", "inbox.ru", "list.ru", "bk.ru", "rambler.ru", "web.de", "freenet.de",
    "t-online.de", "libero.it", "virgilio.it", "tiscali.it", "orange.fr", "wanadoo.fr",
    "free.fr", "laposte.net", "sfr.fr", "naver.com", "hanmail.net", "daum.net",
    "rediffmail.com", "seznam.cz", "wp.pl", "o2.pl", "interia.pl", "onet.pl",
    # China / Hong Kong / Taiwan free and ISP mail
    "qq.com", "foxmail.com", "163.com", "126.com", "yeah.net", "188.com", "netease.com",
    "sina.com", "sina.cn", "sina.com.cn", "sohu.com", "tom.com", "21cn.com", "139.com",
    "189.cn", "wo.cn", "aliyun.com", "netvigator.com", "hinet.net",
    # Canada / US / UK / AU / NZ ISP mail
    "rogers.com", "sympatico.ca", "bell.net", "bellnet.ca", "shaw.ca", "telus.net",
    "videotron.ca", "cogeco.ca", "eastlink.ca", "sasktel.net", "mts.net",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net",
    "charter.net", "earthlink.net", "optonline.net", "frontier.com", "windstream.net",
    "btinternet.com", "sky.com", "virginmedia.com", "ntlworld.com", "talktalk.net",
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "xtra.co.nz",
    # marketplaces and storefront hosts (a supplier's page there is not its domain)
    "alibaba.com", "aliexpress.com", "1688.com", "made-in-china.com",
    "globalsources.com", "indiamart.com", "tradeindia.com", "amazon.com", "ebay.com",
    "etsy.com", "shopify.com", "myshopify.com", "wix.com", "wixsite.com",
    "squarespace.com", "wordpress.com", "blogspot.com", "google.com",
    "linkedin.com", "facebook.com"})
SHARED_MAIL_BRANDS = frozenset({
    "gmail", "googlemail", "yahoo", "ymail", "hotmail", "outlook", "live", "msn",
    "windowslive", "aol", "gmx", "yandex", "protonmail", "icloud", "zoho", "rediffmail",
    "mail", "email", "inbox"})
# Second-level labels under a two-letter country code that are public suffixes
# (``co.uk``, ``com.cn``, ``com.au``, ``ne.jp``); stdlib has no public suffix list.
_PUBLIC_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "ne",
                                  "or", "go", "gen", "ltd", "plc", "biz", "info", "mil"})


def mail_domain(address: Any) -> str | None:
    """The domain of a mail address (lower case, a valid DNS name), else None."""
    if not isinstance(address, str) or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().strip(">").lower()
    return domain if ids.valid("dns_domain", domain) else None


def website_domain(value: Any) -> str | None:
    """The host of a website value (``https://www.acme.com/x`` -> ``acme.com``), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        host = (urlsplit(text if "://" in text else "https://" + text).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host if ids.valid("dns_domain", host) else None


def domain_under(domain: str, registered: str) -> bool:
    """``domain`` equals ``registered`` or is one of its subdomains. One direction
    only: ``acme.com`` is not under ``sales.acme.com``, and ``alibaba.com`` is not
    under a storefront ``acmepack.en.alibaba.com``."""
    return domain == registered or domain.endswith("." + registered)


def registrable_domain(domain: str) -> str:
    """The domain an organisation registers (``mail.acme.co.uk`` -> ``acme.co.uk``).

    Approximates the public suffix list: a two-letter country code with a common
    second-level label (``co``, ``com``, ``net``, ``org`` ...) is a suffix of two
    labels, every other suffix is one label."""
    labels = domain.lower().strip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _PUBLIC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def shared_mail_domain(domain: str) -> bool:
    """True when ``domain`` belongs to a shared mail provider or marketplace (free
    mail, ISP mail, a marketplace storefront host): never distinctive for one
    party, whatever its suffix or subdomain."""
    domain = domain.lower().strip(".")
    if any(domain_under(domain, shared) for shared in SHARED_MAIL_DOMAINS):
        return True
    return registrable_domain(domain).split(".")[0] in SHARED_MAIL_BRANDS


def party_domain(party_domains: Sequence[tuple[str, str]], counterparts: Sequence[str],
                 holders: Callable[[str], set[str]], party: str) -> dict[str, Any] | None:
    """The ``party_domain`` feature: a counterparty mail domain at or under one of the
    party's domains (``(source, domain)``: where the domain was read, e.g. ``website``).
    ``match`` when that domain is distinctive, else ``not_distinctive`` with its reason
    (``shared_mail_provider``, or ``shared_by_suppliers``: ``holders(counterpart)``, the
    ids of every party holding a domain the counterpart is under, names another party).
    None when no counterpart is under any of the party's domains."""
    found = []
    for source, domain in party_domains:
        for counterpart in counterparts:
            if not domain_under(counterpart, domain):
                continue
            reason = ("shared_mail_provider" if shared_mail_domain(domain) else
                      "shared_by_suppliers" if holders(counterpart) - {party} else None)
            found.append({"status": "not_distinctive" if reason else "match",
                          "matched_on": source, "party_domain": domain,
                          "counterparty_domain": counterpart,
                          **({"not_distinctive_reason": reason} if reason else {})})
    return next((f for f in found if f["status"] == "match"), found[0]) if found else None


def day(value: Any) -> date | None:
    """The calendar day an ISO date or timestamp text starts with, else None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def date_order(target_day: date | None, message_day: date | None) -> dict[str, Any]:
    """The ``message_predates_target`` veto: ``message_predates_target`` when the message
    is dated before the target's day, ``after_target`` otherwise, ``not_comparable``
    without both days."""
    if target_day is None or message_day is None:
        return {"status": "not_comparable"}
    delta = (message_day - target_day).days
    return {"status": "message_predates_target" if delta < 0 else "after_target",
            "target_date": target_day.isoformat(), "message_date": message_day.isoformat(),
            "days_after_target": delta}


def petra_thread(conversation: Any, key: str, threads: Mapping[str, Iterable[str]]) -> bool:
    """The ``petra_thread`` feature: the conversation holds an internal message naming the
    same ``key`` (a target, or a supplier scope). ``threads``: conversation id -> the keys
    internal messages of it name."""
    return bool(key) and isinstance(conversation, str) and bool(conversation) \
        and key in set(threads.get(conversation) or ())


# The agreeing features that read a record other than the link's two ends (the mention
# itself and its identity candidate read only those); also the supplier features of a
# corroborated purchase-order twin, which read the order's supplier record
# (``ontology.identity_scoring.TWIN_CORROBORATING``).
TWIN_SUPPLIER_FEATURES = ("supplier_name", "supplier_number")
CORROBORATING_FEATURES = tuple(name for name, spec in MENTION_FEATURES.items()
                               if spec["role"] in ("agrees", "fold")
                               and name != "identifier_mention") + TWIN_SUPPLIER_FEATURES
CORROBORATION_NOT_VISIBLE = "corroboration_not_visible"


def corroboration_handles(reason: Iterable[Mapping[str, Any]]) -> list[str] | None:
    """The L1 handles the corroborating features of a link cite (the records its
    corroboration rests on, and only those), or None when one citation names no handle
    (never readable)."""
    out: list[str] = []
    for feature in reason:
        if not isinstance(feature, Mapping) or feature.get("feature") not in \
                CORROBORATING_FEATURES:
            continue
        for citation in feature.get("citations") or ():
            handle = citation.get("l1_handle") if isinstance(citation, Mapping) else None
            if not isinstance(handle, str):
                return None
            out.append(handle)
    return list(dict.fromkeys(out))


def corroboration_objects(conn: Any, reason: Iterable[Mapping[str, Any]],
                          cache: dict[str, str | None]) -> set[str] | None:
    """The objects whose versions the corroborating features of a link cite (their
    citations' L1 handles, :func:`corroboration_handles`), or None when one is not in the
    store (never readable). ``cache``: handle -> object id, kept by the caller for one
    call."""
    handles = corroboration_handles(reason)
    if handles is None:
        return None
    out: set[str] = set()
    for handle in handles:
        if handle not in cache:
            row = conn.execute("SELECT object_id FROM object_versions WHERE l1_handle = ?",
                               (handle,)).fetchone()
            cache[handle] = row[0] if row else None
        if cache[handle] is None:
            return None
        out.add(cache[handle])
    return out


def reader_view(tier: str, reason: Sequence[Mapping[str, Any]],
                why_not: Sequence[Mapping[str, Any]], readable: bool
                ) -> tuple[str, list[Any], list[Any]]:
    """A stored mention link as one reader sees it: when the reader may not read every
    record its corroborating features cite (``readable`` False), the link is the lead
    its mention is, without those features (nothing drawn from a record the reader may
    not read is shown; nor is an alias lead, which rests on that corroboration), and
    says ``corroboration_not_visible``. The one rule of ``plx.around``, ``plx.object``
    and ``operating.case_mail``."""
    drawn = [f for f in reason if f.get("feature") in CORROBORATING_FEATURES]
    if readable or not drawn:
        return tier, list(reason), list(why_not)
    return (TIER if tier == CORROBORATED else tier,
            [f for f in reason if f.get("feature") not in CORROBORATING_FEATURES
             and f.get("feature") != REFERENCE_ALIAS_LEAD],
            [*why_not, {"feature": CORROBORATION_NOT_VISIBLE, "agrees": None}])


def corroborable_link_types(link_types: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """The declared link types whose ``mention`` binding allows ``corroborated``."""
    return frozenset(link_id for link_id, link in link_types.items()
                     if any(b.get("kind") == BINDING_KIND
                            and b.get("tier_ceiling") == CORROBORATED
                            for b in link.get("bindings", ())))


def _covers(row: Any, start: str, end: str | None) -> bool:
    """Whether a stored row's [recorded_at, superseded_at) covers [start, end)."""
    if row["recorded_at"] > start:
        return False
    return row["superseded_at"] is None or (end is not None and row["superseded_at"] >= end)


def _held(rows: Iterable[Any], start: str, end: str | None) -> bool:
    """Whether ``rows``, the stored rows of one fact (one link between two objects, or one
    value of one property), cover [start, end) together. An incremental build records an
    unchanged fact again as a new row from the time the previous one is superseded: those
    rows join without a gap and hold the fact over their whole span. A gap (the fact was
    absent for a while) breaks it."""
    reach = start
    for row in sorted(rows, key=lambda r: r["recorded_at"]):
        if row["superseded_at"] is not None and row["superseded_at"] <= reach:
            continue
        if row["recorded_at"] > reach:
            return False
        if row["superseded_at"] is None:
            return True
        reach = row["superseded_at"]
    return end is not None and reach >= end


def _value_cite(row: Any) -> dict[str, Any]:
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}


def _domain_holders(conn: Any, prop: str) -> dict[str, set[str]]:
    """domain -> ids of every object holding it as a ``prop`` value, at any time (a
    domain once held by two parties is distinctive for neither)."""
    out: dict[str, set[str]] = {}
    for row in _query(conn, "SELECT object_id, value FROM object_values WHERE property = ?",
                      (prop,)):
        domain = website_domain(row["value"])
        if domain:
            out.setdefault(domain, set()).add(row["object_id"])
    return out


def _suffix_holders(holders: Mapping[str, set[str]]) -> Callable[[str], set[str]]:
    def held(counterpart: str) -> set[str]:
        labels = counterpart.split(".")
        found: set[str] = set()
        for i in range(len(labels) - 1):
            found |= holders.get(".".join(labels[i:]), set())
        return found
    return held


def corroborate_links(conn: Any, link_types: Iterable[str]) -> int:
    """Raise every stored ``lead`` mention link of ``link_types`` that
    :data:`MENTION_FEATURES` corroborate to ``corroborated``, in place; returns how many.

    Each agreeing feature beyond the mention cites what it read, and the link then cites
    those handles too (``evidence_handles``) and carries their markings, so a reader who
    may not see the party record or the internal message never sees the corroborated
    link. A feature agrees only when what it read holds over the link's whole time
    (otherwise the link stays a lead). Memory is bounded by one document's links."""
    types = sorted(set(link_types))
    if not types:
        return 0
    party = MENTION_FEATURES["party_domain"]
    held = _suffix_holders(_domain_holders(conn, party["property"]))
    marks = ", ".join("?" for _ in types)
    raised, after = 0, ""
    while True:
        documents = [row[0] for row in conn.execute(
            f"SELECT DISTINCT from_object FROM links WHERE binding_kind = ? AND tier = ? "
            f"AND link_type IN ({marks}) AND from_object > ? ORDER BY from_object LIMIT ?",
            (BINDING_KIND, TIER, *types, after, STORE_LINK_BATCH))]
        if not documents:
            return raised
        after = documents[-1]
        for document in documents:
            raised += _corroborate_document(conn, document, types, held)


def _corroborate_document(conn: Any, document: str, types: Sequence[str],
                          held: Callable[[str], set[str]]) -> int:
    rows = _query(conn, "SELECT * FROM links WHERE from_object = ? AND binding_kind = ? "
                  "ORDER BY link_id", (document, BINDING_KIND)).fetchall()
    # (version handle, link type) -> target -> that version's links to it
    named: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for row in rows:
        named.setdefault((json.loads(row["evidence_handles"])[0], row["link_type"]),
                         {}).setdefault(row["to_object"], []).append(row)
    raised = 0
    for row in rows:
        if row["tier"] != TIER or row["link_type"] not in types:
            continue
        agreed = _agreeing(conn, row, named, held)
        if agreed is None or 1 + sum(f["feature"] != "same_record_twin"
                                     for f, _c, _m in agreed) < CORROBORATION_MIN:
            continue
        reason = json.loads(row["reason"]) + [feature for feature, _cites, _marks in agreed]
        why_not = [f for f in json.loads(row["why_not"]) if f.get("feature") != "uncorroborated"]
        evidence = list(dict.fromkeys(json.loads(row["evidence_handles"])
                                      + [h for _f, cites, _m in agreed for h in cites]))
        conn.execute("UPDATE links SET tier = ?, reason = ?, why_not = ?, evidence_handles = ?, "
                     "markings = ? WHERE link_id = ?",
                     (CORROBORATED, json.dumps(reason, sort_keys=True, separators=(",", ":")),
                      json.dumps(why_not, sort_keys=True, separators=(",", ":")),
                      json.dumps(evidence, separators=(",", ":")),
                      _marks(row["markings"], *(m for _f, _c, ms in agreed for m in ms)),
                      row["link_id"]))
        raised += 1
    return raised


def _agreeing(conn: Any, row: Any, named: Mapping[tuple[str, str], Mapping[str, list[Any]]],
              held: Callable[[str], set[str]]
              ) -> list[tuple[dict[str, Any], list[str], list[str]]] | None:
    """The agreeing features beyond the mention (feature, cited handles, markings), then
    the ``same_record_twin`` folds (never counted towards :data:`CORROBORATION_MIN`), or
    None when a veto holds or the mention is not an exact identifier of its target."""
    why_not = {f.get("feature") for f in json.loads(row["why_not"]) if isinstance(f, dict)}
    if why_not & {"quoted_history", "label_kind_conflict"}:
        return None
    if not exact_candidate(json.loads(row["reason"])):
        return None
    version = json.loads(row["evidence_handles"])[0]
    start, end = row["recorded_at"], row["superseded_at"]
    targets = named[(version, row["link_type"])]
    folds: list[tuple[dict[str, Any], list[str], list[str]]] = []
    if len(targets) > 1:
        found = twin_folds(conn, row, targets, start, end)
        if found is None:
            return None   # several_targets
        folds = found
    values = {r["property"]: r for r in _query(
        conn, "SELECT * FROM object_values WHERE object_id = ? AND l1_handle = ?",
        (row["from_object"], version))}
    sent = next((day(values[p]["value"]) for p in
                 MENTION_FEATURES["message_predates_target"]["document_property"]
                 if p in values and day(values[p]["value"])), None)
    for target in _query(conn, "SELECT value, recorded_at, superseded_at FROM object_values "
                         "WHERE object_id = ? AND property = ?",
                         (row["to_object"], MENTION_FEATURES["message_predates_target"]
                          ["property"])):
        if target["recorded_at"] <= start and (target["superseded_at"] is None
                                               or target["superseded_at"] > start) \
                and date_order(day(target["value"]), sent)["status"] == \
                "message_predates_target":
            return None
    agreed = []
    sender = mail_domain((values.get("sender") or {"value": None})["value"])
    if sender is not None:
        found = _party_domain(conn, row, sender, held)
        if found is not None:
            agreed.append(found)
    mention = next((f for f in json.loads(row["reason"])
                    if f.get("feature") == "identifier_mention"), {})
    conversation = (values.get("conversation_id") or {"value": None})["value"]
    if mention.get("sender_class") != "internal" and conversation:
        found = _petra_thread(conn, row, conversation)
        if found is not None:
            agreed.append(found)
    return agreed + folds if agreed else agreed


def _candidate(reason: Iterable[Any]) -> Mapping[str, Any]:
    return next((f for f in reason if isinstance(f, Mapping)
                 and f.get("feature") == "identity_candidate"), {})


def exact_candidate(reason: Sequence[Any]) -> bool:
    """Whether a mention link's target answered the mention exactly: a reviewed alias (a
    person confirmed it), or an identifier-index row of the mention's own kind holding the
    mention's own normalised value (a stored field). A row reached only by reading the
    text as another kind (a number-shape guess) is not exact and never corroborates."""
    mention = next((f for f in reason if isinstance(f, Mapping)
                    and f.get("feature") == "identifier_mention"), {})
    candidate = _candidate(reason)
    if candidate.get("via") == "reviewed_alias":
        return True
    return (candidate.get("via") == "identifier_index"
            and isinstance(mention.get("normalized"), str) and bool(mention["normalized"])
            and candidate.get("matched_kind") == mention.get("identifier_kind")
            and candidate.get("matched_normalized") == mention["normalized"])


def _handle_relation(handle: Any) -> str | None:
    """The relation a source handle names (``api://<relation>/...``), else None."""
    if not isinstance(handle, str) or "://" not in handle:
        return None
    return handle.split("://", 1)[1].split("/", 1)[0] or None


def _stored_field(candidate: Mapping[str, Any]) -> tuple[str, str] | None:
    """(relation, field) of the stored value an identifier-index candidate matched."""
    cite = (candidate.get("citations") or [{}])[0]
    path = cite.get("field_path") if isinstance(cite, Mapping) else None
    relation = _handle_relation(cite.get("l1_handle") if isinstance(cite, Mapping) else None)
    if relation is None or not isinstance(path, str) or not path.startswith("row."):
        return None
    return relation, path[len("row."):]


def _verified_field_pairs() -> frozenset[tuple[str, frozenset[tuple[str, str]]]]:
    """(identifier kind, {(relation, field), (relation, field)}) of every verified link
    binding of the identifier grammar between two single stored fields."""
    out = set()
    for binding in ids.link_bindings():
        ends = [binding.get(side) or {} for side in ("from", "to")]
        if binding.get("tier") != "verified" or not all(
                isinstance(e.get("relation"), str) and isinstance(e.get("field"), str)
                and e["field"] != "*" and e["relation"] != "*" and "(" not in e["field"]
                for e in ends):
            continue
        out.add((binding["identifier_kind"],
                 frozenset((e["relation"], e["field"]) for e in ends)))
    return frozenset(out)


def _twin_basis(conn: Any, first: Sequence[Any], second: Sequence[Any], start: str,
                end: str | None) -> tuple[dict[str, Any], list[str], list[str]] | None:
    """Why two targets of one document version are one record over [start, end), from
    their links ``first`` and ``second``: (basis, cited handles, markings), else None."""
    a, b = first[0]["to_object"], second[0]["to_object"]
    joins: dict[tuple[str, str, str], list[Any]] = {}
    for link in _query(conn, "SELECT * FROM links WHERE ((from_object = ? AND to_object = ?) "
                       "OR (from_object = ? AND to_object = ?)) AND binding_kind = "
                       "'same_record' AND tier IN ('verified', 'reviewed') ORDER BY link_id",
                       (a, b, b, a)):
        joins.setdefault((link["link_type"], link["from_object"], link["to_object"]),
                         []).append(link)
    for _key, links in sorted(joins.items()):
        if _held(links, start, end):
            link = max(links, key=lambda r: r["recorded_at"])
            return ({"basis": "same_record_link"}, json.loads(link["evidence_handles"]),
                    [r["markings"] for r in links])
    pairs = _verified_field_pairs()
    for left in first:
        lc = _candidate(json.loads(left["reason"]))
        for right in second:
            rc = _candidate(json.loads(right["reason"]))
            ends = (_stored_field(lc), _stored_field(rc))
            if (None in ends or ends[0] == ends[1] or not _covers(left, start, end)
                    or not _covers(right, start, end)
                    or not exact_candidate(json.loads(left["reason"]))
                    or not exact_candidate(json.loads(right["reason"]))
                    or lc.get("via") != "identifier_index" or rc.get("via") != "identifier_index"
                    or lc.get("matched_kind") != rc.get("matched_kind")
                    or lc.get("matched_normalized") != rc.get("matched_normalized")
                    or (lc.get("matched_kind"), frozenset(ends)) not in pairs):
                continue
            return ({"basis": "stored_field"},
                    [lc["citations"][0]["l1_handle"], rc["citations"][0]["l1_handle"]],
                    [left["markings"], right["markings"]])
    return None


def twin_folds(conn: Any, row: Any, targets: Mapping[str, Sequence[Any]], start: str,
                end: str | None) -> list[tuple[dict[str, Any], list[str], list[str]]] | None:
    """The ``same_record_twin`` features of ``row`` when every two targets of its document
    version are one record's twins (:func:`_twin_basis`), else None (``several_targets``):
    a third, unrelated object, or two that are not twins, still vetoes."""
    order = sorted(targets)
    out = []
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            basis = _twin_basis(conn, targets[a], targets[b], start, end)
            if basis is None:
                return None
            if row["to_object"] not in (a, b):
                continue
            twin = targets[b if a == row["to_object"] else a]
            candidate = _candidate(json.loads(twin[0]["reason"]))
            own = _candidate(json.loads(row["reason"]))
            feature = {"feature": "same_record_twin", "agrees": True, **basis[0],
                       "property": own.get("property"), "twin_property": candidate.get("property"),
                       "citations": list(candidate.get("citations") or [])}
            out.append((feature, [*(c["l1_handle"] for c in feature["citations"]), *basis[1]],
                        [twin[0]["markings"], *basis[2]]))
    return out


def _party_domain(conn: Any, row: Any, sender: str, held: Callable[[str], set[str]]
                  ) -> tuple[dict[str, Any], list[str], list[str]] | None:
    start, end = row["recorded_at"], row["superseded_at"]
    prop = MENTION_FEATURES["party_domain"]["property"]
    parties: dict[tuple[str, str], list[Any]] = {}
    for link in _query(conn, "SELECT link_id, link_type, to_object, evidence_handles, "
                       "markings, recorded_at, superseded_at FROM links WHERE from_object = ? "
                       "AND tier = 'verified' AND binding_kind <> ? ORDER BY link_id",
                       (row["to_object"], BINDING_KIND)):
        parties.setdefault((link["link_type"], link["to_object"]), []).append(link)
    for (_type, party), links in sorted(parties.items()):
        if not _held(links, start, end):
            continue
        domains: dict[str, list[Any]] = {}
        for value in _query(conn, "SELECT * FROM object_values WHERE object_id = ? AND "
                            "property = ? ORDER BY recorded_at, l1_handle", (party, prop)):
            domain = website_domain(value["value"])
            if domain is not None:
                domains.setdefault(domain, []).append(value)
        for domain, values in domains.items():
            if not _held(values, start, end):
                continue
            found = party_domain([(prop, domain)], [sender], held, party)
            if found is not None and found["status"] == "match":
                # Citations only (a reader is shown those it may read): no party id,
                # domain or link id in the feature itself. The newest row of each fact
                # is cited; every row's markings are carried.
                value, link = values[-1], max(links, key=lambda r: r["recorded_at"])
                feature = {"feature": "party_domain", "agrees": True,
                           "citations": [_value_cite(value)]}
                return (feature, [value["l1_handle"], *json.loads(link["evidence_handles"])],
                        [*(r["markings"] for r in links), *(v["markings"] for v in values)])
    return None


def _petra_thread(conn: Any, row: Any, conversation: str
                  ) -> tuple[dict[str, Any], list[str], list[str]] | None:
    start, end = row["recorded_at"], row["superseded_at"]
    prop = MENTION_FEATURES["petra_thread"]["document_property"]
    threads: dict[str, set[str]] = {}
    found: list[Any] = []
    for sibling in _query(conn, "SELECT DISTINCT object_id, l1_handle FROM object_values "
                          "WHERE property = ? AND value = ? AND object_id <> ? "
                          "ORDER BY object_id, l1_handle",
                          (prop, conversation, row["from_object"])).fetchall():
        named: list[Any] = []
        for link in _query(conn, "SELECT * FROM links WHERE from_object = ? AND to_object = ? "
                           "AND binding_kind = ? ORDER BY link_id",
                           (sibling["object_id"], row["to_object"], BINDING_KIND)):
            handles = json.loads(link["evidence_handles"])
            mention = next((f for f in json.loads(link["reason"])
                            if f.get("feature") == "identifier_mention"), {})
            vetoed = {f.get("feature") for f in json.loads(link["why_not"])} \
                & {"quoted_history", "label_kind_conflict"}
            if handles[0] == sibling["l1_handle"] and mention.get("sender_class") == "internal" \
                    and not vetoed:
                named.append(link)
        if named and _held(named, start, end):
            threads.setdefault(conversation, set()).add(row["to_object"])
            found.append(named[0])
    if not petra_thread(conversation, row["to_object"], threads):
        return None
    first = found[0]
    cite = next(f for f in json.loads(first["reason"])
                if f.get("feature") == "identifier_mention")["citations"]
    feature = {"feature": "petra_thread", "agrees": True, "citations": cite}
    return feature, [json.loads(first["evidence_handles"])[0]], [first["markings"]]


# --------------------------------------------------------------------------- alias leads

REFERENCE_ALIAS_LEAD = "reference_alias_lead"
# Why a document version naming a supplier's own number gave no alias lead.
ALIAS_NO_CANDIDATE = ("no_corroborated_target", "several_targets", "alias_rejected")


def _reference_mention(mention: Mapping[str, Any], cited: Any) -> dict[str, Any]:
    """What an alias lead keeps of an unanswered mention: its kind, value as read and
    normalized, span, and the field it is read in (the citation a person reviews)."""
    return {"identifier_kind": mention["identifier_kind"],
            "normalized": mention["normalized"], "raw": mention["text"],
            "span": [mention["start"], mention["end"]],
            "citation": {"l1_handle": mention["citation"], "field_path": mention["field_path"],
                         "digest": cited["digest"], "lineage": cited["lineage"]}}


def propose_reference_aliases(conn: Any, unanswered: Mapping[str, Sequence[Mapping[str, Any]]],
                              aliases: Any = None) -> dict[str, Any]:
    """Alias leads (ROADMAP A10; decision 2026-10-19-reference-alias-leads): a document
    version (``unanswered``: version handle -> its mentions of a ``reference_alias``
    kind no index row or reviewed alias answers now) whose current mention links reach
    exactly one object of a type the kind's ``reference_alias`` names, at tier
    ``corroborated`` (two independent features agree, ER-05), proposes that the
    supplier's number names that object: each such link gains a
    ``reference_alias_lead`` reason feature citing the field the number is read in.
    A lead is never identity (ER-02): nothing is joined through it, and only a
    person's ``alias`` review (``ontology.reviews.alias_proposals``) makes the number
    an alias, which the next build resolves like any reviewed alias. A pair ``aliases``
    (``ontology.reviews.BuildAliases``) holds rejected (``reject_alias`` in force) is
    not proposed again. Returns counts: ``alias_numbers`` (unanswered numbers seen),
    ``alias_leads`` and, per typed reason, the numbers that gave none
    (``alias_no_candidate``: :data:`ALIAS_NO_CANDIDATE`)."""
    counts: dict[str, Any] = {"alias_numbers": 0, "alias_leads": 0,
                              "alias_no_candidate": dict.fromkeys(ALIAS_NO_CANDIDATE, 0)}
    types: dict[str, str | None] = {}
    for handle in sorted(unanswered):
        numbers = unanswered[handle]
        counts["alias_numbers"] += len(numbers)
        source = _store_version(conn, handle)
        links = [row for row in _query(
            conn, "SELECT * FROM links WHERE from_object = ? AND binding_kind = ? AND tier = ? "
            "AND superseded_at IS NULL ORDER BY link_id",
            (source["object_id"], BINDING_KIND, CORROBORATED))
            if json.loads(row["evidence_handles"])[0] == handle]
        for row in links:
            if row["to_object"] not in types:
                found = conn.execute("SELECT object_type FROM objects WHERE object_id = ?",
                                     (row["to_object"],)).fetchone()
                types[row["to_object"]] = found[0] if found else None
        leads: dict[str, list[dict[str, Any]]] = {}
        for number in numbers:
            of = ids.reference_alias_of(number["identifier_kind"])
            targets = sorted({row["to_object"] for row in links
                              if types[row["to_object"]] in of})
            if len(targets) != 1:
                counts["alias_no_candidate"][
                    "several_targets" if targets else "no_corroborated_target"] += 1
                continue
            rejected = getattr(aliases, "rejected", None)
            if rejected is not None and rejected(targets[0], number["identifier_kind"],
                                                 number["normalized"]):
                counts["alias_no_candidate"]["alias_rejected"] += 1
                continue
            counts["alias_leads"] += 1
            leads.setdefault(targets[0], []).append(
                {"feature": REFERENCE_ALIAS_LEAD, "agrees": None,
                 "citations": [number["citation"]],
                 "identifier_kind": number["identifier_kind"],
                 "normalized": number["normalized"], "raw": number["raw"],
                 "span": number["span"]})
        for row in links:
            if row["to_object"] in leads:
                reason = json.loads(row["reason"]) + leads[row["to_object"]]
                conn.execute("UPDATE links SET reason = ? WHERE link_id = ?",
                             (json.dumps(reason, sort_keys=True, separators=(",", ":")),
                              row["link_id"]))
    return counts


def reference_alias_outcome(counts: Mapping[str, Any]) -> str:
    """One typed outcome of a build's mention step for the supplier-number join (its
    ``counts``): ``alias_joined`` (a link in force now made through a reviewed alias,
    ``reviewed_alias_links_open``; a stretch an alias held before a rejection is not),
    ``alias_lead_pending`` (a lead awaits a person), else why no candidate was found
    (the most advanced :data:`ALIAS_NO_CANDIDATE` reason seen), ``no_supplier_number``
    (no unanswered supplier number read), or ``mentions_not_run``."""
    if not counts:
        return "mentions_not_run"
    if counts.get("reviewed_alias_links_open"):
        return "alias_joined"
    if counts.get("alias_leads"):
        return "alias_lead_pending"
    seen = counts.get("alias_no_candidate") or {}
    for reason in reversed(ALIAS_NO_CANDIDATE):
        if seen.get(reason):
            return reason
    return "no_supplier_number"


# --------------------------------------------------------------------------- consumers

def documents_mentions(links: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Links in the ``plx.documents`` mention item shape (contract
    ``operating/ontology_tool_contracts.json`` plx.documents)."""
    out = []
    for link in links:
        mention = link["mention"]
        out.append({"identifier_kind": mention["identifier_kind"],
                    "normalized": mention["normalized"][:200], "span": mention["span"],
                    "object_id": link["to"]["object_id"], "tier": link["tier"],
                    "external_assertion": bool(link["external_assertion"])})
    return out


def confirmation_support(links: Iterable[Mapping[str, Any]], fact: str) -> dict[str, Any]:
    """Whether these mention links may support ``fact`` (a fact class such as
    ``payment``, ``receipt``, ``quantity`` or ``reference``).

    A payment, receipt or quantity fact needs a link whose text an internal sender
    wrote (external assertions never confirm one by themselves) *and* whose tier is
    ``corroborated`` or stronger: a lead mention alone never confirms it, so today
    (every mention link is a lead) mentions only point to the evidence that must
    confirm it. Any other fact class may cite every link, still at its tier.

    ``evidence`` counts the usable links once per source document: the links of every
    copy of one document (``from.copy_group``: one message in several mailboxes, one
    file attached and stored) are one entry listing each copy's links and citations, so
    no copy's provenance is lost and none counts twice. A link without a copy group is
    its document's own entry (a link without a document, its own). ``evidence_count``
    is the number of entries. Only the links passed in are read: a reader's copies are
    the copies it may see."""
    links = list(links)
    internal = [l for l in links if not l.get("external_assertion")]
    external = [l["link_id"] for l in links if l.get("external_assertion")]
    if fact not in EXTERNAL_UNCONFIRMABLE:
        usable = links
        reason = None if usable else "no_mention"
    else:
        usable = [l for l in internal if l.get("tier") in CONFIRMING_TIERS]
        if usable:
            reason = None
        elif internal:
            reason = "lead_only"
        elif external:
            reason = "external_assertion_only"
        else:
            reason = "no_mention"
    evidence = _evidence(usable)
    return {"fact": fact, "supported": bool(usable), "reason": reason,
            "usable_links": [l["link_id"] for l in usable], "external_links": external,
            "evidence": evidence, "evidence_count": len(evidence)}


def _evidence(links: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One entry per copy group (else per document, else per link), in first-seen order."""
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for link in links:
        source = link.get("from") if isinstance(link.get("from"), Mapping) else {}
        group, citation = source.get("copy_group"), source.get("citation")
        key = (("copy", group) if isinstance(group, str) and group else
               ("document", citation) if isinstance(citation, str) and citation else
               ("link", link["link_id"]))
        entry = entries.setdefault(key, {"copy_group": key[1] if key[0] == "copy" else None,
                                         "links": [], "citations": []})
        entry["links"].append(link["link_id"])
        if isinstance(citation, str) and citation and citation not in entry["citations"]:
            entry["citations"].append(citation)
    return list(entries.values())


def span_holds(resolved: Mapping[str, Any] | None, link: Mapping[str, Any]) -> bool:
    """Whether a resolved L1 row (``MailCaptureStore.resolve`` shape: ``row`` with the
    field path's field) still holds the link's span at its offsets."""
    if not resolved:
        return False
    mention = link["mention"]
    path = mention["field_path"]
    if not path.startswith("row."):
        return False
    text = resolved.get("row", {}).get(path[4:])
    if not isinstance(text, str):
        return False
    return text[mention["start"]:mention["end"]] == link["normalisation"]["raw"]


__all__ = ["BINDING_KIND", "DOCUMENT_KINDS", "Document", "EXTERNAL_UNCONFIRMABLE",
           "FORMAT", "FindResolver", "LINK_TYPE", "PAGE_BREAK", "Resolver", "SENDER_CLASSES",
           "TIER", "TextField", "beside_index", "candidate_piece", "candidate_via", "cited_page",
           "confirmation_support",
           "copy_group",
           "document_mentions",
           "documents_mentions", "QuotedRanges", "extract", "extractor_version",
           "file_text_fields", "is_paged_file", "mail_documents", "page_citation", "project",
           "quoted_ranges", "sender_class", "dmarc_pass", "span_holds", "mention_link_types",
           "mention_candidates", "store_mention_links", "propose_reference_aliases",
           "reference_alias_outcome", "REFERENCE_ALIAS_LEAD", "ALIAS_NO_CANDIDATE",
           "text_pages", "held_spans", "CORROBORATED",
           "CORROBORATION_MIN", "MENTION_FEATURES", "SHARED_MAIL_BRANDS", "SHARED_MAIL_DOMAINS",
           "CORROBORATING_FEATURES", "corroborable_link_types", "corroborate_links",
           "corroboration_handles", "corroboration_objects", "date_order", "day", "reader_view",
           "domain_under", "mail_domain", "party_domain", "petra_thread",
           "registrable_domain", "shared_mail_domain", "website_domain"]
