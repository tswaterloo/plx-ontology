"""Case originals of the capture store, served per reader through their source's gate.

The stakeholder briefs, maps
and evidence files that ``scripts.case_originals read`` holds in ``<state>/case-originals``
(bytes by SHA-256, extracted text, one receipt per file) are served through the same
per-reader gate as the source item each one was read from. A reader who could open the
file at its source reads it here; nobody else does.

Source unit (decided from the receipt, never declared by an operator):

* ``sharepoint``: the receipt's canonical drive and item id. The gate is the SharePoint
  capture's own item gate for the item's current captured version
  (``CaseRuntime.captures``: path keys, item permissions recorded at capture, the
  employee's item ACL, the live re-read and capture-age bounds ``plx.read`` applies).
  An item the SharePoint capture does not hold has no known ACL.
* ``teams_chat``: the chat message the file was attached to (``teams_message_url``,
  ``contextType`` chat). The gate is the Teams capture's gate for that message's current
  captured version (chat membership history, the people exclusion). A chat the Teams
  capture does not hold has no known ACL.
* ``repository``: the code repository the file was committed to (``repository_ref``, a
  GitHub blob at a commit). The gate is the live
  audience's grant of ``repo:<owner>/<name>`` to the reader (default deny, exact pattern
  only, never ``*``; ``CaseRuntime._captured_original_gate``). A reader it does not grant
  is refused as before, and a name match on such a file still counts as
  ``source_acl_unknown`` for that reader (``search``), so an unauthorised reader's answers
  are unchanged.
* an archive member or mail attachment (``parent``) inherits its container's unit.

Anything else (a file named only by a path or a link, a malformed repository reference, a
channel message, a receipt whose bytes were not read or did not match) has no known
source ACL and is served to nobody: it is a named gap (``coverage``), never served to
everyone.

The people exclusion holds before any source gate (``CaseRuntime``): a guard that cannot
decide withholds every captured original, and a file in an excluded person's OneDrive
(``/personal/<segment>/``) is withheld from every reader; an owner segment the guard
cannot decide offline withholds the file too.

Citations reuse ``original://``: ``original://<entry id>`` (``co-``/``nr-``/``rv-`` plus 12
hex) and ``original://<entry id>#page=<n>``. A page is a line window of the stored
extracted text (``operating.case_originals`` text pages), so a page citation names the
same text while that text is unchanged; every read checks the stored bytes against the
receipt (re-hashed, unless this process hashed the same file unchanged since:
``_bytes_ok``) and digests the text (``text_sha256``), so a changed file or extraction
never reads as the one cited. Text read through OCR labels every page ``ocr``.

A captured original whose bytes the SharePoint capture already serves as that item's
current version (same SHA-256) with usable extracted text (``text_status`` extracted or
OCR-read), and the members of such a container, are left out of document searches (the
``sharepoint`` family lists them); their ``original://`` citation still reads for a
reader of the item. When the capture holds that version without text (an unsupported
type such as a zip map, an OCR failure, over a bound), the original and its members are
listed as any other, behind the same item gate, and ``text_original_of`` names the
original for a reader of the SharePoint copy (the host points ``plx.read`` of that copy
and a use case's document list to it).

A document search (``search``) matches the query's words in a file's name or in its stored
extracted text (``extractions/<sha256>.txt``, the reading ``scripts.case_originals read``
wrote; never the bytes, never re-extracted at query time). Matching originals the reader
may not open are counted, never named (``withheld``), and only by a match in the file
name: their text is never matched for a reader who cannot open them.
By gate kind, those with no known
source ACL (``source_acl_unknown``) and those whose source gate refused this reader
(``source_gate_denied``). A file the people exclusion withholds, or whose owner it cannot
decide, is never counted, and a guard that cannot decide counts nothing. A Teams chat
attachment is counted only when the bound guard clears the message's sender and the
chat's members as the Teams capture records them (a chat it does not hold: never); a
file with no known source item only when its source names a SharePoint site or a
repository. The gate order is text-blind (file-name matches first, then by name), so no
text of an original the reader may not open decides which originals are gated.

Refusal kind (internal, never part of a tool result): every reader answer that does not
open an original reads ``original_not_visible`` alike, but ``refusal_kind`` tells the
evaluation scorer why, as ``REFUSED`` or ``FAILED``. ``REFUSED`` is a decided refusal: the
source ACL denies this reader, the original has no known source ACL (no source item, or
an item a configured capture does not hold) and is served to nobody by policy, or the
people exclusion decided the file is excluded. ``FAILED`` is anything else: an
exception, a people exclusion that cannot decide, a source capture that is not
configured, a store that is unreadable or locked, a timeout, a transient live-read
failure (429, 5xx, a permission list not read in full), a deletion (a live 404 or 410,
or one the capture recorded). A failure never reads as a
refusal (``evals.runner.originals_held``).

Read-only: nothing here writes to the capture store or any source. Evidence label:
``isolated-fixture`` / ``injected-transport`` in tests; live-unverified.
"""

from __future__ import annotations

import bisect
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .cases import CitationUnresolvable

