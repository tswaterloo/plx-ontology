"""Chunks of business records' free-text fields (``record_text.json``).

Descriptions, notes, comments and reasons only: a figure in such a field is never truth
(the record's typed values answer; the gate checks). One chunk per record and field, cited
to the value's own L1 handle and field path. Keyed as ``plx.find`` reads a value: the
object's audience unit and the union of the value's markings and the object's current
and latest version markings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import MIN_BODY, Chunk, make_chunk
from . import headers as hd
from .normalise import normalise
from .permissions import family, parse_markings, valid_key
from .projection import BATCH, ProjectionChunker
from .split import pack

CONFIG = Path(__file__).with_name("record_text.json")


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "plx-record-text/v1" or not isinstance(data.get("fields"), dict):
        raise ValueError("record_text_config_invalid")
    return data


def record_kind(audience_key: str) -> str:
    if audience_key.startswith("api:public."):
        return "portal_text"
    if family(audience_key) == "bc":
        return "bc_text"
    if family(audience_key) == "mc":
        return "mc_task"
    return "record_text"


def wordy(text: str, min_chars: int, share: float) -> bool:
    stripped = "".join(text.split())
    if len(stripped) < min_chars:
        return False
    return sum(ch.isalpha() for ch in stripped) / len(stripped) >= share


class RecordChunker(ProjectionChunker):
    """Free-text record fields (module docstring)."""

    def __init__(self, *args: Any, config: Mapping[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.config = dict(config or load_config())

    def record_chunks(self, kinds: Sequence[str] | None = None, limit: int | None = None
                      ) -> Iterator[Chunk]:
        produced = 0
        fields: Mapping[str, Sequence[str]] = self.config["fields"]
        min_chars = int(self.config.get("min_chars", 20))
        share = float(self.config.get("min_letter_share", 0.4))
        for object_type, props in sorted(fields.items()):
            after = ""
            while True:
                rows = self.conn.execute(
                    "SELECT object_id, audience_key, missing_from_source_since AS missing "
                    "FROM objects WHERE object_type = ? AND object_id > ? ORDER BY object_id "
                    "LIMIT ?", (object_type, after, BATCH)).fetchall()
                if not rows:
                    break
                after = rows[-1]["object_id"]
                for chunk in self._records(rows, list(props), min_chars, share):
                    if kinds is not None and chunk.kind not in kinds:
                        continue
                    self.counts.chunks[chunk.kind] += 1
                    yield chunk
                    produced += 1
                    if limit is not None and produced >= limit:
                        return

    def _records(self, rows: Sequence[Any], props: list[str], min_chars: int,
                 share: float) -> Iterator[Chunk]:
        live = [r for r in rows if r["missing"] is None]
        self.counts.skip("missing_from_source", len(rows) - len(live))
        ids = [r["object_id"] for r in live]
        latest = self._latest_markings(ids)
        current = {row["object_id"]: row["markings"] for row in self.conn.execute(
            "SELECT object_id, markings FROM object_versions WHERE superseded_at IS NULL "
            "AND object_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
        texts: dict[str, list[Any]] = {}
        for row in self.conn.execute(
                "SELECT object_id, property, value, markings, l1_handle, field_path, value_type "
                "FROM object_values WHERE object_id IN (SELECT value FROM json_each(?)) "
                "AND superseded_at IS NULL AND property IN (SELECT value FROM json_each(?)) "
                "ORDER BY object_id, property", (json.dumps(ids), json.dumps(props))):
            if isinstance(row["value"], str) and wordy(row["value"], min_chars, share):
                texts.setdefault(row["object_id"], []).append(row)
        if not texts:
            return
        idents = self._identifiers(list(texts))
        for record in live:
            oid = record["object_id"]
            if oid not in texts:
                continue
            audience = record["audience_key"]
            if not valid_key(audience):
                self.counts.skip("audience_key_unknown")
                continue
            if oid not in current:
                self.counts.skip("no_current_version")
                continue
            kind = record_kind(audience)
            self.counts.documents[kind] += 1
            for row in texts[oid]:
                marks = parse_markings(row["markings"], current[oid], latest.get(oid, "[]"))
                if marks is None:
                    self.counts.skip("markings_unknown")
                    continue
                own = [(k, v) for k, v, m in idents.get(oid, ())
                       if (held := parse_markings(m)) is not None and set(held) <= set(marks)]
                labels = [hd.id_label(k, v) for k, v in own][:4]
                keys = [f"{k}:{v}" for k, v in own]
                type_name = oid.split(":", 1)[0]
                title = f"{type_name} {own[0][1]}" if own else type_name
                about = f"the {row['property'].replace('_', ' ')} of {title}"
                for index, piece in enumerate(pack(normalise(row["value"]))):
                    if len(piece) < MIN_BODY:
                        self.counts.skip(f"too_short:{kind}")
                        continue
                    yield make_chunk(
                        kind=kind, parts=(audience, row["l1_handle"], row["field_path"],
                                          str(index)),
                        source_handle=row["l1_handle"], object_id=oid, title=title,
                        author_role=None, occurred_at=None,
                        header=hd.build(kind=kind, title=title, date=None, author_role=None,
                                        ids=labels, about=about,
                                        where=f"field {row['field_path']}"),
                        body=piece, linked_ids=keys, audience_key=audience, markings=marks)


__all__ = ["CONFIG", "RecordChunker", "load_config", "record_kind", "wordy"]
