"""Chunks of the served projection's captured text (``text_fields`` / ``text_chunks``).

Read-only (``mode=ro``). One *document* is one captured version (``version_handle``):
a mail message, an attachment, a SharePoint file, a container member, a Teams message,
a Mission Control task. The objects projected from one version (an ``Attachment`` and the
``CustomerPoDocument`` read from it) share its text: it is chunked once per audience unit,
under the most basic of them (``_PRIMARY``), with every object's markings (the union: a
reader must hold all of them).

Per kind (``operating.chunking`` docstring): a mail message is its own text, quoted
history removed; an attachment, file or member is chunked by page, section, slide or
sheet rows; a Teams message carries a window of its replies; an MC task is its title,
stage, bucket and description.

Keying (``permissions``): ``objects.audience_key`` and the markings union of the text field
and the object's current and latest versions; only the current version of an object
present at its source is chunked (``missing_from_source_since`` set: not chunked, counted).
"""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import identifiers as ids
from ontology.mentions import cited_page

from . import MIN_BODY, Chunk, make_chunk
from . import headers as hd
from .normalise import normalise
from .permissions import Exclusion, family, parse_markings, valid_key
from .split import is_workbook_text, mail_own_text, pack, sections, sheet_blocks

# document_kind -> chunk kind (workbooks become sheet_row, maps map_stage, by content).
DOCUMENT_KINDS = {"email": "mail", "attachment": "attachment", "sharepoint_file": "sharepoint",
                  "container_member": "attachment", "teams_message": "teams",
                  "mc_task": "mc_task"}
# The object a shared version is chunked under: the captured item itself first.
_PRIMARY = ("Email", "Attachment", "SharePointFile", "ContainerMember", "TeamsChatMessage",
            "TeamsChannelMessage", "MCTask")
# Link types that lead from a captured item to the item it sits in (header context).
_PARENT = {"Attachment.attached_to", "ContainerMember.in_attachment", "ContainerMember.in_file",
           "TeamsChatMessage.in_chat", "TeamsChannelMessage.in_channel"}
# Properties read for headers, per object type (only at markings the chunk carries).
_META = ("id", "subject", "sender", "sent_at", "received_at", "conversation_id", "name", "path",
         "last_modified", "from", "created_at", "reply_to_id", "chat_id", "channel_id",
         "team_id", "title", "stage", "bucket", "task_id", "topic", "description",
         "display_name", "po_number", "order_number", "vendor_name", "lot_number", "code",
         "erp_code")
_NAME_PROPS = ("name", "vendor_name", "display_name", "title", "topic")
_MAP_TITLE = re.compile(r"process[ _-]?map|\bmap\b|workflow|\bSOP\b|procedure", re.I)
TEAMS_WINDOW = 4            # replies a Teams message carries with it
BATCH = 400                 # documents whose metadata one query batch reads


@dataclass
class Counts:
    """What a pass did; totals only (no ids, no text)."""
    documents: Counter = field(default_factory=Counter)
    chunks: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    # Audience units the people exclusion withholds now (their stored rows are deleted).
    withheld: set = field(default_factory=set)

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] += n

    def as_dict(self) -> dict[str, Any]:
        return {"documents": dict(self.documents), "chunks": dict(self.chunks),
                "skipped": dict(self.skipped), "withheld_units": len(self.withheld)}


@dataclass
class _Field:
    field_id: int
    object_id: str
    citation: str
    field_path: str
    document_kind: str
    sender_class: str
    markings: str
    object_type: str
    audience_key: str
    version_markings: str


@dataclass
class _Doc:
    handle: str            # version handle (a page citation's base)
    audience_key: str
    fields: list[_Field]
    objects: list[str]     # object ids projected from this version, primary first
    markings: tuple[str, ...] = ()


@dataclass
class _Ids:
    """What a document's chunks may state: ids (``kind:normalized``), their labels, names
    of same-unit linked objects, and ids of linked objects in another unit (``cross``),
    stated by a chunk only where its own text names them."""
    keys: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    cross: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, value: str) -> None:
        key = f"{kind}:{value}"
        if key not in self.keys:
            self.keys.append(key)
            self.labels.append(hd.id_label(kind, value))

    def for_body(self, body: str) -> tuple[list[str], list[str], list[str]]:
        keys, labels = list(self.keys), list(self.labels)
        for kind, value in self.cross:
            key = f"{kind}:{value}"
            if key not in keys and hd.label_allowed(value, body, False):
                keys.append(key)
                labels.append(hd.id_label(kind, value))
        return keys, labels, list(self.names)


