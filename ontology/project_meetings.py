"""The meetings capture as a projection source (source family ``meetings``).

``MeetingsCaptures`` reads the store ``integrations.capture_meetings`` writes (calendar
events, Planner tasks, Teams meeting transcripts) the way ``ontology.project_mc.McCaptures``
reads the Mission Control capture: the file directly, read-only, in one read transaction,
after checking the tables and columns it reads (``meetings_capture_layout_unsupported``).

Relations (``RELATIONS``):

* ``calendarEvents``: one record per retained version of every captured event, ``record_key``
  its canonical path (mailbox object id, event id), ``l1_handle``
  ``calendar-event://<mailbox>/<event>@<generation>-<capture-id>``, ``audience_key`` the
  calendar's unit ``meetings:calendar:<mailbox>``;
* ``meetings``: the meeting itself, read from its organizer's own copy of the event
  (``row.is_organizer``) and keyed by its iCalUId (the one id every attendee's copy shares,
  distinct per occurrence of a series), so each attendee's ``CalendarEvent`` links to one
  ``Meeting``; a meeting whose organizer's calendar is not captured has no ``Meeting``
  (its attendees' events still carry everything they hold);
* ``plannerTasks``: per task version, path (plan, task), unit ``meetings:plan:<plan>``;
* ``meetingTranscripts``: per transcript version, path (organizer, meeting, transcript),
  unit ``meetings:organizer:<organizer>``.

A reader needs the unit's grant and passes the item gate per handle
(``operating.case_runtime.CaseRuntime.resolve_citation``: the version's captured source ACL,
and the people exclusion re-checked on the organizer, attendees and task people).
Completeness per relation as for MC: ``source_not_captured`` when no unit of its kind was
captured, ``source_capture_incomplete`` while some unit's latest run did not finish.

:meth:`documents` gives each event's subject and body preview (``calendar_event``), each
task's title, description and checklist (``planner_task``) and each transcript's text
(``meeting_transcript``: one ``[hh:mm:ss] speaker: text`` line per cue, cited by the
version's handle) as ``ontology.mentions.Document`` values, so the text index and the
mention links reach POs, vendors and customers named in meetings as they reach mail. The
writer of an invitation, a task or a spoken line is not a verified sender: sender class
``unknown``, every mention an external assertion and a lead.

Stdlib only; never a source read (the capture is L1).
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from integrations.capture_store import FORMAT, REMOVALS_COLUMN, make_handle, removals_of
from ontology.project import (Reopenable, CapturedRecord, ProjectionError, _Relations,
                              _file_marks, _watermark, begin_snapshot)
from ontology.store import canonical

SOURCE = "meetings"
NOT_CAPTURED = "source_not_captured"
INCOMPLETE = "source_capture_incomplete"
CALENDAR_EVENTS, MEETINGS = "calendarEvents", "meetings"
PLANNER_TASKS, TRANSCRIPTS = "plannerTasks", "meetingTranscripts"
RELATIONS = (CALENDAR_EVENTS, MEETINGS, PLANNER_TASKS, TRANSCRIPTS)
# Text a mention is read from, per relation: (document kind, row fields).
_DOCUMENT_FIELDS = {CALENDAR_EVENTS: ("calendar_event", ("subject", "body_preview")),
                    PLANNER_TASKS: ("planner_task", ("title", "description")),
                    TRANSCRIPTS: ("meeting_transcript", ("content_text",))}


def _kinds() -> dict[str, Any]:
    from integrations.capture_meetings import EVENT_KIND, TASK_KIND, TRANSCRIPT_KIND
    return {CALENDAR_EVENTS: EVENT_KIND, MEETINGS: EVENT_KIND, PLANNER_TASKS: TASK_KIND,
            TRANSCRIPTS: TRANSCRIPT_KIND}


def _field_text(value: Any) -> str | None:
    """A row field as mention text: a string, or a checklist's item titles."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        titles = [item.get("title") for item in value if isinstance(item, Mapping)
                  and isinstance(item.get("title"), str) and item["title"].strip()]
        return "\n".join(titles) or None
    return None


