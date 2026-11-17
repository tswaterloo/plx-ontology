"""The case host's served texts and the evidence permissions it checks before returning them."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable
from urllib.parse import unquote

from ontology import mentions as mn

from .claim_checks_grammar import PAGE_TEXT


PROTOCOL_VERSION = "2025-11-25"


MAX_LINE_BYTES = 65536


MAX_RESULT_BYTES = 48000


MAX_ORIGINAL_TEXT_CHARS = 40000


DEFAULT_ORIGINAL_TEXT_CHARS = 16000


# Bound of the plx.source_citation host read's column_offset (a served Portal row has at
# most ~190).
MAX_ROW_COLUMNS = 10_000


CALLS_PER_MINUTE = 120


RECENT_REQUEST_IDS = 4096


# Bounds of the tool evidence one session keeps for the claim gate (``_note_returned``):
# count entries (oldest dropped first) and money cells with a currency (new ones dropped).
MAX_RETURNED_COUNTS = 2000


MAX_RETURNED_CURRENCIES = 50_000


# The evidence handles one session keeps for its final answers (``plx.case_get`` answer
# mode): the oldest dropped first (a claim citing a dropped one is unsupported).
MAX_RETURNED_HANDLES = 20_000


# The texts one session keeps how much of it was served (``_note_texts``): the oldest
# dropped first, and a text served only in part takes its handles out of the evidence
# with it (a claim citing one is then unsupported, never answerable). Spans kept per text.
MAX_SERVED_TEXTS = 20_000


MAX_SERVED_SPANS = 64


# The handles (aliases) one served text keeps, to drop with it.
MAX_TEXT_HANDLES = 16


def _text_key(handle: str, field: str) -> tuple[Any, ...]:
    """The record and field a served or cited text is of, whatever alias names it: a
    mirror row (``api://``) by its relation and its key (decoded, keys sorted), any other
    record by its handle without the version after its last ``@`` (a capture revision, a
    file version; a mailbox's ``@`` is followed by a ``/``, so it stays) and without its
    fragment, but with the page it names (``#page=N``: another page is another text)."""
    body, _, fragment = handle.partition("#")
    page = next((part.partition("=")[2] for part in fragment.split("&")
                 if part.startswith("page=")), None)
    head, at, version = body.rpartition("@")
    if at and "://" in head and "/" not in version:
        body = head
    if body.startswith("api://"):
        relation, _, key = body[len("api://"):].partition("/")
        try:
            decoded = json.loads(unquote(key))
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            return ("row", relation, json.dumps(decoded, sort_keys=True, default=str),
                    page, field)
    return ("record", body, page, field)


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Character spans, sorted, overlapping or touching ones joined."""
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _served_texts(result: Any) -> list[tuple[list[str], str, Any, Any]]:
    """What one ``plx.read`` result (as it left the output boundary) served of each text:
    ``(handles, field, version, served)``, ``served`` True for the whole text, else
    ``(start, end, total or None, sha256 of the served characters or None)``. The handles are the asked citation and the one the
    text's envelope names; the version the read's digest (a window's text SHA-256 when
    there is none). A truncated envelope outside a window served its first part only. A
    result that is not resolved serves nothing."""
    if not isinstance(result, dict) or result.get("status") not in ("resolved",
                                                                      "changed_since"):
        return []
    asked = result.get("citation")
    window = result.get("text_window") if isinstance(result.get("text_window"),
                                                     dict) else None
    version = result.get("digest") or (window or {}).get("sha256")
    envelopes = []
    if isinstance(result.get("content"), dict):
        envelopes.append((None, result["content"]))
    if isinstance(result.get("fields"), dict):
        envelopes += [(path, value) for path, value in result["fields"].items()
                      if isinstance(value, dict)]
    out = []
    for path, envelope in envelopes:
        origin = envelope.get("origin") if isinstance(envelope.get("origin"), dict) else {}
        field = origin.get("field_path") or path
        text = envelope.get("untrusted_text")
        page_text = _original_page(origin.get("citation")) and field is None
        if page_text:
            # An original page's text: the field a page claim cites (``PAGE_TEXT``),
            # versioned by its citation, as the ``pages`` entries below are.
            field = PAGE_TEXT
        if not isinstance(field, str) or not isinstance(text, str):
            continue
        handles = [h for h in dict.fromkeys((asked, origin.get("citation")))
                   if isinstance(h, str)]
        if window is not None and window.get("field_path", field) == field \
                and isinstance(window.get("offset"), int) \
                and isinstance(window.get("chars"), int):
            total = window.get("total_chars")
            served: Any = (window["offset"], window["offset"] + window["chars"],
                           total if isinstance(total, int) else None,
                           _sha(text) if len(text) == window["chars"] else None)
            if served[0] == 0 and served[2] is not None and served[1] >= served[2]:
                served = True
        elif envelope.get("truncated") not in (None, False):
            served = (0, len(text), None, _sha(text))
        else:
            served = True
        out.append((handles, field, origin["citation"] if page_text else version, served))
    nested = [entry for item in result.get("attachment_matches") or ()
              if isinstance(item, dict) for entry in _served_texts(item)]
    return out + _served_pages(result, window) + _served_hits(result, version) + nested


def _served_hits(result: dict[str, Any], version: Any
                 ) -> list[tuple[list[str], str, Any, Any]]:
    """What a ``find`` read served: each hit's snippet, a part of its text as a window's
    is (``_served_texts``): on an original's page (``page_citation``) the part of that
    page's ``PAGE_TEXT`` from ``page_offset``, else the part of the text its envelope
    names from ``offset``; hashed, so the gate counts it only where its re-read holds the
    same characters there. Never a whole text."""
    matches = result.get("matches") if isinstance(result.get("matches"), dict) else {}
    hits = matches.get("hits") if isinstance(matches.get("hits"), list) else []
    version = version or matches.get("sha256")
    total = matches.get("total_chars") if isinstance(matches.get("total_chars"), int) \
        else None
    out = []
    for hit in hits:
        envelope = hit.get("text") if isinstance(hit, dict) else None
        origin = envelope.get("origin") if isinstance(envelope, dict) \
            and isinstance(envelope.get("origin"), dict) else {}
        text = envelope.get("untrusted_text") if isinstance(envelope, dict) else None
        if not isinstance(text, str) or not text or envelope.get("truncated") \
                or len(text) != hit.get("chars"):
            continue
        cited, start = origin.get("citation"), hit.get("offset")
        if _original_page(hit.get("page_citation")):
            cited, start = hit["page_citation"], hit.get("page_offset")
        if not isinstance(start, int) or not isinstance(cited, str):
            continue
        served = (start, start + len(text), None if "page_citation" in hit else total,
                  _sha(text))
        if _original_page(cited) and origin.get("field_path") is None:
            out.append(([cited], PAGE_TEXT, cited, served))
        elif isinstance(origin.get("field_path"), str):
            handles = [h for h in dict.fromkeys((result.get("citation"), cited))
                       if isinstance(h, str)]
            out.append((handles, origin["field_path"], version, served))
    return out


def _sha(text: str) -> str:
    """The SHA-256 of served characters: what the answer gate compares with the same
    characters of the text it re-reads (``MCPServer.served_parts``)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _original_page(citation: Any) -> bool:
    return isinstance(citation, str) and citation.startswith("original://") \
        and mn.cited_page(citation)[1] is not None


def _served_pages(result: dict[str, Any], window: dict[str, Any] | None
                  ) -> list[tuple[list[str], str, Any, Any]]:
    """What a read of an original's whole text served of each page its ``pages`` lists:
    the part of the page (in the page's own characters) the returned text covers, as
    ``_served_texts`` says it for that page's citation and ``PAGE_TEXT``: whole only
    when the returned text holds the whole page."""
    content = result.get("content")
    pages = result.get("pages")
    if not isinstance(content, dict) or not isinstance(pages, list) \
            or not isinstance(content.get("untrusted_text"), str):
        return []
    if window is not None and isinstance(window.get("offset"), int) \
            and isinstance(window.get("chars"), int) and "field_path" not in window:
        low, high = window["offset"], window["offset"] + window["chars"]
    elif window is None and content.get("truncated") not in (None, False):
        low, high = 0, len(content["untrusted_text"])
    elif window is None:
        low, high = 0, None
    else:
        return []
    out = []
    for entry in pages:
        citation, offset, chars = (entry.get("page_citation"), entry.get("offset"),
                                   entry.get("chars")) if isinstance(entry, dict) \
            else (None, None, None)
        if not _original_page(citation) or not isinstance(offset, int) \
                or not isinstance(chars, int) or chars < 1:
            continue
        start = max(low, offset) - offset
        end = (chars if high is None else min(high, offset + chars) - offset)
        if end <= start:
            continue
        # The page's served characters as this result's text holds them (located from
        # the window's own offset), hashed so a re-read can tell they are the page's.
        piece = content["untrusted_text"][offset + start - low:offset + end - low]
        served: Any = True if (start, end) == (0, chars) else (
            start, end, chars, _sha(piece) if len(piece) == end - start else None)
        out.append(([citation], PAGE_TEXT, citation, served))
    return out


# Every capture store the item permission read of one call opens (``_permission_reads``)
# waits for writers' locks within this one budget, shared (seconds); a store opened once it
# is spent still waits up to ``PERMISSION_READ_FLOOR`` for a lock. Reading the stores takes
# time of its own (the live SharePoint capture's ~90,000 items: 1-2.3 s under load,
# 2026-11-02), which is no lock wait: it never fails the read.
PERMISSION_READ_TIMEOUT = 2.0


PERMISSION_READ_FLOOR = 0.05


# Why a call's evidence that an item's permissions gate was not kept (``EVIDENCE_META_KEY``,
# on that call's answer): a SharePoint store that records no permission positions, or an
# item permission read that failed (any error, a lock past the budget included). Evidence
# no item permission gates (Portal, BC, mail records) is kept: the projection generation
# and the reader's grant it was read under were decided.
PERMISSIONS_UNRECORDED = "permissions_unrecorded"


PERMISSIONS_UNREADABLE = "permissions_unreadable"


EVIDENCE_META_KEY = "plx/session_evidence"


# The bytes that note adds to an answer's ``_meta`` at most (both reasons are as long).
_UNKEPT_NOTE_BYTES = len(json.dumps({EVIDENCE_META_KEY: {
    "kept": False, "reason": PERMISSIONS_UNREADABLE}}, separators=(",", ":"))) - 1


# An evidence handle of a source with item permission reads that names no item of it.
_UNMAPPED = ("?",)


# The prefix of an unrestricted reader's evidence scope (``_evidence_scope``).
UNRESTRICTED_SCOPE = "unrestricted:"


# The ``store_meta`` keys that name a projection generation (``content_digest`` among
# them: without it a store's generation is all of its meta). A delta layer rewrites the
# others (``freshness``, ``source_freshness``, ``source_condition_unmet``,
# ``relation_completeness``) every cycle. With the session evidence scoped on all of
# them, every session that spanned a delta lost all it had been returned and the gate
# refused each claim citing it (11-13, Postgres the only store:
# "cites_what_this_run_did_not_return" on a count the agent read 60 s before). Across a
# delta cycle the evidence is kept but no count or sum stays complete (``MCPServer._bind``):
# the delta may have added or hidden a member, so a count read before it is "at least";
# a claim is re-read as its record is now (``claim_changed_since``).
GENERATION_META_KEYS = ("content_digest", "schema_digest", "format", "catalog_hash",
                        "steps_digest", "built_at", "l1_generations", "as_known_at")


# Between a scope's generation digest and its whole-meta digest (``_evidence_scope``).
SCOPE_CYCLE_MARK = "~"


def _evidence_scope(meta: dict[str, Any], policy: Any) -> str:
    """The digest of a projection generation and a reader's grant (its ``ReadPolicy``) that
    session evidence is kept under, then the digest of its whole ``store_meta``: two scopes
    of one generation differ by a delta cycle only (``GENERATION_META_KEYS``; a store with
    no ``content_digest`` has only the whole digest). The item permissions it was read
    under are compared item by item (``_permission_reads``, ``MCPServer._bind``)."""
    grant = [policy.tenant, *(sorted((key, sorted(principals))
                                     for key, principals in mapping.items())
                              for mapping in (policy.audiences, policy.markings))]
    digest = hashlib.sha256(json.dumps([sorted(meta.items()), grant]).encode()).hexdigest()
    if meta.get("content_digest"):
        named = sorted((key, meta[key]) for key in GENERATION_META_KEYS if key in meta)
        digest = hashlib.sha256(json.dumps([named, grant]).encode()).hexdigest() \
            + SCOPE_CYCLE_MARK + digest
    return UNRESTRICTED_SCOPE + digest if getattr(policy, "unrestricted", False) is True \
        else digest


class _ItemPermissions:
    """The item permissions one call is read under (``_permission_reads``): each Teams
    conversation's membership (``unit_acl``) read whole, and each SharePoint capture held
    at one snapshot (a read transaction begun before the call: what a writer commits after
    is not in it) whose drive items are digested only when asked (``digests``: the items
    session evidence names, not the live capture's ~90,000, a scan of which took 1-5 s
    under load, 2026-11-02, before every call). A capture not in WAL mode, whose open read
    would hold its writer, is read whole at once instead. ``close`` ends the snapshots; a
    read after it raises (its caller drops what any item gates)."""

    def __init__(self, *, unrestricted: bool = False) -> None:
        self.units: dict[tuple[str, ...], str] = {}
        # Per SharePoint capture, in folder order: a store held in its snapshot, or its
        # item permissions read whole.
        self.sharepoint: list[Any] = []
        self.closed = False
        self._whole: dict[tuple[str, ...], str] | None = None
        # An unrestricted reader's call (``EmployeeEntitlement.unrestricted``): nothing is
        # read and no evidence binds to an item permission (``MCPServer._bind``).
        self.unrestricted = unrestricted

    def digests(self, keys: Iterable[tuple[str, ...]]) -> dict[tuple[str, ...], str]:
        """The digest of each of ``keys`` (``_permission_reads`` keys) at the snapshot; an
        item no capture holds has no entry."""
        if self.closed:
            raise RuntimeError("item_permissions_closed")
        keys = set(keys)
        wanted = {key[1:] for key in keys if key[0] == "sharepoint" and len(key) == 3}
        out: dict[tuple[str, ...], str] = {}
        for source in self.sharepoint if wanted else ():
            items = (source.pinned_item_permissions(wanted) if not isinstance(source, dict)
                     else {item: source[item] for item in wanted if item in source})
            for (drive_id, item_id), digest in items.items():
                key = ("sharepoint", drive_id, item_id)
                out[key] = digest if key not in out else hashlib.sha256(
                    (out[key] + digest).encode()).hexdigest()
        out.update((key, self.units[key]) for key in keys if key in self.units)
        return out

    def whole(self) -> dict[tuple[str, ...], str]:
        """The digest of every SharePoint drive item at the snapshot (a scan of every item:
        only when any change at all decides, ``_whole_decides``)."""
        if self.closed:
            raise RuntimeError("item_permissions_closed")
        if self._whole is None:
            out: dict[tuple[str, ...], str] = {}
            for source in self.sharepoint:
                items = source if isinstance(source, dict) else \
                    source.pinned_item_permissions(None)
                for (drive_id, item_id), digest in items.items():
                    key = ("sharepoint", drive_id, item_id)
                    out[key] = digest if key not in out else hashlib.sha256(
                        (out[key] + digest).encode()).hexdigest()
            self._whole = out
        return self._whole

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for source in self.sharepoint:
            if not isinstance(source, dict):
                try:
                    source.end_permission_read()
                finally:
                    source.close()


def _evidence_items(evidence: dict[str, Any]) -> tuple[set[tuple[str, ...]], bool]:
    """The items (``_permission_reads`` keys) whose permissions gate some session evidence
    (``_drop_changed``'s handles), and whether a change to any item at all decides some of
    it (``_drop_changed``): a complete count (an item granted since may join its group) or
    a handle of an item-gated source naming no item of it."""
    from integrations import capture_store
    kinds = {kind.scheme: kind for kind in map(capture_store.source_kind,
                                               capture_store.source_kinds()) if kind.unit_acl}
    handles: set[Any] = set(evidence["handles"])
    for entry in evidence["counts"]:
        handles.update(entry["records"])
    for key in evidence["currencies"]:
        handles.add(key[0])
    for cited in evidence["currency_citations"].values():
        handles.add(cited[0])
    for entry in evidence.get("texts", {}).values():
        handles.update(entry["handles"])
    items = {_permission_item(handle, kinds) for handle in handles}
    whole = _UNMAPPED in items or any(entry["complete"] for entry in evidence["counts"])
    return {item for item in items if item is not None and item != _UNMAPPED}, whole


def _unkept_reason(exc: BaseException) -> str:
    return (PERMISSIONS_UNRECORDED if getattr(exc, "code", None) == PERMISSIONS_UNRECORDED
            else PERMISSIONS_UNREADABLE)


def _permission_item(handle: Any, kinds: dict[str, Any]) -> tuple[str, ...] | None:
    """The item (a ``_permission_reads`` key) whose recorded permissions gate an evidence
    handle; None for a handle of a source without item permission reads; ``_UNMAPPED`` for
    a handle of such a source that names no item of it. ``kinds``: the unit ACL source
    kinds by scheme."""
    from integrations import capture_sharepoint, capture_store
    if not isinstance(handle, str):
        return None
    scheme = handle.partition("://")[0]
    if scheme == capture_sharepoint.SCHEME:
        item = capture_sharepoint.SharePointCaptureStore.item_of(handle)
        return _UNMAPPED if item is None else ("sharepoint", *item)
    kind = kinds.get(scheme)
    if kind is None:
        return None
    try:
        _, path, _, _ = capture_store.parse_handle(handle)
    except ValueError:
        return _UNMAPPED
    return ("unit", kind.name, kind.unit(path))


def _one_generation(scope: str | None, kept: str | None) -> bool:
    """Whether two evidence scopes (``_evidence_scope``) are of one generation and grant,
    a delta cycle apart."""
    if not scope or not kept or SCOPE_CYCLE_MARK not in scope \
            or SCOPE_CYCLE_MARK not in kept:
        return False
    return scope.split(SCOPE_CYCLE_MARK, 1)[0] == kept.split(SCOPE_CYCLE_MARK, 1)[0]


def _drop_changed(evidence: dict[str, Any], changed: set[tuple[str, ...]] | None) -> None:
    """Drop from session evidence what the items whose permissions changed (``changed``;
    None: every item, as when their permissions could not be read) gate: a count listing a
    record of one, a money cell of one or whose currency was read on one, its handles and
    what was served of its texts (a handle of such a source naming no item counts as
    changed). No count left is complete: a grant since may add a record to its group (a
    lower bound now)."""
    from integrations import capture_store
    kinds = {kind.scheme: kind for kind in map(capture_store.source_kind,
                                               capture_store.source_kinds()) if kind.unit_acl}
    seen: dict[Any, bool] = {}

    def affected(handle: Any) -> bool:
        if handle not in seen:
            item = _permission_item(handle, kinds)
            seen[handle] = item is not None and (changed is None or item == _UNMAPPED
                                                 or item in changed)
        return seen[handle]

    counts = evidence["counts"]
    kept = [dict(entry, complete=False) for entry in counts
            if not any(affected(record) for record in entry["records"])]
    counts.clear()
    counts.extend(kept)
    currencies, cites = evidence["currencies"], evidence["currency_citations"]
    for key in [key for key in currencies
                if affected(key[0]) or (key in cites and affected(cites[key][0]))]:
        del currencies[key]
        cites.pop(key, None)
    handles = evidence["handles"]
    for handle in [handle for handle in handles if affected(handle)]:
        del handles[handle]
    # What was served of a text of such an item goes too: a read under the new
    # permissions starts it over.
    texts = evidence.get("texts", {})
    for key in [key for key, entry in texts.items()
                if any(affected(handle) for handle in entry["handles"])]:
        del texts[key]
