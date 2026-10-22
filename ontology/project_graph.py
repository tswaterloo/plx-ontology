"""Captured Microsoft Graph sources beside mail and SharePoint: attachments as cited
mention documents and Teams channel and chat messages as a
projection source.

``ontology.project.MailCaptures`` and ``SharePointCaptures`` project captured mail and
SharePoint files as ``Email``, ``Attachment`` and ``SharePointFile`` objects (schema
domain ``documents``). A message's mentions come from its subject, body and attachment
names; this module makes an attachment's own text a mention document too, so an
identifier written inside a PDF or spreadsheet reaches the PO/SO/AO/vendor/customer it
names as ``Attachment.mentions_<type>`` lead links (``ontology.schema
.ATTACHMENT_MENTION_LINK_RULE``), cited to the page and span it was read from.

:func:`attachment_document`: the ``ontology.mentions.Document`` of one captured
attachment: its name under its handle and its text under ``row.content_text``, a PDF's
pages cited ``<handle>&page=<n>`` with page offsets. The text is the one reading of the
captured file (``integrations.captured_text.captured_text``: the pinned bytes through
the deterministic extractor, or the capture's own OCR text of a scan), the same reading
``MailCaptureStore.resolve`` and the typed fields (``ontology.document_fields``) cite.
The sender class is ``unknown``: whoever sent the message did not necessarily write the
file, so every mention is an external assertion (it can never confirm a payment,
receipt or quantity).

Containers (a zip, an ``.eml``, an ``.msg``; a mail attachment or a SharePoint file):
each member file is a ``ContainerMember`` object of its own (:func:`member_records`,
relation ``containerMembers`` of both capture sources) and a mention document of its
own (:func:`member_documents`), cited ``<container handle>&member=<path>&member_sha256=
<hex>`` (``integrations.captured_text.member_handle``), its text the one reading of its
bytes (``integrations.captured_text.container_members``). A member the extractor could
not read is a member in state ``unread`` with the reason, and every part of the
container's structure that was not read (an encrypted or over-cap member, an undecoded
``.msg`` property) is a member record in state ``unread`` or ``skipped_by_cap``: an
unread part is a visible gap, never a silent omission. A member has its container's
audience and validity interval; every reader is gated by the container
(``split_member`` in the capture stores' ``resolve``).

Permissions are those of the message: the attachment object has the message's audience
(``mail:<mailbox>``) and every cited handle, page citations included, is re-gated per
reader through ``MailCaptureStore.item_gate``. Source-kind pluggable: any capture source
whose ``documents()`` yields ``Document`` values feeds ``store_mention_links`` the same way.

:class:`TeamsCaptures` (source family ``teams``) reads the Teams capture store
(``integrations.capture_teams``: the versioned ``integrations.capture_store`` file) the way
``ontology.project_mc.McCaptures`` reads the MC capture: read-only, in one read transaction,
after checking the tables and columns it reads (``teams_capture_layout_unsupported``).
Relation ``channelMessages``: one record per retained version of every channel message,
``record_key`` its canonical path (team, channel, message), ``l1_handle`` its capture handle
``teams-channel://<team>/<channel>/<message>@<generation>-<capture-id>``, ``row`` the
captured row (``capture_teams.message_row``) and ``audience_key`` the channel's unit
``teams:channel:<team>/<channel>``. Relation ``chatMessages`` (the chats of every account
on the mail allowlist, ``docs/decisions/2026-10-07-ovs-teams-capture.md`` item 2) the
same way per chat: path (chat, message), handle ``teams-chat://<chat>/<message>@...``, unit
``teams:chat:<chat>``. A reader needs that unit granted *and* passes the item gate per
handle (``operating.case_runtime.CaseRuntime.resolve_citation`` over the Teams capture: the
message's and the conversation's latest captured membership), as for mail and SharePoint.
A channel or chat without a captured membership is never visible, so none of its messages
is projected (``docs/decisions/2026-10-10-followup-decisions.md`` item 6). Completeness
per relation as for MC: ``source_not_captured`` when no conversation of its kind is
projected, ``source_capture_incomplete`` while one's latest round did not finish or one is
excluded (a run but no captured membership: its members read failed, ``capture_teams``
``members_unread``).

:class:`SharePointListCaptures` reads the SharePoint list-item capture store
(``integrations.capture_sharepoint_lists``) the same way, for relation ``listItems`` of the
``sharepoint`` family (``ontology.project.SharePointCaptures`` serves it).
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from integrations.capture_store import FORMAT, REMOVALS_COLUMN, make_handle, removals_of
from integrations.captured_text import (ContainerReading, byte_pinned, captured_text,
                                        container_members, member_handle)
from ontology.identifiers import copy_key
from ontology.mentions import Document, TextField, file_text_fields
from ontology.project import (CAPTURED_RELATIONS, MEMBER_RELATION, CapturedRecord,
                              ProjectionError, RowSample, _Relations, _sampled)
from ontology.store import canonical

TEXT_FIELD = "row.content_text"
TEAMS = "teams"
CHANNEL_MESSAGES = "channelMessages"
CHAT_MESSAGES = "chatMessages"
NOT_CAPTURED = "source_not_captured"
INCOMPLETE = "source_capture_incomplete"
# Member states: its text was read; it is a container whose own members follow (the
# extractor reads no text of a zip); not read (``reason``); not read by a cap (``cap``).
MEMBER_READ, MEMBER_CONTAINER, MEMBER_UNREAD, MEMBER_SKIPPED = (
    "read", "container", "unread", "skipped_by_cap")


def attachment_document(handle: str, row: Mapping[str, Any], blobs: str | Path, *,
                        recorded_at: str | None = None, superseded_at: str | None = None,
                        date: str | None = None) -> Document:
    """The mention document of one captured attachment version (``handle``: its
    byte-pinned ``graph-mail://...#attachment=<id>&sha256=<hex>`` citation; ``row``: its
    metadata with ``content_sha256`` and the capture's ``text_status``/``text_sha256``)."""
    fields: list[TextField] = []
    if isinstance(row.get("name"), str):
        fields.append(TextField(handle, "row.name", row["name"]))
    found = captured_text(blobs, row)
    # Read on its first pages only: its name only, and why (as ``SharePointCaptures``).
    withheld = None if found is None else found.text_withheld
    text = None if found is None or withheld is not None else found.text
    if text is not None:
        fields.extend(file_text_fields(handle, TEXT_FIELD, text, paged=found.paged))
    return Document(handle, "attachment", tuple(fields), sender_class="unknown",
                    digest=row.get("content_sha256"), recorded_at=recorded_at,
                    superseded_at=superseded_at, date=date,
                    copy_key=copy_key("mail", "attachments", row), text_withheld=withheld)


def _members(blobs: str | Path, row: Mapping[str, Any]
             ) -> tuple[str, ContainerReading] | None:
    digest = row.get("content_sha256")
    found = container_members(blobs, row) if isinstance(digest, str) else None
    return None if found is None else (digest, found)


def member_rows(container: str, found: ContainerReading, keys: Mapping[str, Any]
                ) -> Iterator[tuple[str, dict[str, Any]]]:
    """(member handle, ``containerMembers`` row) of every member file and every
    structural part not read of one container (``container``: its byte-pinned handle;
    ``keys``: the container's key fields, copied into each row)."""
    for member in found.members:
        status = member.text_status
        state = (MEMBER_READ if status == "extracted" else
                 MEMBER_CONTAINER if member.container else MEMBER_UNREAD)
        yield member_handle(container, member.path, member.sha256), {
            **keys, "container": container, "path": member.path, "name": member.name,
            "kind": "file", "content_sha256": member.sha256, "size": member.size,
            "mime_type": member.mime_type, "state": state,
            "reason": None if state != MEMBER_UNREAD else f"text_{status}"}
    for gap in found.gaps:
        path = gap["path"]
        yield member_handle(container, path, gap.get("sha256")), {
            **keys, "container": container, "path": path,
            "name": PurePosixPath(path.rpartition("!/")[2]).name, "kind": gap["kind"],
            "content_sha256": gap.get("sha256"), "size": gap.get("size"), "mime_type": None,
            "state": gap["state"], "reason": gap.get("reason") or gap.get("cap"),
            **({"cap": gap["cap"], "limit": gap["limit"]} if "cap" in gap else {})}


def member_records(records: Iterable[Any], blobs: str | Path, keys: Mapping[str, str]
                   ) -> Iterator[Any]:
    """``containerMembers`` records of a capture source's file records (attachments,
    drive items): one per member and per part not read of each container (module
    docstring), keyed by the container's record key and the member path, cited by the
    member handle, with the container's audience and validity interval. ``keys``: each
    member row field naming the container and the container row field it copies."""
    for rec in records:
        found = _members(blobs, rec.row)
        if found is None:
            continue
        digest, reading = found
        container = byte_pinned(rec.l1_handle, digest)
        names = {member: rec.row.get(source) for member, source in keys.items()}
        for handle, row in member_rows(container, reading, names):
            yield CapturedRecord(
                source=rec.source, relation=MEMBER_RELATION,
                record_key=canonical([rec.record_key, row["path"]]), row=row,
                l1_handle=handle, field_prefix="row",
                digest=sha256(canonical(row).encode("utf-8")).hexdigest(),
                recorded_at=rec.recorded_at, superseded_at=rec.superseded_at,
                last_seen_at=rec.last_seen_at, audience_key=rec.audience_key,
                missing_from_source_since=rec.missing_from_source_since,
                removals=rec.removals)


def member_documents(handle: str, row: Mapping[str, Any], blobs: str | Path, *,
                     recorded_at: str | None = None, superseded_at: str | None = None,
                     date: str | None = None) -> Iterator[Document]:
    """The mention documents of the member files of one captured container version
    (``handle``: its capture handle; ``row``: its metadata with ``content_sha256``):
    each member's name and text under its member handle, a PDF member's pages cited
    ``&page=<n>`` (one read on its first pages only: its name and ``text_withheld``; its
    skipped pages are a ``containerMembers`` gap row); nothing for a file that is not a
    container. The author is unknown."""
    found = _members(blobs, row)
    if found is None:
        return
    digest, reading = found
    container = byte_pinned(handle, digest)
    for member in reading.members:
        cited = member_handle(container, member.path, member.sha256)
        fields: list[TextField] = [TextField(cited, "row.name", member.name)] \
            if member.name else []
        withheld = None if member.reading is None else member.reading.text_withheld
        text = None if member.reading is None or withheld is not None else member.reading.text
        if text is not None:
            fields.extend(file_text_fields(cited, TEXT_FIELD, text,
                                           paged=member.reading.paged))
        # A member's copy key does not depend on the system holding its container.
        yield Document(cited, "container_member", tuple(fields), sender_class="unknown",
                       digest=member.sha256, recorded_at=recorded_at,
                       superseded_at=superseded_at, date=date, text_withheld=withheld,
                       copy_key=copy_key(None, MEMBER_RELATION,
                                         {"content_sha256": member.sha256}))


class TeamsCaptures:
    """Every retained channel- and chat-message version of a Teams capture store file,
    read-only."""

    source = TEAMS
    _NEEDED = {
        "meta": {"key", "value"},
        "runs": {"run_id", "kind", "unit", "state", "finished_at", "updated_at"},
        "versions": {"capture_id", "kind", "path", "unit", "digest", "payload",
                     "recorded_at", "superseded_at", "last_seen_at",
                     "missing_from_source_since"},
        "removals": {"kind", "path", "since", "until"},
        "unit_acl": {"kind", "unit"},
    }

    def __init__(self, path: str | Path) -> None:
        from integrations.capture_teams import CHANNEL_KIND, CHAT_KIND, RELATIONS

        # relation -> the capture kind holding it (``capture_teams.RELATIONS``)
        self.kinds = {RELATIONS[kind.name]: kind for kind in (CHANNEL_KIND, CHAT_KIND)}
        self.path = Path(path).expanduser()
        if not self.path.is_file():
            raise ProjectionError("teams_capture_missing", str(self.path))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._snapshot = False
        try:
            for table, columns in self._NEEDED.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("teams_capture_layout_unsupported",
                                            f"{table} lacks {sorted(columns - have)}")
            meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FORMAT or not meta.get("generation"):
                raise ProjectionError("teams_capture_layout_unsupported",
                                        "not a plx-capture-store/v1 file")
        except BaseException:
            self._conn.close()
            raise
        self.generation = meta["generation"]
        self._units = {relation: self._unit_states(kind.name)
                       for relation, kind in self.kinds.items()}
        # A unit with a run but no captured membership is excluded (never projected).
        self._excluded = {relation: self._conn.execute(
            "SELECT 1 FROM runs WHERE kind = ? AND unit NOT IN "
            "(SELECT unit FROM unit_acl WHERE kind = ?) LIMIT 1",
            (kind.name, kind.name)).fetchone() is not None
            for relation, kind in self.kinds.items()}

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "TeamsCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _unit_states(self, kind: str) -> dict[str, dict[str, Any]]:
        """Unit (channel or chat) of ``kind`` -> state of its latest run, for the units with
        a captured membership (the others are never visible)."""
        out: dict[str, dict[str, Any]] = {}
        for run in self._conn.execute(
                "SELECT r.* FROM runs r JOIN (SELECT unit, MAX(run_id) AS run_id FROM runs "
                "WHERE kind = ? GROUP BY unit) l ON l.run_id = r.run_id WHERE r.unit IN "
                "(SELECT unit FROM unit_acl WHERE kind = ?) ORDER BY r.unit",
                (kind, kind)):
            full = self._conn.execute(
                "SELECT MAX(finished_at) FROM runs WHERE kind = ? AND unit = ? "
                "AND state = 'complete'", (kind, run["unit"])).fetchone()[0]
            settled = run["state"] == "complete"
            out[run["unit"]] = {"settled": settled, "full_at": full if settled else None,
                                "refreshed_at": run["updated_at"]}
        return out

    def capture_gap(self, relation: str) -> str | None:
        units = self._units.get(relation)
        if relation not in CAPTURED_RELATIONS[TEAMS] or not units:
            return NOT_CAPTURED
        return (None if all(u["settled"] for u in units.values())
                and not self._excluded[relation] else INCOMPLETE)

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data) of the captured channels and chats."""
        relations: dict[str, Any] = {}
        for relation, kind in self.kinds.items():
            units = self._units[relation].values()
            if not units:
                continue
            last = self._conn.execute("SELECT MAX(capture_id) FROM versions WHERE kind = ?",
                                      (kind.name,)).fetchone()[0]
            relations[relation] = {
                "last_refresh_at": min(u["refreshed_at"] for u in units),
                "last_capture_id": last,
                "last_full_traversal_at": (min(u["full_at"] for u in units)
                                           if self.capture_gap(relation) is None else None)}
        return {"generation": f"teams-capture/{self.generation}", "relations": relations}

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[Any]]:
        self._end_snapshot()
        self._conn.execute("BEGIN")  # one snapshot for every relation
        self._snapshot = True

        def stream(relation: str) -> Iterator[Any]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            kind = self.kinds.get(relation)
            if relation not in CAPTURED_RELATIONS[TEAMS] or kind is None:
                return
            for unit in sorted(self._units[relation]):
                for row in self._conn.execute(
                        f"SELECT *, {REMOVALS_COLUMN} FROM versions "
                        "WHERE kind = ? AND unit = ? "
                        "ORDER BY path, recorded_at, capture_id", (kind.name, unit)):
                    path = kind.canonical_path(
                        [unquote(p) for p in str(row["path"]).split("/")])
                    yield CapturedRecord(
                        source=TEAMS, relation=relation, record_key=canonical(list(path)),
                        row=json.loads(row["payload"])["row"],
                        l1_handle=make_handle(kind, path, self.generation,
                                              row["capture_id"]),
                        field_prefix="row", digest=row["digest"],
                        recorded_at=row["recorded_at"], superseded_at=row["superseded_at"],
                        last_seen_at=row["last_seen_at"], audience_key=kind.unit(path),
                        missing_from_source_since=row["missing_from_source_since"],
                        removals=removals_of(row))

        return _Relations(relations, stream)


class SharePointListCaptures:
    """Every retained list-item version of a SharePoint list-item capture store file
    (``integrations.capture_sharepoint_lists``), read-only: relation ``listItems`` of source
    family ``sharepoint``, served through ``ontology.project.SharePointCaptures``.

    Read like :class:`TeamsCaptures`: in one read transaction, after checking the tables and
    columns it reads (``sharepoint_list_capture_layout_unsupported``). One record per retained
    version, ``record_key`` its canonical path (site, list, item), ``l1_handle``
    ``sharepoint-list://<site>/<list>/<item>@<generation>-<capture-id>``, ``row`` the captured
    Graph listItem and ``audience_key`` the list's unit ``sharepoint-list:<site>/<list>``; a
    reader passes the item gate per handle (``CaseRuntime.resolve_citation``: the item's
    captured permissions). ``capture_gap``: ``source_not_captured`` when no list was captured,
    ``source_capture_incomplete`` while a list's latest run did not complete."""

    _NEEDED = {key: columns for key, columns in TeamsCaptures._NEEDED.items()
               if key != "unit_acl"}

    def __init__(self, path: str | Path) -> None:
        from integrations.capture_sharepoint_lists import LIST_KIND

        self.kind = LIST_KIND
        self.path = Path(path).expanduser()
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            for table, columns in self._NEEDED.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("sharepoint_list_capture_layout_unsupported",
                                          f"{table} lacks {sorted(columns - have)}")
            meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FORMAT or not meta.get("generation"):
                raise ProjectionError("sharepoint_list_capture_layout_unsupported",
                                      "not a plx-capture-store/v1 file")
        except BaseException:
            self._conn.close()
            raise
        self.generation = meta["generation"]
        self._units = {run["unit"]: run for run in self._conn.execute(
            "SELECT r.* FROM runs r JOIN (SELECT unit, MAX(run_id) AS run_id FROM runs "
            "WHERE kind = ? GROUP BY unit) l ON l.run_id = r.run_id ORDER BY r.unit",
            (self.kind.name,))}

    def close(self) -> None:
        self._conn.close()

    def capture_gap(self) -> str | None:
        if not self._units:
            return NOT_CAPTURED
        return None if all(u["state"] == "complete" for u in self._units.values()) \
            else INCOMPLETE

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data) of the captured lists."""
        last = self._conn.execute("SELECT MAX(capture_id) FROM versions WHERE kind = ?",
                                  (self.kind.name,)).fetchone()[0]
        units = self._units.values()
        return {"last_refresh_at": min((u["updated_at"] for u in units), default=None),
                "last_capture_id": last,
                "last_full_traversal_at": (min(u["finished_at"] for u in units)
                                           if self.capture_gap() is None else None)}

    def records(self, relation: str, sample: RowSample | None = None) -> Iterator[Any]:
        """Every retained version, read by one statement (one snapshot of the store);
        ``sample``: only the rows it keeps (``ontology.project.RowSample``)."""
        for row in _sampled(sample, self._conn.execute(
                f"SELECT *, {REMOVALS_COLUMN} FROM versions WHERE kind = ? "
                "ORDER BY unit, path, recorded_at, capture_id", (self.kind.name,))):
            path = self.kind.canonical_path([unquote(p) for p in str(row["path"]).split("/")])
            yield CapturedRecord(
                source="sharepoint", relation=relation, record_key=canonical(list(path)),
                row=json.loads(row["payload"])["row"],
                l1_handle=make_handle(self.kind, path, self.generation, row["capture_id"]),
                field_prefix="row", digest=row["digest"], recorded_at=row["recorded_at"],
                superseded_at=row["superseded_at"], last_seen_at=row["last_seen_at"],
                audience_key=self.kind.unit(path),
                missing_from_source_since=row["missing_from_source_since"],
                removals=removals_of(row))


__all__ = ["CHANNEL_MESSAGES", "CHAT_MESSAGES", "INCOMPLETE", "MEMBER_CONTAINER",
           "MEMBER_READ", "MEMBER_RELATION", "MEMBER_SKIPPED", "MEMBER_UNREAD", "NOT_CAPTURED",
           "SharePointListCaptures", "TEAMS", "TEXT_FIELD", "TeamsCaptures",
           "attachment_document", "member_documents", "member_records", "member_rows"]