STORE_DIR = "case-originals"
SCHEME = "original://"
ENTRY_ID = re.compile(r"^(?:co|nr|rv)-[0-9a-f]{12}$")
HANDLE = re.compile(r"^original://((?:co|nr|rv)-[0-9a-f]{12})(?:#page=([1-9][0-9]{0,3}))?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PERSONAL = re.compile(r"^/personal/([^/]+)/", re.IGNORECASE)
_TEAMS_MESSAGE = re.compile(r"^/l/message/([^/]+)/([0-9]{1,20})$")
# A repository-sourced original's ``repository_ref`` (a GitHub blob at a commit, the shape
# ``scripts.case_originals`` reads from the local object store): owner and name.
_REPOSITORY_REF = re.compile(r"^https://github\.com/([A-Za-z0-9_.\-]{1,100})/"
                             r"([A-Za-z0-9_.\-]{1,100})/blob/[0-9a-f]{40}/[^\x00-\x20?#]+$")
RECEIPT_SCHEMA = "plx-case-original-receipt/v1"
OCR_METHOD = "tesseract-ocr"
TEXT_ONLY_METHODS = frozenset({"utf8-text"})
PAGE_METHOD = "captured-text-lines"
MAX_PARENT_DEPTH = 8
# Captured originals one document search gates at most (each a source-gate decision).
MAX_LISTING_CHECKS = 200
# Gate kinds of matching originals a reader may not open (``search``'s ``withheld``).
WITHHELD_UNKNOWN_ACL = "source_acl_unknown"
WITHHELD_GATE_DENIED = "source_gate_denied"
_WORD = re.compile(r"[^\W_]+")
# Characters of stored-text vocabularies one store keeps in memory (plus 16 per word).
WORDS_BOUND = 32_000_000
# Stored files whose equal hash one store remembers (``_bytes_ok``), and how long a file
# must be unchanged before its hash is remembered.
HASHED_BOUND = 100_000
SETTLED_NS = 2_000_000_000
# Page windows of stored texts one store keeps (``_pages_of``; 128 per page).
SPLITS_BOUND = 8_000_000

# Why an original has no source ACL (``coverage``): served to nobody.
NOT_READ = "bytes_not_read"
NO_SOURCE_ITEM = "source_item_unidentified"
REPOSITORY = "repository_file"
CHANNEL = "teams_channel_unsupported"
PARENT_UNKNOWN = "container_unknown"
SOURCE_NOT_CAPTURED = "source_not_captured"

# Why a reader is not served an original (``refusal_kind``; never in a tool result).
REFUSED = "refused"   # decided: ACL denies, no known ACL by policy, or people excluded
FAILED = "failed"     # undecided: an exception, guard, configuration or store failure
_KIND = "refusal_kind"   # the attribute a gate's CitationUnresolvable carries it in


def refusal(code: str, kind: str) -> CitationUnresolvable:
    """A ``CitationUnresolvable(code)`` that carries its refusal ``kind`` (``REFUSED`` or
    ``FAILED``) for the scorer only: same type and message, so every answer built from it
    is unchanged."""
    exc = CitationUnresolvable(code)
    setattr(exc, _KIND, kind)
    return exc


def kind_of(exc: BaseException) -> str:
    """The refusal kind an exception carries: ``REFUSED`` only when set so, else
    ``FAILED`` (an unmarked exception is never a refusal)."""
    return REFUSED if getattr(exc, _KIND, None) == REFUSED else FAILED


def _onedrive_owner(url: Any) -> str | None:
    """The ``/personal/<segment>/`` of a OneDrive URL, lowercased, or None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not (parts.hostname or "").endswith("-my.sharepoint.com"):
        return None
    found = _PERSONAL.match(unquote(parts.path))
    return found.group(1).lower() if found else None


def _teams_chat(url: Any) -> tuple[str, str] | str | None:
    """(chat id, message id) of a Teams chat message link; ``CHANNEL`` for any other Teams
    message link; None when ``url`` is no Teams message link."""
    if not isinstance(url, str) or not url.startswith("https://teams.microsoft.com/l/message/"):
        return None
    try:
        parts = urlsplit(url)
        context = json.loads((parse_qs(parts.query).get("context") or ["{}"])[0])
    except ValueError:
        return CHANNEL
    found = _TEAMS_MESSAGE.match(parts.path)
    if found is None or not isinstance(context, Mapping) \
            or context.get("contextType") != "chat":
        return CHANNEL
    chat = unquote(found.group(1))
    if not re.fullmatch(r"19:[A-Za-z0-9_.\-]{1,200}@[A-Za-z0-9_.\-]{1,64}", chat):
        return CHANNEL
    return chat, found.group(2)


def source_unit(receipt: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(unit, None) of a receipt's own canonical source, or (None, reason) when it names
    no source whose ACL a capture keeps. A ``parent`` is resolved by the caller."""
    source = receipt.get("canonical_source")
    if not isinstance(source, Mapping):
        return None, NO_SOURCE_ITEM
    owner = _onedrive_owner(source.get("url"))
    drive, item = source.get("drive_id"), source.get("item_id")
    if isinstance(drive, str) and drive and isinstance(item, str) and item:
        return {"kind": "sharepoint", "drive_id": drive, "item_id": item,
                "onedrive_owner": owner}, None
    chat = _teams_chat(source.get("teams_message_url"))
    if isinstance(chat, tuple):
        return {"kind": "teams_chat", "chat_id": chat[0], "message_id": chat[1],
                "onedrive_owner": owner}, None
    if chat == CHANNEL:
        return None, CHANNEL
    if source.get("repository_ref"):
        ref = source["repository_ref"]
        found = _REPOSITORY_REF.match(ref) if isinstance(ref, str) else None
        if found is None or any(set(part) == {"."} for part in found.groups()):
            return None, REPOSITORY
        return {"kind": "repository", "repository": f"{found.group(1)}/{found.group(2)}",
                "onedrive_owner": owner}, None
    return None, NO_SOURCE_ITEM


def repository_resource(unit: Mapping[str, Any]) -> str | None:
    """The live-audience resource that grants a ``repository`` unit
    (``repo:<owner>/<name>``), or None for any other unit."""
    if unit.get("kind") != "repository" or not isinstance(unit.get("repository"), str):
        return None
    return f"repo:{unit['repository']}"


def vocabulary(text: str) -> tuple[str, ...]:
    """The distinct casefolded words of ``text``, sorted (``holds_words`` looks them up)."""
    return tuple(sorted(set(_WORD.findall(text.casefold()))))


def holds_words(words: Sequence[str], name: str, words_of_text: Sequence[str]) -> bool:
    """Does every one of ``words`` (casefolded) occur in the file ``name`` or begin a word
    of its text (``vocabulary``)? No words: False (nothing was asked)."""
    if not words:
        return False
    folded = name.casefold()
    for word in words:
        if word in folded:
            continue
        at = bisect.bisect_left(words_of_text, word)
        if at >= len(words_of_text) or not words_of_text[at].startswith(word):
            return False
    return True


def serves_text(current: Mapping[str, Any] | None) -> bool:
    """Whether the SharePoint capture serves usable extracted text for a current version
    (``SourceLookup.sharepoint``): read (``extracted``) or OCR-read; anything else (an
    unsupported type, a failure, a bound, OCR pending, a status not stated) is not."""
    from integrations.captured_text import TEXT_READ
    return isinstance(current, Mapping) and current.get("text_status") in TEXT_READ


def _readable_receipt(receipt: Any) -> bool:
    """Bytes read in full, matching what the registry pinned (a discovered file has no
    pin), stored at their content address."""
    if not isinstance(receipt, Mapping) or receipt.get("schema") != RECEIPT_SCHEMA:
        return False
    sha = receipt.get("sha256")
    return (receipt.get("body_status") == "read" and receipt.get("match") is not False
            and isinstance(sha, str) and bool(_HEX64.match(sha))
            and receipt.get("stored") == f"{STORE_DIR}/{sha}"
            and isinstance(receipt.get("entry_id"), str)
            and bool(ENTRY_ID.match(receipt["entry_id"])))


class _Entry:
    __slots__ = ("id", "receipt", "unit", "reason", "root")

    def __init__(self, entry_id: str, receipt: Mapping[str, Any]) -> None:
        self.id, self.receipt = entry_id, receipt
        self.unit: dict[str, Any] | None = None
        self.reason: str | None = None
        self.root = entry_id   # the outermost container (itself for a file)


def _unchanged_key(path: Path, stat: os.stat_result, sha256: str) -> tuple:
    """What a remembered hash of a stored file is valid for (``_bytes_ok``)."""
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns, sha256)


