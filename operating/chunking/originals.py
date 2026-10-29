"""Chunks of the case originals store (``<state>/case-originals``), keyed by source unit.

An original is served through the gate of the source item it was read from
(``operating.captured_originals``): a SharePoint item, a Teams chat message or a
repository. Its chunks carry that unit's audience key (``sharepoint:<drive>``,
``teams:chat:<chat>``, ``repo:<owner>/<name>``) and its ``original://<id>#page=<n>``
citation, which the original's own gate re-checks per reader. An original with no known
source ACL (no source item, an item or chat the capture does not hold, a channel
message, unread bytes) is served to nobody, so it is not chunked (counted). An original
whose bytes the SharePoint capture already serves with text is the same document as that
SharePoint file's chunks (``captured_originals`` lists it under ``sharepoint``): not
chunked again. The people exclusion holds first (unit and OneDrive owner).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, Sequence

from ontology import identifiers as ids
from operating.captured_originals import (CapturedOriginals, SourceLookup, STORE_DIR,
                                          repository_resource, serves_text)
from operating.case_originals import _text_extraction

from . import MIN_BODY, Chunk, make_chunk
from . import headers as hd
from .normalise import normalise
from .permissions import Exclusion
from .projection import Counts
from .split import is_workbook_text, pack, sections, sheet_blocks

_MAP_TITLE = re.compile(r"process[ _-]?map|\bmap\b|workflow|\bSOP\b|procedure", re.I)


def unit_key(unit: dict[str, Any]) -> str | None:
    kind = unit.get("kind")
    if kind == "sharepoint" and unit.get("drive_id"):
        return f"sharepoint:{unit['drive_id']}"
    if kind == "teams_chat" and unit.get("chat_id"):
        return f"teams:chat:{unit['chat_id']}"
    if kind == "repository":
        return repository_resource(unit)
    return None


class OriginalsChunker:
    def __init__(self, state_dir: str | Path, exclusion: Exclusion,
                 counts: Counts | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.exclusion = exclusion
        self.counts = counts or Counts()
        self.store = CapturedOriginals(self.state_dir, gate=lambda *_: False)
        self.lookup = SourceLookup(self.state_dir,
                                   sharepoint_dir=self.state_dir / "sharepoint-capture")

    def chunks(self, kinds: Sequence[str] | None = None, limit: int | None = None
               ) -> Iterator[Chunk]:
        if not (self.state_dir / STORE_DIR).is_dir():
            return
        entries = self.store.entries()
        pairs = [(e.unit["drive_id"], e.unit["item_id"]) for e in entries.values()
                 if e.unit is not None and e.unit["kind"] == "sharepoint"]
        held = self.lookup.sharepoint(pairs) if pairs else {}
        produced = 0
        for entry_id in sorted(entries):
            entry = entries[entry_id]
            unit = entry.unit
            if unit is None:
                self.counts.skip(f"original_no_source_acl:{entry.reason}")
                continue
            key = unit_key(unit)
            if key is None:
                self.counts.skip("original_no_source_acl:unit")
                continue
            if unit["kind"] == "sharepoint":
                current = held.get((unit["drive_id"], unit["item_id"]))
                if current is None:
                    self.counts.skip("original_no_source_acl:source_not_captured")
                    continue
                root = entries[entry.root].receipt
                if current.get("content_sha256") == root.get("sha256") and serves_text(current):
                    self.counts.skip("original_served_as_sharepoint")
                    continue
            elif unit["kind"] == "teams_chat":
                if self.lookup.teams_chat(unit["chat_id"], unit["message_id"]) is None:
                    self.counts.skip("original_no_source_acl:source_not_captured")
                    continue
            if not self.exclusion.unit_clear(key) or not self.exclusion.onedrive_clear(
                    unit.get("onedrive_owner")):
                self.counts.skip("people_exclusion:original")
                self.counts.withheld.add(key)
                continue
            text = self._text(entry.receipt)
            if not text:
                self.counts.skip("no_text:original")
                continue
            self.counts.documents["original"] += 1
            for chunk in self._document(entry_id, entry.receipt, key, text):
                if kinds is not None and chunk.kind not in kinds:
                    continue
                self.counts.chunks[chunk.kind] += 1
                yield chunk
                produced += 1
                if limit is not None and produced >= limit:
                    return

    def _text(self, receipt: dict[str, Any]) -> str:
        facts = receipt.get("extraction") or {}
        ref = facts.get("text_ref") if isinstance(facts, dict) else None
        if ref != f"{STORE_DIR}/extractions/{receipt.get('sha256')}.txt":
            return ""
        try:
            return (self.state_dir / ref).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def _document(self, entry_id: str, receipt: dict[str, Any], key: str, text: str
                  ) -> Iterator[Chunk]:
        name = str(receipt.get("name") or "") or None
        date = receipt.get("last_modified") or None
        where = receipt.get("watch_folder") or None
        pages = _text_extraction(text.encode("utf-8"))["pages"]
        found_ids = [m for m in ids.find_identifiers(text[:200_000])
                     if m.normalized and m.kind not in hd.TECHNICAL]
        keys = sorted({f"{m.kind}:{m.normalized}" for m in found_ids})
        labels = list(dict.fromkeys(hd.id_label(m.kind, m.normalized) for m in found_ids))
        for page in pages:
            piece_text = text[page["start"]:page["end"]]
            citation = f"original://{entry_id}#page={page['page']}"
            if is_workbook_text(piece_text):
                units = [(s.label, s.text, "sheet_row") for s in sheet_blocks(piece_text)]
            else:
                found = sections(piece_text)
                kind = "map_stage" if name and _MAP_TITLE.search(name) and len(found) > 1 \
                    else "original"
                units = [(s.label, p, kind) for s in found for p in pack(s.text)]
            for index, (label, body, kind) in enumerate(units):
                body = normalise(body)
                if len(body) < MIN_BODY:
                    self.counts.skip(f"too_short:{kind}")
                    continue
                spot = " · ".join(p for p in (f"page {page['page']}", label) if p)
                about = f"{spot} of '{hd.one_line(name or 'case document')}'"
                header = hd.build(kind=kind, title=name, date=date, author_role=None,
                                  ids=labels, about=about, where=where)
                yield make_chunk(kind=kind, parts=(key, citation, str(index)),
                                 source_handle=citation, object_id=None, title=name,
                                 author_role=None, occurred_at=date, header=header, body=body,
                                 linked_ids=keys, audience_key=key, markings=())


__all__ = ["OriginalsChunker", "unit_key"]