def open_ro(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def field_text(conn: sqlite3.Connection, field_id: int) -> str:
    """A text field reassembled from its overlapping stored chunks (start offsets)."""
    text = ""
    for row in conn.execute("SELECT start, body FROM text_chunks WHERE field_id = ? "
                            "ORDER BY start", (field_id,)):
        text = text[: row["start"]] + zlib.decompress(row["body"]).decode("utf-8")
    return text


def _json_ids(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)))


class ProjectionChunker:
    """Every chunk of the projection's captured text (module docstring)."""

    def __init__(self, conn: sqlite3.Connection, exclusion: Exclusion,
                 counts: Counts | None = None) -> None:
        self.conn = conn
        self.exclusion = exclusion
        self.counts = counts or Counts()

    # ------------------------------------------------------------ documents

    def documents(self, kinds: Sequence[str] | None = None) -> list[_Doc]:
        """The current captured versions holding text, grouped and keyed."""
        wanted = None
        if kinds is not None:
            asked = set(kinds) | ({"sharepoint", "attachment"}
                                  if {"sheet_row", "map_stage"} & set(kinds) else set())
            wanted = {d for d, k in DOCUMENT_KINDS.items() if k in asked}
        rows = self.conn.execute(
            "SELECT f.field_id, f.object_id, f.version_handle, f.citation, f.field_path, "
            "f.document_kind, f.sender_class, f.markings, o.object_type, o.audience_key, "
            "o.missing_from_source_since AS missing, v.markings AS version_markings, "
            "v.superseded_at FROM text_fields f JOIN objects o ON o.object_id = f.object_id "
            "LEFT JOIN object_versions v ON v.object_id = f.object_id "
            "AND v.l1_handle = f.version_handle")
        groups: dict[tuple[str, str], list[_Field]] = defaultdict(list)
        for row in rows:
            if wanted is not None and row["document_kind"] not in wanted:
                continue
            if row["document_kind"] not in DOCUMENT_KINDS:
                self.counts.skip("document_kind_unknown")
                continue
            if row["version_markings"] is None or row["superseded_at"] is not None:
                continue   # an older version: only the current one is searchable
            if row["missing"] is not None:
                self.counts.skip("missing_from_source")
                continue
            groups[(row["version_handle"], row["audience_key"])].append(_Field(
                row["field_id"], row["object_id"], row["citation"], row["field_path"],
                row["document_kind"], row["sender_class"], row["markings"],
                row["object_type"], row["audience_key"], row["version_markings"]))
        out: list[_Doc] = []
        for (handle, audience), fields in groups.items():
            objects = sorted({f.object_id: f.object_type for f in fields}.items(),
                             key=lambda item: (_PRIMARY.index(item[1]) if item[1] in _PRIMARY
                                               else len(_PRIMARY), item[0]))
            out.append(_Doc(handle, audience, fields, [oid for oid, _ in objects]))
        out.sort(key=lambda d: (d.fields[0].document_kind, d.handle))
        return out

    def _keyed(self, doc: _Doc, latest: Mapping[str, str]) -> bool:
        """Key the document (audience valid, markings known, exclusion clear) or count why
        it is not chunked."""
        if not valid_key(doc.audience_key):
            self.counts.skip("audience_key_unknown")
            return False
        marks = parse_markings(*(f.markings for f in doc.fields),
                               *(f.version_markings for f in doc.fields),
                               *(latest.get(oid, "[]") for oid in doc.objects))
        if marks is None:
            self.counts.skip("markings_unknown")
            return False
        if not self.exclusion.unit_clear(doc.audience_key):
            self.counts.skip(f"people_exclusion:{family(doc.audience_key)}")
            self.counts.withheld.add(doc.audience_key)
            return False
        doc.markings = marks
        return True

    # ------------------------------------------------------------ metadata

    def _latest_markings(self, object_ids: Sequence[str]) -> dict[str, str]:
        """The markings of each object's latest version, as the gate reads it
        (``ORDER BY julianday(recorded_at) DESC LIMIT 1``). Versions tied at that time:
        the union of their markings, so whichever one the gate's LIMIT picks, the chunk
        asks at least as much."""
        rows: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for row in self.conn.execute(
                "SELECT object_id, julianday(recorded_at) AS t, markings FROM object_versions "
                "WHERE object_id IN (SELECT value FROM json_each(?))", (_json_ids(object_ids),)):
            rows[row["object_id"]].append((row["t"], row["markings"]))
        out: dict[str, str] = {}
        for oid, versions in rows.items():
            dated = [v for v in versions if v[0] is not None]
            if not dated:
                out[oid] = "unknown"      # no readable time: parse fails, nothing written
                continue
            top = max(t for t, _ in dated)
            union = parse_markings(*(m for t, m in dated if t == top))
            out[oid] = json.dumps(list(union)) if union is not None else "unknown"
        return out

    def _current_markings(self, object_ids: Sequence[str]) -> dict[str, str]:
        return {row["object_id"]: row["markings"] for row in self.conn.execute(
            "SELECT object_id, markings FROM object_versions WHERE superseded_at IS NULL AND "
            "object_id IN (SELECT value FROM json_each(?))", (_json_ids(object_ids),))}

    def _values(self, object_ids: Sequence[str]) -> dict[str, dict[str, tuple[Any, str]]]:
        out: dict[str, dict[str, tuple[Any, str]]] = defaultdict(dict)
        if not object_ids:
            return out
        for row in self.conn.execute(
                "SELECT object_id, property, value, markings FROM object_values WHERE "
                "object_id IN (SELECT value FROM json_each(?)) AND superseded_at IS NULL "
                "AND property IN (SELECT value FROM json_each(?))",
                (_json_ids(object_ids), json.dumps(_META))):
            out[row["object_id"]].setdefault(row["property"], (row["value"], row["markings"]))
        return out

    def _links(self, object_ids: Sequence[str]) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        if not object_ids:
            return out
        for row in self.conn.execute(
                "SELECT l.from_object, l.to_object, l.link_type, l.binding_kind, "
                "o.object_type, o.audience_key FROM link_rows l JOIN objects o "
                "ON o.object_id = l.to_object WHERE l.from_object IN "
                "(SELECT value FROM json_each(?)) AND l.tier != 'contradicted'",
                (_json_ids(object_ids),)):
            out[row["from_object"]].append(row)
        return out

    def _identifiers(self, object_ids: Sequence[str]) -> dict[str, list[tuple[str, str, str]]]:
        """object -> [(kind, normalized, markings)] of its business identifiers."""
        out: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        if not object_ids:
            return out
        for row in self.conn.execute(
                "SELECT DISTINCT object_id, identifier_kind, normalized, markings, role "
                "FROM identifiers WHERE object_id IN (SELECT value FROM json_each(?)) "
                "AND superseded_at IS NULL ORDER BY role DESC",
                (_json_ids(object_ids),)):
            if row["identifier_kind"] not in hd.TECHNICAL:
                out[row["object_id"]].append((row["identifier_kind"], row["normalized"],
                                              row["markings"]))
        return out

    # ------------------------------------------------------------ chunks

    def chunks(self, kinds: Sequence[str] | None = None, limit: int | None = None
               ) -> Iterator[Chunk]:
        docs = self.documents(kinds)
        teams = [d for d in docs if d.fields[0].document_kind == "teams_message"]
        other = [d for d in docs if d.fields[0].document_kind != "teams_message"]
        produced = 0
        for start in range(0, len(other), BATCH):
            for chunk in self._batch(other[start:start + BATCH], kinds):
                yield chunk
                produced += 1
                if limit is not None and produced >= limit:
                    return
        if teams and (kinds is None or "teams" in kinds):
            for chunk in self._teams(teams):
                yield chunk
                produced += 1
                if limit is not None and produced >= limit:
                    return

    def _context(self, docs: Sequence[_Doc]) -> dict[str, Any]:
        objects = sorted({oid for d in docs for oid in d.objects})
        latest = self._latest_markings(objects)
        links = self._links(objects)
        parents = sorted({row["to_object"] for oid in objects for row in links.get(oid, ())
                          if row["link_type"] in _PARENT})
        grand = self._links(parents)
        parents += sorted({row["to_object"] for oid in parents for row in grand.get(oid, ())
                           if row["link_type"] in _PARENT} - set(parents))
        targets = sorted({row["to_object"] for oid in objects + parents
                          for row in (links.get(oid) or grand.get(oid) or ())})
        current = self._current_markings(targets)
        target_latest = self._latest_markings(targets)
        return {"latest": latest, "links": {**grand, **links},
                "values": self._values(objects + parents + targets),
                "idents": self._identifiers(objects + targets),
                "target_marks": {t: (current[t], target_latest.get(t, "unknown"))
                                 for t in targets if t in current}}

    def _batch(self, docs: Sequence[_Doc], kinds: Sequence[str] | None) -> Iterator[Chunk]:
        ctx = self._context(docs)
        for doc in docs:
            if not self._keyed(doc, ctx["latest"]):
                continue
            kind = DOCUMENT_KINDS[doc.fields[0].document_kind]
            made = 0
            for chunk in self._document(doc, kind, ctx):
                made += 1
                if kinds is not None and chunk.kind not in kinds:
                    continue
                self.counts.chunks[chunk.kind] += 1
                yield chunk
            if made:
                self.counts.documents[kind] += 1
            else:
                self.counts.skip(f"no_text:{kind}")

    def _value(self, ctx: Mapping[str, Any], oid: str | None, prop: str,
               marks: Sequence[str]) -> Any:
        """A metadata value of ``oid`` the chunk's reader may read (its markings within
        the chunk's), else None."""
        if oid is None:
            return None
        found = ctx["values"].get(oid, {}).get(prop)
        if found is None:
            return None
        value, markings = found
        held = parse_markings(markings)
        return value if held is not None and set(held) <= set(marks) else None

    def _parent(self, ctx: Mapping[str, Any], oid: str) -> str | None:
        for row in ctx["links"].get(oid, ()):
            if row["link_type"] in _PARENT:
                return row["to_object"]
        return None

    def _ids_and_names(self, ctx: Mapping[str, Any], doc: _Doc, oids: Sequence[str],
                       text: str) -> "_Ids":
        """The ids and names a chunk of this document may state (review 2026-10-29):

        * its own objects' identifiers (same unit) at markings the chunk carries;
        * the ids its own text names (the identifier grammar);
        * a linked object in the SAME audience unit: its ids and its name, only when that
          object's current and latest version markings, and the id's or name's own
          markings, are all within the chunk's;
        * a linked object in ANOTHER unit: never its name or label, and its ids only where
          the chunk's own text names them (``_Ids.for_body``, per chunk), so a reader of
          this unit learns nothing it could not read in the text itself.
        """
        out = _Ids()
        chunk_marks = set(doc.markings)

        def within(*marks: Any) -> bool:
            held = parse_markings(*marks)
            return held is not None and set(held) <= chunk_marks

        for oid in oids:
            for kind, value, marks in ctx["idents"].get(oid, ()):
                if within(marks):
                    out.add(kind, value)
        for mention in ids.find_identifiers(text):
            if mention.normalized and mention.kind not in hd.TECHNICAL:
                out.add(mention.kind, mention.normalized)
        for oid in oids:
            for row in ctx["links"].get(oid, ()):
                if row["link_type"] in _PARENT:
                    continue
                target = row["to_object"]
                if row["audience_key"] != doc.audience_key:
                    for kind, value, _marks in ctx["idents"].get(target, ())[:2]:
                        out.cross.append((kind, value))
                    continue
                version = ctx["target_marks"].get(target)
                if version is None or not within(*version):
                    continue
                for kind, value, marks in ctx["idents"].get(target, ())[:2]:
                    if within(marks):
                        out.add(kind, value)
                for prop in _NAME_PROPS:
                    name = self._value(ctx, target, prop, doc.markings)
                    if isinstance(name, str) and name.strip():
                        out.names.append(name)
                        break
        return out

    def _document(self, doc: _Doc, kind: str, ctx: Mapping[str, Any]) -> Iterator[Chunk]:
        primary = doc.objects[0]
        marks = doc.markings
        fields = sorted(doc.fields, key=lambda f: (f.field_path != "row.name",
                                                   cited_page(f.citation)[1] or 0, f.citation))
        texts = {}
        for f in fields:
            if f.citation in texts and f.field_path == "row.name":
                continue
            texts[(f.citation, f.field_path)] = field_text(self.conn, f.field_id)
        full = "\n".join(texts.values())
        value = lambda prop, oid=primary: self._value(ctx, oid, prop, marks)  # noqa: E731
        parent = self._parent(ctx, primary)
        grand = self._parent(ctx, parent) if parent else None
        if kind == "mail":
            yield from self._mail(doc, ctx, texts, full, value)
        elif kind == "mc_task":
            yield from self._mc(doc, ctx, texts, full, value)
        else:
            yield from self._file(doc, kind, ctx, texts, full, value, parent, grand)

    def _emit(self, doc: _Doc, kind: str, part: str, citation: str, title: str | None,
              date: str | None, role: str | None, about: str | None, where: str | None,
              body: str, found: "_Ids") -> Chunk | None:
        body = normalise(body)
        if len(body) < MIN_BODY:
            self.counts.skip(f"too_short:{kind}")
            return None
        keys, labels, names = found.for_body(body)
        header = hd.build(kind=kind, title=title, date=date, author_role=role, ids=labels,
                          names=names, about=about, where=where)
        return make_chunk(kind=kind, parts=(doc.audience_key, citation, part),
                          source_handle=citation, object_id=doc.objects[0], title=title,
                          author_role=role, occurred_at=date, header=header, body=body,
                          linked_ids=keys, audience_key=doc.audience_key,
                          markings=doc.markings)

    def _mail(self, doc, ctx, texts, full, value) -> Iterator[Chunk]:
        sender = value("sender")
        role = hd.sender_role(sender if isinstance(sender, str) else None,
                              doc.fields[0].sender_class)
        # Fail closed: a sender the chunk may not state (absent, or marked beyond the
        # chunk) cannot be cleared by the exclusion, so the message is withheld.
        if not isinstance(sender, str) or not sender.strip():
            self.counts.skip("people_exclusion:sender_undecided")
            return
        if not self.exclusion.identity_clear(sender):
            self.counts.skip("people_exclusion:sender")
            return
        subject = value("subject")
        date = value("sent_at") or value("received_at")
        own, forwarded = mail_own_text(full)
        named = self._ids_and_names(ctx, doc, doc.objects, own)
        about = (f"forwarded message in thread '{hd.one_line(subject or '')}'" if forwarded
                 else f"message from {role} in thread '{hd.one_line(subject or '(no subject)')}'")
        citation = doc.fields[0].citation
        for index, piece in enumerate(pack(own)):
            chunk = self._emit(doc, "mail", str(index), citation, subject, date, role, about,
                               None, piece, named)
            if chunk:
                yield chunk

    def _mc(self, doc, ctx, texts, full, value) -> Iterator[Chunk]:
        title, stage, bucket, task = (value(p) for p in ("title", "stage", "bucket", "task_id"))
        named = self._ids_and_names(ctx, doc, doc.objects, full)
        about = "Mission Control task" + (f" {task}" if task else "") + (
            f" '{hd.one_line(title)}'" if title else "") + (f", stage {stage}" if stage else "")
        where = f"bucket {bucket}" if bucket else None
        citation = doc.fields[0].citation
        for index, piece in enumerate(pack(full)):
            chunk = self._emit(doc, "mc_task", str(index), citation, title, None, None, about,
                               where, piece, named)
            if chunk:
                yield chunk

    def message_sender_clear(self, ctx: Mapping[str, Any], doc: _Doc, mail: str | None
                             ) -> bool:
        """A mail-family file (attachment, container member) is chunked only when the
        message it came with has a sender the exclusion clears (none found: withheld)."""
        if family(doc.audience_key) != "mail":
            return True
        sender = self._value(ctx, mail, "sender", doc.markings) if mail else None
        return isinstance(sender, str) and bool(sender.strip()) \
            and self.exclusion.identity_clear(sender)

    def _file(self, doc, kind, ctx, texts, full, value, parent, grand) -> Iterator[Chunk]:
        mail_of = next((o for o in (parent, grand) if o and ctx["values"].get(o, {})
                        .get("sender")), None)
        if not self.message_sender_clear(ctx, doc, mail_of):
            self.counts.skip("people_exclusion:attachment_sender")
            return
        name = value("name") or value("path")
        path = value("path")
        date = value("last_modified")
        context_title = None
        if parent is not None:
            mail = parent if ctx["values"].get(parent, {}).get("subject") else grand
            if mail:
                context_title = self._value(ctx, mail, "subject", doc.markings)
                date = date or self._value(ctx, mail, "sent_at", doc.markings)
            outer = self._value(ctx, parent, "name", doc.markings)
            if outer and outer != name:
                path = f"inside {outer}"
        named = self._ids_and_names(ctx, doc, doc.objects, full)
        where = (f"attached to email '{hd.one_line(context_title)}'" if context_title
                 else (path if isinstance(path, str) else None))
        title = name if isinstance(name, str) else None
        for (citation, field_path), text in texts.items():
            if field_path == "row.name":
                continue
            page = cited_page(citation)[1]
            if is_workbook_text(text):
                units = [(s.label, s.text, "sheet_row") for s in sheet_blocks(text)]
            else:
                found = sections(text)
                use_map = bool(title and _MAP_TITLE.search(title)) and len(found) > 1
                unit_kind = "map_stage" if use_map else kind
                units = [(s.label, piece, unit_kind) for s in found for piece in pack(s.text)]
            for index, (label, piece, unit_kind) in enumerate(units):
                where_label = " · ".join(p for p in (f"page {page}" if page else None, label)
                                         if p)
                about = (f"{where_label} of '{hd.one_line(title or 'document')}'" if where_label
                         else f"text of '{hd.one_line(title or 'document')}'")
                chunk = self._emit(doc, unit_kind, f"{field_path}:{index}", citation, title,
                                   date, None, about, where, piece, named)
                if chunk:
                    yield chunk

    # ------------------------------------------------------------ Teams

    def _teams(self, docs: Sequence[_Doc]) -> Iterator[Chunk]:
        """Each Teams message with a window of its replies (channel: the replies to it, or
        the replies after it in its thread; chat: the next messages of the chat)."""
        ctx_all: dict[str, Any] = {"latest": {}, "links": {}, "values": {}, "idents": {},
                                   "target_marks": {}}
        for start in range(0, len(docs), BATCH):
            part = self._context(docs[start:start + BATCH])
            for key in ctx_all:
                ctx_all[key].update(part[key])
        keyed: list[_Doc] = []
        for doc in docs:
            if self._keyed(doc, ctx_all["latest"]):
                keyed.append(doc)
        convo: dict[tuple[str, str], list[tuple[str, _Doc, str, dict]]] = defaultdict(list)
        for doc in keyed:
            oid = doc.objects[0]
            sender = self._value(ctx_all, oid, "from", doc.markings)
            try:
                who = json.loads(sender) if isinstance(sender, str) else {}
            except ValueError:
                who = {}
            if not isinstance(who, Mapping):
                who = {}
            # Fail closed: a user message whose author id is absent (or marked beyond the
            # chunk) is undecidable; an app/bot message names no person.
            author = who.get("id")
            if str(who.get("kind") or "") not in ("application", "bot") and not author:
                self.counts.skip("people_exclusion:sender_undecided")
                continue
            if author and not self.exclusion.identity_clear(author):
                self.counts.skip("people_exclusion:sender")
                continue
            text = "\n".join(field_text(self.conn, f.field_id) for f in doc.fields)
            created = self._value(ctx_all, oid, "created_at", doc.markings) or ""
            convo[(doc.audience_key, self._value(ctx_all, oid, "chat_id", doc.markings)
                   or self._value(ctx_all, oid, "channel_id", doc.markings) or "")].append(
                (created, doc, text, {"who": who, "reply_to": self._value(
                    ctx_all, oid, "reply_to_id", doc.markings), "id": self._value(
                    ctx_all, oid, "id", doc.markings) or oid}))
        for (audience, _conv), messages in convo.items():
            messages.sort(key=lambda m: (m[0], m[1].handle))
            channel = audience.startswith("teams:channel:")
            for index, (created, doc, text, meta) in enumerate(messages):
                if channel:
                    replies = [m for m in messages[index + 1:]
                               if m[3]["reply_to"] and m[3]["reply_to"] in (
                                   _native(doc), meta["reply_to"])][:TEAMS_WINDOW]
                else:
                    replies = messages[index + 1:index + 1 + TEAMS_WINDOW]
                own = normalise(text)
                if len(own) < MIN_BODY and not replies:
                    self.counts.skip("too_short:teams")
                    continue
                window = [own] + [f"Reply ({_role(m[3]['who'])}): {normalise(m[2])}"
                                  for m in replies]
                body = pack("\n\n".join(w for w in window if w))[0]
                marks = parse_markings(doc.markings, *(m[1].markings for m in replies))
                parent = self._parent(ctx_all, doc.objects[0])
                topic = (self._value(ctx_all, parent, "topic", doc.markings)
                         or self._value(ctx_all, parent, "display_name", doc.markings)
                         or self._value(ctx_all, doc.objects[0], "subject", doc.markings))
                keys, labels, names = self._ids_and_names(ctx_all, doc, doc.objects,
                                                          body).for_body(body)
                where = ("channel" if channel else "chat") + (
                    f" '{hd.one_line(topic)}'" if topic else "")
                about = (f"Teams {'channel' if channel else 'chat'} message from "
                         f"{_role(meta['who'])}" + (f" with {len(replies)} replies"
                                                    if replies else ""))
                self.counts.documents["teams"] += 1
                chunk = make_chunk(
                    kind="teams", parts=(doc.audience_key, doc.fields[0].citation, "w"),
                    source_handle=doc.fields[0].citation, object_id=doc.objects[0],
                    title=topic, author_role=_role(meta["who"]), occurred_at=created or None,
                    header=hd.build(kind="teams", title=topic, date=created or None,
                                    author_role=_role(meta["who"]), ids=labels, names=names,
                                    about=about, where=where),
                    body=body, linked_ids=keys, audience_key=doc.audience_key,
                    markings=marks or ())
                self.counts.chunks["teams"] += 1
                yield chunk