class SourceLookup:
    """The source captures' current versions, read-only, without any reader: which
    SharePoint version and which Teams message version a unit's gate decides on.
    ``sharepoint_dir``: the SharePoint capture's state directory (None: none held)."""

    def __init__(self, state_dir: str | Path, *, sharepoint_dir: str | Path | None,
                 audience: Any = None) -> None:
        self.state_dir = Path(state_dir)
        self.sharepoint_dir = None if sharepoint_dir is None else Path(sharepoint_dir)
        self.audience = audience

    def sharepoint(self, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """(drive, item) -> {l1_handle, content_sha256, text_status} of each current
        captured version."""
        from integrations import capture_sharepoint as cs
        wanted = sorted(set(pairs))
        if not wanted or not self.sharepoint_configured():
            return {}
        store = cs.SharePointCaptureStore.open_read_only(self.sharepoint_dir)
        try:
            out = {}
            for drive, item in wanted:
                row = store.current(drive, item)
                if row is not None and row.get("removed_reason") is None:
                    out[(drive, item)] = {"l1_handle": row["l1_handle"],
                                          "content_sha256": row["content_sha256"],
                                          "text_status": row.get("text_status")}
            return out
        finally:
            store.close()

    def sharepoint_configured(self) -> bool:
        """Whether a SharePoint capture is wired and exists (else no item is decided)."""
        from integrations import capture_sharepoint as cs
        return self.sharepoint_dir is not None and cs.store_exists(self.sharepoint_dir)

    def sharepoint_deleted(self, drive_id: str, item_id: str) -> bool:
        """Whether the SharePoint capture recorded the item deleted: its latest captured
        version is gone from the source (``absent_after_resync``). False for an item it
        never captured or one moved out of its scope; True on any doubt (no capture, an
        unreadable store): a refusal is only claimed for an item not known deleted."""
        from integrations import capture_sharepoint as cs
        if not self.sharepoint_configured():
            return True
        try:
            store = cs.SharePointCaptureStore.open_read_only(self.sharepoint_dir)
            try:
                latest = store._item_latest({"drive_id": drive_id, "item_id": item_id})
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - undecided: not a refusal
            return True
        return latest is not None and latest["removed_reason"] == cs.ABSENT

    def teams_chat(self, chat_id: str, message_id: str) -> str | None:
        """The capture handle of the message's current captured version, or None."""
        found = self.teams_chat_decided(chat_id, message_id)
        return None if found == FAILED else found

    def teams_chat_decided(self, chat_id: str, message_id: str) -> str | None:
        """``teams_chat``, telling apart a message the Teams capture does not hold (None)
        from a capture that is absent or cannot be opened (``FAILED``)."""
        from integrations.capture_store import CaptureError, CaptureStore, make_handle
        from integrations.capture_teams import CHAT_KIND, DB_FILE, STORE_DIR as TEAMS_DIR
        from integrations.live_source_guard import LiveSourceAudience
        path = self.state_dir / TEAMS_DIR / DB_FILE
        if not path.is_file():
            return FAILED
        audience = self.audience or LiveSourceAudience({}, tenant="lookup")
        try:
            store = CaptureStore.open_read_only(path, audience)
        except CaptureError:
            return FAILED
        try:
            for found, capture_id in store.current_versions(
                    CHAT_KIND.name, f"teams:chat:{chat_id}"):
                if tuple(found) == (chat_id, message_id):
                    return make_handle(CHAT_KIND, found, store.generation, capture_id)
            return None
        finally:
            store.close()


    def teams_chat_people(self, chat_id: str, message_id: str) -> list[Any] | None:
        """Everyone the people exclusion must clear before anything about a chat
        attachment is told, without any reader: the message's sender and participants,
        its recorded membership, and the chat's latest recorded membership. None when the
        Teams capture does not hold the message or its chat's membership (undecidable)."""
        from integrations.capture_store import CaptureError, CaptureStore
        from integrations.capture_teams import CHAT_KIND, DB_FILE, STORE_DIR as TEAMS_DIR
        from integrations.live_source_guard import LiveSourceAudience
        path = self.state_dir / TEAMS_DIR / DB_FILE
        if not path.is_file():
            return None
        audience = self.audience or LiveSourceAudience({}, tenant="lookup")
        unit = f"teams:chat:{chat_id}"
        try:
            store = CaptureStore.open_read_only(path, audience)
        except CaptureError:
            return None
        try:
            membership = store.unit_acl(CHAT_KIND.name, unit)
            record = next((r for r in store.current_records(CHAT_KIND.name, unit)
                           if tuple(r.path) == (chat_id, message_id)), None)
        except (CaptureError, ValueError):
            return None
        finally:
            store.close()
        if membership is None or record is None or not isinstance(record.payload, Mapping):
            return None
        row = record.payload.get("row") or {}
        meta = record.payload.get("meta") or {}
        return [membership, row.get("from"), row.get("participants"),
                (meta.get("source_acl") or {}) if isinstance(meta, Mapping) else {}]


def sharepoint_items(state_dir: str | Path) -> list[tuple[str, str]]:
    """The (canonical drive id, item id) of every SharePoint item a served-shape case
    original under ``<state_dir>/case-originals`` names as its source unit (a member's:
    its outermost container's), sorted, each once: the items the SharePoint capture
    reads first so their gate can decide (``capture_sharepoint`` Named items first).
    Read-only; serves nothing (the gate here opens no original)."""
    entries = CapturedOriginals(state_dir, gate=lambda _unit, _actor: False).entries()
    return sorted({(e.unit["drive_id"], e.unit["item_id"]) for e in entries.values()
                   if e.unit is not None and e.unit["kind"] == "sharepoint"})


class CapturedOriginals:
    """``<state>/case-originals`` read as originals, each behind its source's gate.

    ``gate(unit, actor)``: may ``actor`` read the source unit now (the runtime's people
    exclusion, then the source capture's gate): True opens it; ``REFUSED`` is a decided
    refusal; anything else (False, ``FAILED``, an exception) denies as a failure. Every
    denial reads alike; only ``refusal_kind`` tells them apart.
    ``lookup``: a :class:`SourceLookup` (or a callable returning one: the host may wire
    its SharePoint capture after the runtime is built).
    ``exclusion_clear(owner, people) -> bool``: True only when the people exclusion
    decides that a file with this OneDrive owner segment (None: no OneDrive), whose
    source names ``people`` (a Teams chat attachment's sender and members; None: no such
    people), is withheld from nobody; without it (or on any exception) no withheld
    original is counted."""

    def __init__(self, state_dir: str | Path, *, gate: Callable[[Mapping[str, Any], Any], bool],
                 lookup: Any = None,
                 exclusion_clear: Callable[[str | None, Any], bool] | None = None
                 ) -> None:
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / STORE_DIR
        self._gate = gate
        self._lookup = lookup
        self._exclusion_clear = exclusion_clear
        self._lock = threading.Lock()
        self._cache: tuple[tuple, dict[str, _Entry]] | None = None
        self._versions: tuple[Any, dict] | None = None   # ``_by_version``
        # (sha256, text file mtime_ns, size) -> vocabulary of the stored extracted text,
        # least recently used out first past WORDS_BOUND characters.
        from integrations.captured_text import ReadingMemo
        self._words: Any = ReadingMemo(WORDS_BOUND, lambda words: sum(map(len, words or ()))
                                       + 16 * len(words or ()))
        # Stored files hashed equal to their receipt, while unchanged (``_bytes_ok``).
        self._hashed: OrderedDict[tuple, bool] = OrderedDict()
        # SHA-256 of a stored extracted text -> its page windows (``_pages_of``), least
        # recently used out first past SPLITS_BOUND (128 per page).
        self._splits: Any = ReadingMemo(SPLITS_BOUND, lambda pages: 64 + 128 * len(
            pages or ()))

    # ---- index -------------------------------------------------------------

    def _receipt_files(self) -> list[Path]:
        receipts = self.root / "receipts"
        if not receipts.is_dir():
            return []
        return sorted(p for folder in receipts.iterdir() if folder.is_dir()
                      for p in folder.glob("*.json"))

    def _signature(self) -> tuple:
        receipts = self.root / "receipts"
        if not receipts.is_dir():
            return ()
        return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in receipts.iterdir()
                            if f.is_dir()))

    def entries(self) -> dict[str, _Entry]:
        """Every entry with a receipt, its source unit or the reason it has none."""
        signature = self._signature()
        with self._lock:
            if self._cache is not None and self._cache[0] == signature:
                return self._cache[1]
        latest: dict[str, Mapping[str, Any]] = {}
        for path in self._receipt_files():
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entry_id = receipt.get("entry_id") if isinstance(receipt, Mapping) else None
            if not isinstance(entry_id, str) or not ENTRY_ID.match(entry_id):
                continue
            held = latest.get(entry_id)
            # A readable receipt wins over an unreadable one, then the newest read.
            rank = (_readable_receipt(receipt), str(receipt.get("read_at") or ""))
            if held is None or rank > (_readable_receipt(held), str(held.get("read_at") or "")):
                latest[entry_id] = receipt
        entries = {entry_id: _Entry(entry_id, receipt) for entry_id, receipt in latest.items()}
        for entry in entries.values():
            self._place(entry, entries)
        with self._lock:
            self._cache = (signature, entries)
        return entries

    @staticmethod
    def _place(entry: _Entry, entries: Mapping[str, _Entry]) -> None:
        if not _readable_receipt(entry.receipt):
            entry.reason = NOT_READ
            return
        node, depth = entry.receipt, 0
        while node.get("parent") is not None:
            parent = entries.get(str(node.get("parent")))
            depth += 1
            if parent is None or depth > MAX_PARENT_DEPTH:
                entry.reason = PARENT_UNKNOWN
                return
            node, entry.root = parent.receipt, parent.id
        entry.unit, entry.reason = source_unit(node)

    def _source_lookup(self) -> SourceLookup | None:
        lookup = self._lookup() if callable(self._lookup) else self._lookup
        return lookup if isinstance(lookup, SourceLookup) else None

    # ---- gate --------------------------------------------------------------

    def _unit_for_gate(self, entry: _Entry, lookup: SourceLookup | None,
                       held: Mapping[tuple[str, str], Mapping[str, Any]] | None = None
                       ) -> dict[str, Any] | None:
        """The unit with the source version its gate decides on, or None (not held). A
        repository unit needs no capture: its gate is the live audience's grant."""
        unit = entry.unit
        if unit is not None and unit["kind"] == "repository":
            return {**unit, "handle": repository_resource(unit)}
        if unit is None or lookup is None:
            return None
        if unit["kind"] == "sharepoint":
            pair = (unit["drive_id"], unit["item_id"])
            current = (held if held is not None else lookup.sharepoint([pair])).get(pair)
            return None if current is None else {**unit, "handle": current["l1_handle"],
                                                  "content_sha256": current["content_sha256"]}
        if unit["kind"] == "teams_chat":
            handle = lookup.teams_chat(unit["chat_id"], unit["message_id"])
            return None if handle is None else {**unit, "handle": handle}
        return None

    def _allowed(self, unit: Mapping[str, Any] | None, actor: Any) -> bool:
        return unit is not None and self._decision(unit, actor) is True

    def _decision(self, unit: Mapping[str, Any], actor: Any) -> bool | str:
        """True (open), ``REFUSED`` or ``FAILED`` of the gate on a held unit."""
        try:
            decided = self._gate(unit, actor)
        except Exception:  # noqa: BLE001 - a failing gate denies, never leaks
            return FAILED
        return True if decided is True else REFUSED if decided == REFUSED else FAILED

    def _unheld_kind(self, entry: _Entry, lookup: SourceLookup | None) -> str:
        """Why an entry has no unit its gate decides on: ``REFUSED`` when it has no known
        source ACL (served to nobody by policy: no source item, or an item or chat the
        configured capture does not hold), ``FAILED`` when no lookup or source capture
        is configured, the capture cannot be opened or it recorded the item deleted."""
        unit = entry.unit
        if unit is None:
            # A container whose receipt is gone or unreadable says nothing of an ACL.
            return FAILED if entry.reason == PARENT_UNKNOWN else REFUSED
        if lookup is None:
            return FAILED
        if unit["kind"] == "sharepoint":
            # A deletion the capture recorded is a failure to serve, never a refusal.
            return FAILED if not lookup.sharepoint_configured() or lookup.sharepoint_deleted(
                unit["drive_id"], unit["item_id"]) else REFUSED
        if unit["kind"] == "teams_chat":
            found = lookup.teams_chat_decided(unit["chat_id"], unit["message_id"])
            return REFUSED if found is None else FAILED
        return FAILED

    def _visible_entry(self, entry_id: str, actor: Any) -> _Entry:
        entry = self.entries().get(entry_id)
        if entry is None or not self._allowed(
                self._unit_for_gate(entry, self._source_lookup()), actor):
            raise CitationUnresolvable("original_not_visible")
        return entry

    def refusal_kind(self, handle: Any, actor: Any) -> str | None:
        """Why ``actor`` is not served the captured original ``handle`` now: None when the
        gate opens it, ``REFUSED`` for a decided refusal, ``FAILED`` for anything else
        (an unknown entry, an exception, an undecided guard, a source not configured, an
        unreadable store). For the evaluation scorer only (``evals.runner``): it reads
        no bytes and never changes what ``read`` or ``search`` answer."""
        match = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None:
            return FAILED
        try:
            entry = self.entries().get(match.group(1))
            if entry is None:
                return FAILED
            lookup = self._source_lookup()
            unit = self._unit_for_gate(entry, lookup)
            if unit is None:
                return self._unheld_kind(entry, lookup)
            decided = self._decision(unit, actor)
        except Exception:  # noqa: BLE001 - a failing check is a failure, never a refusal
            return FAILED
        return None if decided is True else decided

    # ---- content -----------------------------------------------------------

    def _bytes_ok(self, receipt: Mapping[str, Any]) -> None:
        """The stored bytes still hash to the receipt's SHA-256, else
        ``original_missing`` / ``original_content_changed``. A file this process hashed
        before and found equal is not hashed again while it is the same unchanged file
        (``_unchanged_key``: device, inode, size, modification and change times, the
        last two set by the kernel on every write, the change time by nothing a writer
        can set back); a file is remembered only once it is older than ``SETTLED_NS``,
        so a write within the clock's granularity of the hash is never missed."""
        path = self.state_dir / receipt["stored"]
        try:
            before = os.stat(path)
        except OSError:
            raise CitationUnresolvable("original_missing") from None
        key = _unchanged_key(path, before, receipt["sha256"])
        with self._lock:
            if key in self._hashed:
                self._hashed.move_to_end(key)
                return
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
                after = os.fstat(handle.fileno())
        except OSError:
            raise CitationUnresolvable("original_missing") from None
        if digest.hexdigest() != receipt["sha256"]:
            raise CitationUnresolvable("original_content_changed")
        if _unchanged_key(path, after, receipt["sha256"]) == key and time.time_ns() - max(
                after.st_mtime_ns, after.st_ctime_ns) > SETTLED_NS:
            with self._lock:
                self._hashed[key] = True
                while len(self._hashed) > HASHED_BOUND:
                    self._hashed.popitem(last=False)

    def _extraction(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """The stored text as line-window pages, with the receipt's extraction facts."""
        facts = receipt.get("extraction") if isinstance(receipt.get("extraction"), Mapping) \
            else {}
        methods = sorted(m for m in facts.get("methods") or [] if isinstance(m, str))
        ref = facts.get("text_ref")
        text = ""
        if ref is not None:
            if ref != f"{STORE_DIR}/extractions/{receipt['sha256']}.txt":
                raise CitationUnresolvable("original_text_unreadable")
            try:
                text = (self.state_dir / ref).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise CitationUnresolvable("original_text_unreadable") from None
        pages = self._pages_of(text) if text else {
            "text": "", "pages": [], "text_sha256": hashlib.sha256(b"").hexdigest()}
        ocr = OCR_METHOD in methods
        return {"status": str(facts.get("status") or "unread"), "method": PAGE_METHOD,
                "error": None, "text": pages["text"], "pages": pages["pages"],
                "ocr_pages": [p["page"] for p in pages["pages"]] if ocr else [],
                "warnings": sorted(str(r) for r in facts.get("unread_reasons") or []),
                "text_sha256": pages["text_sha256"], "source_methods": methods}

    def _pages_of(self, text: str) -> dict[str, Any]:
        """``case_originals._text_extraction`` of the stored text (its line-window pages
        and ``text_sha256``), remembered by the SHA-256 of the text just read: the text
        is read and digested on every read, only its page split is kept. Each caller
        gets its own page records."""
        from .case_originals import _text_extraction
        content = text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        pages = self._splits.read(digest, lambda: tuple(_text_extraction(content)["pages"]))
        if pages is None:   # the split raised: read as before, nothing remembered
            pages = _text_extraction(content)["pages"]
        return {"text": text, "text_sha256": digest,
                "pages": [{**page, "lines": list(page["lines"])} for page in pages]}

    @staticmethod
    def _summary(extraction: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": extraction["status"], "method": extraction["method"],
                "error": None, "page_count": len(extraction["pages"]),
                "ocr_pages": extraction["ocr_pages"], "warnings": extraction["warnings"],
                "text_sha256": extraction["text_sha256"],
                "source_methods": extraction["source_methods"]}

    @staticmethod
    def _text_source(extraction: Mapping[str, Any]) -> str:
        if extraction["ocr_pages"]:
            return "ocr"
        if set(extraction["source_methods"]) <= TEXT_ONLY_METHODS:
            return "original_text"
        return "machine_converted"

    def _view(self, entry: _Entry, handle: str, page: str | None
              ) -> tuple[dict[str, Any], dict[str, Any]]:
        receipt = entry.receipt
        self._bytes_ok(receipt)
        extraction = self._extraction(receipt)
        unit = entry.unit or {}
        view: dict[str, Any] = {
            "citation": handle, "original_id": entry.id, "sha256": receipt["sha256"],
            "filename": str(receipt.get("name") or ""),
            "media_type": receipt.get("detected_media_type"),
            "bytes": receipt.get("bytes"), "extraction": self._summary(extraction),
            "evidence_mode": "captured case original (bytes read at capture, re-hashed)",
            "provenance": {"source": "case-original-capture", "source_unit": unit.get("kind"),
                           "container": None if entry.root == entry.id else entry.root,
                           "read_at": receipt.get("read_at")}}
        if page is not None:
            number = int(page)
            spans = [p for p in extraction["pages"] if p["page"] == number]
            if not spans:
                raise CitationUnresolvable("original_page_not_found")
            view.update(page=number, text=extraction["text"][spans[0]["start"]:spans[0]["end"]],
                        text_source=self._text_source(extraction),
                        lines=list(spans[0]["lines"]))
        return view, extraction

    # ---- the OriginalStore surface ------------------------------------------

    @staticmethod
    def handles(handle: Any) -> bool:
        return isinstance(handle, str) and HANDLE.fullmatch(handle) is not None

    def resolve(self, handle: Any, actor: Any) -> dict[str, Any]:
        """Content-stable view of a captured original (or one page) for ``actor``."""
        match = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None:
            raise CitationUnresolvable("invalid_original_citation")
        entry = self._visible_entry(match.group(1), actor)
        return self._view(entry, handle, match.group(2))[0]

    def read(self, handle: Any, actor: Any) -> dict[str, Any]:
        """``resolve`` plus, for a file citation, its page citations."""
        match = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None:
            raise CitationUnresolvable("invalid_original_citation")
        entry = self._visible_entry(match.group(1), actor)
        view, extraction = self._view(entry, handle, match.group(2))
        if "page" in view:
            return view
        return {**view, "page_citations": [f"{handle}#page={p['page']}"
                                           for p in extraction["pages"]]}

    def text_document(self, handle: Any, actor: Any) -> dict[str, Any]:
        """The whole extracted text of a file citation, after ``resolve``'s checks."""
        match = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None or match.group(2):
            raise CitationUnresolvable("original_file_citation_required")
        entry = self._visible_entry(match.group(1), actor)
        view, extraction = self._view(entry, handle, None)
        return {"view": view, "text": extraction["text"], "pages": list(extraction["pages"]),
                "ocr_pages": list(extraction["ocr_pages"]),
                "warnings": list(extraction["warnings"]),
                "text_sha256": extraction["text_sha256"]}

    def _stored_words(self, receipt: Mapping[str, Any]) -> tuple[str, ...]:
        """The vocabulary of a readable receipt's stored extracted text (no text: none).
        Read from the stored reading, never the bytes; kept while that file is unchanged."""
        facts = receipt.get("extraction")
        ref = facts.get("text_ref") if isinstance(facts, Mapping) else None
        if ref is None or ref != f"{STORE_DIR}/extractions/{receipt['sha256']}.txt":
            return ()
        path = self.state_dir / ref
        try:
            stat = path.stat()
        except OSError:
            return ()
        def read() -> tuple[str, ...]:
            try:
                return vocabulary(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                return ()   # not text: no words (the same bytes read alike every time)

        words = self._words.read((receipt["sha256"], stat.st_mtime_ns, stat.st_size), read)
        return words if isinstance(words, tuple) else ()

    def _countable(self, entry: _Entry, entries: Mapping[str, _Entry],
                   lookup: SourceLookup, chats: dict[tuple[str, str], Any]) -> bool:
        """May a withheld match on ``entry`` be counted: the people exclusion decides it
        withholds the file from nobody. A SharePoint item: its (or its container's)
        OneDrive owner. A Teams chat attachment: the message's sender and the chat's
        members, read from the Teams capture (not held: undecidable, never counted). A
        file with no known source item is counted only when its source names a SharePoint
        site or a repository (no person's mail or chat), a repository file with the same
        checks; anything else counts nothing."""
        if self._exclusion_clear is None:
            return False
        unit, people = entry.unit, None
        # A repository file is counted exactly as before its grant existed (as a file of
        # no known source item): the same source checks, the same owner.
        if unit is None or unit["kind"] == "repository":
            source = entries[entry.root].receipt.get("canonical_source")
            if not isinstance(source, Mapping) or source.get("teams_message_url") \
                    or source.get("attachment_name"):
                return False
            url = source.get("url")
            host = (urlsplit(url).hostname or "") if isinstance(url, str) else ""
            if not (host.endswith(".sharepoint.com") or source.get("repository_ref")):
                return False
            owner = _onedrive_owner(url)
        else:
            owner = unit.get("onedrive_owner")
            if unit["kind"] == "teams_chat":
                key = (unit["chat_id"], unit["message_id"])
                if key not in chats:
                    try:
                        chats[key] = lookup.teams_chat_people(*key)
                    except Exception:  # noqa: BLE001 - an unreadable capture: undecided
                        chats[key] = None
                people = chats[key]
                if people is None:
                    return False
            elif unit["kind"] != "sharepoint":
                return False
        try:
            return self._exclusion_clear(owner, people) is True
        except Exception:  # noqa: BLE001 - an undecided exclusion counts nothing
            return False

    def list_visible(self, actor: Any, limit: int = 50, *, words: Sequence[str] = (),
                     max_checks: int = MAX_LISTING_CHECKS) -> tuple[list[dict[str, Any]], bool]:
        """(summaries, cut) of ``search``."""
        found = self.search(actor, limit, words=words, max_checks=max_checks)
        return found["found"], found["cut"]

    def search(self, actor: Any, limit: int = 50, *, words: Sequence[str] = (),
               max_checks: int = MAX_LISTING_CHECKS) -> dict[str, Any]:
        """The captured originals ``actor`` may read now for a document search.

        ``found``: summaries, those holding every one of ``words`` first (file name, then
        stored extracted text: ``match`` is ``name``, ``text`` or None). Originals the
        SharePoint family already lists with its text (same bytes as the item's current
        captured version, read with usable text: ``serves_text``; and their members) are
        left out. ``cut``: the gate bound or ``limit``
        ended the scan. ``withheld``: gate kind -> number of originals whose file name
        holds every word (never their text: decision 2026-10-27 refused originals name only)
        that this reader may not open (counts only; an original with the bytes of its
        item's current SharePoint version is never counted, whatever its text; None when no words were given or
        the people exclusion cannot decide); with ``cut`` a lower bound."""
        entries = self.entries()
        lookup = self._source_lookup()
        if lookup is None:
            return {"found": [], "cut": False, "withheld": None}
        candidates = [e for e in entries.values() if e.unit is not None]
        held = lookup.sharepoint((e.unit["drive_id"], e.unit["item_id"]) for e in candidates
                                 if e.unit["kind"] == "sharepoint")

        def same_bytes(entry: _Entry) -> bool:
            root = entries[entry.root]
            unit = entry.unit or {}
            current = held.get((unit.get("drive_id"), unit.get("item_id")))
            return (unit.get("kind") == "sharepoint" and current is not None
                    and current["content_sha256"] == root.receipt["sha256"])

        def duplicate(entry: _Entry) -> bool:
            unit = entry.unit or {}
            return same_bytes(entry) and serves_text(
                held.get((unit.get("drive_id"), unit.get("item_id"))))

        folded = list(dict.fromkeys(w.casefold() for w in words if w))

        def name_match(entry: _Entry) -> bool:
            name = str(entry.receipt.get("name") or "").casefold()
            return bool(folded) and all(w in name for w in folded)

        def order(entry: _Entry) -> tuple:
            # Text-blind: an original's text is matched only after the gate admits the
            # reader, so no text of one it may not open decides what is gated first.
            name = str(entry.receipt.get("name") or "").casefold()
            return (not name_match(entry), name, entry.id)

        units: dict[str, dict[str, Any] | None] = {}

        def gated_unit(entry: _Entry) -> dict[str, Any] | None:
            if entry.id not in units:
                units[entry.id] = self._unit_for_gate(entry, lookup, held)
            return units[entry.id]

        refusals: dict[str, bool] = {}

        def refused_repository(entry: _Entry) -> bool:
            # A repository file this reader's grant does not open reads as it did before
            # the grant existed (decision 2026-10-29): counted with the originals of no
            # known source ACL, never charged to the gate bound. Name-only, text-blind.
            unit = gated_unit(entry)
            if unit is None or unit["kind"] != "repository":
                return False
            if entry.id not in refusals:
                refusals[entry.id] = not self._allowed(unit, actor)
            return refusals[entry.id]

        chats: dict[tuple[str, str], Any] = {}
        withheld: dict[str, int] | None = None
        if folded and self._countable_at_all():
            # Originals with no known source ACL whose file name matches: served to
            # nobody, counted.
            withheld = {}
            for entry in entries.values():
                if not _readable_receipt(entry.receipt) or not name_match(entry):
                    continue
                if (entry.unit is None or gated_unit(entry) is None
                        or refused_repository(entry)) \
                        and self._countable(entry, entries, lookup, chats):
                    withheld[WITHHELD_UNKNOWN_ACL] = withheld.get(WITHHELD_UNKNOWN_ACL, 0) + 1

        admitted: list[tuple[int, _Entry, str | None]] = []
        checks, cut, top = 0, False, 0
        for entry in sorted((e for e in candidates if not duplicate(e)), key=order):
            # An original whose bytes are the SharePoint copy's (held there without
            # text) is listed to a reader its item gate admits, and is otherwise
            # invisible: never counted withheld and never charged to the gate bound,
            # so a refused reader's answer is the same whether or not the capture
            # holds the copy's text.
            copy = same_bytes(entry)
            if refused_repository(entry):
                continue   # as an original of no known source ACL (counted above)
            if checks >= max_checks:
                cut = True
                break
            unit = gated_unit(entry)
            if unit is None:
                checks += 1
                continue   # no known source ACL: never gated (counted above)
            if not self._allowed(unit, actor):
                checks += not copy
                if not copy and withheld is not None and name_match(entry) \
                        and self._countable(entry, entries, lookup, chats):
                    withheld[WITHHELD_GATE_DENIED] = withheld.get(WITHHELD_GATE_DENIED, 0) + 1
                continue
            checks += 1
            name = str(entry.receipt.get("name") or "").casefold()
            match = "name" if name_match(entry) else (
                "text" if folded and holds_words(folded, name,
                                                 self._stored_words(entry.receipt))
                else None)
            admitted.append(({"name": 0, "text": 1, None: 2}[match], entry, match))
            top += match == "name" or not folded
            if top >= limit:
                cut = True   # no later original can rank above these
                break
        admitted.sort(key=lambda item: item[0])   # stable: gate order within a rank
        if len(admitted) > limit:
            cut = True
        found = [{"original_id": entry.id, "citation": SCHEME + entry.id,
                  "filename": str(entry.receipt.get("name") or ""),
                  "sha256": entry.receipt["sha256"],
                  "media_type": entry.receipt.get("detected_media_type"),
                  "bytes": entry.receipt.get("bytes"), "match": match}
                 for _rank, entry, match in admitted[:limit]]
        return {"found": found, "cut": cut,
                "withheld": None if withheld is None else dict(sorted(withheld.items()))}

    def _by_version(self) -> dict[tuple[str, str, str], list[_Entry]]:
        """(drive, item, sha256) -> the root originals of a SharePoint item with those
        bytes (by entry id), rebuilt when the entries change."""
        entries = self.entries()
        with self._lock:
            if self._versions is not None and self._versions[0] is entries:
                return self._versions[1]
        index: dict[tuple[str, str, str], list[_Entry]] = {}
        for entry in sorted(entries.values(), key=lambda e: e.id):
            unit = entry.unit
            if entry.root == entry.id and unit is not None and unit["kind"] == "sharepoint":
                index.setdefault((unit["drive_id"], unit["item_id"],
                                  entry.receipt["sha256"]), []).append(entry)
        with self._lock:
            self._versions = (entries, index)
        return index

    def text_original_of(self, drive_id: Any, item_id: Any, content_sha256: Any,
                         actor: Any) -> str | None:
        """The ``original://`` citation of the captured original holding extracted text
        whose bytes are the SharePoint item's current captured version
        (``content_sha256``) when the capture serves that version without usable text
        (``serves_text``) and ``actor`` may open it through the item's gate; else None
        (also on any failure: nothing is named that the gate did not open)."""
        try:
            lookup = self._source_lookup()
            pair = (drive_id, item_id)
            if lookup is None or not isinstance(content_sha256, str):
                return None
            current = lookup.sharepoint([pair]).get(pair)
            if current is None or current["content_sha256"] != content_sha256 \
                    or serves_text(current):
                return None
            for entry in self._by_version().get((drive_id, item_id, content_sha256), ()):
                if not self._stored_words(entry.receipt):
                    continue
                if self._allowed(self._unit_for_gate(entry, lookup, {pair: current}), actor):
                    return SCHEME + entry.id
        except Exception:  # noqa: BLE001 - a failing lookup or gate names nothing
            return None
        return None

    def _countable_at_all(self) -> bool:
        """The people exclusion decides at all (a file in no OneDrive is withheld from
        nobody): else no withheld original is counted."""
        if self._exclusion_clear is None:
            return False
        try:
            return self._exclusion_clear(None, None) is True
        except Exception:  # noqa: BLE001 - an undecided exclusion counts nothing
            return False

    # ---- coverage (no reader) ----------------------------------------------

    def coverage(self) -> dict[str, Any]:
        """Counts only: captured originals with a source gate (by unit kind) and those
        served to nobody (by reason); ``entries``: entry id -> state."""
        entries = self.entries()
        lookup = self._source_lookup()
        held = {} if lookup is None else lookup.sharepoint(
            (e.unit["drive_id"], e.unit["item_id"]) for e in entries.values()
            if e.unit is not None and e.unit["kind"] == "sharepoint")
        states: dict[str, str] = {}
        for entry in entries.values():
            if entry.unit is None:
                states[entry.id] = f"unknown_acl:{entry.reason}"
            elif self._unit_for_gate(entry, lookup, held) is None:
                states[entry.id] = f"unknown_acl:{SOURCE_NOT_CAPTURED}:{entry.unit['kind']}"
            else:
                states[entry.id] = f"servable:{entry.unit['kind']}"
        counts: dict[str, int] = {}
        for state in states.values():
            counts[state] = counts.get(state, 0) + 1
        return {"entries": len(entries),
                "servable": sum(s.startswith("servable") for s in states.values()),
                "unknown_acl": sum(s.startswith("unknown_acl") for s in states.values()),
                "by_state": dict(sorted(counts.items())), "states": states}


__all__ = ["CapturedOriginals", "FAILED", "HANDLE", "REFUSED", "SourceLookup",
           "WITHHELD_GATE_DENIED", "WITHHELD_UNKNOWN_ACL", "holds_words", "kind_of",
           "refusal", "repository_resource", "serves_text", "source_unit", "vocabulary"]