class MeetingsCaptures(Reopenable):
    """Every retained version of a meetings capture store file, read-only."""

    source = SOURCE
    _NEEDED = {
        "meta": {"key", "value"},
        "runs": {"run_id", "kind", "unit", "state", "finished_at", "updated_at"},
        "versions": {"capture_id", "kind", "path", "unit", "digest", "lineage", "payload",
                     "recorded_at", "superseded_at", "last_seen_at",
                     "missing_from_source_since"},
        "removals": {"kind", "path", "since", "until"},
    }

    def __init__(self, path: str | Path) -> None:
        self.kinds = _kinds()
        self.path = Path(path).expanduser()
        if not self.path.is_file():
            raise ProjectionError("meetings_capture_missing", str(self.path))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._opened = _file_marks(self._conn)   # before anything is read
        self._marks: list[Any] | None = None
        self._snapshot = False
        try:
            for table, columns in self._NEEDED.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("meetings_capture_layout_unsupported",
                                          f"{table} lacks {sorted(columns - have)}")
            meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FORMAT or not meta.get("generation"):
                raise ProjectionError("meetings_capture_layout_unsupported",
                                      "not a plx-capture-store/v1 file")
        except BaseException:
            self._conn.close()
            raise
        self.generation = meta["generation"]
        self._units = {kind.name: self._unit_states(kind.name)
                       for kind in set(self.kinds.values())}

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "MeetingsCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- completeness ----------------------------------------------------------

    def _unit_states(self, kind: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for run in self._conn.execute(
                "SELECT r.* FROM runs r JOIN (SELECT unit, MAX(run_id) AS run_id FROM runs "
                "WHERE kind = ? GROUP BY unit) l ON l.run_id = r.run_id ORDER BY r.unit",
                (kind,)):
            full = self._conn.execute(
                "SELECT MAX(finished_at) FROM runs WHERE kind = ? AND unit = ? "
                "AND state = 'complete'", (kind, run["unit"])).fetchone()[0]
            settled = run["state"] == "complete"
            out[run["unit"]] = {"settled": settled, "full_at": full if settled else None,
                                "refreshed_at": run["updated_at"]}
        return out

    def capture_gap(self, relation: str) -> str | None:
        kind = self.kinds.get(relation)
        units = self._units.get(kind.name) if kind is not None else None
        if not units:
            return NOT_CAPTURED
        return None if all(u["settled"] for u in units.values()) else INCOMPLETE

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data) per captured relation."""
        relations: dict[str, dict[str, Any]] = {}
        for relation, kind in self.kinds.items():
            units = self._units.get(kind.name)
            if not units:
                continue
            last = self._conn.execute("SELECT MAX(capture_id) FROM versions WHERE kind = ?",
                                      (kind.name,)).fetchone()[0]
            relations[relation] = {
                "last_refresh_at": min(u["refreshed_at"] for u in units.values()),
                "last_capture_id": last,
                "last_full_traversal_at": (min(u["full_at"] for u in units.values())
                                           if self.capture_gap(relation) is None else None)}
        return {"generation": f"meetings-capture/{self.generation}", "relations": relations}

    # -- records ---------------------------------------------------------------

    def _versions(self, kind: Any) -> Iterator[tuple[tuple[str, ...], sqlite3.Row,
                                                     dict[str, Any]]]:
        for row in self._conn.execute(
                f"SELECT *, {REMOVALS_COLUMN} FROM versions WHERE kind = ? "
                "ORDER BY unit, path, recorded_at, capture_id", (kind.name,)):
            try:
                path = kind.canonical_path([unquote(p) for p in str(row["path"]).split("/")])
            except ValueError:
                continue
            yield path, row, json.loads(row["payload"])

    def watermark(self) -> list[Any] | None:
        """The file state the snapshot of the last :meth:`records` reads
        (``ontology.project.begin_snapshot``; None: no reuse of its fingerprints)."""
        return _watermark(self, self._marks, self._opened) if self._snapshot else None

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        self._end_snapshot()
        self._marks = begin_snapshot(self._conn)  # one snapshot for every relation
        self._snapshot = True

        def stream(relation: str) -> Iterator[CapturedRecord]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            kind = self.kinds.get(relation)
            if kind is None:
                return
            for path, row, payload in self._versions(kind):
                record = payload.get("row") or {}
                key = canonical(list(path))
                if relation == MEETINGS:
                    # the organizer's own copy only, keyed by the meeting's iCalUId
                    if record.get("is_organizer") is not True or not record.get("ical_uid"):
                        continue
                    key = canonical([record["ical_uid"]])
                yield CapturedRecord(
                    source=SOURCE, relation=relation, record_key=key, row=record,
                    l1_handle=make_handle(kind, path, self.generation, row["capture_id"]),
                    field_prefix="row", digest=row["digest"], recorded_at=row["recorded_at"],
                    superseded_at=row["superseded_at"], last_seen_at=row["last_seen_at"],
                    audience_key=kind.unit(path),
                    missing_from_source_since=row["missing_from_source_since"],
                    removals=removals_of(row), capture_lineage=row["lineage"])

        return _Relations(relations, stream)

    def documents(self) -> Iterator[Any]:
        """Every captured event, task and transcript version as an ``ontology.mentions.
        Document`` (author unverified: sender class ``unknown``)."""
        from ontology.mentions import Document, TextField

        for relation, (doc_kind, names) in _DOCUMENT_FIELDS.items():
            kind = self.kinds[relation]
            for path, row, payload in self._versions(kind):
                handle = make_handle(kind, path, self.generation, row["capture_id"])
                record = payload.get("row") or {}
                fields = tuple(TextField(handle, f"row.{name}", text) for name in names
                               if (text := _field_text(record.get(name))) is not None)
                if not fields:
                    continue
                yield Document(handle, doc_kind, fields, sender_class="unknown",
                               digest=row["digest"], recorded_at=row["recorded_at"],
                               superseded_at=row["superseded_at"],
                               date=record.get("start") or record.get("meeting_start")
                               or record.get("created_at"))


__all__ = ["CALENDAR_EVENTS", "INCOMPLETE", "MEETINGS", "MeetingsCaptures", "NOT_CAPTURED",
           "PLANNER_TASKS", "RELATIONS", "SOURCE", "TRANSCRIPTS"]