_MESSAGES = {"Email": "sender", "TeamsChatMessage": "from", "TeamsChannelMessage": "from"}
_CARRIED = ("Attachment.attached_to", "ContainerMember.in_attachment", "ContainerMember.in_file")


def withheld_messages(conn: sqlite3.Connection, exclusion: Exclusion) -> set[str]:
    """Object ids of every mail or Teams message whose sender the exclusion does not clear
    (excluded, undecidable, absent), and of the attachments and container members carried
    by such a mail, in whichever unit they sit: what an incremental run purges from the
    store before writing, so an excluded sender's message never stays in a colleague's
    mailbox or chat (``purge_withheld``). Values are read at any marking: this decides
    only what is removed."""
    cleared: set[str] = set()
    every: set[str] = set()
    for object_type, prop in _MESSAGES.items():
        for row in conn.execute(
                "SELECT o.object_id, (SELECT v.value FROM object_values v WHERE v.object_id = "
                "o.object_id AND v.property = ? AND v.superseded_at IS NULL LIMIT 1) AS who "
                "FROM objects o WHERE o.object_type = ?", (prop, object_type)):
            every.add(row["object_id"])
            who = row["who"]
            if prop == "from":
                try:
                    data = json.loads(who) if isinstance(who, str) else {}
                except ValueError:
                    data = {}
                data = data if isinstance(data, Mapping) else {}
                if str(data.get("kind") or "") in ("application", "bot") and not data.get("id"):
                    cleared.add(row["object_id"])
                    continue
                who = data.get("id")
            if isinstance(who, str) and who.strip() and exclusion.identity_clear(who):
                cleared.add(row["object_id"])
    withheld = every - cleared
    frontier = set(withheld)
    while frontier:
        found = {row[0] for row in conn.execute(
            "SELECT from_object FROM link_rows WHERE to_object IN (SELECT value FROM "
            "json_each(?)) AND link_type IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(frontier)), json.dumps(_CARRIED)))} - withheld
        withheld |= found
        frontier = found
    return withheld


def _native(doc: _Doc) -> str:
    """The Teams message id of a document (the last path segment of its handle)."""
    tail = doc.handle.split("@", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail


def _role(who: Mapping[str, Any]) -> str:
    kind = str(who.get("kind") or "")
    if kind == "user":
        return "Teams user"
    if kind in ("application", "bot"):
        return "app/bot"
    return "participant"


__all__ = ["Counts", "DOCUMENT_KINDS", "ProjectionChunker", "field_text", "open_ro",
           "withheld_messages"]
