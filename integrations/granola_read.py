"""Granola public API reader: meeting notes, decisions and action items. GET only, through
the one guarded transport; off until its setting exists.

Vince is moving meeting notes from Circleback to Granola. ``GET /v1/notes`` (cursor paged,
``created_after``/``updated_after``) lists notes; ``GET /v1/notes/{id}?include=transcript``
reads one with its transcript. Each note becomes one ``plx-meeting-transcript/v1`` document
through ``integrations.meeting_transcripts.from_json`` (``source="granola"``), so it passes
the same people-exclusion admission (``meeting_transcripts.admit``) and carries the same
markings (``meeting``; also ``hr`` and ``pii`` when pay, HR or personal matters are spoken of)
as every other meeting source: this module admits nothing itself, it only reads and shapes.

Decisions and action items: the API serves a note's summary, not separate decision records.
They are read from the summary's own headed sections (``Decisions``/``Decided``, ``Action
items``/``Next steps``/``Follow-ups``, a markdown heading or a bold line) as bullet lines
only; text outside such a section is never classified. An action item keeps its owner only
when the bullet names one explicitly (``Name: text`` or ``Name - text`` or ``[Name] text``);
no owner is inferred. Each extracted line cites the note it came from.

Setting (name only): ``GRANOLA_API_KEY`` (a read key); none: disabled, nothing sent.

Ontology (``BINDINGS``): a note binds to ``MeetingTranscript`` (its ``Meeting`` is formed
only when the organizer's calendar event is captured, joined on the meeting's calendar
identity, never on a title). Paths and fields are from Granola's public API reference and
unverified until the ``granola.notes`` live check has a result.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from . import live_source_grammar as grammar
from . import meeting_transcripts as mt
from . import capture_vendor as cv
from . import vendor_sweep as vs
from .http import SourceUnavailable
from .live_source_guard import GuardedTransport, Secret, jsonable

SOURCE = "granola"
SETTING_KEY = "GRANOLA_API_KEY"
REQUIRED_SETTINGS = (SETTING_KEY,)
AUDIENCE_UNIT = "meetings:granola"
MAX_PAGES = 20
PAGE_SIZE = 30
MAX_NOTES_PER_SWEEP = 300

BINDINGS: dict[str, dict[str, Any]] = {
    "GranolaNote": {"ontology_type": "MeetingTranscript", "join_key": "granola_note_id",
                    "properties": {"subject": "subject", "meeting_start": "meeting_start",
                                   "meeting_end": "meeting_end", "speakers": "speakers",
                                   "cue_count": "cue_count"}},
    "GranolaDecision": {"ontology_type": None, "properties": {}, "join_key": "granola_note_id",
                        "why_unbound": "no ontology decision-from-meeting type yet"},
    "GranolaActionItem": {"ontology_type": None, "properties": {},
                          "join_key": "granola_note_id",
                          "why_unbound": "no ontology action-item type yet; a task is made "
                                         "only through an approved Mission Control action"},
}

_DECISIONS = re.compile(r"decisions?|decided|agreed|resolutions?", re.I)
_ACTIONS = re.compile(r"action\s+items?|next\s+steps?|follow[- ]?ups?|to[- ]?dos?", re.I)
_HEADING = re.compile(r"^\s*(?:#{1,6}\s+|\*\*)\s*([^*#\n:]{2,60}?)\s*(?:\*\*)?\s*:?\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+(.+?)\s*$")
_OWNER = re.compile(r"^(?:\[([^\]]{1,60})\]\s*|([A-Z][A-Za-z.'\- ]{0,58}?)\s*(?::|\s[-–—]\s)\s*)(.+)$")


def settings_from_env(environ: Mapping[str, str] | None = None
                      ) -> tuple[dict[str, str], Secret]:
    env = os.environ if environ is None else environ
    missing = vs.missing_settings(REQUIRED_SETTINGS, env)
    if missing:
        raise vs.CredentialsAbsent(missing)
    key = Secret("Bearer " + env[SETTING_KEY].strip())
    return {"Authorization": key.reveal()}, key


def extract_sections(summary: Any, note_id: str) -> dict[str, list[dict[str, Any]]]:
    """Decisions and action items from a summary's headed bullet sections only."""
    out: dict[str, list[dict[str, Any]]] = {"decisions": [], "action_items": []}
    if not isinstance(summary, str):
        return out
    section = None
    for line in summary.splitlines():
        heading = _HEADING.match(line)
        if heading and not _BULLET.match(line):
            label = heading.group(1)
            section = ("decisions" if _DECISIONS.fullmatch(label.strip()) else
                       "action_items" if _ACTIONS.fullmatch(label.strip()) else None)
            continue
        bullet = _BULLET.match(line)
        if section is None or not bullet:
            continue
        text = bullet.group(1).strip()
        entry: dict[str, Any] = {"text": text[:500], "citation": f"granola://note/{note_id}"}
        if section == "action_items":
            owner = _OWNER.match(text)
            entry["owner"] = ((owner.group(1) or owner.group(2)).strip() if owner else None)
            if owner:
                entry["text"] = owner.group(3).strip()[:500]
        out[section].append(entry)
    return out


