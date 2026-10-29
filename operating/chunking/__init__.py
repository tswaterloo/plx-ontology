"""Structure-aware chunks of every searchable text, keyed exactly as its source's gate.

The served
projection (``text_fields`` / ``text_chunks``: mail, attachments, SharePoint files,
container members, Teams messages, Mission Control tasks; plus a configured list of
free-text record fields) and the case originals store become ``search.chunks`` rows.
``python3 -m operating.embed run`` drives it; this package only reads (``mode=ro``).

What a chunk is (``Chunk``):

* one unit of meaning by structure, never a fixed window: one mail message (thread
  subject, sender class, quoted history removed: ``ontology.mentions.quoted_ranges``);
  one Teams message plus a window of its replies; one document section, slide or page;
  spreadsheet rows with their header row repeated; one record's free-text field.
* a contextual header (``headers``: source system, title/thread, date, author role,
  linked ids, a deterministic one-line "about") prepended before embedding.
* normalised text (``normalise``): unit and currency spellings, unambiguous dates
  written ISO beside the original; a value is never rewritten, only spelled once more.

Permissions (non-negotiable; ``permissions``): every chunk carries the audience unit the
projection keys its object with (``objects.audience_key``, the unit ``ReadPolicy`` grants
and ``plx.find`` filters in SQL) and the union of every marking a reader of that text must
hold (the text field's, the current version's and the latest version's: the three
``_VISIBLE_OBJECT`` / ``chunk_matches`` check). ``source_handle`` is the citation the item
gate re-checks per reader. A chunk whose unit or markings cannot be established is not
written (counted). The people exclusion holds before anything is chunked: a guard that
cannot decide refuses every mail, Teams, SharePoint and original chunk.

Numbers are never truth here: a vector finds a passage; the record or the quoted passage
answers, and the answer gate checks it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Sequence

# Authority of a chunk's source (contract): 4 system of record, 3 approved document,
# 2 email (and task systems), 1 chat.
AUTHORITY = {"portal_text": 4, "bc_text": 4, "record_text": 4, "sharepoint": 3, "original": 3,
             "sheet_row": 3, "map_stage": 3, "mail": 2, "attachment": 2, "mc_task": 2,
             "teams": 1}
KINDS = tuple(sorted(AUTHORITY))
# Body characters one chunk holds at most (about 400 tokens: retrieval quality and CPU
# embedding cost both prefer short, whole units; a longer unit is split at structure).
MAX_BODY = 1600
MIN_BODY = 24


@dataclass(frozen=True)
class Chunk:
    """One ``search.chunks`` row (embedding columns aside)."""
    chunk_id: str
    source_handle: str
    object_id: str | None
    kind: str
    title: str | None
    author_role: str | None
    occurred_at: str | None
    header: str
    body: str
    linked_ids: tuple[str, ...]
    audience_key: str
    markings: tuple[str, ...]
    authority: int
    dup_key: str = field(default="")   # sha256 of the normalised body (exact duplicates)

    @property
    def content_sha(self) -> str:
        return content_sha(self.header, self.body)


def content_sha(header: str, body: str) -> str:
    return hashlib.sha256((header + "\n\n" + body).encode("utf-8")).hexdigest()


def body_sha(body: str) -> str:
    return hashlib.sha256(" ".join(body.split()).casefold().encode("utf-8")).hexdigest()


def chunk_id(kind: str, *parts: str) -> str:
    """Deterministic id of one chunk: its kind and what pins it (citation, field, part)."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{kind}:{digest}"


_CONTROL = dict.fromkeys(c for c in range(32) if c not in (9, 10)) | {0x7f: None}


def clean(text: str | None) -> str | None:
    """Text as Postgres stores it: no NUL or other control characters (tab and newline
    kept); extracted text sometimes carries them."""
    return None if text is None else text.translate(_CONTROL)


def make_chunk(*, kind: str, parts: Sequence[str], source_handle: str, object_id: str | None,
               title: str | None, author_role: str | None, occurred_at: str | None,
               header: str, body: str, linked_ids: Sequence[str], audience_key: str,
               markings: Sequence[str]) -> Chunk:
    if kind not in AUTHORITY:
        raise ValueError(f"chunk_kind_unknown:{kind}")
    header, body, title = clean(header) or "", clean(body) or "", clean(title)
    return Chunk(chunk_id=chunk_id(kind, *parts), source_handle=source_handle,
                 object_id=object_id, kind=kind, title=title, author_role=clean(author_role),
                 occurred_at=occurred_at, header=header, body=body,
                 linked_ids=tuple(sorted(set(linked_ids))), audience_key=audience_key,
                 markings=tuple(sorted(set(markings))), authority=AUTHORITY[kind],
                 dup_key=body_sha(body))


__all__ = ["AUTHORITY", "KINDS", "MAX_BODY", "MIN_BODY", "Chunk", "body_sha", "chunk_id", "clean",
           "content_sha", "make_chunk"]
