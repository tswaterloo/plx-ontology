"""A case original's receipts read as entries (``operating.captured_originals``): each
entry's newest readable receipt, the source unit its outermost container names (a
SharePoint item, a Teams chat message, a repository file) or why it names none. Standard
library only and never a source read, so the SharePoint capture can ask which items case
originals name (``sharepoint_items``) without importing the gated reader and the tool
layer behind it."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

STORE_DIR = "case-originals"
ENTRY_ID = re.compile(r"^(?:co|nr|rv)-[0-9a-f]{12}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PERSONAL = re.compile(r"^/personal/([^/]+)/", re.IGNORECASE)
_TEAMS_MESSAGE = re.compile(r"^/l/message/([^/]+)/([0-9]{1,20})$")
RECEIPT_SCHEMA = "plx-case-original-receipt/v1"
MAX_PARENT_DEPTH = 8

# A repository-sourced original's ``repository_ref`` (a GitHub blob at a commit, the shape
# ``scripts.case_originals`` reads from the local object store): owner and name.
_REPOSITORY_REF = re.compile(r"^https://github\.com/([A-Za-z0-9_.\-]{1,100})/"
                             r"([A-Za-z0-9_.\-]{1,100})/blob/[0-9a-f]{40}/[^\x00-\x20?#]+$")

# Why an original has no source ACL (``coverage``): served to nobody.
NOT_READ = "bytes_not_read"
NO_SOURCE_ITEM = "source_item_unidentified"
REPOSITORY = "repository_file"
CHANNEL = "teams_channel_unsupported"
PARENT_UNKNOWN = "container_unknown"
SOURCE_NOT_CAPTURED = "source_not_captured"


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


def receipt_files(root: Path) -> list[Path]:
    """Every receipt under ``<root>/receipts``, sorted."""
    receipts = root / "receipts"
    if not receipts.is_dir():
        return []
    return sorted(p for folder in receipts.iterdir() if folder.is_dir()
                  for p in folder.glob("*.json"))


def read_entries(root: Path, unit_of: Callable[[Mapping[str, Any]], tuple[Any, Any]]
                 = source_unit) -> dict[str, _Entry]:
    """Every entry with a receipt under ``root`` (``<state>/case-originals``): its newest
    readable receipt (a readable one wins over an unreadable one, then the newest read)
    and the source unit ``unit_of`` reads from its outermost container's, or the reason
    it has none."""
    latest: dict[str, Mapping[str, Any]] = {}
    for path in receipt_files(root):
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
        _place(entry, entries, unit_of)
    return entries


def _place(entry: _Entry, entries: Mapping[str, _Entry],
           unit_of: Callable[[Mapping[str, Any]], tuple[Any, Any]]) -> None:
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
    entry.unit, entry.reason = unit_of(node)


def sharepoint_items(state_dir: str | Path) -> list[tuple[str, str]]:
    """The (canonical drive id, item id) of every SharePoint item a served-shape case
    original under ``<state_dir>/case-originals`` names as its source unit (a member's:
    its outermost container's), sorted, each once: the items the SharePoint capture
    reads first so their gate can decide (``capture_sharepoint`` Named items first).
    Read-only; serves nothing (no original is opened)."""
    entries = read_entries(Path(state_dir) / STORE_DIR)
    return sorted({(e.unit["drive_id"], e.unit["item_id"]) for e in entries.values()
                   if e.unit is not None and e.unit["kind"] == "sharepoint"})


__all__ = ["CHANNEL", "ENTRY_ID", "MAX_PARENT_DEPTH", "NOT_READ", "NO_SOURCE_ITEM",
           "PARENT_UNKNOWN", "RECEIPT_SCHEMA", "REPOSITORY", "SOURCE_NOT_CAPTURED", "STORE_DIR",
           "read_entries", "receipt_files", "sharepoint_items", "source_unit"]