def map_note(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One ``GET /v1/notes/{id}?include=transcript`` body -> a ``GranolaNote`` record (the
    transcript document of ``meeting_transcripts``) with its decisions and action items. A
    note with no transcript lines or no date is refused by the shared parser
    (``TranscriptRefused``): it is counted, never stored half-read."""
    note_id = vs.text(raw.get("id"), 40)
    if note_id is None or not grammar.TYPES["granola_note_id"].valid(note_id):
        raise SourceUnavailable("granola note has no valid id")
    event = raw.get("calendar_event") if isinstance(raw.get("calendar_event"), Mapping) else {}
    owner = raw.get("owner") if isinstance(raw.get("owner"), Mapping) else {}
    attendees = [{"name": a.get("name"), "email": a.get("email")}
                 for a in (raw.get("attendees") or event.get("attendees") or [])
                 if isinstance(a, Mapping)]
    transcript = [{"speaker": (seg.get("speaker") or {}).get("name")
                   if isinstance(seg.get("speaker"), Mapping) else seg.get("speaker"),
                   "timestamp": seg.get("start_time") or seg.get("timestamp"),
                   "text": seg.get("text")}
                  for seg in raw.get("transcript") or [] if isinstance(seg, Mapping)]
    summary = raw.get("summary_markdown") or raw.get("summary_text") or raw.get("summary")
    doc = {"id": note_id, "title": raw.get("title") or event.get("event_title"),
           "start": event.get("scheduled_start_time") or raw.get("created_at"),
           "end": event.get("scheduled_end_time"),
           "organizer": {"name": owner.get("name"), "email": owner.get("email")},
           "attendees": attendees, "transcript": transcript,
           "notes": summary if isinstance(summary, str) else None}
    meeting = mt.from_json(doc, source="granola")
    found = extract_sections(summary, note_id)
    return {"record_type": "GranolaNote", "ontology_type": "MeetingTranscript",
            "source": SOURCE, "audience_unit": AUDIENCE_UNIT,
            "citation": f"granola://note/{note_id}",
            "join_keys": {"granola_note_id": note_id},
            "meeting": meeting, "decisions": found["decisions"],
            "action_items": found["action_items"],
            "updated_at": vs.text(raw.get("updated_at"), 40)}


class GranolaReader(vs.VendorClient):
    def _list(self, query: Mapping[str, str]) -> tuple[list[Mapping[str, Any]], str | None]:
        body, _h = self.get_json(grammar.url("granola.notes", dict(query)))
        rows = body.get("notes") if isinstance(body, Mapping) else None
        if not isinstance(rows, list):
            raise SourceUnavailable("granola notes payload invalid")
        cursor = vs.text(body.get("cursor"), 2000) if body.get("hasMore") is True else None
        if cursor is not None and not grammar.external_query_ok(
                "granola.notes", "cursor=" + quote(cursor, safe="")):
            raise SourceUnavailable("granola cursor invalid")
        if body.get("hasMore") is True and cursor is None:
            raise SourceUnavailable("granola cursor missing")
        return [r for r in rows if isinstance(r, Mapping)], cursor

    def note(self, note_id: str) -> Mapping[str, Any]:
        body, _h = self.get_json(grammar.url("granola.note", {"include": "transcript"},
                                             note=note_id))
        if not isinstance(body, Mapping):
            raise SourceUnavailable("granola note payload invalid")
        return body

    def notes(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Notes updated since the saved watermark; refused notes are counted, not lost
        silently (``refused``: reason -> count)."""
        since = entry.get("cursor")
        query: dict[str, str] = {"page_size": str(PAGE_SIZE)}
        if since:
            query["updated_after"] = since
        records: list[dict[str, Any]] = []
        refused: dict[str, int] = {}
        newest = since
        for _ in range(MAX_PAGES):
            rows, cursor = self._list(query)
            for row in rows:
                note_id = vs.text(row.get("id"), 40)
                if note_id is None or not grammar.TYPES["granola_note_id"].valid(note_id):
                    refused["note_id_invalid"] = refused.get("note_id_invalid", 0) + 1
                    continue
                full = self.note(note_id)
                try:
                    records.append(map_note(full))
                except mt.TranscriptRefused as exc:
                    refused[str(exc)] = refused.get(str(exc), 0) + 1
                updated = vs.text(full.get("updated_at") or row.get("updated_at"), 40)
                if updated and (newest is None or updated > newest):
                    newest = updated
            if cursor is None:
                return {"records": records, "cursor": newest, "complete": True,
                        "refused": refused}
            if len(records) + sum(refused.values()) >= MAX_NOTES_PER_SWEEP:
                return {"records": records, "cursor": since, "complete": False,
                        "refused": refused}
            query = {"page_size": str(PAGE_SIZE), "cursor": cursor}
            if since:
                query["updated_after"] = since
        raise SourceUnavailable("granola page bound reached")


def _owner(record: Mapping[str, Any]) -> str | None:
    organizer = (record.get("meeting") or {}).get("organizer") or {}
    return organizer.get("address") if isinstance(organizer, Mapping) else None


def _admitted(record: Mapping[str, Any], people: Any) -> dict[str, Any]:
    """The note with its meeting admitted (``TranscriptRefused`` otherwise: withheld)."""
    return {**record, "meeting": mt.admit(dict(record["meeting"]), people)}


def sweep(environ: Mapping[str, str] | None = None, *, state_path: str | Path,
          transport: GuardedTransport | None = None, now: datetime | None = None,
          store: Any = None, people: Any = None) -> dict[str, Any]:
    """One bounded incremental read. With ``store`` each note is saved under its owner
    (unit ``granola:<owner address>``) only once ``meeting_transcripts.admit`` clears every
    person in it against ``people`` (the people exclusion); without a guard nothing is read
    (``people_exclusion_unavailable``), so no note is saved unchecked."""
    env = os.environ if environ is None else environ
    if store is not None and (people is None or getattr(people, "unavailable", "absent")):
        return {"source": SOURCE, "blocked": "people_exclusion_unavailable",
                "requests_sent": 0, "evidence_label": "not-run"}
    missing = vs.missing_settings(REQUIRED_SETTINGS, env)
    if missing:
        return {"source": SOURCE, "blocked": "credentials_absent", "missing_settings": missing,
                "requests_sent": 0, "evidence_label": "not-run"}
    headers, secret = settings_from_env(env)
    transport = transport or GuardedTransport()
    transport.register_secret(secret)
    reader = GranolaReader(transport, headers)
    persist = cv.persist_into(store, cv.GRANOLA_KIND, _owner, binding="granola",
                              source_api="granola.note", admit=lambda r: _admitted(r, people))
    out = vs.run_sweep(SOURCE, vs.SweepState(state_path), reader.notes, transport, now=now,
                       persist=persist)
    out["evidence_label"] = "injected-transport" if transport.sender_injected else "live-read"
    return out


def probe(environ: Mapping[str, str] | None = None, *, state_path: str | Path | None = None,
          transport: GuardedTransport | None = None) -> dict[str, Any]:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = sweep(environ, state_path=state_path or Path(tmp) / "state.json",
                       transport=transport)
    records = result.pop("records", [])
    result["notes"] = len(records)
    result["decisions"] = sum(len(r["decisions"]) for r in records)
    result["action_items"] = sum(len(r["action_items"]) for r in records)
    result["ok"] = "error" not in result and "blocked" not in result
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.granola_read probe", file=sys.stderr)
        return 2
    result = probe()
    print(json.dumps(jsonable(result), sort_keys=True))
    return 0 if result.get("ok") or result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
